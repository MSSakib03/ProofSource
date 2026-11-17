import logging
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, inspect, text

db = SQLAlchemy()


def utcnow() -> datetime:
    """Naive UTC 'now' (what the DB columns store); replaces the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# MariaDB/ToolsDB: always utf8mb4 (Bangla and every other script) with a binary collation, so
# text comparisons behave exactly like SQLite. The default *_ci collations are case- AND
# accent-insensitive: "José" and "Jose" would be merged into one leaderboard entry.
MYSQL_TABLE_ARGS = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_bin", "mysql_engine": "InnoDB"}


class Hub(db.Model):
    """
    Central contest = central control panel. It owns the shared rules (dates, points, policies)
    and one child Contest per Wikisource project; project admins configure books etc. there.
    (Table keeps the old name 'hubs' for backward compatibility.)
    """
    __tablename__ = 'hubs'
    __table_args__ = MYSQL_TABLE_ARGS
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), nullable=False, unique=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default="")
    created_by = db.Column(db.String(100), nullable=False, default="")
    admins = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow)
    # --- v7: central control panel ---
    link = db.Column(db.String(500), default="")
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    approval_status = db.Column(db.String(30), default='approved', index=True)
    approved_by = db.Column(db.String(100), nullable=True)
    proofread_points = db.Column(db.Integer, default=3)
    validate_points = db.Column(db.Integer, default=0)
    custom_points_policy = db.Column(db.String(10), default='project')   # yes | no | project
    bonus_policy = db.Column(db.String(10), default='project')           # yes | no | project
    review_policy = db.Column(db.String(10), default='project')          # yes (required) | no | project
    # --- v8 ---
    submission_policy = db.Column(db.String(10), default='project')      # auto | enroll | manual | project
    delete_requested_by = db.Column(db.String(100), nullable=True)
    delete_requested_at = db.Column(db.DateTime, nullable=True)
    delete_reason = db.Column(db.Text, default="")
    # --- v10: deleted contests wait in the trash for a week before they are really removed ---
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)
    deleted_by = db.Column(db.String(100), nullable=True)
    # --- v10: notice board of the central contest (the Bangla one only for central contests with a bn project) ---
    announcement_en = db.Column(db.Text, default="")
    announcement_bn = db.Column(db.Text, default="")

    contests = db.relationship('Contest', backref='hub', lazy=True)


class Contest(db.Model):
    __tablename__ = 'contests'
    __table_args__ = MYSQL_TABLE_ARGS
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), nullable=False, unique=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default="")
    wiki_domain = db.Column(db.String(100), nullable=False, default="bn.wikisource.org")
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    proofread_points = db.Column(db.Integer, default=3)
    validate_points = db.Column(db.Integer, default=1)
    max_bonus_points = db.Column(db.Integer, default=0)
    jury_mode = db.Column(db.String(30), default='accept_reject')
    jury_gets_points = db.Column(db.Boolean, default=False)
    reject_action = db.Column(db.String(30), default='hold_batch')
    created_by = db.Column(db.String(100), nullable=False, default="")
    admins = db.Column(db.Text, default="")
    juries = db.Column(db.Text, default="")
    excluded_users = db.Column(db.Text, default="")
    approval_status = db.Column(db.String(30), default='pending', index=True)
    approved_by = db.Column(db.String(100), nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    # --- v6 ---
    review_enabled = db.Column(db.Boolean, default=True)
    bulk_review_enabled = db.Column(db.Boolean, default=False)
    announcement_en = db.Column(db.Text, default="")
    announcement_bn = db.Column(db.Text, default="")
    last_backup_at = db.Column(db.DateTime, nullable=True)
    sync_started_at = db.Column(db.DateTime, nullable=True)
    hub_id = db.Column(db.Integer, db.ForeignKey('hubs.id', ondelete='SET NULL'), nullable=True, index=True)
    # --- v7 ---
    allow_custom_points = db.Column(db.Boolean, default=True)
    review_assignment = db.Column(db.String(10), default='open')   # open | equal
    delete_requested_by = db.Column(db.String(100), nullable=True)
    delete_requested_at = db.Column(db.DateTime, nullable=True)
    delete_reason = db.Column(db.Text, default="")
    # --- v8: how eligible pages enter the contest ---
    #   auto   = every eligible page is listed automatically (original behaviour)
    #   enroll = semi-automatic: only pages of participants who enrolled are listed automatically
    #   manual = participants pick and submit their eligible pages themselves
    submission_mode = db.Column(db.String(10), default='auto')
    link = db.Column(db.String(500), default="")   # optional contest page on the wiki (title links to it)
    # --- v10: trash (see Hub.deleted_at) ---
    deleted_at = db.Column(db.DateTime, nullable=True, index=True)
    deleted_by = db.Column(db.String(100), nullable=True)

    books = db.relationship('IndexBook', backref='contest', cascade="all, delete-orphan", lazy=True)
    pages = db.relationship('ContestPage', backref='contest', cascade="all, delete-orphan", lazy=True)
    enrollments = db.relationship('Enrollment', backref='contest', cascade="all, delete-orphan", lazy=True)

    @property
    def lang_code(self):
        parts = (self.wiki_domain or "bn.wikisource.org").split(".")
        if parts[0] in ("wikisource", "www"):
            return "mul"
        return parts[0]

    @property
    def is_review_enabled(self):
        return self.review_enabled is not False


class IndexBook(db.Model):
    __tablename__ = 'index_books'
    __table_args__ = MYSQL_TABLE_ARGS
    id = db.Column(db.Integer, primary_key=True)
    contest_id = db.Column(db.Integer, db.ForeignKey('contests.id', ondelete='CASCADE'), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    base_name = db.Column(db.String(255), nullable=False)
    total_pages = db.Column(db.Integer, default=0)
    custom_points = db.Column(db.Integer, nullable=True)
    excluded_pages = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    # --- v6: optional per-book window (falls back to contest window) ---
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    # --- v9: books are synced oldest first, so a sync that runs out of time continues where it stopped ---
    last_synced_at = db.Column(db.DateTime, nullable=True)

    pages = db.relationship('ContestPage', backref='book', cascade="all, delete-orphan", lazy=True)

    def window(self, contest):
        return (self.start_time or contest.start_time, self.end_time or contest.end_time)


class ContestPage(db.Model):
    __tablename__ = 'contest_pages'
    __table_args__ = (
        db.Index('ix_cp_contest_status', 'contest_id', 'review_status'),
        # one row per page of a book: two syncs running at once can never record a page twice
        db.Index('uq_cp_book_page', 'book_id', 'page_number', unique=True),
        db.Index('ix_cp_contest_proofreader', 'contest_id', 'proofreader'),
        MYSQL_TABLE_ARGS,
    )
    id = db.Column(db.Integer, primary_key=True)
    contest_id = db.Column(db.Integer, db.ForeignKey('contests.id', ondelete='CASCADE'), nullable=False, index=True)
    book_id = db.Column(db.Integer, db.ForeignKey('index_books.id', ondelete='CASCADE'), nullable=False, index=True)
    page_number = db.Column(db.Integer, nullable=False, index=True)
    page_title = db.Column(db.String(255), nullable=False)
    wiki_level = db.Column(db.Integer, default=1)
    proofreader = db.Column(db.String(100), nullable=True, index=True)
    validator = db.Column(db.String(100), nullable=True, index=True)
    proofread_rev_id = db.Column(db.BigInteger, nullable=True)
    proofread_time = db.Column(db.DateTime, nullable=True)
    review_status = db.Column(db.String(30), default='unproofread', index=True)
    reviewed_by = db.Column(db.String(100), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    base_points = db.Column(db.Integer, default=0)
    bonus_points = db.Column(db.Integer, default=0)
    jury_note = db.Column(db.String(500), nullable=True)
    locked_by_jury = db.Column(db.String(100), nullable=True)
    jury_lock_expires = db.Column(db.DateTime, nullable=True)
    reserved_by_user = db.Column(db.String(100), nullable=True)
    user_lock_expires = db.Column(db.DateTime, nullable=True)
    # --- v6: incremental sync ---
    last_rev_id = db.Column(db.BigInteger, nullable=True)
    # --- v7: equal review distribution ---
    assigned_to = db.Column(db.String(100), nullable=True, index=True)
    # --- v8: a listed page that stopped qualifying is kept as 'reverted' (never deleted) ---
    reverted_from = db.Column(db.String(30), nullable=True)     # status it had before
    revert_reason = db.Column(db.String(30), nullable=True)     # not_proofread | ineligible | excluded
    reverted_at = db.Column(db.DateTime, nullable=True)
    # --- v10: the proofreader was told on their talk page that this page needs a fix / was not accepted ---
    notified_at = db.Column(db.DateTime, nullable=True)

    def counts_as_accepted(self, contest):
        if self.review_status == 'accepted':
            return True
        return (not contest.is_review_enabled) and self.review_status == 'pending_review'

    @property
    def total_points(self):
        if self.review_status == 'accepted':
            return self.base_points + self.bonus_points
        return 0


class AuditLog(db.Model):
    """Append-only action history. Rows can never be updated or deleted through the ORM."""
    __tablename__ = 'audit_logs'
    __table_args__ = MYSQL_TABLE_ARGS
    id = db.Column(db.Integer, primary_key=True)
    contest_id = db.Column(db.Integer, nullable=True, index=True)   # no FK: log survives contest deletion
    username = db.Column(db.String(100), nullable=False, default="")
    action = db.Column(db.String(60), nullable=False, index=True)
    target = db.Column(db.String(255), default="")
    old_value = db.Column(db.Text, default="")
    new_value = db.Column(db.Text, default="")
    details = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


@event.listens_for(AuditLog, 'before_update')
def _audit_no_update(mapper, connection, target):
    raise RuntimeError("Audit log is append-only")


@event.listens_for(AuditLog, 'before_delete')
def _audit_no_delete(mapper, connection, target):
    raise RuntimeError("Audit log is append-only")


class SlugAlias(db.Model):
    """Old slugs keep working after a contest's slug is changed (they redirect to the new one)."""
    __tablename__ = 'slug_aliases'
    __table_args__ = MYSQL_TABLE_ARGS
    id = db.Column(db.Integer, primary_key=True)
    old_slug = db.Column(db.String(100), nullable=False, unique=True, index=True)
    contest_id = db.Column(db.Integer, db.ForeignKey('contests.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class Enrollment(db.Model):
    """Semi-automatic submission: participants who registered for a contest."""
    __tablename__ = 'enrollments'
    __table_args__ = (
        db.UniqueConstraint('contest_id', 'username', name='uq_enrollment_user'),
        MYSQL_TABLE_ARGS,
    )
    id = db.Column(db.Integer, primary_key=True)
    contest_id = db.Column(db.Integer, db.ForeignKey('contests.id', ondelete='CASCADE'), nullable=False, index=True)
    username = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)


class ToolAdmin(db.Model):
    """Tool administrators added from the UI (in addition to PROOFSOURCE_SUPER_ADMINS)."""
    __tablename__ = 'tool_admins'
    __table_args__ = MYSQL_TABLE_ARGS
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), nullable=False, unique=True)
    added_by = db.Column(db.String(100), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=utcnow)


