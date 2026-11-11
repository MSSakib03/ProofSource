import os
import re
import csv
import io
import json
import base64
import hashlib
import logging
import random
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote, urlsplit
import requests
from flask import Flask, g, render_template, request, jsonify, session, redirect, url_for, Response, stream_with_context
from sqlalchemy import func, case, and_, or_
from models import db, Contest, IndexBook, ContestPage, OAuthLogin, AuditLog, SlugAlias, Hub, migrate_schema
import db_config
import security
from security import limiter
from wiki_engine import (
    is_super_admin_name,
    normalize_wiki_domain,
    is_user_wiki_admin,
    identify_bot_users,
    parse_excluded_pages_spec,
    parse_excluded_pages_strict,
    get_namespace_prefixes,
    extract_base_book_name,
    normalize_index_title,
    fetch_index_page_count,
    fetch_all_existing_page_levels,
    fetch_live_book_pages_from_wiki,
    fetch_page_content_and_image,
    render_wikitext_preview,
    edit_wikisource_live,
    USER_AGENT
)
import backup_engine

def _load_secret_key() -> str:
    """Same key for every uWSGI worker: env var first, else a file kept in the tool's home."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = os.path.join(os.path.expanduser("~"), ".proofsource_secret_key")
    try:
        with open(key_file, "r") as f:
            k = f.read().strip()
            if k:
                return k
    except OSError:
        pass
    k = secrets.token_hex(32)
    try:
        # created 0600 from the start: tool homes on Toolforge are readable by other tools
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(k)
    except OSError:
        pass
    return k

app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config["SQLALCHEMY_DATABASE_URI"] = db_config.resolve_database_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = db_config.engine_options(app.config["SQLALCHEMY_DATABASE_URI"])
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("PROOFSOURCE_MAX_UPLOAD_MB", "200")) * 1024 * 1024  # backups
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("PROOFSOURCE_INSECURE_COOKIES") != "1"   # 1 only for local http dev
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["FORCED_SYNC_MIN_SECONDS"] = int(os.environ.get("PROOFSOURCE_FORCED_SYNC_SECONDS", "30"))
LOGIN_LIFETIME = app.config["PERMANENT_SESSION_LIFETIME"]
log = logging.getLogger("proofsource")

# ---------- MediaWiki OAuth 2.0 ----------
OAUTH2_BASE = os.environ.get("OAUTH2_BASE", "https://meta.wikimedia.org/w/rest.php/oauth2").rstrip("/")
OAUTH2_CLIENT_ID = os.environ.get("OAUTH2_CLIENT_ID", "").strip()
OAUTH2_CLIENT_SECRET = os.environ.get("OAUTH2_CLIENT_SECRET", "").strip()
OAUTH2_REDIRECT_URI = os.environ.get("OAUTH2_REDIRECT_URI", "https://proofsource.toolforge.org/auth/callback").strip()

PUBLIC_HOSTS = frozenset(h for h in [urlsplit(OAUTH2_REDIRECT_URI).netloc.lower()] if h)

db.init_app(app)

def oauth2_configured() -> bool:
    return bool(OAUTH2_CLIENT_ID and OAUTH2_CLIENT_SECRET)

def _token_request(data: dict) -> dict:
    data = dict(data)
    data["client_id"] = OAUTH2_CLIENT_ID
    data["client_secret"] = OAUTH2_CLIENT_SECRET
    r = requests.post(
        OAUTH2_BASE + "/access_token",
        data=data,
        headers={"User-Agent": USER_AGENT},
        timeout=20
    )
    try:
        js = r.json()
    except ValueError:
        raise RuntimeError(f"Token endpoint returned HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code != 200 or "access_token" not in js:
        raise RuntimeError(js.get("error_description") or js.get("message") or js.get("error") or str(js))
    return js

def _fetch_username(access_token: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Authorization": "Bearer " + access_token}
    try:
        r = requests.get(OAUTH2_BASE + "/resource/profile", headers=headers, timeout=15)
        if r.status_code == 200:
            name = r.json().get("username")
            if name:
                return name
    except Exception:
        pass
    # Fallback: ask the API who we are
    r = requests.get(
        "https://meta.wikimedia.org/w/api.php",
        params={"action": "query", "meta": "userinfo", "format": "json"},
        headers=headers,
        timeout=15
    ).json()
    ui = r.get("query", {}).get("userinfo", {})
    if ui.get("name") and "anon" not in ui:
        return ui["name"]
    raise RuntimeError("Could not identify the Wikimedia user from the OAuth 2.0 token.")

def _expiry_from(js: dict):
    try:
        return datetime.utcnow() + timedelta(seconds=int(js.get("expires_in", 14400)) - 120)
    except (TypeError, ValueError):
        return None

def _enc(token):
    return security.encrypt_secret(token, app.secret_key)

def _dec(token):
    return security.decrypt_secret(token, app.secret_key)

def get_login_record():
    req = request._get_current_object()
    cached = g.get("login_cache")
    if cached and cached[0] is req:
        return cached[1]
    rec = None
    lid = session.get("login_id")
    if lid and isinstance(lid, str):
        rec = db.session.get(OAuthLogin, lid)
        if rec and rec.created_at and rec.created_at < datetime.utcnow() - LOGIN_LIFETIME:
            # server-side expiry: a stolen or forgotten cookie stops working after the session lifetime
            db.session.delete(rec)
            db.session.commit()
            rec = None
    g.login_cache = (req, rec)
    return rec

def get_current_user():
    rec = get_login_record()
    return rec.username if rec else None

def get_valid_access_token():
    rec = get_login_record()
    if not rec:
        return None
    if rec.expires_at and rec.expires_at <= datetime.utcnow():
        refresh = _dec(rec.refresh_token)
        if not refresh:
            return None
        try:
            js = _token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        except Exception:
            return None
        rec.access_token = _enc(js["access_token"])
        rec.refresh_token = _enc(js.get("refresh_token") or refresh)
        rec.expires_at = _expiry_from(js)
        db.session.commit()
    return _dec(rec.access_token)

def purge_expired_logins():
    OAuthLogin.query.filter(OAuthLogin.created_at < datetime.utcnow() - LOGIN_LIFETIME).delete(synchronize_session=False)

def get_oauth_creds() -> dict:
    tok = get_valid_access_token()
    return {"access_token": tok} if tok else {}


# =====================================================================
#  Generic helpers
# =====================================================================

VALID_DECISIONS = {"accepted", "needs_fix", "rejected", "pending_review"}
DT_FMT_IN = "%Y-%m-%dT%H:%M"


def err(code: str, status: int = 400, **extra):
    """Errors carry a stable code; the frontend translates it (i18n), English text is a fallback."""
    body = {"success": False, "error_code": code, "error": extra.pop("message", code.replace("_", " "))}
    body.update(extra)
    return jsonify(body), status


# Column limits (MariaDB rejects over-long values in strict mode; SQLite silently accepted them).
LEN_TITLE, LEN_SLUG, LEN_USERS, LEN_TEXT, LEN_NOTE = 255, 100, 10000, 20000, 500
JURY_MODES = ("in_app_validate", "accept_reject")
REJECT_ACTIONS = ("hold_batch", "instant_revert")


def clip(val, n: int) -> str:
    return str(val if val is not None else "").strip()[:n]


def to_int(val, default=0, lo=None, hi=None) -> int:
    """Parses user input as an int; raises ValueError('invalid_value') on garbage."""
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        raise ValueError("invalid_value")
    try:
        n = int(val)
    except (TypeError, ValueError):
        raise ValueError("invalid_value")
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def rate_limit(bucket: str, limit: int, per_seconds: float, user: str = None):
    """Returns an error response when the caller exceeded the limit, else None."""
    key = f"{bucket}:{(user or get_current_user() or 'anon').lower()}"
    if not limiter.hit(key, limit, per_seconds):
        return err("rate_limited", 429)
    return None


def parse_dt_in(val):
    if not val:
        return None
    val = str(val).strip().replace("Z", "")
    for fmt in (DT_FMT_IN, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    raise ValueError(f"bad datetime: {val}")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def split_users(raw: str) -> list:
    if not raw:
        return []
    return [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]


def user_in_list(username: str, raw: str) -> bool:
    if not username:
        return False
    u_low = username.strip().lower()
    return any(u_low == item.lower() for item in split_users(raw))


def is_contest_admin(contest: Contest, username: str) -> bool:
    if not username:
        return False
    u_low = username.strip().lower()
    if is_super_admin_name(username):
        return True
    if contest.created_by and contest.created_by.strip().lower() == u_low:
        return True
    return user_in_list(username, contest.admins)


def is_reviewer(contest: Contest, username: str) -> bool:
    return bool(username) and (user_in_list(username, contest.juries) or is_contest_admin(contest, username))


def is_super_admin(username: str) -> bool:
    return is_super_admin_name(username)


def is_listed_admin(obj, username: str) -> bool:
    """Admin by name (creator or in the admin list) — super-admin powers are NOT counted here."""
    return bool(username) and (same_user(obj.created_by, username) or user_in_list(username, obj.admins))


def can_delete(obj, username: str) -> bool:
    """Only the tool admin deletes, and only when a listed admin asked for it — or when the
    tool admin is a listed admin of this contest themself."""
    if not is_super_admin(username):
        return False
    return bool(obj.delete_requested_by) or is_listed_admin(obj, username)


def delete_request_info(obj):
    if not obj.delete_requested_by:
        return None
    return {"by": obj.delete_requested_by, "iso": iso(obj.delete_requested_at), "reason": obj.delete_reason or ""}


def can_approve_contest(contest: Contest, username: str) -> bool:
    if not username:
        return False
    return is_user_wiki_admin(contest.wiki_domain, username)


def same_user(a, b) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def generate_unique_slug(custom_slug: str = "") -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '', str(custom_slug or "").strip()).lower()[:60]

    def taken(s):
        return (Contest.query.filter_by(slug=s).first() is not None
                or SlugAlias.query.filter_by(old_slug=s).first() is not None)

    if cleaned:
        candidate = cleaned
        counter = 1
        while taken(candidate):
            counter += 1
            candidate = f"{cleaned}-{counter}"
        return candidate
    while True:
        rand_slug = str(random.randint(100000, 999999))
        if not taken(rand_slug):
            return rand_slug


def allows_custom_points(c: Contest) -> bool:
    h = c.hub
    if not h:
        return True
    pol = h.custom_points_policy or 'project'
    return pol == 'yes' or (pol == 'project' and c.allow_custom_points is not False)


def allows_bonus(c: Contest) -> bool:
    return not (c.hub and (c.hub.bonus_policy or 'project') == 'no')


def get_book_points(contest: Contest, book: IndexBook) -> int:
    if book.custom_points is not None and allows_custom_points(contest):
        return book.custom_points
    return contest.proofread_points


def validation_visible(c: Contest, user: str = None) -> bool:
    """Validation stats are shown only when validation earns points, and never as a jury's own work
    when juries don't get validation points."""
    if not c.validate_points or c.validate_points <= 0:
        return False
    if user and user_in_list(user, c.juries) and not c.jury_gets_points:
        return False
    return True


def locked_fields(c: Contest) -> list:
    """Fields a child contest's admin cannot change (they come from the central contest)."""
    if not c.hub:
        return []
    out = ["start_time", "end_time", "proofread_points", "validate_points", "wiki_domain", "hub_slug"]
    if (c.hub.review_policy or 'project') != 'project':
        out.append("review_enabled")
    if (c.hub.custom_points_policy or 'project') != 'project':
        out.append("allow_custom_points")
    if (c.hub.bonus_policy or 'project') == 'no':
        out.append("max_bonus_points")
    return out


def apply_central_rules(c: Contest):
    """Force the central contest's rules onto a child contest."""
    h = c.hub
    if not h:
        return
    if h.start_time:
        c.start_time = h.start_time
    if h.end_time:
        c.end_time = h.end_time
    c.proofread_points = h.proofread_points if h.proofread_points is not None else c.proofread_points
    c.validate_points = h.validate_points or 0
    if (h.review_policy or 'project') == 'yes':
        c.review_enabled = True
    elif h.review_policy == 'no':
        c.review_enabled = False
    if (h.custom_points_policy or 'project') == 'yes':
        c.allow_custom_points = True
    elif h.custom_points_policy == 'no':
        c.allow_custom_points = False
    if (h.bonus_policy or 'project') == 'no':
        c.max_bonus_points = 0
    c.approval_status = h.approval_status or c.approval_status


def log_action(contest_id, action, target="", old=None, new=None, details=None, username=None):
    """Append an audit entry (added to the current transaction; caller commits)."""
    def s(v):
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    db.session.add(AuditLog(
        contest_id=contest_id,
        username=username if username is not None else (get_current_user() or ""),
        action=action, target=str(target or "")[:255],
        old_value=s(old), new_value=s(new), details=s(details)))


