"""
Database configuration.

Production on Toolforge should use ToolsDB (MariaDB). SQLite remains the default for local
development and tests only: Toolforge tool homes live on NFS, where SQLite's file locking is
unreliable with several uWSGI workers (risk of "database is locked" errors and corruption),
and the database file would sit in a shared filesystem instead of an access-controlled server.

Resolution order:
  1. DATABASE_URL                       – any SQLAlchemy URL (used as-is)
  2. TOOLSDB_DATABASE (e.g. s12345__proofsource)
       credentials from TOOL_TOOLSDB_USER / TOOL_TOOLSDB_PASSWORD (Toolforge envvars)
       or ~/replica.my.cnf; host from TOOLSDB_HOST (default tools.db.svc.wikimedia.cloud)
  3. sqlite:///proofsource_v5.db        – local development
"""
import configparser
import os
from urllib.parse import quote_plus

DEFAULT_SQLITE_URL = "sqlite:///proofsource_v5.db"
TOOLSDB_HOST = "tools.db.svc.wikimedia.cloud"


def _replica_cnf_credentials():
    path = os.environ.get("TOOLSDB_CNF") or os.path.join(os.path.expanduser("~"), "replica.my.cnf")
    if not os.path.exists(path):
        return None, None
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(path)
        user = cp.get("client", "user", fallback="").strip().strip("'\"")
        password = cp.get("client", "password", fallback="").strip().strip("'\"")
    except (configparser.Error, OSError):
        return None, None
    return (user or None), (password or None)


def toolsdb_credentials():
    user = os.environ.get("TOOL_TOOLSDB_USER")
    password = os.environ.get("TOOL_TOOLSDB_PASSWORD")
    if user and password:
        return user, password
    return _replica_cnf_credentials()


def build_mysql_url(user: str, password: str, database: str, host: str = TOOLSDB_HOST) -> str:
    return (f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}@{host}/"
            f"{quote_plus(database)}?charset=utf8mb4")


def resolve_database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        return url
    database = (os.environ.get("TOOLSDB_DATABASE") or "").strip()
    if database:
        user, password = toolsdb_credentials()
        if not (user and password):
            raise RuntimeError("TOOLSDB_DATABASE is set but no ToolsDB credentials were found "
                               "(TOOL_TOOLSDB_USER/TOOL_TOOLSDB_PASSWORD or ~/replica.my.cnf).")
        if "__" not in database:
            database = f"{user}__{database}"   # ToolsDB names must start with '<user>__'
        return build_mysql_url(user, password, database, os.environ.get("TOOLSDB_HOST") or TOOLSDB_HOST)
    return DEFAULT_SQLITE_URL


def engine_options(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"timeout": 30}}
    # ToolsDB allows only a few connections per tool; every uWSGI worker has its own pool,
    # so keep pools small. Idle connections are dropped by the server, so recycle and ping.
    return {
        "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", "280")),
        "pool_pre_ping": True,
        "pool_size": int(os.environ.get("DB_POOL_SIZE", "2")),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "3")),
    }


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def redact(url: str) -> str:
    """URL safe to print (password hidden)."""
    try:
        from sqlalchemy.engine import make_url
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<database url>"
