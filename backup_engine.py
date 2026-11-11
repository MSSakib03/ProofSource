"""
Backup, restore and snapshot helpers for ProofSource.

- Contest-level export (JSON = full restorable backup, with SHA-256 checksum and an HMAC
  signature made with the server secret, so only files this server produced can be restored).
- Validated restore inside one transaction, after a safety snapshot of the current data.
  Approval, ownership, wiki and slug are never taken from a backup file.
- Whole-database snapshots stored OUTSIDE the app folder, so a broken database never takes its
  backups down with it:
    * SQLite  -> online backup API copy (OAuth tokens stripped from the copy)
    * MariaDB -> gzip'd JSON dump of every table except OAuth logins (restorable with manage.py)
"""
import os
import csv
import gzip
import hmac
import io
import json
import glob
import time
import hashlib
import sqlite3
from datetime import datetime, date
from decimal import Decimal

BACKUP_SCHEMA_VERSION = 1
SNAPSHOT_KEEP = int(os.environ.get("PROOFSOURCE_SNAPSHOT_KEEP", "21"))
SNAPSHOT_INTERVAL_HOURS = float(os.environ.get("PROOFSOURCE_SNAPSHOT_HOURS", "12"))


JSON_BACKUPS_KEEP = int(os.environ.get("PROOFSOURCE_JSON_BACKUPS_KEEP", "10"))
# Tables never copied into snapshots: OAuth tokens are credentials, not contest data.
SNAPSHOT_SKIP_TABLES = {"oauth_logins"}


def backup_dir() -> str:
    d = os.environ.get("PROOFSOURCE_BACKUP_DIR") or os.path.join(os.path.expanduser("~"), "proofsource_backups")
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)   # Toolforge tool homes are readable by other tools by default
    except OSError:
        pass
    return d


def _private_open(path, mode="w", **kw):
    """Open a new file readable only by the tool account."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, mode, **kw)


def _dt(v):
    return v.strftime("%Y-%m-%dT%H:%M:%SZ") if v else None


def _parse_dt(v):
    if not v:
        return None
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")


CONTEST_FIELDS = [
    "slug", "title", "description", "wiki_domain", "proofread_points", "validate_points",
    "max_bonus_points", "jury_mode", "jury_gets_points", "reject_action", "created_by", "admins",
    "juries", "excluded_users", "approval_status", "approved_by", "review_enabled",
    "bulk_review_enabled", "announcement_en", "announcement_bn",
]
CONTEST_DT_FIELDS = ["start_time", "end_time", "last_synced_at", "created_at", "last_backup_at"]

BOOK_FIELDS = ["title", "base_name", "total_pages", "custom_points", "excluded_pages", "is_active"]
BOOK_DT_FIELDS = ["start_time", "end_time", "created_at"]

PAGE_FIELDS = [
    "page_number", "page_title", "wiki_level", "proofreader", "validator", "proofread_rev_id",
    "review_status", "reviewed_by", "base_points", "bonus_points", "jury_note", "last_rev_id",
]
PAGE_DT_FIELDS = ["proofread_time", "reviewed_at"]


def _checksum(data: dict) -> str:
    canon = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _signature(contest_id, checksum: str, secret: str) -> str:
    msg = f"proofsource-backup:{contest_id}:{checksum}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def export_contest(contest, ContestPage, AuditLog, page_chunk: int = 2000, secret: str = None) -> dict:
    """Full restorable export. Pages are read in chunks so 50k+ pages don't blow up memory."""
    c = {f: getattr(contest, f) for f in CONTEST_FIELDS}
    c.update({f: _dt(getattr(contest, f)) for f in CONTEST_DT_FIELDS})
    c["hub_slug"] = contest.hub.slug if contest.hub else None

    books = []
    for b in contest.books:
        bd = {"_id": b.id}
        bd.update({f: getattr(b, f) for f in BOOK_FIELDS})
        bd.update({f: _dt(getattr(b, f)) for f in BOOK_DT_FIELDS})
        books.append(bd)

    pages = []
    last_id = 0
    while True:
        chunk = (ContestPage.query.filter(ContestPage.contest_id == contest.id, ContestPage.id > last_id)
                 .order_by(ContestPage.id.asc()).limit(page_chunk).all())
        if not chunk:
            break
        for p in chunk:
            pd = {"book_id": p.book_id}
            pd.update({f: getattr(p, f) for f in PAGE_FIELDS})
            pd.update({f: _dt(getattr(p, f)) for f in PAGE_DT_FIELDS})
            pages.append(pd)
        last_id = chunk[-1].id

    audit = [
        {"username": a.username, "action": a.action, "target": a.target, "old_value": a.old_value,
         "new_value": a.new_value, "details": a.details, "created_at": _dt(a.created_at)}
        for a in AuditLog.query.filter_by(contest_id=contest.id).order_by(AuditLog.id.asc()).all()
    ]

    data = {"contest": c, "books": books, "pages": pages, "audit": audit}
    checksum = _checksum(data)
    out = {
        "format": "proofsource-backup",
        "schema_version": BACKUP_SCHEMA_VERSION,
        "contest_id": contest.id,
        "exported_at": _dt(datetime.utcnow()),
        "counts": {"books": len(books), "pages": len(pages), "audit": len(audit)},
        "checksum": checksum,
        "data": data,
    }
    if secret:
        out["signature"] = _signature(contest.id, checksum, secret)
    return out


