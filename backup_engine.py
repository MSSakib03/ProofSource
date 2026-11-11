"""
Backup, restore and snapshot helpers for ProofSource.

- Contest-level export (JSON = full restorable backup, with SHA-256 checksum).
- Validated restore inside one transaction, after a safety snapshot of the current data.
- Whole-database snapshots (SQLite online backup API) stored OUTSIDE the app folder,
  so a broken database never takes its backups down with it.
"""
import os
import csv
import io
import json
import glob
import time
import hashlib
import sqlite3
from datetime import datetime

BACKUP_SCHEMA_VERSION = 1
SNAPSHOT_KEEP = int(os.environ.get("PROOFSOURCE_SNAPSHOT_KEEP", "21"))
SNAPSHOT_INTERVAL_HOURS = float(os.environ.get("PROOFSOURCE_SNAPSHOT_HOURS", "12"))


def backup_dir() -> str:
    d = os.environ.get("PROOFSOURCE_BACKUP_DIR") or os.path.join(os.path.expanduser("~"), "proofsource_backups")
    os.makedirs(d, exist_ok=True)
    return d


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


def export_contest(contest, ContestPage, AuditLog, page_chunk: int = 2000) -> dict:
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
    return {
        "format": "proofsource-backup",
        "schema_version": BACKUP_SCHEMA_VERSION,
        "contest_id": contest.id,
        "exported_at": _dt(datetime.utcnow()),
        "counts": {"books": len(books), "pages": len(pages), "audit": len(audit)},
        "checksum": _checksum(data),
        "data": data,
    }


def validate_backup(payload, expected_contest_id=None) -> list:
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
    if payload.get("checksum") != _checksum(data):
        errs.append("checksum_mismatch")
    counts = payload.get("counts", {})
    if counts.get("books") != len(data["books"]) or counts.get("pages") != len(data["pages"]):
        errs.append("count_mismatch")
    if expected_contest_id is not None and payload.get("contest_id") != expected_contest_id:
        errs.append("different_contest")
    book_ids = {b.get("_id") for b in data["books"]}
    for p in data["pages"]:
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


def save_json_file(payload: dict, prefix: str) -> str:
    name = f"{prefix}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    path = os.path.join(backup_dir(), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def restore_contest(db, contest, payload, models, username: str) -> dict:
    """
    Replace a contest's books/pages with the backup content in ONE transaction.
    Caller must have validated the payload. A safety JSON of the current state is written first.
    """
    Contest, IndexBook, ContestPage, AuditLog, Hub = (
        models["Contest"], models["IndexBook"], models["ContestPage"], models["AuditLog"], models["Hub"])

    safety = export_contest(contest, ContestPage, AuditLog)
    safety_path = save_json_file(safety, f"pre-restore-contest{contest.id}")

    data = payload["data"]
    c = data["contest"]
    try:
        ContestPage.query.filter_by(contest_id=contest.id).delete(synchronize_session=False)
        IndexBook.query.filter_by(contest_id=contest.id).delete(synchronize_session=False)
        db.session.flush()

        for f in CONTEST_FIELDS:
            if f == "slug":
                continue  # keep current slug (avoids collisions); old links keep working
            if f in c:
                setattr(contest, f, c[f])
        for f in ("start_time", "end_time", "last_synced_at"):
            if c.get(f):
                setattr(contest, f, _parse_dt(c[f]))
        if c.get("hub_slug"):
            hub = Hub.query.filter_by(slug=c["hub_slug"]).first()
            contest.hub_id = hub.id if hub else contest.hub_id

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


def snapshot_database(engine, reason: str = "auto") -> dict:
    """Online, consistent copy of the SQLite DB into the backup folder; verified and pruned."""
    src = sqlite_path_from_engine(engine)
    if not src or not os.path.exists(src):
        return {"ok": False, "error": "not_sqlite"}
    if not _integrity_ok(src):
        return {"ok": False, "error": "source_integrity_failed"}
    name = f"db-{reason}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.sqlite"
    dest = os.path.join(backup_dir(), name)
    tmp = dest + ".tmp"
    s = sqlite3.connect(src, timeout=30)
    d = sqlite3.connect(tmp)
    with d:
        s.backup(d)
    d.close(); s.close()
    if not _integrity_ok(tmp):
        os.remove(tmp)
        return {"ok": False, "error": "snapshot_integrity_failed"}
    os.replace(tmp, dest)
    snaps = sorted(glob.glob(os.path.join(backup_dir(), "db-*.sqlite")))
    for old in snaps[:-SNAPSHOT_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return {"ok": True, "file": name}


_last_check = [0.0]


def maybe_auto_snapshot(engine) -> None:
    """Called on requests; cheap. At most one snapshot per interval across all workers (file lock)."""
    now = time.time()
    if now - _last_check[0] < 600:
        return
    _last_check[0] = now
    try:
        d = backup_dir()
        snaps = sorted(glob.glob(os.path.join(d, "db-*.sqlite")))
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
    for p in sorted(glob.glob(os.path.join(backup_dir(), "db-*.sqlite")), reverse=True):
        out.append({"file": os.path.basename(p), "size": os.path.getsize(p),
                    "time": datetime.utcfromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out