def contest_status(c: Contest, now=None) -> str:
    now = now or datetime.utcnow()
    if c.approval_status == 'pending':
        return "pending_approval"
    if c.approval_status == 'rejected':
        return "rejected"
    if now < c.start_time:
        return "upcoming"
    if now > c.end_time:
        return "ended"
    return "running"


def find_contest_by_slug(slug: str):
    slug = (slug or "").lower()
    c = Contest.query.filter_by(slug=slug).first()
    if c:
        return c, False
    alias = SlugAlias.query.filter_by(old_slug=slug).first()
    if alias:
        return db.session.get(Contest, alias.contest_id), True
    return None, False


# =====================================================================
#  Sync (incremental, never destructive on partial failure)
# =====================================================================

def _reset_page(p_obj, level=1):
    p_obj.wiki_level = level
    p_obj.proofreader = None
    p_obj.validator = None
    p_obj.review_status = 'unproofread'
    p_obj.reviewed_by = None
    p_obj.reviewed_at = None
    p_obj.base_points = 0
    p_obj.bonus_points = 0
    p_obj.jury_note = None
    p_obj.locked_by_jury = None
    p_obj.jury_lock_expires = None


def sync_book_with_wikisource(contest: Contest, book: IndexBook, full: bool = False) -> bool:
    """Returns True if the wiki data was complete. All changes are committed at once."""
    if not book.is_active:
        return True
    book.title = normalize_index_title(contest.wiki_domain, book.base_name)
    start_t, end_t = book.window(contest)
    existing_pages = {p.page_number: p for p in ContestPage.query.filter_by(book_id=book.id).all()}
    known_revs = {} if full else {n: p.last_rev_id for n, p in existing_pages.items() if p.last_rev_id}

    live_map, complete = fetch_live_book_pages_from_wiki(
        contest.wiki_domain, book.base_name, start_t, end_t, known_revs=known_revs)

    if book.total_pages == 0:
        cnt = fetch_index_page_count(contest.wiki_domain, book.base_name)
        if cnt > 0:
            book.total_pages = cnt

    excluded_page_nums = parse_excluded_pages_spec(book.excluded_pages)
    excluded_usernames = {u.lower() for u in split_users(contest.excluded_users)}
    candidate_users = [lp["proofreader"] for lp in live_map.values() if lp.get("eligible") and lp.get("proofreader")]
    bot_usernames = identify_bot_users(contest.wiki_domain, candidate_users)
    blocked_users = excluded_usernames.union(bot_usernames)
    book_pts = get_book_points(contest, book)

    for page_num, lp in live_map.items():
        p_obj = existing_pages.get(page_num)
        if lp.get("unchanged"):
            # nothing changed on the wiki; only re-apply exclusions that admins may have edited
            if p_obj and p_obj.review_status != 'unproofread' and (
                    page_num in excluded_page_nums or (p_obj.proofreader or "").lower() in excluded_usernames):
                _reset_page(p_obj, p_obj.wiki_level or 1)
            continue

        proofreader_name = (lp.get("proofreader") or "").strip()
        is_excluded = page_num in excluded_page_nums or proofreader_name.lower() in blocked_users

        if not is_excluded and lp.get("eligible") and lp["level"] in (3, 4):
            if not p_obj:
                p_obj = ContestPage(
                    contest_id=contest.id, book_id=book.id, page_number=page_num,
                    page_title=lp["page_title"], wiki_level=lp["level"], proofreader=proofreader_name,
                    validator=lp.get("validator"), proofread_rev_id=lp.get("rev_id"),
                    proofread_time=lp.get("proofread_time") or datetime.utcnow(),
                    review_status='pending_review', base_points=book_pts)
                db.session.add(p_obj)
            else:
                p_obj.page_title = lp["page_title"]
                p_obj.wiki_level = lp["level"]
                p_obj.proofread_rev_id = lp.get("rev_id")
                if not same_user(p_obj.proofreader, proofreader_name) and p_obj.review_status != 'unproofread':
                    # a different person now owns the proofread revision -> review again
                    p_obj.review_status = 'pending_review'
                    p_obj.reviewed_by = None
                    p_obj.reviewed_at = None
                    p_obj.bonus_points = 0
                p_obj.proofreader = proofreader_name
                if lp.get("validator"):
                    p_obj.validator = lp.get("validator")
                if p_obj.review_status in ('unproofread', 'needs_fix'):
                    p_obj.review_status = 'pending_review'
                    p_obj.base_points = book_pts
                    p_obj.proofread_time = lp.get("proofread_time") or datetime.utcnow()
                elif p_obj.review_status in ('accepted', 'pending_review'):
                    p_obj.base_points = book_pts
            p_obj.last_rev_id = lp.get("rev_latest")
        else:
            if p_obj:
                if p_obj.review_status != 'unproofread':
                    _reset_page(p_obj, lp.get("level", 1))
                p_obj.last_rev_id = lp.get("rev_latest")

    # pages that disappeared from the wiki listing: only trust that if the listing was complete
    if complete:
        for page_num, p_obj in existing_pages.items():
            if (page_num not in live_map or page_num in excluded_page_nums) and p_obj.review_status != 'unproofread':
                _reset_page(p_obj, 1)
                p_obj.last_rev_id = None

    db.session.commit()
    return complete


def sync_contest_books(contest: Contest, full: bool = False) -> dict:
    """Guarded so two workers never sync the same contest at the same time."""
    now = datetime.utcnow()
    if contest.sync_started_at and (now - contest.sync_started_at).total_seconds() < 600:
        return {"skipped": True, "reason": "sync_in_progress"}
    contest.sync_started_at = now
    db.session.commit()
    complete = True
    try:
        for book in list(contest.books):
            if book.is_active:
                try:
                    complete = sync_book_with_wikisource(contest, book, full=full) and complete
                except Exception:
                    db.session.rollback()
                    complete = False
        contest.last_synced_at = datetime.utcnow()
    finally:
        contest.sync_started_at = None
        db.session.commit()
    try:
        assign_pending(contest)
    except Exception:
        db.session.rollback()
    return {"skipped": False, "complete": complete}


# =====================================================================
#  Fast statistics (SQL aggregates; never load 50k rows into Python)
# =====================================================================

def _accepted_expr(contest):
    if contest.is_review_enabled:
        return ContestPage.review_status == 'accepted'
    return ContestPage.review_status.in_(['accepted', 'pending_review'])


def book_stats_map(contest_id: int) -> dict:
    rows = (db.session.query(ContestPage.book_id, ContestPage.review_status, func.count(ContestPage.id))
            .filter(ContestPage.contest_id == contest_id)
            .group_by(ContestPage.book_id, ContestPage.review_status).all())
    out = {}
    for book_id, st, n in rows:
        d = out.setdefault(book_id, {"listed": 0, "accepted": 0, "pending": 0, "rejected": 0, "needs_fix": 0})
        if st != 'unproofread':
            d["listed"] += n
        if st == 'accepted':
            d["accepted"] += n
        elif st == 'pending_review':
            d["pending"] += n
        elif st == 'rejected':
            d["rejected"] += n
        elif st == 'needs_fix':
            d["needs_fix"] += n
    return out


def compute_contest_leaderboard(contest: Contest) -> list:
    acc = _accepted_expr(contest)
    rows = (db.session.query(
                ContestPage.proofreader,
                func.count(ContestPage.id),
                func.sum(case((acc, 1), else_=0)),
                func.sum(case((ContestPage.review_status == 'pending_review', 1), else_=0)),
                func.sum(case((ContestPage.review_status == 'needs_fix', 1), else_=0)),
                func.sum(case((ContestPage.review_status == 'rejected', 1), else_=0)),
                func.sum(case((acc, ContestPage.bonus_points), else_=0)),
                func.sum(case((acc, ContestPage.base_points + ContestPage.bonus_points), else_=0)))
            .filter(ContestPage.contest_id == contest.id,
                    ContestPage.review_status != 'unproofread',
                    ContestPage.proofreader.isnot(None))
            .group_by(ContestPage.proofreader).all())

    vrows = []
    if contest.validate_points and contest.validate_points > 0:
        vrows = (db.session.query(ContestPage.validator, func.count(ContestPage.id))
                 .filter(ContestPage.contest_id == contest.id,
                         ContestPage.review_status != 'unproofread',
                         ContestPage.wiki_level == 4,
                         ContestPage.validator.isnot(None))
                 .group_by(ContestPage.validator).all())

    juries_low = {u.lower() for u in split_users(contest.juries)}
    excluded_low = {u.lower() for u in split_users(contest.excluded_users)}
    blocked_low = excluded_low.union(identify_bot_users(contest.wiki_domain, [v[0] for v in vrows]) if vrows else set())

    stats = {}

    def ensure(u):
        key = u.strip().lower()
        if key not in stats:
            stats[key] = {"username": u, "submitted": 0, "accepted": 0, "validated": 0, "pending": 0,
                          "needs_fix": 0, "rejected": 0, "bonus": 0, "points": 0}
        return stats[key]

    for u, sub, acc_n, pen, nf, rej, bonus, pts in rows:
        if not u or u.lower() in blocked_low:
            continue
        s = ensure(u)
        s["submitted"] += int(sub or 0)
        s["accepted"] += int(acc_n or 0)
        s["pending"] += 0 if not contest.is_review_enabled else int(pen or 0)
        s["needs_fix"] += int(nf or 0)
        s["rejected"] += int(rej or 0)
        s["bonus"] += int(bonus or 0)
        s["points"] += int(pts or 0)

    for v, n in vrows:
        if not v or v.lower() in blocked_low:
            continue
        if v.lower() in juries_low and not contest.jury_gets_points:
            continue
        s = ensure(v)
        s["validated"] += int(n or 0)
        s["points"] += int(n or 0) * contest.validate_points

    board = list(stats.values())
    board.sort(key=lambda x: (x["points"], x["accepted"], x["submitted"]), reverse=True)
    return board


def compute_jury_statistics(contest: Contest) -> list:
    rows = (db.session.query(ContestPage.reviewed_by, ContestPage.review_status, func.count(ContestPage.id))
            .filter(ContestPage.contest_id == contest.id, ContestPage.reviewed_by.isnot(None),
                    ContestPage.review_status.in_(['accepted', 'needs_fix', 'rejected']))
            .group_by(ContestPage.reviewed_by, ContestPage.review_status).all())
    j_map = {}
    for j_name in split_users(contest.juries):
        j_map[j_name.lower()] = {"username": j_name, "reviewed": 0, "accepted": 0, "needs_fix": 0, "rejected": 0}
    for j, st, n in rows:
        d = j_map.setdefault(j.lower(), {"username": j, "reviewed": 0, "accepted": 0, "needs_fix": 0, "rejected": 0})
        d["reviewed"] += n
        d[st] += n
    res = list(j_map.values())
    res.sort(key=lambda x: x["reviewed"], reverse=True)
    return res


def compute_daily_activity(contest: Contest) -> list:
    day = func.date(ContestPage.proofread_time)
    acc = _accepted_expr(contest)
    rows = (db.session.query(day, func.count(ContestPage.id), func.sum(case((acc, 1), else_=0)))
            .filter(ContestPage.contest_id == contest.id, ContestPage.review_status != 'unproofread',
                    ContestPage.proofread_time.isnot(None))
            .group_by(day).order_by(day).all())
    result = [{"date": str(d)[:10], "proofread": int(n), "accepted": int(a or 0)} for d, n, a in rows if d]
    return result[-30:]


def backup_reminder(contest: Contest, now=None):
    now = now or datetime.utcnow()
    if contest.last_backup_at:
        days = (now - contest.last_backup_at).days
        return {"level": "week", "days": days} if days >= 7 else None
    age = (now - (contest.created_at or now)).days
    if age >= 30:
        return {"level": "month", "days": age}
    if age >= 7:
        return {"level": "week", "days": age}
    return None


# =====================================================================
#  App lifecycle
# =====================================================================

_schema_ready = [False]


@app.before_request
def init_db():
    if not _schema_ready[0]:
        migrate_schema()
        _schema_ready[0] = True
        db_config_warnings()
    if request.endpoint != "static":
        backup_engine.maybe_auto_snapshot(db.engine)


@app.before_request
def csrf_protect():
    """Every state-changing request must carry the session's CSRF token in X-CSRF-Token."""
    reason = security.csrf_failure_reason(PUBLIC_HOSTS)
    if reason:
        log.warning("CSRF check failed (%s) for %s %s", reason, request.method, request.path)
        return err("csrf_failed", 403)
    return None


@app.after_request
def add_security_headers(resp):
    return security.apply_security_headers(resp, https=app.config["SESSION_COOKIE_SECURE"])


@app.errorhandler(ValueError)
def handle_value_error(e):
    """Bad user input (non-numeric numbers, invalid domain...) is a 400, never a 500 with a trace."""
    code = str(e) if re.fullmatch(r"[a-z_]{3,40}", str(e) or "") else "invalid_value"
    return err(code, 400)


