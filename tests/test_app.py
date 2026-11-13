"""
ProofSource automated tests (no network: every wiki call is mocked).
Run from the repo root:   python3 -m unittest discover -s tests -v

To run the same suite against MariaDB (as on ToolsDB), point it at an empty database:
    PROOFSOURCE_TEST_DATABASE_URL='mysql+pymysql://user:pw@localhost/s1__proofsource_test?charset=utf8mb4' \
        python3 -m unittest discover -s tests
"""
import atexit
import io
import os
import sys
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

TMP = tempfile.mkdtemp(prefix="ps_test_")
os.environ["DATABASE_URL"] = os.environ.get("PROOFSOURCE_TEST_DATABASE_URL") or ("sqlite:///" + os.path.join(TMP, "test.db"))
USING_SQLITE = os.environ["DATABASE_URL"].startswith("sqlite")
os.environ["PROOFSOURCE_BACKUP_DIR"] = os.path.join(TMP, "backups")
os.environ["SECRET_KEY"] = "test"
os.environ["OAUTH2_CLIENT_ID"] = "cid"
os.environ["OAUTH2_CLIENT_SECRET"] = "csec"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wiki_engine  # noqa: E402


def _no_network(*a, **k):
    raise RuntimeError("network disabled in tests")


wiki_engine.requests.get = _no_network
wiki_engine.requests.post = _no_network

import app as A  # noqa: E402
import backup_engine  # noqa: E402
from models import db, Contest, IndexBook, ContestPage, AuditLog, OAuthLogin, SlugAlias, Hub  # noqa: E402

A.app.config["SESSION_COOKIE_SECURE"] = False
A.app.config["FORCED_SYNC_MIN_SECONDS"] = 0   # tests sync back to back

from flask.testing import FlaskClient  # noqa: E402


class CSRFClient(FlaskClient):
    """Behaves like the real frontend: sends the session's CSRF token on unsafe requests."""
    send_csrf = True

    def open(self, *args, **kw):
        method = (kw.get("method") or "GET").upper()
        if self.send_csrf and method not in ("GET", "HEAD", "OPTIONS"):
            with self.session_transaction() as s:
                tok = s.setdefault("csrf_token", "test-csrf-token")
            headers = dict(kw.pop("headers", None) or {})
            headers.setdefault("X-CSRF-Token", tok)
            kw["headers"] = headers
        return super().open(*args, **kw)


A.app.test_client_class = CSRFClient
A.is_user_wiki_admin = lambda domain, user: (user or "").lower() == "sysop"
A.identify_bot_users = lambda domain, users: {u.lower() for u in users if u and u.lower().endswith("bot")}
A.fetch_index_page_count = lambda domain, base: 100


def live(entries, complete=True):
    """Build a fake fetch_live_book_pages_from_wiki returning the given pages."""
    def _f(domain, base, start, end, known_revs=None):
        _f.calls.append({"start": start, "end": end, "known": dict(known_revs or {})})
        out = {}
        for n, (user, lvl, rev) in entries.items():
            if known_revs and known_revs.get(n) == rev:
                out[n] = {"page_number": n, "page_title": f"Page:{base}/{n}", "unchanged": True, "rev_latest": rev}
                continue
            out[n] = {"page_number": n, "page_title": f"Page:{base}/{n}", "level": lvl, "eligible": lvl >= 3,
                      "proofreader": user, "validator": user if lvl == 4 else None, "rev_id": rev,
                      "proofread_time": start + timedelta(hours=1), "rev_latest": rev}
        return out, complete
    _f.calls = []
    return _f


def fake_page_content(*a, **k):
    return {"header": "", "body": "text", "footer": "", "first_body": "", "first_rev_id": 1,
            "diff_tokens": [], "rendered_html": "<p>x</p>", "image_url": "", "rev_id": 5}


A.fetch_page_content_and_image = fake_page_content
A.edit_wikisource_live = lambda **k: {"saved_on_wiki": True, "revid": 999, "user": k.get("username"), "level": k.get("level")}