VALID_PAGE_STATUSES = {"unproofread", "pending_review", "accepted", "needs_fix", "rejected"}


def _page_row_problem(p) -> str:
    if not isinstance(p, dict):
        return "bad_page_row"
    if not isinstance(p.get("page_number"), int) or not (0 < p["page_number"] <= 100000):
        return "bad_page_row"
    if p.get("review_status") not in VALID_PAGE_STATUSES:
        return "bad_page_row"
    for f in ("base_points", "bonus_points", "wiki_level"):
        if p.get(f) is not None and not isinstance(p.get(f), int):
            return "bad_page_row"
    if not isinstance(p.get("page_title"), str) or not (0 < len(p["page_title"]) <= 255):
        return "bad_page_row"
    return ""


def validate_backup(payload, expected_contest_id=None, secret: str = None, allow_unsigned: bool = False) -> list:
    """Return a list of human-readable problems. Empty list = safe to restore."""
    errs = []
    if not isinstance(payload, dict):
        return ["not_json_object"]
    if payload.get("format") != "proofsource-backup":
        errs.append("wrong_format")
    if payload.get("schema_version") != BACKUP_SCHEMA_VERSION:
        errs.append("unsupported_schema_version")
    data = payload.get("data")
    if not isinstance(data, dict):
        return errs + ["missing_data"]
    for key in ("contest", "books", "pages"):
        if key not in data:
            errs.append(f"missing_{key}")
    if errs:
        return errs
    if not isinstance(data.get("books"), list) or not isinstance(data.get("pages"), list) \
            or not isinstance(data.get("contest"), dict):
        return errs + ["missing_data"]
    checksum = _checksum(data)
    if payload.get("checksum") != checksum:
        errs.append("checksum_mismatch")
    elif secret and not allow_unsigned:
        sig = payload.get("signature")
        if not isinstance(sig, str) or not hmac.compare_digest(sig, _signature(payload.get("contest_id"), checksum, secret)):
            errs.append("signature_invalid")
    counts = payload.get("counts", {})
    if counts.get("books") != len(data["books"]) or counts.get("pages") != len(data["pages"]):
        errs.append("count_mismatch")
    if expected_contest_id is not None and payload.get("contest_id") != expected_contest_id:
        errs.append("different_contest")
    if any(not isinstance(b, dict) for b in data["books"]):
        return errs + ["bad_book_row"]
    book_ids = {b.get("_id") for b in data["books"]}
    for p in data["pages"]:
        prob = _page_row_problem(p)
        if prob:
            errs.append(prob)
            break
        if p.get("book_id") not in book_ids:
            errs.append("page_with_unknown_book")
            break
    c = data["contest"]
    for f in ("title", "start_time", "end_time", "wiki_domain"):
        if not c.get(f):
            errs.append(f"contest_missing_{f}")
    try:
        _parse_dt(c.get("start_time")); _parse_dt(c.get("end_time"))
    except (TypeError, ValueError):
        errs.append("bad_dates")
    return errs