def db_config_warnings():
    url = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_config.is_sqlite(url):
        if os.environ.get("TOOL_TOOLSDB_USER") or os.path.exists(os.path.expanduser("~/replica.my.cnf")):
            log.warning("ProofSource is running on SQLite on Toolforge. Use ToolsDB (MariaDB): "
                        "set TOOLSDB_DATABASE and migrate with `python3 manage.py copy-db`.")
        path = backup_engine.sqlite_path_from_engine(db.engine)
        if path and os.path.exists(path):
            try:
                os.chmod(path, 0o600)   # tool homes are shared; keep the database private
            except OSError:
                pass


_ASSET_VERSIONS = {}


def asset_url(path: str) -> str:
    """/static URL with a content hash, so browsers never keep a stale CSS/JS after a deploy."""
    v = _ASSET_VERSIONS.get(path)
    if v is None:
        try:
            with open(os.path.join(app.static_folder, path), "rb") as f:
                v = hashlib.sha256(f.read()).hexdigest()[:10]
        except OSError:
            v = "0"
        _ASSET_VERSIONS[path] = v
    return f"/static/{path}?v={v}"


@app.context_processor
def template_helpers():
    return {"csp_nonce": security.csp_nonce, "asset_url": asset_url}


def render_spa(init: dict):
    init = dict(init)
    init["csrf_token"] = security.csrf_token()
    return render_template("index.html", initial=init)


def initial_route_data(lang=None, slug=None, hub_slug=None):
    data = {"contest_id": None, "slug": None, "hub_slug": hub_slug, "redirect_to": None}
    if slug:
        c, via_alias = find_contest_by_slug(slug)
        if c:
            data["contest_id"] = c.id
            data["slug"] = c.slug
            if via_alias or (lang and lang != c.lang_code):
                data["redirect_to"] = f"/contest/{c.lang_code}/{c.slug}"
    return data


@app.route("/favicon.ico")
def favicon_ico():
    return redirect("/static/favicon.svg", code=301)


@app.route("/")
def index():
    return render_spa(initial_route_data())


@app.route("/contest/<lang>/<slug>")
def contest_page(lang, slug):
    init = initial_route_data(lang, slug)
    if init["redirect_to"]:
        return redirect(init["redirect_to"], code=301)
    return render_spa(init)


@app.route("/hub/<slug>")
def hub_page_legacy(slug):
    return redirect("/central/" + quote(slug, safe=""), code=301)


@app.route("/central/<slug>")
def central_page(slug):
    return render_spa(initial_route_data(hub_slug=slug.lower()))


@app.route("/contest/<lang>/<slug>/me")
def contest_me_page(lang, slug):
    init = initial_route_data(lang, slug)
    if init["redirect_to"]:
        return redirect(init["redirect_to"] + "/me", code=301)
    init["subview"] = "me"
    return render_spa(init)


@app.route("/contest/<lang>/<slug>/user/<path:username>")
def contest_user_page(lang, slug, username):
    init = initial_route_data(lang, slug)
    if init["redirect_to"]:
        return redirect(init["redirect_to"] + "/user/" + quote(username, safe=""), code=301)
    init["subview"] = "user"
    init["username"] = username
    return render_spa(init)


def _plain(msg: str, status: int):
    """Plain-text error page: nothing from the URL is ever rendered as HTML."""
    return Response(msg, status=status, mimetype="text/plain")


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