class Base(unittest.TestCase):
    def setUp(self):
        with A.app.app_context():
            db.drop_all()
            from models import migrate_schema
            migrate_schema()
        self.c = A.app.test_client()
        A.fetch_live_book_pages_from_wiki = live({})
        A.limiter.reset()

    def login(self, name, client=None):
        client = client or self.c
        with A.app.app_context():
            rec = OAuthLogin(id=os.urandom(8).hex(), username=name, access_token="tok",
                             expires_at=datetime.utcnow() + timedelta(hours=1))
            db.session.add(rec)
            db.session.commit()
            lid = rec.id
        with client.session_transaction() as s:
            s["login_id"] = lid
        return client

    def make_contest(self, review=True, bulk=False, books="Index:Book.pdf", entries=None, **extra):
        A.fetch_live_book_pages_from_wiki = live(entries or {})
        self.login("Admin")
        now = datetime.utcnow()
        body = {"title": "Test", "slug": extra.pop("slug", "test"), "wiki_domain": "bn.wikisource.org",
                "start_time": (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M"),
                "end_time": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M"),
                "proofread_points": 3, "juries": "Admin, Rev", "books": books,
                "review_enabled": review, "bulk_review_enabled": bulk}
        body.update(extra)
        r = self.c.post("/api/contests", json=body)
        self.assertEqual(r.status_code, 200, r.json)
        cid = r.json["contest_id"]
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            c.approval_status = "approved"
            db.session.commit()
        return cid


class OAuthTests(Base):
    def test_login_redirect_and_callback(self):
        r = self.c.get("/login")
        self.assertEqual(r.status_code, 302)
        self.assertIn("redirect_uri=https%3A%2F%2Fproofsource.toolforge.org%2Fauth%2Fcallback", r.headers["Location"])
        from urllib.parse import urlparse, parse_qs
        state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]

        class R:
            def __init__(s, js): s.status_code = 200; s._js = js; s.text = ""
            def json(s): return s._js
        with mock.patch.object(A.requests, "post", lambda *a, **k: R({"access_token": "AT", "refresh_token": "RT", "expires_in": 14400})), \
             mock.patch.object(A.requests, "get", lambda *a, **k: R({"username": "Some User"})):
            r = self.c.get(f"/auth/callback?code=x&state={state}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/api/session").json["current_user"], "Some User")

    def test_state_mismatch(self):
        self.c.get("/login")
        self.assertEqual(self.c.get("/auth/callback?code=x&state=bad").status_code, 400)


class ContestLinkTests(Base):
    def test_contest_page_link_is_validated_and_returned(self):
        cid = self.make_contest(link="https://bn.wikisource.org/wiki/WS:Contest")
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["contest"]["link"], "https://bn.wikisource.org/wiki/WS:Contest")
        r = self.c.post(f"/api/contests/{cid}/config", json={"link": "javascript:alert(1)"})
        self.assertEqual(r.json.get("error_code"), "invalid_link")
        self.assertTrue(self.c.post(f"/api/contests/{cid}/config", json={"link": ""}).json["success"])
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["contest"]["link"], "")

    def test_new_contest_defaults_to_accept_in_tool_only(self):
        cid = self.make_contest()
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["contest"]["jury_mode"], "accept_reject")


