#!/usr/bin/env python3
"""
ProofSource maintenance commands (run from the repository root, inside the tool's venv).

  python3 manage.py check-db
      Show which database is configured and how many rows each table has.

  python3 manage.py copy-db --from SOURCE [--to URL] [--force]
      Copy every table into the configured database (or --to URL), creating the schema first.
      SOURCE may be an SQLAlchemy URL, a path to an SQLite file, or a snapshot dump
      (db-*.json.gz written by the automatic MariaDB snapshots). OAuth logins are not copied:
      users simply log in again. Refuses to write into a non-empty database unless --force.

      Typical move from SQLite to ToolsDB on Toolforge:
        toolforge envvars create TOOLSDB_DATABASE proofsource
        python3 manage.py copy-db --from ~/www/python/src/instance/proofsource_v5.db

  python3 manage.py snapshot [--reason NAME]
      Write a whole-database snapshot to the backup folder now (suitable for a scheduled job:
      toolforge jobs run --schedule "17 */12 * * *" --command "..." snapshot).
"""
import argparse
import gzip
import json
import os
import sys
from datetime import datetime

from sqlalchemy import Boolean, DateTime, create_engine, func, inspect, select

import db_config
from models import db

SKIP_TABLES = {"oauth_logins"}


def _engine(url: str):
    return create_engine(url, **db_config.engine_options(url))


def _source_url(src: str) -> str:
    if "://" in src:
        return src
    return "sqlite:///" + os.path.abspath(os.path.expanduser(src))


def _counts(engine) -> dict:
    insp = inspect(engine)
    out = {}
    with engine.connect() as conn:
        for t in db.metadata.sorted_tables:
            if insp.has_table(t.name):
                out[t.name] = conn.execute(select(func.count()).select_from(t)).scalar()
    return out


def _coerce(table, row: dict) -> dict:
    """JSON dumps carry datetimes as ISO strings; turn them back into Python values."""
    out = {}
    for k, v in row.items():
        if k not in table.c:
            continue
        col_type = table.c[k].type
        if v is not None and isinstance(col_type, DateTime) and isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", ""))
        elif v is not None and isinstance(col_type, Boolean):
            v = bool(v)
        out[k] = v
    return out


def _rows_from_engine(src_engine, table):
    insp = inspect(src_engine)
    if not insp.has_table(table.name):
        return
    have = {c["name"] for c in insp.get_columns(table.name)}
    cols = [c for c in table.columns if c.name in have]   # older databases may lack new columns
    pk = [c for c in table.primary_key.columns if c.name in have]
    q = select(*cols)
    if pk:
        q = q.order_by(*pk)
    with src_engine.connect() as conn:
        for r in conn.execute(q).mappings():
            yield dict(r)


def _rows_from_dump(dump: dict, table):
    for r in dump.get("tables", {}).get(table.name, []):
        yield _coerce(table, r)


def copy_db(source: str, target_url: str, force: bool = False) -> dict:
    target = _engine(target_url)
    db.metadata.create_all(target)
    existing = {k: v for k, v in _counts(target).items() if v and k not in SKIP_TABLES}
    if existing and not force:
        raise SystemExit(f"Target database is not empty ({existing}). Use --force to add to it anyway.")

    dump, src_engine = None, None
    if source.endswith(".json.gz") or source.endswith(".json"):
        opener = gzip.open if source.endswith(".gz") else open
        with opener(os.path.expanduser(source), "rt", encoding="utf-8") as f:
            dump = json.load(f)
        if dump.get("format") != "proofsource-db-dump":
            raise SystemExit("Not a ProofSource database dump.")
    else:
        src_engine = _engine(_source_url(source))

    copied, seen_ids, orphans = {}, {}, {}
    with target.begin() as conn:
        for table in db.metadata.sorted_tables:     # parents before children (foreign keys)
            if table.name in SKIP_TABLES:
                continue
            rows = _rows_from_dump(dump, table) if dump is not None else _rows_from_engine(src_engine, table)
            # SQLite never enforced foreign keys, so old data can hold rows whose parent is gone
            # (e.g. slug aliases of a deleted contest). MariaDB would refuse them: skip and report.
            fks = [(fk.parent.name, fk.column.table.name) for fk in table.foreign_keys]
            ids = seen_ids.setdefault(table.name, set())
            batch, n = [], 0
            for row in rows:
                if any(row.get(col) is not None and row.get(col) not in seen_ids.get(parent, ()) for col, parent in fks):
                    orphans[table.name] = orphans.get(table.name, 0) + 1
                    continue
                if "id" in row:
                    ids.add(row["id"])
                batch.append(row)
                if len(batch) >= 1000:
                    conn.execute(table.insert(), batch)
                    n += len(batch)
                    batch = []
            if batch:
                conn.execute(table.insert(), batch)
                n += len(batch)
            copied[table.name] = n
    for k, v in orphans.items():
        copied[f"{k} (orphans skipped)"] = v
    return copied


def main(argv=None):
    ap = argparse.ArgumentParser(description="ProofSource maintenance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-db")
    cp = sub.add_parser("copy-db")
    cp.add_argument("--from", dest="source", required=True)
    cp.add_argument("--to", dest="target", default=None)
    cp.add_argument("--force", action="store_true")
    sp = sub.add_parser("snapshot")
    sp.add_argument("--reason", default="manual")
    args = ap.parse_args(argv)

    url = db_config.resolve_database_url()
    if args.cmd == "check-db":
        engine = _engine(url)
        print("database:", db_config.redact(url), f"({engine.dialect.name})")
        for k, v in _counts(engine).items():
            print(f"  {k:15s} {v}")
        return 0
    if args.cmd == "copy-db":
        target_url = args.target or url
        print("copying", db_config.redact(_source_url(args.source)) if "://" in args.source or not args.source.endswith(".gz")
              else args.source, "->", db_config.redact(target_url))
        for k, v in copy_db(args.source, target_url, args.force).items():
            print(f"  {k:15s} {v} rows")
        print("done. OAuth logins were not copied: users log in again once.")
        return 0
    if args.cmd == "snapshot":
        import backup_engine
        res = backup_engine.snapshot_database(_engine(url), args.reason)
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