@app.route("/login")
def oauth_login():
    if not oauth2_configured():
        return _plain("OAuth 2.0 client ID / secret not configured on server "
                      "(set OAUTH2_CLIENT_ID and OAUTH2_CLIENT_SECRET).", 500)
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    session["oauth_state"] = state
    session["oauth_pkce"] = verifier
    params = {
        "response_type": "code",
        "client_id": OAUTH2_CLIENT_ID,
        "redirect_uri": OAUTH2_REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect(OAUTH2_BASE + "/authorize?" + urlencode(params))

@app.route("/oauth-callback")
@app.route("/auth/callback")
def oauth_callback():
    if request.args.get("error"):
        # Never echo the query string back: it is attacker-controlled (reflected XSS).
        return _plain("Login was cancelled or refused by Wikimedia. Please go back and try again.", 400)
    code = request.args.get("code")
    state = request.args.get("state") or ""
    expected = session.pop("oauth_state", None)
    verifier = session.pop("oauth_pkce", None)
    if not code:
        return redirect(url_for("index"))
    if not expected or not secrets.compare_digest(state, expected):
        return _plain("OAuth state mismatch (session expired?). Please go back and click Login again.", 400)
    try:
        req = {"grant_type": "authorization_code", "code": code, "redirect_uri": OAUTH2_REDIRECT_URI}
        if verifier:
            req["code_verifier"] = verifier
        js = _token_request(req)
        username = _fetch_username(js["access_token"])
    except Exception:
        log.exception("OAuth callback failed")
        return _plain("Login failed while talking to Wikimedia. Please go back and try again.", 400)

    old = get_login_record()
    if old:
        db.session.delete(old)
    purge_expired_logins()
    rec = OAuthLogin(
        id=secrets.token_hex(32),
        username=username,
        access_token=_enc(js["access_token"]),
        refresh_token=_enc(js.get("refresh_token")),
        expires_at=_expiry_from(js)
    )
    db.session.add(rec)
    db.session.commit()
    session.clear()            # new session (prevents fixation); a fresh CSRF token is issued lazily
    session.permanent = True
    session["login_id"] = rec.id
    return redirect(url_for("index"))

@app.route("/logout", methods=["POST"])
def oauth_logout():
    rec = get_login_record()
    if rec:
        db.session.delete(rec)
        db.session.commit()
    session.clear()
    g.pop("login_cache", None)
    return jsonify({"success": True, "csrf_token": security.csrf_token()})


@app.route("/api/session", methods=["GET"])
def api_session():
    user = get_current_user()
    return jsonify({
        "logged_in": bool(user),
        "current_user": user,
        "is_super_admin": is_super_admin(user),
        "csrf_token": security.csrf_token(),
    })


# =====================================================================
#  Contests
# =====================================================================

@app.route("/api/contests/by-slug/<lang>/<slug>", methods=["GET"])
def api_contest_by_slug(lang, slug):
    c, via_alias = find_contest_by_slug(slug)
    if not c:
        return jsonify({"found": False}), 404
    return jsonify({"found": True, "contest_id": c.id, "slug": c.slug, "lang_code": c.lang_code, "via_alias": via_alias})


@app.route("/api/contests", methods=["GET"])
def api_list_contests():
    now = datetime.utcnow()
    current_user = get_current_user()
    contests = Contest.query.order_by(Contest.start_time.desc()).all()

    counts = {}
    for cid, st, n in (db.session.query(ContestPage.contest_id, ContestPage.review_status, func.count(ContestPage.id))
                       .group_by(ContestPage.contest_id, ContestPage.review_status).all()):
        d = counts.setdefault(cid, {"listed": 0, "accepted": 0})
        if st != 'unproofread':
            d["listed"] += n
        if st == 'accepted':
            d["accepted"] += n
    book_counts = dict(db.session.query(IndexBook.contest_id, func.count(IndexBook.id)).group_by(IndexBook.contest_id).all())

    result = []
    for c in contests:
        is_adm = is_contest_admin(c, current_user)
        if c.approval_status in ('rejected', 'pending') and not is_adm:
            # approval check only when needed (it calls the wiki API)
            if not can_approve_contest(c, current_user):
                if c.approval_status == 'rejected':
                    continue
        can_approve = can_approve_contest(c, current_user) if c.approval_status == 'pending' and current_user else False
        cnt = counts.get(c.id, {"listed": 0, "accepted": 0})
        result.append({
            "id": c.id, "slug": c.slug, "lang_code": c.lang_code, "title": c.title,
            "description": c.description, "wiki_domain": c.wiki_domain,
            "start_iso": iso(c.start_time), "end_iso": iso(c.end_time),
            "status": contest_status(c, now), "approval_status": c.approval_status,
            "created_by": c.created_by, "book_count": book_counts.get(c.id, 0),
            "listed_pages": cnt["listed"], "accepted_pages": cnt["accepted"],
            "is_admin": is_adm, "is_jury": is_reviewer(c, current_user), "can_approve": can_approve,
            "review_enabled": c.is_review_enabled,
            "hub_slug": c.hub.slug if c.hub else None,
        })
    centrals = [central_brief(h, now) for h in Hub.query.order_by(Hub.start_time.desc()).all()
                if h.approval_status != 'rejected' or is_central_admin(h, current_user)]
    delete_requests = []
    if is_super_admin(current_user):
        delete_requests = ([{"type": "contest", "id": c.id, "slug": c.slug, "lang_code": c.lang_code, "title": c.title,
                             **delete_request_info(c)} for c in contests if c.delete_requested_by] +
                           [{"type": "central", "slug": h.slug, "title": h.title, **delete_request_info(h)}
                            for h in Hub.query.filter(Hub.delete_requested_by.isnot(None)).all()])
    return jsonify({"contests": result, "centrals": centrals, "delete_requests": delete_requests})


MAX_BOOKS_PER_REQUEST = 100


def add_books_from_multiline(contest: Contest, raw_books: str):
    lines = [b.strip() for b in str(raw_books or "").replace("\r", "\n").split("\n") if b.strip()]
    added = []
    for raw_b in lines[:MAX_BOOKS_PER_REQUEST]:   # every book costs wiki API calls
        base_name = extract_base_book_name(contest.wiki_domain, raw_b)
        norm_title = normalize_index_title(contest.wiki_domain, raw_b)
        if not base_name or len(norm_title) > LEN_TITLE:
            continue
        existing = IndexBook.query.filter_by(contest_id=contest.id, base_name=base_name).first()
        if existing:
            existing.is_active = True
            existing.title = norm_title
            b_obj = existing
        else:
            b_obj = IndexBook(contest_id=contest.id, title=norm_title, base_name=base_name,
                              total_pages=fetch_index_page_count(contest.wiki_domain, base_name),
                              custom_points=None, excluded_pages="", is_active=True)
            db.session.add(b_obj)
            db.session.flush()
        added.append(norm_title)
        log_action(contest.id, "book_add", norm_title)
        try:
            sync_book_with_wikisource(contest, b_obj)
        except Exception:
            db.session.rollback()
    return added


def _apply_hub(contest: Contest, hub_slug: str):
    hub_slug = (hub_slug or "").strip().lower()
    if not hub_slug:
        contest.hub_id = None
        return None
    hub = Hub.query.filter_by(slug=hub_slug).first()
    if not hub:
        raise ValueError("hub_not_found")
    contest.hub_id = hub.id
    return hub


@app.route("/api/contests", methods=["POST"])
def api_create_contest():
    current_user = get_current_user()
    if not current_user:
        return err("auth_required", 401)
    data = json_body()
    if not (data.get("title") or "").strip():
        return err("title_required")
    try:
        start_t = parse_dt_in(data["start_time"])
        end_t = parse_dt_in(data["end_time"])
    except (KeyError, ValueError):
        return err("invalid_dates")
    if end_t <= start_t:
        return err("end_before_start")

    wiki_domain = normalize_wiki_domain(data.get("wiki_domain") or "bn.wikisource.org")
    if not wiki_domain:
        return err("invalid_project")
    if data.get("jury_mode", JURY_MODES[0]) not in JURY_MODES or \
            data.get("reject_action", REJECT_ACTIONS[0]) not in REJECT_ACTIONS:
        return err("invalid_value")
    limited = rate_limit("create", 10, 3600, current_user)
    if limited:
        return limited
    auto_approved = is_user_wiki_admin(wiki_domain, current_user)
    admins_raw = clip(data.get("admins"), LEN_USERS)
    if not user_in_list(current_user, admins_raw):
        admins_raw = f"{current_user}, {admins_raw}".strip(", ")

    c = Contest(
        slug=generate_unique_slug(data.get("slug", "")),
        title=clip(data["title"], LEN_TITLE),
        description=clip(data.get("description"), LEN_TEXT),
        wiki_domain=wiki_domain, start_time=start_t, end_time=end_t,
        proofread_points=to_int(data.get("proofread_points"), 3, 0, 10000),
        validate_points=to_int(data.get("validate_points"), 0, 0, 10000),
        max_bonus_points=to_int(data.get("max_bonus_points"), 0, 0, 10000),
        jury_mode=data.get("jury_mode", "in_app_validate"),
        jury_gets_points=bool(data.get("jury_gets_points", False)),
        reject_action=data.get("reject_action", "hold_batch"),
        created_by=current_user, admins=admins_raw,
        juries=clip(data.get("juries") or current_user, LEN_USERS),
        excluded_users=clip(data.get("excluded_users"), LEN_USERS),
        approval_status='approved' if auto_approved else 'pending',
        approved_by=current_user if auto_approved else None,
        review_enabled=bool(data.get("review_enabled", True)),
        bulk_review_enabled=bool(data.get("bulk_review_enabled", False)),
        announcement_en=clip(data.get("announcement_en"), LEN_TEXT),
        announcement_bn=clip(data.get("announcement_bn"), LEN_TEXT),
        review_assignment="equal" if data.get("review_assignment") == "equal" else "open",
        allow_custom_points=bool(data.get("allow_custom_points", True)),
        last_synced_at=datetime.utcnow())
    db.session.add(c)
    db.session.flush()
    log_action(c.id, "contest_create", c.slug, new={"title": c.title, "start": iso(c.start_time), "end": iso(c.end_time)})
    db.session.commit()
    add_books_from_multiline(c, data.get("books", "") or "")
    db.session.commit()
    return jsonify({"success": True, "contest_id": c.id, "slug": c.slug, "lang_code": c.lang_code,
                    "approval_status": c.approval_status})


@app.route("/api/contests/<int:cid>/approve", methods=["POST"])
def api_approve_contest(cid):
    c = Contest.query.get_or_404(cid)
    current_user = get_current_user()
    if not can_approve_contest(c, current_user):
        return err("approve_forbidden", 403)
    decision = json_body().get("decision", "approved")
    old = c.approval_status
    c.approval_status = "approved" if decision == "approved" else "rejected"
    c.approved_by = current_user
    log_action(c.id, "contest_approval", c.slug, old=old, new=c.approval_status)
    db.session.commit()
    return jsonify({"success": True, "approval_status": c.approval_status})


def contest_payload(c: Contest, current_user):
    now = datetime.utcnow()
    is_adm = is_contest_admin(c, current_user)
    is_jry = is_reviewer(c, current_user)
    can_app = can_approve_contest(c, current_user) if c.approval_status == 'pending' and current_user else False

    bstats = book_stats_map(c.id)
    books_data = []
    total_book_pages = 0
    for b in c.books:
        excl_set = parse_excluded_pages_spec(b.excluded_pages)
        excl_in_range = len([p for p in excl_set if 1 <= p <= b.total_pages]) if b.total_pages > 0 else len(excl_set)
        effective_pages = max(0, b.total_pages - excl_in_range)
        if b.is_active:
            total_book_pages += effective_pages
        st = bstats.get(b.id, {"listed": 0, "accepted": 0, "pending": 0, "rejected": 0, "needs_fix": 0})
        accepted = st["accepted"] + (0 if c.is_review_enabled else st["pending"])
        bs, be = b.window(c)
        books_data.append({
            "id": b.id, "title": b.title, "base_name": b.base_name, "total_pages": b.total_pages,
            "effective_pages": effective_pages, "points": get_book_points(c, b),
            "custom_points": b.custom_points, "excluded_pages": b.excluded_pages or "",
            "is_active": b.is_active, "listed": st["listed"], "accepted": accepted,
            "pending": st["pending"] if c.is_review_enabled else 0, "rejected": st["rejected"],
            "start_iso": iso(b.start_time), "end_iso": iso(b.end_time),
            "effective_start_iso": iso(bs), "effective_end_iso": iso(be),
        })

    totals = {"listed": 0, "accepted": 0, "pending": 0}
    for st in bstats.values():
        totals["listed"] += st["listed"]
        totals["accepted"] += st["accepted"]
        totals["pending"] += st["pending"]
    if not c.is_review_enabled:
        totals["accepted"] += totals["pending"]
        totals["pending"] = 0
    rejected_hold_count = ContestPage.query.filter_by(contest_id=c.id, review_status='rejected', wiki_level=3).count()

    my_needs_fix, my_pages_count = [], 0
    if current_user:
        my_needs_fix = [
            {"id": p.id, "page_title": p.page_title, "proofreader": p.proofreader, "jury_note": p.jury_note}
            for p in ContestPage.query.filter_by(contest_id=c.id, review_status='needs_fix', proofreader=current_user).limit(200).all()]
        my_pages_count = _user_pages_query(c, current_user).count()

    leaderboard = compute_contest_leaderboard(c)
    stale = (not c.last_synced_at) or (now - c.last_synced_at).total_seconds() > 300

    return {
        "contest": {
            "id": c.id, "slug": c.slug, "lang_code": c.lang_code, "title": c.title,
            "description": c.description, "wiki_domain": c.wiki_domain,
            "start_time": c.start_time.strftime(DT_FMT_IN), "end_time": c.end_time.strftime(DT_FMT_IN),
            "start_iso": iso(c.start_time), "end_iso": iso(c.end_time),
            "last_synced_iso": iso(c.last_synced_at),
            "status": contest_status(c, now), "approval_status": c.approval_status,
            "created_by": c.created_by, "approved_by": c.approved_by,
            "proofread_points": c.proofread_points, "validate_points": c.validate_points,
            "max_bonus_points": c.max_bonus_points or 0, "jury_mode": c.jury_mode,
            "jury_gets_points": c.jury_gets_points, "reject_action": c.reject_action,
            "admins": c.admins, "juries": c.juries,
            "juries_list": split_users(c.juries), "admins_list": split_users(c.admins),
            "excluded_users": c.excluded_users or "",
            "review_enabled": c.is_review_enabled, "bulk_review_enabled": bool(c.bulk_review_enabled),
            "announcement_en": c.announcement_en or "", "announcement_bn": c.announcement_bn or "",
            "hub_slug": c.hub.slug if c.hub else "", "hub_title": c.hub.title if c.hub else "",
            "locked_fields": locked_fields(c),
            "delete_request": delete_request_info(c),
            "can_request_delete": is_listed_admin(c, current_user) and not c.hub_id,
            "can_delete": can_delete(c, current_user) and not c.hub_id,
            "is_super_admin": is_super_admin(current_user),
            "allow_custom_points": allows_custom_points(c), "allow_bonus": allows_bonus(c),
            "custom_points_choice": (c.hub.custom_points_policy if c.hub else "project"),
            "review_assignment": c.review_assignment or "open",
            "validation_enabled": bool(c.validate_points and c.validate_points > 0),
            "validation_visible": validation_visible(c, current_user),
            "my_review_stats": my_review_stats(c, current_user) if is_jry and c.is_review_enabled else None,
            "is_admin": is_adm, "is_jury": is_jry, "can_approve": can_app,
            "my_pages_count": my_pages_count,
            "last_backup_iso": iso(c.last_backup_at) if is_adm else None,
            "backup_reminder": backup_reminder(c, now) if is_adm else None,
            "needs_sync": stale and c.approval_status == "approved",
            "sync_running": bool(c.sync_started_at and (now - c.sync_started_at).total_seconds() < 600),
        },
        "stats": {
            "total_books": len([b for b in c.books if b.is_active]),
            "total_book_pages": total_book_pages,
            "listed_pages": totals["listed"], "accepted_pages": totals["accepted"],
            "pending_pages": totals["pending"], "rejected_hold_count": rejected_hold_count,
            "participants": len(leaderboard),
        },
        "books": books_data,
        "leaderboard": leaderboard,
        "jury_stats": compute_jury_statistics(c) if c.is_review_enabled else [],
        "daily_activity": compute_daily_activity(c),
        "needs_fix_alerts": my_needs_fix,
    }


@app.route("/api/contests/<int:cid>", methods=["GET"])
def api_get_contest(cid):
    """Returns cached data immediately. ?sync=1 forces a blocking sync (manual button)."""
    c = Contest.query.get_or_404(cid)
    user = get_current_user()
    if request.args.get("sync") == "1" and c.approval_status == "approved" and user \
            and not _sync_too_soon(c, True) and rate_limit("sync", 10, 600, user) is None:
        sync_contest_books(c)
    return jsonify(contest_payload(c, user))


def _sync_too_soon(c: Contest, force: bool) -> bool:
    """A sync walks the wiki history of every changed page; never run them back to back."""
    if not c.last_synced_at:
        return False
    age = (datetime.utcnow() - c.last_synced_at).total_seconds()
    return age < (app.config["FORCED_SYNC_MIN_SECONDS"] if force else 120)


@app.route("/api/contests/<int:cid>/sync", methods=["POST"])
def api_sync_contest(cid):
    """Background sync called by the frontend after showing cached data."""
    c = Contest.query.get_or_404(cid)
    if c.approval_status != "approved":
        return jsonify({"success": True, "skipped": True, "reason": "not_approved"})
    user = get_current_user()
    force = bool(json_body().get("force")) and bool(user)   # only logged-in users can force a sync
    if _sync_too_soon(c, force):
        return jsonify({"success": True, "skipped": True, "reason": "fresh"})
    limited = rate_limit("sync", 10, 600, user) if force else None
    if limited:
        return limited
    res = sync_contest_books(c)
    return jsonify({"success": True, **res})


CONFIG_FIELDS_TEXT = ["title", "description", "wiki_domain", "jury_mode", "reject_action", "admins", "juries",
                      "excluded_users", "announcement_en", "announcement_bn", "review_assignment"]
CONFIG_FIELDS_INT = ["proofread_points", "validate_points", "max_bonus_points"]
CONFIG_FIELDS_BOOL = ["jury_gets_points", "review_enabled", "bulk_review_enabled", "allow_custom_points"]
BOOL_DEFAULTS = {"review_enabled": True, "allow_custom_points": True}


@app.route("/api/contests/<int:cid>/config", methods=["POST"])
def api_update_contest_config(cid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("admin_only", 403)
    data = dict(json_body())
    locked = set(locked_fields(c))
    for f in locked:
        data.pop(f, None)
    if data.get("review_assignment") not in (None, "open", "equal") or \
            data.get("jury_mode") not in (None,) + JURY_MODES or \
            data.get("reject_action") not in (None,) + REJECT_ACTIONS:
        return err("invalid_value")
    if "title" in data and not clip(data.get("title"), LEN_TITLE):
        return err("title_required")
    domain_changed = False
    if "wiki_domain" in data and data["wiki_domain"] is not None:
        dom = normalize_wiki_domain(data["wiki_domain"])
        if not dom:
            return err("invalid_project")
        data["wiki_domain"] = dom
        domain_changed = dom != c.wiki_domain

    changes = {}
    try:
        new_start = parse_dt_in(data["start_time"]) if data.get("start_time") else c.start_time
        new_end = parse_dt_in(data["end_time"]) if data.get("end_time") else c.end_time
    except ValueError:
        return err("invalid_dates")
    if new_end <= new_start:
        return err("end_before_start")

    new_slug = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get("slug") or "").strip()).lower()[:60]
    if new_slug and new_slug != c.slug:
        clash = (Contest.query.filter(Contest.slug == new_slug, Contest.id != c.id).first()
                 or SlugAlias.query.filter(SlugAlias.old_slug == new_slug, SlugAlias.contest_id != c.id).first())
        if clash:
            return err("slug_taken")
        SlugAlias.query.filter_by(old_slug=new_slug, contest_id=c.id).delete()
        if not SlugAlias.query.filter_by(old_slug=c.slug).first():
            db.session.add(SlugAlias(old_slug=c.slug, contest_id=c.id))
        changes["slug"] = (c.slug, new_slug)
        c.slug = new_slug

    old_default_pts = c.proofread_points
    for f in CONFIG_FIELDS_TEXT:
        if f in data and data[f] is not None:
            val = clip(data[f], LEN_TITLE if f == "title" else LEN_TEXT if f.startswith("announcement") or f == "description" else LEN_USERS)
            if val != (getattr(c, f) or ""):
                changes[f] = (getattr(c, f), val)
                setattr(c, f, val)
    for f in CONFIG_FIELDS_INT:
        if f in data and data[f] not in (None, ""):
            val = to_int(data[f], 0, 0, 10000)
            if val != getattr(c, f):
                changes[f] = (getattr(c, f), val)
                setattr(c, f, val)
    for f in CONFIG_FIELDS_BOOL:
        if f in data:
            val = bool(data[f])
            cur = getattr(c, f)
            cur = BOOL_DEFAULTS.get(f, False) if cur is None else bool(cur)
            if val != cur:
                changes[f] = (cur, val)
                setattr(c, f, val)

    window_changed = new_start != c.start_time or new_end != c.end_time
    if window_changed:
        changes["window"] = ([iso(c.start_time), iso(c.end_time)], [iso(new_start), iso(new_end)])
        c.start_time, c.end_time = new_start, new_end

    if domain_changed and not c.hub_id and not is_user_wiki_admin(c.wiki_domain, get_current_user()):
        # approval was given for the old wiki; an admin of the new wiki has to approve again
        changes["approval_status"] = (c.approval_status, "pending")
        c.approval_status, c.approved_by = "pending", None

    apply_central_rules(c)   # child contests: central rules always win

    if "proofread_points" in changes or "allow_custom_points" in changes:
        for b in c.books:
            if b.custom_points == old_default_pts:
                b.custom_points = None
        _recalc_points(c)

    for key, (old, new) in changes.items():
        action = {"window": "contest_dates_change", "juries": "reviewers_change", "excluded_users": "user_exclusion",
                  "announcement_en": "announcement_change", "announcement_bn": "announcement_change",
                  "proofread_points": "points_change"}.get(key, "config_change")
        log_action(c.id, action, key, old=old, new=new)

    if window_changed:
        ContestPage.query.filter_by(contest_id=c.id).update({"last_rev_id": None}, synchronize_session=False)
        c.last_synced_at = None
    db.session.commit()

    if {"review_assignment", "juries", "review_enabled"} & set(changes):
        assign_pending(c, rebalance=True)

    if (data.get("books") or "").strip():
        add_books_from_multiline(c, data["books"])
        db.session.commit()
    return jsonify({"success": True, "changes": list(changes.keys())})