class SyncTests(Base):
    def test_sync_scores_and_incremental(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 11), 2: ("Bob", 3, 12), 3: ("Alice", 1, 13)})
        d = self.c.get(f"/api/contests/{cid}").json
        self.assertEqual(d["stats"]["listed_pages"], 2)
        # second sync: unchanged revisions are passed as known_revs
        A.fetch_live_book_pages_from_wiki = f = live({1: ("Alice", 3, 11), 2: ("Bob", 3, 12), 3: ("Alice", 1, 13)})
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        self.assertEqual(f.calls[0]["known"], {1: 11, 2: 12})   # level<3 pages are never stored
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["stats"]["listed_pages"], 2)

    def test_incomplete_sync_never_wipes_pages(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 11), 2: ("Bob", 3, 12)})
        A.fetch_live_book_pages_from_wiki = live({1: ("Alice", 3, 11)}, complete=False)   # page 2 missing: network glitch
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["stats"]["listed_pages"], 2)
        A.fetch_live_book_pages_from_wiki = live({1: ("Alice", 3, 11)}, complete=True)    # truly gone
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["stats"]["listed_pages"], 1)

    def test_bots_and_excluded_users_get_no_points(self):
        cid = self.make_contest(entries={1: ("HelperBot", 3, 1), 2: ("Spammer", 3, 2), 3: ("Alice", 3, 3)},
                                excluded_users="Spammer")
        names = [r["username"] for r in self.c.get(f"/api/contests/{cid}").json["leaderboard"]]
        self.assertEqual(names, ["Alice"])

    def test_per_book_window_used(self):
        cid = self.make_contest(books="")
        A.fetch_live_book_pages_from_wiki = f = live({})
        r = self.c.post(f"/api/contests/{cid}/books", json={"title": "Index:B.pdf", "start_time": "2020-01-01T00:00",
                                                           "end_time": "2020-02-01T00:00"})
        self.assertTrue(r.json["success"])
        self.assertEqual(f.calls[-1]["start"], datetime(2020, 1, 1))