class OAuthLogin(db.Model):
    """OAuth 2.0 tokens are stored server-side (they are too long for a cookie)."""
    __tablename__ = 'oauth_logins'
    __table_args__ = MYSQL_TABLE_ARGS
    id = db.Column(db.String(64), primary_key=True)
    username = db.Column(db.String(255), nullable=False, index=True)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)


# Which of two rows for the same page survives the clean-up: the one that carries a decision.
_STATUS_RANK = {"accepted": 6, "rejected": 5, "needs_fix": 5, "pending_review": 4, "reverted": 3, "eligible": 2, "unproofread": 1}


def dedupe_contest_pages(conn) -> int:
    """Removes duplicate (book, page number) rows left by older versions, keeping the row with the
    most advanced review state (then the oldest). Returns how many rows were removed."""
    removed = 0
    dups = conn.execute(text("SELECT book_id, page_number FROM contest_pages "
                             "GROUP BY book_id, page_number HAVING COUNT(*) > 1")).fetchall()
    for book_id, number in dups:
        rows = conn.execute(text("SELECT id, review_status, reviewed_at FROM contest_pages "
                                 "WHERE book_id = :b AND page_number = :n"), {"b": book_id, "n": number}).fetchall()
        keep = max(rows, key=lambda r: (_STATUS_RANK.get(r[1], 0), r[2] is not None, -r[0]))
        for r in rows:
            if r[0] != keep[0]:
                conn.execute(text("DELETE FROM contest_pages WHERE id = :i"), {"i": r[0]})
                removed += 1
    if removed:
        logging.getLogger("proofsource").warning("removed %s duplicate page rows before adding the unique index", removed)
    return removed


def migrate_schema():
    """
    Backward-compatible migration: create new tables, then ADD any missing columns /
    indexes to existing tables. Never drops or rewrites existing data.
    """
    db.create_all()
    engine = db.engine
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing_cols = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                default_sql = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, bool):
                        default_sql = f" DEFAULT {1 if val else 0}"
                    elif isinstance(val, (int, float)):
                        default_sql = f" DEFAULT {val}"
                    elif isinstance(val, str) and engine.dialect.name == "sqlite":
                        default_sql = " DEFAULT ''"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}{default_sql}'))
            existing_idx = {i["name"] for i in insp.get_indexes(table.name)}
            for idx in table.indexes:
                if idx.name and idx.name not in existing_idx:
                    try:
                        if idx.name == "uq_cp_book_page":
                            dedupe_contest_pages(conn)
                        idx.create(conn)
                    except Exception:
                        logging.getLogger("proofsource").exception("could not create index %s", idx.name)