def _recalc_points(contest: Contest, book: IndexBook = None):
    books = [book] if book else contest.books
    for b in books:
        pts = get_book_points(contest, b)
        (ContestPage.query.filter(ContestPage.book_id == b.id,
                                  ContestPage.review_status.in_(['accepted', 'pending_review']))
         .update({"base_points": pts}, synchronize_session=False))


def _validate_book_input(data, total_pages=0):
    excl = clip(data.get("excluded_pages"), 5000)
    _, bad = parse_excluded_pages_strict(excl, total_pages or 0)
    if bad:
        return None, err("invalid_excluded_pages", invalid=bad)
    try:
        bs = parse_dt_in(data.get("start_time")) if data.get("start_time") else None
        be = parse_dt_in(data.get("end_time")) if data.get("end_time") else None
    except ValueError:
        return None, err("invalid_dates")
    if bs and be and be <= bs:
        return None, err("end_before_start")
    return {"excluded_pages": excl, "start_time": bs, "end_time": be}, None


@app.route("/api/contests/<int:cid>/books", methods=["POST"])
def api_add_contest_book(cid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("unauthorized", 403)
    data = json_body()
    raw_title = clip(data.get("title"), 1000)
    if not raw_title:
        return err("book_title_required")
    vals, e = _validate_book_input(data)
    if e:
        return e
    base_name = extract_base_book_name(c.wiki_domain, raw_title)
    norm_title = normalize_index_title(c.wiki_domain, raw_title)
    if not base_name or len(norm_title) > LEN_TITLE:
        return err("book_title_required")
    pts = to_int(data.get("points"), None, 0, 10000)
    pts = pts if pts is not None and pts != c.proofread_points else None

    b_obj = IndexBook.query.filter_by(contest_id=cid, base_name=base_name).first()
    if b_obj:
        b_obj.is_active = True
        b_obj.title = norm_title
    else:
        b_obj = IndexBook(contest_id=cid, title=norm_title, base_name=base_name,
                          total_pages=fetch_index_page_count(c.wiki_domain, base_name), is_active=True)
        db.session.add(b_obj)
    b_obj.custom_points = pts
    b_obj.excluded_pages = vals["excluded_pages"]
    b_obj.start_time, b_obj.end_time = vals["start_time"], vals["end_time"]
    db.session.flush()
    log_action(cid, "book_add", norm_title, new={"points": get_book_points(c, b_obj), "excluded": vals["excluded_pages"],
                                                 "start": iso(vals["start_time"]), "end": iso(vals["end_time"])})
    db.session.commit()
    try:
        sync_book_with_wikisource(c, b_obj, full=True)
    except Exception:
        db.session.rollback()
    return jsonify({"success": True, "book_id": b_obj.id})


@app.route("/api/contests/<int:cid>/books/<int:bid>/update", methods=["POST"])
def api_update_contest_book(cid, bid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("unauthorized", 403)
    book = IndexBook.query.filter_by(id=bid, contest_id=cid).first_or_404()
    data = json_body()
    payload = {"excluded_pages": data.get("excluded_pages", book.excluded_pages or ""),
               "start_time": data.get("start_time", book.start_time.strftime(DT_FMT_IN) if book.start_time else ""),
               "end_time": data.get("end_time", book.end_time.strftime(DT_FMT_IN) if book.end_time else "")}
    vals, e = _validate_book_input(payload, book.total_pages)
    if e:
        return e
    old = {"points": get_book_points(c, book), "excluded": book.excluded_pages or "",
           "start": iso(book.start_time), "end": iso(book.end_time)}
    if "points" in data and data["points"] not in (None, ""):
        p = to_int(data["points"], 0, 0, 10000)
        book.custom_points = None if p == c.proofread_points else p
    window_changed = (vals["start_time"] != book.start_time) or (vals["end_time"] != book.end_time)
    book.excluded_pages = vals["excluded_pages"]
    book.start_time, book.end_time = vals["start_time"], vals["end_time"]
    _recalc_points(c, book)
    new = {"points": get_book_points(c, book), "excluded": book.excluded_pages,
           "start": iso(book.start_time), "end": iso(book.end_time)}
    if old != new:
        log_action(cid, "points_change" if old["points"] != new["points"] else "book_edit", book.title, old=old, new=new)
    db.session.commit()
    try:
        sync_book_with_wikisource(c, book, full=window_changed)
    except Exception:
        db.session.rollback()
    return jsonify({"success": True})


@app.route("/api/contests/<int:cid>/books/<int:bid>/remove", methods=["POST"])
def api_remove_contest_book(cid, bid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("unauthorized", 403)
    book = IndexBook.query.filter_by(id=bid, contest_id=cid).first_or_404()
    mode = json_body().get("mode", "keep_points")
    if mode == "keep_points":
        book.is_active = False
        ContestPage.query.filter_by(book_id=book.id, review_status='unproofread').delete()
    else:
        db.session.delete(book)
    log_action(cid, "book_remove", book.title, details={"mode": mode})
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/contests/<int:cid>/reserve-page", methods=["POST"])
def api_reserve_contest_page(cid):
    c = Contest.query.get_or_404(cid)
    if c.approval_status != 'approved':
        return err("contest_awaiting_approval")
    current_user = get_current_user()
    if not current_user:
        # reservations block a page for everyone else, so they need an account
        return err("auth_required", 401)
    limited = rate_limit("reserve", 30, 600, current_user)
    if limited:
        return limited
    book_id = to_int(json_body().get("book_id"), None)
    books_q = IndexBook.query.filter_by(contest_id=cid, is_active=True)
    if book_id:
        books_q = books_q.filter_by(id=book_id)
    active_books = books_q.all()
    if not active_books:
        return err("no_active_book", 404)

    book = random.choice(active_books) if not book_id else active_books[0]
    live_levels = fetch_all_existing_page_levels(c.wiki_domain, book.base_name)
    prefixes = get_namespace_prefixes(c.wiki_domain)
    excluded_set = parse_excluded_pages_spec(book.excluded_pages)
    now = datetime.utcnow()
    max_p = min(book.total_pages, 100000) if book.total_pages > 0 else 200

    locked = (db.session.query(ContestPage.page_number, ContestPage.reserved_by_user)
              .filter(ContestPage.book_id == book.id, ContestPage.user_lock_expires > now).all())
    reserved_nums = {n for n, u in locked if u != current_user}
    submitted_nums = [n for (n,) in db.session.query(ContestPage.page_number)
                      .filter(ContestPage.book_id == book.id, ContestPage.review_status != 'unproofread').all()]
    submitted_nums += [n for n, _ in locked]

    available_nums = [n for n in range(1, max_p + 1)
                      if live_levels.get(n, 1) < 3 and n not in reserved_nums and n not in excluded_set]
    if not available_nums:
        return err("no_unproofread_pages", 404)

    chosen_num = None
    if submitted_nums:
        spaced = [n for n in available_nums if n >= max(submitted_nums) + 11]
        if spaced:
            chosen_num = spaced[0]
    if not chosen_num:
        chosen_num = random.choice(available_nums)

    page_title = f"{prefixes['page']}:{book.base_name}/{chosen_num}"
    p_rec = ContestPage.query.filter_by(book_id=book.id, page_number=chosen_num).first()
    if not p_rec:
        p_rec = ContestPage(contest_id=cid, book_id=book.id, page_number=chosen_num,
                            page_title=page_title, wiki_level=1, review_status='unproofread')
        db.session.add(p_rec)
    p_rec.reserved_by_user = current_user
    p_rec.user_lock_expires = now + timedelta(minutes=20)
    db.session.commit()
    edit_url = f"https://{c.wiki_domain}/w/index.php?" + urlencode({"title": page_title, "action": "edit"})
    return jsonify({"success": True, "page_title": page_title, "edit_url": edit_url})


def _user_pages_query(c: Contest, username: str):
    """A participant's own contest work: pages they proofread, plus pages they validated only when
    validation earns points (never a jury's in-tool work when juries get no validation points)."""
    cond = ContestPage.proofreader == username
    if validation_visible(c, username):
        cond = or_(cond, and_(ContestPage.validator == username, ContestPage.wiki_level == 4))
    return ContestPage.query.filter(ContestPage.contest_id == c.id, ContestPage.review_status != 'unproofread', cond)


@app.route("/api/contests/<int:cid>/user/<path:username>/pages", methods=["GET"])
def api_contest_user_pages(cid, username):
    c = Contest.query.get_or_404(cid)
    page_no = max(1, request.args.get("page", default=1, type=int))
    per = min(500, max(10, request.args.get("per", default=100, type=int)))
    status = request.args.get("status") or "all"
    base = _user_pages_query(c, username)
    q = base
    if status != "all":
        if status == "accepted" and not c.is_review_enabled:
            q = q.filter(ContestPage.review_status.in_(["accepted", "pending_review"]))
        else:
            q = q.filter(ContestPage.review_status == status)
    q = q.order_by(ContestPage.book_id.asc(), ContestPage.page_number.asc())
    total = q.count()
    pages = q.offset((page_no - 1) * per).limit(per).all()
    titles = {b.id: b.title for b in c.books}
    show_val = validation_visible(c, username)

    board = compute_contest_leaderboard(c)
    rank, me = None, None
    for i, r in enumerate(board, start=1):
        if same_user(r["username"], username):
            rank, me = i, r
            break
    summary = {"rank": rank, "participants": len(board),
               "proofread": me["submitted"] if me else 0, "accepted": me["accepted"] if me else 0,
               "pending": me["pending"] if me else 0, "needs_fix": me["needs_fix"] if me else 0,
               "rejected": me["rejected"] if me else 0, "bonus": me["bonus"] if me else 0,
               "points": me["points"] if me else 0}
    if show_val:
        summary["validated"] = me["validated"] if me else 0
    by_book = {}
    for bid, n in (base.with_entities(ContestPage.book_id, func.count(ContestPage.id))
                   .filter(ContestPage.proofreader == username).group_by(ContestPage.book_id).all()):
        by_book[titles.get(bid, "")] = n

    def row(p):
        is_proof = same_user(p.proofreader, username)
        acc = p.counts_as_accepted(c)
        return {
            "id": p.id, "book_id": p.book_id, "book_title": titles.get(p.book_id, ""),
            "page_number": p.page_number, "page_title": p.page_title, "wiki_level": p.wiki_level,
            "proofreader": p.proofreader, "role": "proofreader" if is_proof else "validator",
            "proofread_rev_id": p.proofread_rev_id, "proofread_iso": iso(p.proofread_time),
            "review_status": "accepted" if acc else p.review_status,
            "total_points": ((p.base_points + p.bonus_points) if acc else 0) if is_proof else c.validate_points,
            "reviewed_by": p.reviewed_by if c.is_review_enabled else None,
            "jury_note": p.jury_note if c.is_review_enabled else None,
        }

    return jsonify({"username": username, "wiki_domain": c.wiki_domain, "total": total, "page": page_no, "per": per,
                    "summary": summary, "by_book": by_book, "show_validation": show_val,
                    "review_enabled": c.is_review_enabled, "pages": [row(p) for p in pages]})


# =====================================================================
#  Review
# =====================================================================

def assign_pending(contest: Contest, rebalance: bool = False) -> dict:
    """
    Equal-distribution mode: every reviewer ends up with about the same number of pages
    (already reviewed + assigned). Pages are handed out in book/page order, keeping runs of
    consecutive pages of the same book with the same reviewer where possible.
    rebalance=True first releases all pending assignments (used when the mode or reviewer list changes).
    """
    if contest.review_assignment != 'equal' or not contest.is_review_enabled:
        return {"assigned": 0}
    juries = split_users(contest.juries)
    if not juries:
        return {"assigned": 0}
    jl = {j.lower(): j for j in juries}
    pending_q = ContestPage.query.filter(ContestPage.contest_id == contest.id,
                                         ContestPage.review_status == 'pending_review')
    if rebalance:
        pending_q.update({"assigned_to": None}, synchronize_session=False)
        db.session.flush()

    load = {j.lower(): 0 for j in juries}
    for name, n in (db.session.query(ContestPage.reviewed_by, func.count(ContestPage.id))
                    .filter(ContestPage.contest_id == contest.id,
                            ContestPage.review_status.in_(['accepted', 'needs_fix', 'rejected']),
                            ContestPage.reviewed_by.isnot(None))
                    .group_by(ContestPage.reviewed_by).all()):
        if name and name.lower() in load:
            load[name.lower()] += n
    for name, n in (db.session.query(ContestPage.assigned_to, func.count(ContestPage.id))
                    .filter(ContestPage.contest_id == contest.id, ContestPage.review_status == 'pending_review',
                            ContestPage.assigned_to.isnot(None))
                    .group_by(ContestPage.assigned_to).all()):
        if name and name.lower() in load:
            load[name.lower()] += n

    unassigned = (ContestPage.query.filter(ContestPage.contest_id == contest.id,
                                           ContestPage.review_status == 'pending_review',
                                           or_(ContestPage.assigned_to.is_(None),
                                               ~func.lower(ContestPage.assigned_to).in_(list(load.keys()))))
                  .order_by(ContestPage.book_id, ContestPage.page_number).all())
    if not unassigned:
        return {"assigned": 0}
    total = sum(load.values()) + len(unassigned)
    target = -(-total // len(juries))   # ceil
    last_owner = {}
    assigned = 0
    for p in unassigned:
        own = (p.proofreader or "").strip().lower()
        prev = last_owner.get(p.book_id)
        if prev and prev != own and load[prev] < target:
            pick = prev
        else:
            candidates = [j for j in load if j != own] or list(load)
            pick = min(candidates, key=lambda j: (load[j], j))
        p.assigned_to = jl[pick]
        load[pick] += 1
        last_owner[p.book_id] = pick
        assigned += 1
    db.session.commit()
    return {"assigned": assigned, "target": target}


def review_scope(c: Contest, reviewer: str, requested: str = None) -> str:
    if c.review_assignment != 'equal':
        return 'all'
    if requested == 'all' and is_contest_admin(c, reviewer):
        return 'all'
    if requested is None and is_contest_admin(c, reviewer) and not user_in_list(reviewer, c.juries):
        return 'all'
    return 'mine'


def my_review_stats(c: Contest, reviewer: str) -> dict:
    done = ContestPage.query.filter(ContestPage.contest_id == c.id, ContestPage.reviewed_by == reviewer,
                                    ContestPage.review_status.in_(['accepted', 'needs_fix', 'rejected'])).count()
    assigned = 0
    if c.review_assignment == 'equal':
        assigned = ContestPage.query.filter(ContestPage.contest_id == c.id, ContestPage.assigned_to == reviewer,
                                            ContestPage.review_status == 'pending_review').count()
    return {"done": done, "assigned_pending": assigned, "mode": c.review_assignment or 'open'}


def _review_query(c: Contest, reviewer: str, book_id=None, status="pending_review", contributor=None, scope='all'):
    now = datetime.utcnow()
    q = ContestPage.query.filter(
        ContestPage.contest_id == c.id,
        ContestPage.review_status != 'unproofread',
        ContestPage.proofreader.isnot(None),
        func.lower(ContestPage.proofreader) != (reviewer or "").strip().lower(),
        or_(ContestPage.locked_by_jury.is_(None), ContestPage.locked_by_jury == reviewer,
            ContestPage.jury_lock_expires.is_(None), ContestPage.jury_lock_expires < now))
    if book_id:
        q = q.filter(ContestPage.book_id == book_id)
    if contributor:
        q = q.filter(func.lower(ContestPage.proofreader) == contributor.strip().lower())
    if status and status != "all":
        q = q.filter(ContestPage.review_status == status)
    if scope == 'mine':
        q = q.filter(or_(ContestPage.assigned_to == reviewer, ContestPage.reviewed_by == reviewer))
    return q


@app.route("/api/contests/<int:cid>/jury/next-page", methods=["GET"])
def api_jury_next_page(cid):
    c = Contest.query.get_or_404(cid)
    reviewer = get_current_user()
    if not is_reviewer(c, reviewer):
        return jsonify({"has_page": False, "error_code": "unauthorized", "error": "Unauthorized"}), 403
    if not c.is_review_enabled:
        return jsonify({"has_page": False, "error_code": "review_disabled", "queue": []})

    book_id = request.args.get("book_id", type=int)
    after_page = request.args.get("after_page", default=0, type=int)
    after_book = request.args.get("after_book", type=int)
    specific_page_id = request.args.get("page_id", type=int)
    status_filter = request.args.get("status", default="pending_review", type=str)
    contributor = (request.args.get("contributor") or "").strip() or None
    peek = request.args.get("peek") == "1"
    now = datetime.utcnow()

    scope = review_scope(c, reviewer, request.args.get("scope"))
    fq = _review_query(c, reviewer, book_id, status_filter, contributor, scope)
    order = (ContestPage.book_id.asc(), ContestPage.page_number.asc())

    if specific_page_id:
        next_page = ContestPage.query.filter(
            ContestPage.id == specific_page_id, ContestPage.contest_id == cid,
            func.lower(ContestPage.proofreader) != (reviewer or "").lower()).first()
    else:
        if after_book:
            nq = fq.filter(or_(ContestPage.book_id > after_book,
                               and_(ContestPage.book_id == after_book, ContestPage.page_number > after_page)))
        else:
            nq = fq.filter(ContestPage.page_number > after_page)
        next_page = nq.order_by(*order).first()
        if not next_page and (after_page > 0 or after_book):
            next_page = fq.order_by(*order).first()

    queue_total = fq.count()
    queue_rows = (fq.with_entities(ContestPage.id, ContestPage.book_id, ContestPage.page_number, ContestPage.review_status)
                  .order_by(*order).limit(500).all())
    queue = [{"id": i, "book_id": b, "page_number": n, "review_status": s} for i, b, n, s in queue_rows]

    stats = my_review_stats(c, reviewer)
    if not next_page:
        return jsonify({"has_page": False, "queue": queue, "queue_total": queue_total, "scope": scope, "my_stats": stats})

    held_by_other = (next_page.locked_by_jury and not same_user(next_page.locked_by_jury, reviewer)
                     and next_page.jury_lock_expires and next_page.jury_lock_expires > now)
    if not peek and not held_by_other:   # never steal another reviewer's live lock
        next_page.locked_by_jury = reviewer
        next_page.jury_lock_expires = now + timedelta(minutes=10)
        db.session.commit()

    wiki_data = fetch_page_content_and_image(c.wiki_domain, next_page.book.base_name,
                                             next_page.page_number, next_page.page_title)
    return jsonify({
        "has_page": True, "queue": queue, "queue_total": queue_total, "scope": scope, "my_stats": stats,
        "page": {
            "id": next_page.id, "book_id": next_page.book_id, "book_title": next_page.book.title,
            "page_number": next_page.page_number, "page_title": next_page.page_title,
            "wiki_level": next_page.wiki_level, "proofreader": next_page.proofreader,
            "review_status": next_page.review_status, "reviewed_by": next_page.reviewed_by,
            "bonus_points": next_page.bonus_points, "jury_note": next_page.jury_note or "",
            "header": wiki_data["header"], "body": wiki_data["body"], "footer": wiki_data["footer"],
            "first_body": wiki_data["first_body"], "first_rev_id": wiki_data["first_rev_id"],
            "diff_tokens": wiki_data["diff_tokens"], "rendered_html": wiki_data["rendered_html"],
            "image_url": wiki_data["image_url"],
            "rev_id": wiki_data["rev_id"] or next_page.proofread_rev_id
        }
    })


@app.route("/api/contests/<int:cid>/jury/lock", methods=["POST"])
def api_jury_lock(cid):
    """Lightweight lock used when the UI shows a prefetched page instantly."""
    c = Contest.query.get_or_404(cid)
    reviewer = get_current_user()
    if not is_reviewer(c, reviewer):
        return err("unauthorized", 403)
    page = ContestPage.query.filter_by(id=to_int(json_body().get("page_id"), 0), contest_id=cid).first_or_404()
    now = datetime.utcnow()
    if page.locked_by_jury and page.locked_by_jury != reviewer and page.jury_lock_expires and page.jury_lock_expires > now:
        return err("page_locked", 409, locked_by=page.locked_by_jury)
    page.locked_by_jury = reviewer
    page.jury_lock_expires = now + timedelta(minutes=10)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/contests/<int:cid>/jury/preview", methods=["POST"])
def api_jury_preview(cid):
    c = Contest.query.get_or_404(cid)
    if not is_reviewer(c, get_current_user()):
        return err("unauthorized", 403)
    limited = rate_limit("preview", 120, 600)
    if limited:
        return limited
    data = json_body()
    html = render_wikitext_preview(c.wiki_domain, clip(data.get("page_title"), LEN_TITLE),
                                   to_int(data.get("wiki_level"), 3, 0, 4),
                                   get_current_user() or "User", str(data.get("header") or ""),
                                   str(data.get("body") or ""), str(data.get("footer") or ""))
    return jsonify({"rendered_html": html})


def _apply_review(c: Contest, page: ContestPage, reviewer: str, decision: str, bonus: int, note: str,
                  oauth_creds: dict, header="", body="", footer="", touch_wiki=True) -> dict:
    """Single place that applies a review decision. Caller commits."""
    old_status = page.review_status
    page.review_status = decision
    page.reviewed_by = reviewer
    page.reviewed_at = datetime.utcnow()
    page.jury_note = (note or "")[:500]
    page.locked_by_jury = None
    page.jury_lock_expires = None
    wiki_result = {"saved_on_wiki": False}

    if decision == "accepted":
        page.base_points = get_book_points(c, page.book)
        page.bonus_points = to_int(bonus, 0, 0, c.max_bonus_points or 0)
        if touch_wiki and c.jury_mode == "in_app_validate":
            wiki_result = edit_wikisource_live(
                wiki_domain=c.wiki_domain, page_title=page.page_title, level=4, username=reviewer,
                header=header, body=body, footer=footer,
                summary=f"Validated via ProofSource ({c.title})", oauth_creds=oauth_creds,
                fetch_existing_if_empty=not body)
            if wiki_result.get("saved_on_wiki"):
                page.wiki_level = wiki_result.get("level", 4)
                page.validator = wiki_result.get("user", reviewer)
                page.last_rev_id = wiki_result.get("revid") or page.last_rev_id
    else:
        page.base_points = 0 if decision != "pending_review" else get_book_points(c, page.book)
        page.bonus_points = 0
        if touch_wiki and decision == "rejected" and c.reject_action == "instant_revert":
            wiki_result = edit_wikisource_live(
                wiki_domain=c.wiki_domain, page_title=page.page_title, level=1, username=reviewer,
                header=header, body=body, footer=footer,
                summary=f"Reverted to unproofread via ProofSource ({c.title})",
                oauth_creds=oauth_creds, fetch_existing_if_empty=True)
            if wiki_result.get("saved_on_wiki"):
                page.wiki_level = 1
                page.last_rev_id = wiki_result.get("revid") or page.last_rev_id

    log_action(c.id, "review_decision", page.page_title, old=old_status, new=decision,
               details={"bonus": page.bonus_points, "note": page.jury_note,
                        "wiki_saved": bool(wiki_result.get("saved_on_wiki"))}, username=reviewer)
    return wiki_result


@app.route("/api/contests/<int:cid>/jury/submit", methods=["POST"])
def api_jury_submit(cid):
    c = Contest.query.get_or_404(cid)
    reviewer = get_current_user()
    if not is_reviewer(c, reviewer):
        return err("unauthorized", 403)
    if not c.is_review_enabled:
        return err("review_disabled")
    data = json_body()
    decision = data.get("decision")
    if decision not in VALID_DECISIONS:
        return err("invalid_decision")
    page = ContestPage.query.filter_by(id=to_int(data.get("page_id"), 0), contest_id=cid).first_or_404()
    if same_user(page.proofreader, reviewer):
        return err("own_page_review")
    if page.review_status == 'unproofread':
        return err("page_not_reviewable")
    if page.locked_by_jury and not same_user(page.locked_by_jury, reviewer) \
            and page.jury_lock_expires and page.jury_lock_expires > datetime.utcnow():
        return err("page_locked", 409, locked_by=page.locked_by_jury)
    if c.review_assignment == 'equal' and page.assigned_to and not same_user(page.assigned_to, reviewer) \
            and not is_contest_admin(c, reviewer):
        return err("not_assigned_to_you", 403)
    if page.review_status == decision and same_user(page.reviewed_by, reviewer) \
            and to_int(data.get("bonus_points"), 0) == (page.bonus_points or 0) \
            and clip(data.get("jury_note"), LEN_NOTE) == (page.jury_note or ""):
        return jsonify({"success": True, "duplicate": True, "book_id": page.book_id,
                        "page_number": page.page_number, "wiki_result": {"saved_on_wiki": False}})

    wiki_result = _apply_review(c, page, reviewer, decision, data.get("bonus_points", 0),
                                clip(data.get("jury_note"), LEN_NOTE), get_oauth_creds(),
                                str(data.get("header") or ""), str(data.get("body") or ""),
                                str(data.get("footer") or ""))
    db.session.commit()
    return jsonify({"success": True, "book_id": page.book_id, "page_number": page.page_number,
                    "wiki_result": wiki_result})


@app.route("/api/contests/<int:cid>/bulk/candidates", methods=["GET"])
def api_bulk_candidates(cid):
    c = Contest.query.get_or_404(cid)
    reviewer = get_current_user()
    if not is_reviewer(c, reviewer):
        return err("unauthorized", 403)
    if not (c.is_review_enabled and c.bulk_review_enabled):
        return err("bulk_disabled", 403)
    q = _review_query(c, reviewer, request.args.get("book_id", type=int),
                      request.args.get("status", "pending_review"),
                      (request.args.get("contributor") or "").strip() or None,
                      review_scope(c, reviewer, request.args.get("scope")))
    rows = (q.with_entities(ContestPage.id, ContestPage.page_title, ContestPage.proofreader, ContestPage.review_status)
            .order_by(ContestPage.book_id, ContestPage.page_number).limit(5000).all())
    return jsonify({"success": True, "total": q.count(),
                    "pages": [{"id": i, "page_title": t, "proofreader": u, "review_status": s} for i, t, u, s in rows]})


@app.route("/api/contests/<int:cid>/bulk/review", methods=["POST"])
def api_bulk_review(cid):
    """Processes up to 50 pages per call (frontend sends chunks). Each page commits on its own,
    so a failure never loses the pages that already succeeded."""
    c = Contest.query.get_or_404(cid)
    reviewer = get_current_user()
    if not is_reviewer(c, reviewer):
        return err("unauthorized", 403)
    if not (c.is_review_enabled and c.bulk_review_enabled):
        return err("bulk_disabled", 403)
    data = json_body()
    decision = data.get("decision")
    if decision not in ("accepted", "rejected"):
        return err("invalid_decision")
    raw_ids = data.get("page_ids") or []
    if not isinstance(raw_ids, list):
        return err("invalid_value")
    ids = [to_int(x, 0) for x in raw_ids][:50]
    touch_wiki = bool(data.get("touch_wiki", True))
    creds = get_oauth_creds()
    done, skipped_own, unchanged, failed = [], [], [], []
    for pid in ids:
        page = ContestPage.query.filter_by(id=pid, contest_id=cid).first()
        if not page or page.review_status == 'unproofread':
            failed.append({"id": pid, "error_code": "page_not_reviewable"})
            continue
        if same_user(page.proofreader, reviewer):
            skipped_own.append(pid)
            continue
        if c.review_assignment == 'equal' and page.assigned_to and not same_user(page.assigned_to, reviewer) \
                and not is_contest_admin(c, reviewer):
            failed.append({"id": pid, "error_code": "not_assigned_to_you"})
            continue
        if page.review_status == decision:
            unchanged.append(pid)
            continue
        try:
            res = _apply_review(c, page, reviewer, decision, 0, clip(data.get("jury_note"), LEN_NOTE), creds, touch_wiki=touch_wiki)
            db.session.commit()
            done.append(pid)
            if touch_wiki and res.get("error"):
                failed.append({"id": pid, "error_code": "wiki_edit_failed", "error": res.get("error"), "status_saved": True})
        except Exception:
            db.session.rollback()
            log.exception("bulk review failed for page %s", pid)
            failed.append({"id": pid, "error_code": "exception", "error": "internal error"})
    log_action(cid, "bulk_review", f"{decision}:{len(ids)}",
               details={"done": len(done), "skipped_own": len(skipped_own), "unchanged": len(unchanged),
                        "failed": len(failed)}, username=reviewer)
    db.session.commit()
    return jsonify({"success": True, "done": done, "skipped_own": skipped_own, "unchanged": unchanged, "failed": failed})


@app.route("/api/contests/<int:cid>/batch-revert", methods=["POST"])
def api_batch_revert(cid):
    c = Contest.query.get_or_404(cid)
    reviewer = get_current_user()
    if not is_contest_admin(c, reviewer):
        return err("unauthorized", 403)
    creds = get_oauth_creds()
    rejected_pages = ContestPage.query.filter_by(contest_id=cid, review_status='rejected', wiki_level=3).limit(50).all()
    reverted, errors = 0, []
    for p in rejected_pages:
        res = edit_wikisource_live(wiki_domain=c.wiki_domain, page_title=p.page_title, level=1, username=reviewer,
                                   header="", body="", footer="",
                                   summary=f"Batch revert rejected page via ProofSource ({c.title})",
                                   oauth_creds=creds, fetch_existing_if_empty=True)
        if res.get("saved_on_wiki"):
            p.wiki_level = 1
            p.last_rev_id = res.get("revid") or p.last_rev_id
            reverted += 1
            db.session.commit()
        else:
            errors.append(res.get("error", "Failed"))
    remaining = ContestPage.query.filter_by(contest_id=cid, review_status='rejected', wiki_level=3).count()
    log_action(cid, "batch_revert", str(len(rejected_pages)), details={"reverted": reverted, "errors": len(errors)})
    db.session.commit()
    return jsonify({"success": True, "total_rejected": len(rejected_pages), "reverted_on_wiki": reverted,
                    "remaining": remaining, "error": errors[0] if errors else None})


# =====================================================================
#  Export, backup, restore, audit
# =====================================================================

@app.route("/api/contests/<int:cid>/export-csv", methods=["GET"])
def api_export_csv(cid):
    c = Contest.query.get_or_404(cid)
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["Rank", "Participant", "Proofread", "Accepted", "Validated", "Pending", "Needs Fix",
                 "Rejected", "Bonus Points", "Total Points"])
    for idx, r in enumerate(compute_contest_leaderboard(c), start=1):
        cw.writerow([idx, r["username"], r["submitted"], r["accepted"], r["validated"], r["pending"],
                     r["needs_fix"], r["rejected"], r["bonus"], r["points"]])
    return Response(si.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename=proofsource_{c.slug}_results.csv"})


@app.route("/api/contests/<int:cid>/backup.json", methods=["GET"])
def api_backup_json(cid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("admin_only", 403)
    limited = rate_limit("backup", 20, 3600)
    if limited:
        return limited
    payload = backup_engine.export_contest(c, ContestPage, AuditLog, secret=app.secret_key)
    try:
        backup_engine.save_json_file(payload, f"contest{c.id}-{c.slug}")   # server-side copy too
    except Exception:
        log.exception("server-side backup copy failed")
    c.last_backup_at = datetime.utcnow()
    log_action(c.id, "backup_export", "json", details=payload["counts"])
    db.session.commit()
    body = json.dumps(payload, ensure_ascii=False)
    fname = f"proofsource_{c.slug}_backup_{datetime.utcnow().strftime('%Y%m%d-%H%M')}.json"
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": f"attachment;filename={fname}"})


@app.route("/api/contests/<int:cid>/backup-pages.csv", methods=["GET"])
def api_backup_pages_csv(cid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("admin_only", 403)
    log_action(c.id, "backup_export", "csv")
    db.session.commit()
    fname = f"proofsource_{c.slug}_pages_{datetime.utcnow().strftime('%Y%m%d-%H%M')}.csv"
    return Response(stream_with_context(backup_engine.pages_csv_iter(c, ContestPage)), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename={fname}"})


def _read_backup_upload():
    if "file" in request.files:
        raw = request.files["file"].read()
    else:
        raw = request.get_data()
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("not_json_object")
    return payload


def _backup_problems(payload, c: Contest, user: str) -> list:
    """Backups are signed with the server secret: a contest admin cannot hand-craft one (e.g. to
    give pages points nobody earned). Only the tool admin may restore unsigned/legacy files."""
    return backup_engine.validate_backup(payload, expected_contest_id=c.id, secret=app.secret_key,
                                         allow_unsigned=is_super_admin(user))


@app.route("/api/contests/<int:cid>/restore/validate", methods=["POST"])
def api_restore_validate(cid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("admin_only", 403)
    try:
        payload = _read_backup_upload()
    except Exception:
        return err("invalid_backup", problems=["not_json"])
    problems = _backup_problems(payload, c, get_current_user())
    return jsonify({"success": not problems, "problems": problems, "counts": payload.get("counts"),
                    "exported_at": payload.get("exported_at")})


@app.route("/api/contests/<int:cid>/restore", methods=["POST"])
def api_restore(cid):
    c = Contest.query.get_or_404(cid)
    user = get_current_user()
    if not is_contest_admin(c, user):
        return err("admin_only", 403)
    try:
        payload = _read_backup_upload()
    except Exception:
        return err("invalid_backup", problems=["not_json"])
    problems = _backup_problems(payload, c, user)
    if problems:
        return err("invalid_backup", problems=problems)
    limited = rate_limit("restore", 5, 600, user)
    if limited:
        return limited
    try:
        backup_engine.snapshot_database(db.engine, f"pre-restore-c{c.id}")
    except Exception:
        log.exception("pre-restore snapshot failed")
    try:
        res = backup_engine.restore_contest(db, c, payload, {
            "Contest": Contest, "IndexBook": IndexBook, "ContestPage": ContestPage,
            "AuditLog": AuditLog, "Hub": Hub}, user)
    except Exception:
        log.exception("restore failed for contest %s", c.id)
        return err("restore_failed", 500)
    return jsonify({"success": True, **res})


@app.route("/api/contests/<int:cid>/audit", methods=["GET"])
def api_audit(cid):
    c = Contest.query.get_or_404(cid)
    if not is_contest_admin(c, get_current_user()):
        return err("admin_only", 403)
    page_no = max(1, request.args.get("page", default=1, type=int))
    per = min(200, max(10, request.args.get("per", default=50, type=int)))
    q = AuditLog.query.filter_by(contest_id=cid)
    action = request.args.get("action")
    if action:
        q = q.filter_by(action=action)
    total = q.count()
    rows = q.order_by(AuditLog.id.desc()).offset((page_no - 1) * per).limit(per).all()
    return jsonify({"success": True, "total": total, "page": page_no, "per": per, "entries": [
        {"id": a.id, "username": a.username, "action": a.action, "target": a.target,
         "old_value": a.old_value, "new_value": a.new_value, "details": a.details,
         "created_iso": iso(a.created_at)} for a in rows]})


@app.route("/api/admin/snapshots", methods=["GET", "POST"])
def api_snapshots():
    user = get_current_user()
    if not is_super_admin(user):
        return err("super_admin_only", 403)
    if request.method == "POST":
        res = backup_engine.snapshot_database(db.engine, "manual")
        return jsonify({"success": res.get("ok", False), **res})
    return jsonify({"success": True, "snapshots": backup_engine.list_snapshots(), "dir": backup_engine.backup_dir()})


# =====================================================================
#  Deletion (request by a contest admin, executed by the tool admin)
# =====================================================================

def _delete_contest(c: Contest, actor: str, reason: str = "") -> str:
    """Server-side JSON backup first, then delete. The audit log keeps the record."""
    payload = backup_engine.export_contest(c, ContestPage, AuditLog, secret=app.secret_key)
    path = backup_engine.save_json_file(payload, f"deleted-contest{c.id}-{c.slug}", keep=None)
    log_action(c.id, "contest_deleted", c.slug, details={"title": c.title, "requested_by": c.delete_requested_by,
                                                         "reason": reason or c.delete_reason, "backup": os.path.basename(path)},
               username=actor)
    SlugAlias.query.filter_by(contest_id=c.id).delete()
    ContestPage.query.filter_by(contest_id=c.id).delete(synchronize_session=False)
    IndexBook.query.filter_by(contest_id=c.id).delete(synchronize_session=False)
    db.session.delete(c)
    return os.path.basename(path)


def _request_delete(obj, user, reason):
    obj.delete_requested_by = user
    obj.delete_requested_at = datetime.utcnow()
    obj.delete_reason = (reason or "").strip()[:1000]


@app.route("/api/contests/<int:cid>/delete-request", methods=["POST", "DELETE"])
def api_contest_delete_request(cid):
    c = Contest.query.get_or_404(cid)
    user = get_current_user()
    if c.hub_id:
        return err("delete_via_central")
    if not is_listed_admin(c, user):
        return err("admin_only", 403)
    if request.method == "DELETE":
        c.delete_requested_by = None; c.delete_requested_at = None; c.delete_reason = ""
        log_action(c.id, "delete_request_cancelled", c.slug)
    else:
        _request_delete(c, user, json_body().get("reason"))
        log_action(c.id, "delete_requested", c.slug, details=c.delete_reason)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/contests/<int:cid>/delete", methods=["POST"])
def api_contest_delete(cid):
    c = Contest.query.get_or_404(cid)
    user = get_current_user()
    if c.hub_id:
        return err("delete_via_central")
    if not can_delete(c, user):
        return err("delete_forbidden", 403)
    backup = _delete_contest(c, user)
    db.session.commit()
    return jsonify({"success": True, "backup": backup})


@app.route("/api/central/<slug>/delete-request", methods=["POST", "DELETE"])
def api_central_delete_request(slug):
    h = Hub.query.filter_by(slug=slug.lower()).first_or_404()
    user = get_current_user()
    if not is_listed_admin(h, user):
        return err("admin_only", 403)
    if request.method == "DELETE":
        h.delete_requested_by = None; h.delete_requested_at = None; h.delete_reason = ""
    else:
        _request_delete(h, user, json_body().get("reason"))
    log_action(None, "central_delete_request" if request.method == "POST" else "central_delete_request_cancelled", h.slug)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/central/<slug>/delete", methods=["POST"])
def api_central_delete(slug):
    h = Hub.query.filter_by(slug=slug.lower()).first_or_404()
    user = get_current_user()
    if not can_delete(h, user):
        return err("delete_forbidden", 403)
    backups = [_delete_contest(c, user, f"central {h.slug} deleted") for c in list(h.contests)]
    log_action(None, "central_deleted", h.slug, details={"title": h.title, "requested_by": h.delete_requested_by,
                                                        "reason": h.delete_reason, "backups": backups}, username=user)
    db.session.delete(h)
    db.session.commit()
    return jsonify({"success": True, "backups": backups})


# =====================================================================
#  Central contests (central control panel)
# =====================================================================

POLICY_VALUES = ("yes", "no", "project")


def central_status(h: Hub, now=None) -> str:
    now = now or datetime.utcnow()
    if h.approval_status == 'pending':
        return "pending_approval"
    if h.approval_status == 'rejected':
        return "rejected"
    if h.start_time and now < h.start_time:
        return "upcoming"
    if h.end_time and now > h.end_time:
        return "ended"
    return "running"


def is_central_admin(h: Hub, user: str) -> bool:
    if not user:
        return False
    if is_super_admin_name(user) or same_user(h.created_by, user):
        return True
    return user_in_list(user, h.admins)


def can_approve_central(h: Hub, user: str, domains=None) -> bool:
    if not user:
        return False
    if is_super_admin_name(user):
        return True
    domains = domains if domains is not None else [c.wiki_domain for c in h.contests]
    return any(is_user_wiki_admin(d, user) for d in set(domains))


def _parse_projects(raw) -> list:
    out, seen = [], set()
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("invalid_project")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid_project")
        if not str(item.get("wiki_domain") or "").strip():
            continue
        dom = normalize_wiki_domain(item.get("wiki_domain"))
        if not dom:
            raise ValueError("invalid_project")
        if dom in seen:
            continue
        seen.add(dom)
        out.append({"wiki_domain": dom, "admins": clip(", ".join(split_users(str(item.get("admins") or ""))), LEN_USERS)})
    return out


def sync_children(h: Hub, projects: list, actor: str):
    """Create / update one child contest per project. Raises ValueError('project_has_data') when a
    project with recorded pages would be removed."""
    existing = {c.wiki_domain: c for c in h.contests}
    wanted = {p["wiki_domain"]: p for p in projects}
    for dom, c in existing.items():
        if dom not in wanted:
            if ContestPage.query.filter(ContestPage.contest_id == c.id, ContestPage.review_status != 'unproofread').count():
                raise ValueError("project_has_data")
            log_action(c.id, "project_removed", dom, username=actor)
            db.session.delete(c)
    for dom, p in wanted.items():
        c = existing.get(dom)
        admins = p["admins"] or h.created_by
        if not c:
            lang = dom.split(".")[0] if dom.count(".") >= 2 else "mul"
            c = Contest(slug=generate_unique_slug(f"{h.slug}-{lang}"), title=h.title, description=h.description or "",
                        wiki_domain=dom, start_time=h.start_time, end_time=h.end_time, created_by=actor,
                        admins=admins, juries=admins, hub_id=h.id, approval_status=h.approval_status,
                        approved_by=h.approved_by, last_synced_at=None)
            db.session.add(c)
            db.session.flush()
            log_action(c.id, "contest_create", c.slug, new={"central": h.slug, "admins": admins}, username=actor)
        elif split_users(c.admins) != split_users(admins):
            log_action(c.id, "config_change", "admins", old=c.admins, new=admins, username=actor)
            c.admins = admins
        c.hub = h
        apply_central_rules(c)


def propagate_central(h: Hub, dates_changed: bool, points_changed: bool):
    for c in h.contests:
        apply_central_rules(c)
        if dates_changed:
            ContestPage.query.filter_by(contest_id=c.id).update({"last_rev_id": None}, synchronize_session=False)
            c.last_synced_at = None
        if points_changed:
            _recalc_points(c)


def _central_fields_from(data: dict, h: Hub):
    if "title" in data:
        if not (data.get("title") or "").strip():
            raise ValueError("title_required")
        h.title = clip(data["title"], LEN_TITLE)
    if "link" in data and data["link"] is not None:
        link = str(data["link"]).strip()
        if link and not security.safe_http_url(link):
            raise ValueError("invalid_link")   # blocks javascript:/data: links (stored XSS)
        h.link = link
    for f, n in (("description", LEN_TEXT), ("admins", LEN_USERS)):
        if f in data and data[f] is not None:
            setattr(h, f, clip(data[f], n))
    if data.get("start_time"):
        h.start_time = parse_dt_in(data["start_time"])
    if data.get("end_time"):
        h.end_time = parse_dt_in(data["end_time"])
    if not h.start_time or not h.end_time:
        raise ValueError("invalid_dates")
    if h.end_time <= h.start_time:
        raise ValueError("end_before_start")
    for f in ("proofread_points", "validate_points"):
        if f in data and data[f] not in (None, ""):
            setattr(h, f, to_int(data[f], 0, 0, 10000))
    for f in ("custom_points_policy", "bonus_policy", "review_policy"):
        if f in data:
            if data[f] not in POLICY_VALUES:
                raise ValueError("invalid_value")
            setattr(h, f, data[f])


def central_brief(h: Hub, now=None) -> dict:
    return {"slug": h.slug, "title": h.title, "status": central_status(h, now), "approval_status": h.approval_status,
            "start_iso": iso(h.start_time), "end_iso": iso(h.end_time), "link": security.safe_http_url(h.link),
            "child_ids": [c.id for c in h.contests], "project_count": len(h.contests)}


@app.route("/api/central", methods=["GET"])
def api_list_central():
    user = get_current_user()
    now = datetime.utcnow()
    out = []
    for h in Hub.query.order_by(Hub.start_time.desc()).all():
        if h.approval_status == 'rejected' and not is_central_admin(h, user):
            continue
        out.append(central_brief(h, now))
    return jsonify({"centrals": out})


@app.route("/api/central", methods=["POST"])
def api_create_central():
    user = get_current_user()
    if not user:
        return err("auth_required", 401)
    data = json_body()
    try:
        projects = _parse_projects(data.get("projects"))
    except ValueError as e:
        return err(str(e))
    if not projects:
        return err("projects_required")
    limited = rate_limit("create", 10, 3600, user)
    if limited:
        return limited
    slug = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get("slug") or "").strip()).lower()[:60] or str(random.randint(100000, 999999))
    if Hub.query.filter_by(slug=slug).first():
        return err("slug_taken")
    h = Hub(slug=slug, title="", created_by=user, admins="")
    if not (data.get("title") or "").strip():
        return err("title_required")
    try:
        _central_fields_from(data, h)
    except ValueError as e:
        return err(str(e))
    if not user_in_list(user, h.admins):
        h.admins = f"{user}, {h.admins}".strip(", ")
    auto = can_approve_central(h, user, [p["wiki_domain"] for p in projects])
    h.approval_status = 'approved' if auto else 'pending'
    h.approved_by = user if auto else None
    db.session.add(h)
    db.session.flush()
    try:
        sync_children(h, projects, user)
    except ValueError as e:
        db.session.rollback()
        return err(str(e))
    log_action(None, "central_create", slug, new={"title": h.title, "projects": [p["wiki_domain"] for p in projects]})
    db.session.commit()
    return jsonify({"success": True, "slug": h.slug, "approval_status": h.approval_status})


@app.route("/api/central/<slug>/config", methods=["POST"])
def api_update_central(slug):
    h = Hub.query.filter_by(slug=slug.lower()).first_or_404()
    user = get_current_user()
    if not is_central_admin(h, user):
        return err("admin_only", 403)
    data = json_body()
    old = (h.start_time, h.end_time, h.proofread_points, h.custom_points_policy)
    try:
        _central_fields_from(data, h)
        if "projects" in data:
            sync_children(h, _parse_projects(data["projects"]), user)
    except ValueError as e:
        db.session.rollback()
        return err(str(e))
    propagate_central(h, dates_changed=(old[0], old[1]) != (h.start_time, h.end_time),
                      points_changed=(old[2], old[3]) != (h.proofread_points, h.custom_points_policy))
    log_action(None, "central_config", h.slug, details={k: v for k, v in data.items() if k != "projects"})
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/central/<slug>/approve", methods=["POST"])
def api_approve_central(slug):
    h = Hub.query.filter_by(slug=slug.lower()).first_or_404()
    user = get_current_user()
    if not can_approve_central(h, user):
        return err("approve_forbidden", 403)
    decision = json_body().get("decision", "approved")
    h.approval_status = "approved" if decision == "approved" else "rejected"
    h.approved_by = user
    for c in h.contests:
        c.approval_status = h.approval_status
        c.approved_by = user
    log_action(None, "central_approval", h.slug, new=h.approval_status)
    db.session.commit()
    return jsonify({"success": True, "approval_status": h.approval_status})


@app.route("/api/central/<slug>", methods=["GET"])
def api_get_central(slug):
    h = Hub.query.filter_by(slug=slug.lower()).first_or_404()
    user = get_current_user()
    now = datetime.utcnow()
    combined, projects, daily = {}, [], {}
    totals = {"listed": 0, "accepted": 0, "books": 0}
    for c in sorted(h.contests, key=lambda x: x.wiki_domain):
        board = compute_contest_leaderboard(c)
        bstats = book_stats_map(c.id)
        listed = sum(v["listed"] for v in bstats.values())
        accepted = sum(v["accepted"] + (0 if c.is_review_enabled else v["pending"]) for v in bstats.values())
        books = len([b for b in c.books if b.is_active])
        totals["listed"] += listed
        totals["accepted"] += accepted
        totals["books"] += books
        projects.append({"contest_id": c.id, "slug": c.slug, "lang_code": c.lang_code, "wiki_domain": c.wiki_domain,
                         "title": c.title, "admins_list": split_users(c.admins), "books": books, "listed": listed,
                         "accepted": accepted, "participants": len(board), "points": sum(r["points"] for r in board),
                         "review_enabled": c.is_review_enabled, "is_admin": is_contest_admin(c, user)})
        for r in board:
            key = r["username"].strip().lower()   # SUL: one global account name on every wiki
            d = combined.setdefault(key, {"username": r["username"], "points": 0, "accepted": 0, "submitted": 0,
                                          "projects": {}})
            d["points"] += r["points"]
            d["accepted"] += r["accepted"]
            d["submitted"] += r["submitted"]
            d["projects"][c.wiki_domain] = d["projects"].get(c.wiki_domain, 0) + r["points"]
        for row in compute_daily_activity(c):
            d = daily.setdefault(row["date"], {"date": row["date"], "proofread": 0, "accepted": 0})
            d["proofread"] += row["proofread"]
            d["accepted"] += row["accepted"]
    board = sorted(combined.values(), key=lambda x: (x["points"], x["accepted"]), reverse=True)
    is_adm = is_central_admin(h, user)
    return jsonify({
        "central": {
            **central_brief(h, now), "description": h.description or "",
            "proofread_points": h.proofread_points, "validate_points": h.validate_points or 0,
            "custom_points_policy": h.custom_points_policy or "project", "bonus_policy": h.bonus_policy or "project",
            "review_policy": h.review_policy or "project",
            "start_time": h.start_time.strftime(DT_FMT_IN) if h.start_time else "",
            "end_time": h.end_time.strftime(DT_FMT_IN) if h.end_time else "",
            "admins": h.admins or "", "admins_list": split_users(h.admins), "created_by": h.created_by,
            "is_admin": is_adm,
            "can_approve": can_approve_central(h, user) if h.approval_status == 'pending' and user else False,
            "delete_request": delete_request_info(h),
            "can_request_delete": is_listed_admin(h, user), "can_delete": can_delete(h, user),
        },
        "projects": projects,
        "leaderboard": board,
        "stats": {"participants": len(board), "listed": totals["listed"], "accepted": totals["accepted"],
                  "books": totals["books"], "projects": len(projects),
                  "daily": sorted(daily.values(), key=lambda x: x["date"])[-30:]},
    })


application = app

if __name__ == "__main__":
    app.run(debug=False, port=5000)