class ReviewTests(Base):
    def setUp(self):
        super().setUp()
        self.cid = self.make_contest(bulk=True, entries={1: ("Alice", 3, 1), 2: ("Admin", 3, 2), 3: ("Bob", 3, 3)})

    def page_id(self, n):
        with A.app.app_context():
            return ContestPage.query.filter_by(contest_id=self.cid, page_number=n).first().id

    def test_cannot_review_own_page_backend(self):
        r = self.c.post(f"/api/contests/{self.cid}/jury/submit", json={"page_id": self.page_id(2), "decision": "accepted"})
        self.assertEqual(r.json["error_code"], "own_page_review")

    def test_review_queue_excludes_own_pages(self):
        r = self.c.get(f"/api/contests/{self.cid}/jury/next-page?status=pending_review").json
        self.assertNotIn(self.page_id(2), [q["id"] for q in r["queue"]])

    def test_accept_scores_and_duplicate(self):
        pid = self.page_id(1)
        r = self.c.post(f"/api/contests/{self.cid}/jury/submit", json={"page_id": pid, "decision": "accepted"})
        self.assertTrue(r.json["success"])
        r2 = self.c.post(f"/api/contests/{self.cid}/jury/submit", json={"page_id": pid, "decision": "accepted"})
        self.assertTrue(r2.json.get("duplicate"))
        board = {x["username"]: x for x in self.c.get(f"/api/contests/{self.cid}").json["leaderboard"]}
        self.assertEqual(board["Alice"]["points"], 3)
        with A.app.app_context():
            self.assertEqual(AuditLog.query.filter_by(contest_id=self.cid, action="review_decision").count(), 1)

    def test_invalid_decision(self):
        r = self.c.post(f"/api/contests/{self.cid}/jury/submit", json={"page_id": self.page_id(1), "decision": "hack"})
        self.assertEqual(r.status_code, 400)

    def test_non_reviewer_forbidden(self):
        other = A.app.test_client()
        self.login("Mallory", other)
        r = other.post(f"/api/contests/{self.cid}/jury/submit", json={"page_id": self.page_id(1), "decision": "accepted"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(other.get(f"/api/contests/{self.cid}/audit").status_code, 403)

    def test_bulk_review_skips_own_and_keeps_partial(self):
        cand = self.c.get(f"/api/contests/{self.cid}/bulk/candidates").json
        ids = [p["id"] for p in cand["pages"]]
        self.assertNotIn(self.page_id(2), ids)
        r = self.c.post(f"/api/contests/{self.cid}/bulk/review",
                        json={"decision": "accepted", "page_ids": ids + [self.page_id(2), 999999]}).json
        self.assertEqual(sorted(r["done"]), sorted(ids))
        self.assertEqual(r["skipped_own"], [self.page_id(2)])
        self.assertEqual(len(r["failed"]), 1)

    def test_bulk_disabled_by_default(self):
        cid2 = self.make_contest(slug="t2", entries={1: ("Alice", 3, 1)})
        self.assertEqual(self.c.get(f"/api/contests/{cid2}/bulk/candidates").status_code, 403)


class ModeTests(Base):
    def test_non_review_mode_counts_pages(self):
        cid = self.make_contest(review=False, entries={1: ("Alice", 3, 1), 2: ("Alice", 3, 2)})
        d = self.c.get(f"/api/contests/{cid}").json
        self.assertEqual(d["leaderboard"][0]["points"], 6)
        self.assertEqual(d["stats"]["pending_pages"], 0)
        self.assertEqual(d["jury_stats"], [])
        r = self.c.get(f"/api/contests/{cid}/jury/next-page").json
        self.assertEqual(r["error_code"], "review_disabled")


class ConfigTests(Base):
    def test_invalid_excluded_pages(self):
        cid = self.make_contest()
        r = self.c.post(f"/api/contests/{cid}/books", json={"title": "Index:X.pdf", "excluded_pages": "1-5, abc, 9-2"})
        self.assertEqual(r.json["error_code"], "invalid_excluded_pages")
        self.assertEqual(sorted(r.json["invalid"]), ["9-2", "abc"])

    def test_excluded_parser(self):
        pages, bad = wiki_engine.parse_excluded_pages_strict("1-5, 12; 20–22")
        self.assertEqual(pages, {1, 2, 3, 4, 5, 12, 20, 21, 22})
        self.assertEqual(bad, [])

    def test_slug_change_keeps_old_link(self):
        cid = self.make_contest(slug="oldname")
        self.c.post(f"/api/contests/{cid}/config", json={"slug": "newname"})
        r = self.c.get("/contest/bn/oldname")
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r.headers["Location"].endswith("/contest/bn/newname"))
        self.assertEqual(self.c.get("/contest/bn/newname").status_code, 200)

    def test_config_changes_are_audited(self):
        cid = self.make_contest()
        self.c.post(f"/api/contests/{cid}/config", json={"juries": "Admin, Rev, Third", "announcement_en": "Hello"})
        acts = {e["action"] for e in self.c.get(f"/api/contests/{cid}/audit").json["entries"]}
        self.assertTrue({"reviewers_change", "announcement_change"} <= acts)

    def test_end_before_start_rejected(self):
        cid = self.make_contest()
        r = self.c.post(f"/api/contests/{cid}/config", json={"start_time": "2030-01-02T00:00", "end_time": "2030-01-01T00:00"})
        self.assertEqual(r.json["error_code"], "end_before_start")

    def test_direct_link_renders_contest_id(self):
        cid = self.make_contest(slug="direct")
        html = self.c.get("/contest/bn/direct").data.decode()
        self.assertIn(f'"contest_id": {cid}', html)


class BackupTests(Base):
    def test_export_validate_restore_roundtrip(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Bob", 3, 2)})
        payload = json.loads(self.c.get(f"/api/contests/{cid}/backup.json").data)
        self.assertEqual(payload["counts"]["pages"], 2)
        self.assertIsNotNone(self.c.get(f"/api/contests/{cid}").json["contest"]["last_backup_iso"])
        # damage the live data
        with A.app.app_context():
            ContestPage.query.filter_by(contest_id=cid).delete()
            db.session.commit()
        # tampered backup is refused and changes nothing
        bad = json.loads(json.dumps(payload))
        bad["data"]["pages"][0]["base_points"] = 999
        r = self.c.post(f"/api/contests/{cid}/restore", data=json.dumps(bad), content_type="application/json")
        self.assertIn("checksum_mismatch", r.json["problems"])
        # valid backup restores
        r = self.c.post(f"/api/contests/{cid}/restore",
                        data={"file": (io.BytesIO(json.dumps(payload).encode()), "b.json")},
                        content_type="multipart/form-data")
        self.assertTrue(r.json["success"], r.json)
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["stats"]["listed_pages"], 2)
        files = os.listdir(os.environ["PROOFSOURCE_BACKUP_DIR"])
        self.assertTrue(any(f.startswith("pre-restore-contest") for f in files))

    def test_backup_of_other_contest_refused(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        cid2 = self.make_contest(slug="other")
        payload = self.c.get(f"/api/contests/{cid}/backup.json").data
        r = self.c.post(f"/api/contests/{cid2}/restore", data=payload, content_type="application/json")
        self.assertIn("different_contest", r.json["problems"])

    def test_pages_csv(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        r = self.c.get(f"/api/contests/{cid}/backup-pages.csv")
        self.assertIn("Alice", r.data.decode("utf-8-sig"))

    def test_audit_is_append_only(self):
        cid = self.make_contest()
        with A.app.app_context():
            a = AuditLog.query.filter_by(contest_id=cid).first()
            a.details = "tamper"
            with self.assertRaises(RuntimeError):
                db.session.commit()
            db.session.rollback()
            a = AuditLog.query.filter_by(contest_id=cid).first()
            db.session.delete(a)
            with self.assertRaises(RuntimeError):
                db.session.commit()
            db.session.rollback()

    def test_snapshot_and_reminder(self):
        cid = self.make_contest()
        with A.app.app_context():
            res = backup_engine.snapshot_database(db.engine, "test")
            self.assertTrue(res["ok"], res)
            c = db.session.get(Contest, cid)
            c.created_at = datetime.utcnow() - timedelta(days=40)
            db.session.commit()
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["contest"]["backup_reminder"]["level"], "month")


def rev(user, level, text, day, revid):
    return {"user": user, "revid": revid, "timestamp": f"2026-01-{day:02d}T10:00:00Z",
            "slots": {"main": {"*": f'<noinclude><pagequality level="{level}" user="{user}" /></noinclude>{text}<noinclude></noinclude>'}}}


class CountingRuleTests(unittest.TestCase):
    S, E = datetime(2026, 1, 10), datetime(2026, 1, 20)

    def a(self, revs):
        return wiki_engine.analyze_page_history(revs, self.S, self.E)

    def test_first_proofread_in_window_with_edit(self):
        r = self.a([rev("OCR", 1, "txt", 1, 1), rev("X", 3, "txt fixed", 12, 2)])
        self.assertTrue(r["eligible"]); self.assertEqual(r["proofreader"], "X")

    def test_status_only_on_others_text_does_not_count(self):
        r = self.a([rev("OCR", 1, "txt", 1, 1), rev("Y", 1, "txt fixed by Y", 11, 2), rev("X", 3, "txt fixed by Y", 12, 3)])
        self.assertFalse(r["eligible"]); self.assertEqual(r["reason"], "status_only_edit")

    def test_own_earlier_edits_then_status_counts(self):
        r = self.a([rev("OCR", 1, "txt", 1, 1), rev("X", 1, "txt half", 11, 2), rev("X", 1, "txt done", 12, 3),
                    rev("X", 3, "txt done", 13, 4)])
        self.assertTrue(r["eligible"]); self.assertEqual(r["proofreader"], "X")

    def test_backward_change_keeps_original_proofreader(self):
        r = self.a([rev("OCR", 1, "t", 1, 1), rev("X", 3, "t2", 11, 2), rev("Y", 4, "t2", 12, 3),
                    rev("Z", 3, "t3", 13, 4)])
        self.assertTrue(r["eligible"]); self.assertEqual(r["proofreader"], "X"); self.assertEqual(r["validator"], "Y")

    def test_already_proofread_before_contest_never_counts(self):
        r = self.a([rev("OCR", 1, "t", 1, 1), rev("W", 4, "t2", 5, 2), rev("Z", 1, "t2", 11, 3), rev("Z", 3, "t3", 12, 4)])
        self.assertFalse(r["eligible"]); self.assertEqual(r["reason"], "first_proofread_outside_window")

    def test_reverted_page_does_not_count(self):
        r = self.a([rev("OCR", 1, "t", 1, 1), rev("X", 3, "t2", 12, 2), rev("Y", 1, "t2", 13, 3)])
        self.assertFalse(r["eligible"])

    def test_created_directly_as_proofread(self):
        r = self.a([rev("X", 3, "new text", 12, 1)])
        self.assertTrue(r["eligible"])


class CentralTests(Base):
    def central(self, client=None, **extra):
        now = datetime.utcnow()
        body = {"title": "Indic Week", "slug": "indic",
                "start_time": (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
                "end_time": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M"),
                "proofread_points": 4, "validate_points": 0, "custom_points_policy": "no",
                "bonus_policy": "no", "review_policy": "project",
                "projects": [{"wiki_domain": "bn.wikisource.org", "admins": "BnAdmin"},
                             {"wiki_domain": "hi.wikisource.org", "admins": "HiAdmin"}]}
        body.update(extra)
        return (client or self.c).post("/api/central", json=body)

    def test_non_sysop_creates_pending_and_sysop_approves(self):
        self.login("Someone")
        r = self.central()
        self.assertEqual(r.json["approval_status"], "pending")
        with A.app.app_context():
            self.assertEqual(Contest.query.count(), 2)
            self.assertTrue(all(c.approval_status == "pending" for c in Contest.query.all()))
        other = A.app.test_client(); self.login("Sysop", other)
        self.assertTrue(other.post("/api/central/indic/approve", json={"decision": "approved"}).json["success"])
        with A.app.app_context():
            self.assertTrue(all(c.approval_status == "approved" for c in Contest.query.all()))

    def test_tool_admin_auto_approved_and_children_follow_rules(self):
        self.login("MS Sakib")
        self.assertEqual(self.central().json["approval_status"], "approved")
        with A.app.app_context():
            bn = Contest.query.filter_by(wiki_domain="bn.wikisource.org").first()
            self.assertEqual(bn.proofread_points, 4)
            self.assertEqual(bn.admins, "BnAdmin")
            cid = bn.id
        child = A.app.test_client(); self.login("BnAdmin", child)
        child.post(f"/api/contests/{cid}/config", json={"proofread_points": 99, "end_time": "2099-01-01T00:00",
                                                        "max_bonus_points": 5, "title": "Bangla part"})
        d = child.get(f"/api/contests/{cid}").json["contest"]
        self.assertEqual(d["proofread_points"], 4)
        self.assertEqual(d["max_bonus_points"], 0)
        self.assertEqual(d["title"], "Bangla part")
        self.assertIn("proofread_points", d["locked_fields"])
        # custom book points are not allowed by the central policy
        child.post(f"/api/contests/{cid}/books", json={"title": "Index:B.pdf", "points": 10})
        self.assertEqual(child.get(f"/api/contests/{cid}").json["books"][0]["points"], 4)

    def test_central_dashboard_and_list(self):
        self.login("MS Sakib")
        self.central()
        with A.app.app_context():
            ids = [c.id for c in Contest.query.all()]
            for cid in ids:
                b = IndexBook(contest_id=cid, title="Index:X", base_name="X"); db.session.add(b); db.session.flush()
                db.session.add(ContestPage(contest_id=cid, book_id=b.id, page_number=1, page_title="Page:X/1",
                                           proofreader="Alice", review_status="accepted", base_points=4))
            db.session.commit()
        d = self.c.get("/api/central/indic").json
        self.assertEqual(len(d["projects"]), 2)
        self.assertEqual(d["leaderboard"][0]["points"], 8)
        lst = self.c.get("/api/contests").json
        self.assertEqual(lst["centrals"][0]["project_count"], 2)

    def test_project_with_data_cannot_be_removed(self):
        self.login("MS Sakib")
        self.central()
        with A.app.app_context():
            c = Contest.query.filter_by(wiki_domain="hi.wikisource.org").first()
            b = IndexBook(contest_id=c.id, title="Index:X", base_name="X"); db.session.add(b); db.session.flush()
            db.session.add(ContestPage(contest_id=c.id, book_id=b.id, page_number=1, page_title="P", proofreader="A",
                                       review_status="pending_review"))
            db.session.commit()
        r = self.c.post("/api/central/indic/config", json={"projects": [{"wiki_domain": "bn.wikisource.org"}]})
        self.assertEqual(r.json["error_code"], "project_has_data")


class AssignmentTests(Base):
    def test_equal_distribution_in_book_order(self):
        entries = {n: (f"P{n % 4}", 3, n) for n in range(1, 31)}
        cid = self.make_contest(entries=entries, juries="J1, J2, J3")
        self.c.post(f"/api/contests/{cid}/config", json={"review_assignment": "equal"})
        with A.app.app_context():
            rows = ContestPage.query.filter_by(contest_id=cid).order_by(ContestPage.page_number).all()
            counts = {}
            for p in rows:
                counts[p.assigned_to] = counts.get(p.assigned_to, 0) + 1
            self.assertEqual(sorted(counts.values()), [10, 10, 10])
            switches = sum(1 for a, b in zip(rows, rows[1:]) if a.assigned_to != b.assigned_to)
            self.assertLessEqual(switches, 4)       # long consecutive runs
            pid = next(p.id for p in rows if p.assigned_to != "J1")
        j1 = A.app.test_client(); self.login("J1", j1)
        q = j1.get(f"/api/contests/{cid}/jury/next-page").json
        self.assertEqual(q["scope"], "mine")
        self.assertEqual(q["queue_total"], 10)
        r = j1.post(f"/api/contests/{cid}/jury/submit", json={"page_id": pid, "decision": "accepted"})
        self.assertEqual(r.json["error_code"], "not_assigned_to_you")

    def test_midway_switch_counts_already_reviewed(self):
        entries = {n: ("Writer", 3, n) for n in range(1, 21)}
        cid = self.make_contest(entries=entries, juries="J1, J2")
        j1 = A.app.test_client(); self.login("J1", j1)
        with A.app.app_context():
            ids = [p.id for p in ContestPage.query.filter_by(contest_id=cid).order_by(ContestPage.page_number).limit(6)]
        for pid in ids:
            j1.post(f"/api/contests/{cid}/jury/submit", json={"page_id": pid, "decision": "accepted"})
        self.c.post(f"/api/contests/{cid}/config", json={"review_assignment": "equal"})
        with A.app.app_context():
            j1_new = ContestPage.query.filter_by(contest_id=cid, assigned_to="J1", review_status="pending_review").count()
            j2_new = ContestPage.query.filter_by(contest_id=cid, assigned_to="J2", review_status="pending_review").count()
        self.assertEqual((j1_new, j2_new), (4, 10))


class MyPagesTests(Base):
    def test_validation_hidden_when_not_rewarded(self):
        cid = self.make_contest(entries={1: ("Alice", 4, 1)}, validate_points=0)
        d = self.c.get(f"/api/contests/{cid}/user/Alice/pages").json
        self.assertFalse(d["show_validation"])
        self.assertNotIn("validated", d["summary"])
        self.assertFalse(self.c.get(f"/api/contests/{cid}").json["contest"]["validation_visible"])

    def test_jury_validation_not_in_my_pages(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)}, validate_points=1)
        with A.app.app_context():
            p = ContestPage.query.filter_by(contest_id=cid).first()
            p.wiki_level = 4; p.validator = "Rev"; db.session.commit()
        d = self.c.get(f"/api/contests/{cid}/user/Rev/pages").json
        self.assertEqual(d["total"], 0)
        self.assertFalse(d["show_validation"])


class DeleteTests(Base):
    def test_request_then_tool_admin_deletes(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        boss = A.app.test_client(); self.login("MS Sakib", boss)
        self.assertEqual(boss.post(f"/api/contests/{cid}/delete").json["error_code"], "delete_forbidden")
        self.assertEqual(self.c.post(f"/api/contests/{cid}/delete").status_code, 403)   # admin cannot delete himself
        self.assertTrue(self.c.post(f"/api/contests/{cid}/delete-request", json={"reason": "test contest"}).json["success"])
        self.assertTrue(boss.get(f"/api/contests/{cid}").json["contest"]["can_delete"])
        self.assertEqual(len(boss.get("/api/contests").json["delete_requests"]), 1)
        r = boss.post(f"/api/contests/{cid}/delete").json
        self.assertTrue(r["success"])
        self.assertTrue(os.path.exists(os.path.join(os.environ["PROOFSOURCE_BACKUP_DIR"], r["backup"])))
        self.assertEqual(boss.get(f"/api/contests/{cid}").status_code, 404)
        with A.app.app_context():
            self.assertEqual(AuditLog.query.filter_by(contest_id=cid, action="contest_deleted").count(), 1)

    def test_tool_admin_who_is_contest_admin_deletes_directly(self):
        boss = A.app.test_client(); self.login("MS Sakib", boss)
        now = datetime.utcnow()
        cid = boss.post("/api/contests", json={"title": "Mine", "start_time": now.strftime("%Y-%m-%dT%H:%M"),
                                               "end_time": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")}).json["contest_id"]
        self.assertTrue(boss.post(f"/api/contests/{cid}/delete").json["success"])

    def test_cancel_request(self):
        cid = self.make_contest()
        self.c.post(f"/api/contests/{cid}/delete-request", json={"reason": "x"})
        self.c.delete(f"/api/contests/{cid}/delete-request")
        self.assertIsNone(self.c.get(f"/api/contests/{cid}").json["contest"]["delete_request"])


class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded_without_data_loss(self):
        path = os.path.join(TMP, "old.db")
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE contests (id INTEGER PRIMARY KEY, slug VARCHAR(100) NOT NULL UNIQUE, title VARCHAR(255) NOT NULL,
              description TEXT, wiki_domain VARCHAR(100) NOT NULL, start_time DATETIME NOT NULL, end_time DATETIME NOT NULL,
              proofread_points INTEGER, validate_points INTEGER, max_bonus_points INTEGER, jury_mode VARCHAR(30),
              jury_gets_points BOOLEAN, reject_action VARCHAR(30), created_by VARCHAR(100) NOT NULL, admins TEXT,
              juries TEXT, excluded_users TEXT, approval_status VARCHAR(30), approved_by VARCHAR(100),
              last_synced_at DATETIME, created_at DATETIME);
            INSERT INTO contests (id, slug, title, wiki_domain, start_time, end_time, created_by, proofread_points)
              VALUES (1, 'old', 'Old contest', 'bn.wikisource.org', '2026-01-01 00:00:00', '2026-02-01 00:00:00', 'X', 3);
            CREATE TABLE index_books (id INTEGER PRIMARY KEY, contest_id INTEGER NOT NULL, title VARCHAR(255) NOT NULL,
              base_name VARCHAR(255) NOT NULL, total_pages INTEGER, custom_points INTEGER, excluded_pages TEXT,
              is_active BOOLEAN, created_at DATETIME);
            CREATE TABLE contest_pages (id INTEGER PRIMARY KEY, contest_id INTEGER NOT NULL, book_id INTEGER NOT NULL,
              page_number INTEGER NOT NULL, page_title VARCHAR(255) NOT NULL, wiki_level INTEGER, proofreader VARCHAR(100),
              validator VARCHAR(100), proofread_rev_id INTEGER, proofread_time DATETIME, review_status VARCHAR(30),
              reviewed_by VARCHAR(100), reviewed_at DATETIME, base_points INTEGER, bonus_points INTEGER,
              jury_note VARCHAR(500), locked_by_jury VARCHAR(100), jury_lock_expires DATETIME,
              reserved_by_user VARCHAR(100), user_lock_expires DATETIME);
        """)
        con.commit(); con.close()
        from flask import Flask
        from models import migrate_schema
        app2 = Flask("m")
        app2.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + path
        db.init_app(app2)
        with app2.app_context():
            migrate_schema()
            c = db.session.get(Contest, 1)
            self.assertEqual(c.title, "Old contest")
            self.assertTrue(c.is_review_enabled)
            self.assertFalse(c.bulk_review_enabled)
        cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(contests)")}
        self.assertTrue({"review_enabled", "hub_id", "last_backup_at"} <= cols)


# Cleanup at interpreter exit (not tearDownModule): test_security.py reuses this module's database.
atexit.register(shutil.rmtree, TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