def save_json_file(payload: dict, prefix: str, keep: int = JSON_BACKUPS_KEEP) -> str:
    """Writes a private JSON file; keeps only the newest `keep` files with this prefix
    (keep=None keeps all, e.g. for deleted contests)."""
    name = f"{prefix}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    path = os.path.join(backup_dir(), name)
    tmp = path + ".tmp"
    with _private_open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)
    if keep:
        olds = sorted(glob.glob(os.path.join(backup_dir(), glob.escape(prefix) + "-*.json")))
        for old in olds[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass
    return path


# Never taken from a backup: who approved/owns the contest, which wiki it runs on and its URL.
RESTORE_PROTECTED_FIELDS = {"slug", "approval_status", "approved_by", "created_by", "wiki_domain"}


def restore_contest(db, contest, payload, models, username: str) -> dict:
    """
    Replace a contest's books/pages with the backup content in ONE transaction.
    Caller must have validated the payload. A safety JSON of the current state is written first.
    """
    IndexBook, ContestPage, AuditLog = models["IndexBook"], models["ContestPage"], models["AuditLog"]

    safety = export_contest(contest, ContestPage, AuditLog)
    safety_path = save_json_file(safety, f"pre-restore-contest{contest.id}")

    data = payload["data"]
    c = data["contest"]
    try:
        ContestPage.query.filter_by(contest_id=contest.id).delete(synchronize_session=False)
        IndexBook.query.filter_by(contest_id=contest.id).delete(synchronize_session=False)
        db.session.flush()

        for f in CONTEST_FIELDS:
            if f in RESTORE_PROTECTED_FIELDS:
                continue
            if f in c:
                setattr(contest, f, c[f])
        for f in ("start_time", "end_time", "last_synced_at"):
            if c.get(f):
                setattr(contest, f, _parse_dt(c[f]))
        # hub membership is never changed by a restore (a file could otherwise attach the
        # contest to someone else's central contest)

        id_map = {}
        for bd in data["books"]:
            b = IndexBook(contest_id=contest.id, **{f: bd.get(f) for f in BOOK_FIELDS})
            for f in BOOK_DT_FIELDS:
                setattr(b, f, _parse_dt(bd.get(f)))
            db.session.add(b)
            db.session.flush()
            id_map[bd["_id"]] = b.id

        batch = []
        for pd in data["pages"]:
            row = {f: pd.get(f) for f in PAGE_FIELDS}
            row.update({f: _parse_dt(pd.get(f)) for f in PAGE_DT_FIELDS})
            row["contest_id"] = contest.id
            row["book_id"] = id_map[pd["book_id"]]
            batch.append(row)
            if len(batch) >= 2000:
                db.session.bulk_insert_mappings(ContestPage, batch)
                batch = []
        if batch:
            db.session.bulk_insert_mappings(ContestPage, batch)

        db.session.add(AuditLog(
            contest_id=contest.id, username=username or "", action="backup_restore",
            target=f"contest:{contest.id}",
            details=json.dumps({"from_exported_at": payload.get("exported_at"),
                                "pages": len(data["pages"]), "books": len(data["books"]),
                                "safety_copy": os.path.basename(safety_path)})))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {"pages": len(data["pages"]), "books": len(data["books"]), "safety_copy": os.path.basename(safety_path)}


def pages_csv_iter(contest, ContestPage, chunk: int = 2000):
    """Streams a CSV of every page record (for spreadsheets; JSON is the restorable format)."""
    titles = {b.id: b.title for b in contest.books}
    return _pages_csv_gen(contest.id, titles, ContestPage, chunk)


def _pages_csv_gen(contest_id, titles, ContestPage, chunk):
    header = ["book_title", "page_number", "page_title", "wiki_level", "proofreader", "validator",
              "review_status", "reviewed_by", "reviewed_at", "base_points", "bonus_points",
              "proofread_time", "proofread_rev_id", "jury_note"]
    buf = io.StringIO()
    w = csv.writer(buf)
    yield "﻿"
    w.writerow(header)
    yield buf.getvalue(); buf.seek(0); buf.truncate()
    last_id = 0
    while True:
        rows = (ContestPage.query.filter(ContestPage.contest_id == contest_id, ContestPage.id > last_id)
                .order_by(ContestPage.id.asc()).limit(chunk).all())
        if not rows:
            break
        for p in rows:
            w.writerow([titles.get(p.book_id, ""), p.page_number, p.page_title, p.wiki_level,
                        p.proofreader or "", p.validator or "", p.review_status, p.reviewed_by or "",
                        _dt(p.reviewed_at) or "", p.base_points, p.bonus_points,
                        _dt(p.proofread_time) or "", p.proofread_rev_id or "", p.jury_note or ""])
        last_id = rows[-1].id
        yield buf.getvalue(); buf.seek(0); buf.truncate()


# ---------------- whole-database snapshots ----------------

def sqlite_path_from_engine(engine):
    if engine.dialect.name != "sqlite":
        return None
    return engine.url.database


def _integrity_ok(path: str) -> bool:
    try:
        con = sqlite3.connect(path)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.close()
        return ok
    except Exception:
        return False


def _prune_snapshots():
    snaps = sorted(glob.glob(os.path.join(backup_dir(), "db-*.sqlite")) +
                   glob.glob(os.path.join(backup_dir(), "db-*.json.gz")), key=os.path.getmtime)
    for old in snaps[:-SNAPSHOT_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass


def _sqlite_snapshot(src: str, reason: str) -> dict:
    if not os.path.exists(src):
        return {"ok": False, "error": "not_found"}
    if not _integrity_ok(src):
        return {"ok": False, "error": "source_integrity_failed"}
    name = f"db-{reason}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.sqlite"
    dest = os.path.join(backup_dir(), name)
    tmp = dest + ".tmp"
    _private_open(tmp, "w").close()
    s = sqlite3.connect(src, timeout=30)
    d = sqlite3.connect(tmp)
    with d:
        s.backup(d)
    for t in SNAPSHOT_SKIP_TABLES:   # no OAuth credentials in backup copies
        try:
            d.execute(f"DELETE FROM {t}")
        except sqlite3.Error:
            pass
    d.commit()
    d.execute("VACUUM")
    d.close(); s.close()
    if not _integrity_ok(tmp):
        os.remove(tmp)
        return {"ok": False, "error": "snapshot_integrity_failed"}
    os.replace(tmp, dest)
    _prune_snapshots()
    return {"ok": True, "file": name}


def _json_default(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == int(v) else float(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    raise TypeError(f"cannot serialise {type(v)}")


def iter_table_rows(conn, table, chunk: int = 5000):
    pk = list(table.primary_key.columns)
    q = table.select()
    if pk:
        q = q.order_by(*pk)
    offset = 0
    while True:
        rows = conn.execute(q.limit(chunk).offset(offset)).mappings().all()
        if not rows:
            break
        for r in rows:
            yield dict(r)
        offset += len(rows)


def dump_database_json(engine, metadata, path: str) -> dict:
    """Portable logical dump (works for MariaDB/ToolsDB and SQLite): one gzip'd JSON document
    {"format": "proofsource-db-dump", "tables": {name: [rows...]}}, written atomically."""
    counts = {}
    tmp = path + ".tmp"
    with _private_open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
        w = io.TextIOWrapper(gz, encoding="utf-8")
        w.write('{"format":"proofsource-db-dump","version":1,"dialect":%s,"created_at":%s,"tables":{' % (
            json.dumps(engine.dialect.name), json.dumps(_dt(datetime.utcnow()))))
        first_table = True
        with engine.connect() as conn:
            for table in metadata.sorted_tables:
                if table.name in SNAPSHOT_SKIP_TABLES:
                    continue
                w.write(("" if first_table else ",") + json.dumps(table.name) + ":[")
                first_table = False
                n = 0
                for row in iter_table_rows(conn, table):
                    w.write(("," if n else "") + json.dumps(row, ensure_ascii=False, default=_json_default))
                    n += 1
                w.write("]")
                counts[table.name] = n
        w.write("}}")
        w.flush()
        w.detach()
    os.replace(tmp, path)
    return counts


def snapshot_database(engine, reason: str = "auto") -> dict:
    """Consistent copy of the whole database into the backup folder; verified and pruned."""
    reason = "".join(ch for ch in reason if ch.isalnum() or ch in "-_")[:40] or "snap"
    src = sqlite_path_from_engine(engine)
    if src:
        return _sqlite_snapshot(src, reason)
    from models import db as _db   # late import: backup_engine stays usable without the app
    name = f"db-{reason}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json.gz"
    dest = os.path.join(backup_dir(), name)
    try:
        counts = dump_database_json(engine, _db.metadata, dest)
    except Exception as e:
        try:
            os.remove(dest + ".tmp")
        except OSError:
            pass
        return {"ok": False, "error": f"dump_failed: {type(e).__name__}"}
    _prune_snapshots()
    return {"ok": True, "file": name, "counts": counts}


_last_check = [0.0]


def maybe_auto_snapshot(engine) -> None:
    """Called on requests; cheap. At most one snapshot per interval across all workers (file lock)."""
    now = time.time()
    if now - _last_check[0] < 600:
        return
    _last_check[0] = now
    try:
        d = backup_dir()
        snaps = sorted(glob.glob(os.path.join(d, "db-*.sqlite")) + glob.glob(os.path.join(d, "db-*.json.gz")),
                       key=os.path.getmtime)
        if snaps and now - os.path.getmtime(snaps[-1]) < SNAPSHOT_INTERVAL_HOURS * 3600:
            return
        lock = os.path.join(d, ".snapshot.lock")
        if os.path.exists(lock) and now - os.path.getmtime(lock) < 900:
            return
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            snapshot_database(engine, "auto")
        finally:
            os.close(fd)
            os.remove(lock)
    except FileExistsError:
        pass
    except Exception:
        pass


def list_snapshots() -> list:
    out = []
    paths = glob.glob(os.path.join(backup_dir(), "db-*.sqlite")) + glob.glob(os.path.join(backup_dir(), "db-*.json.gz"))
    for p in sorted(paths, key=os.path.getmtime, reverse=True):
        out.append({"file": os.path.basename(p), "size": os.path.getsize(p),
                    "time": datetime.utcfromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out
