"""
Tests for submission modes (auto / enrol / manual), reverted pages that stay listed,
tool admins managed from the UI and the central submission policy.
Run from the repo root:   python3 -m unittest discover -s tests -v
"""
import json
import unittest
from datetime import datetime, timedelta

from test_app import A, Base, db, live, Contest, ContestPage, AuditLog  # noqa: F401
from models import Enrollment, ToolAdmin, utcnow


def statuses(cid):
    with A.app.app_context():
        return {p.page_number: p.review_status for p in ContestPage.query.filter_by(contest_id=cid).all()}


class SubmissionModeTests(Base):
    def resync(self, cid, entries):
        A.fetch_live_book_pages_from_wiki = live(entries)
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})

    def test_auto_mode_lists_everything(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Bob", 3, 2)})
        self.assertEqual(statuses(cid), {1: "pending_review", 2: "pending_review"})
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["contest"]["submission_mode"], "auto")

    def test_enroll_mode_lists_only_enrolled_participants(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Bob", 3, 2)}, submission_mode="enroll")
        self.assertEqual(statuses(cid), {1: "eligible", 2: "eligible"})
        self.assertEqual(self.c.get(f"/api/contests/{cid}").json["stats"]["listed_pages"], 0)
        alice = A.app.test_client(); self.login("Alice", alice)
        r = alice.post(f"/api/contests/{cid}/enroll")
        self.assertTrue(r.json["success"])
        self.assertEqual(r.json["listed"], 1)                      # her earlier page is listed at once
        self.assertTrue(alice.post(f"/api/contests/{cid}/enroll").json["already"])
        d = alice.get(f"/api/contests/{cid}").json
        self.assertTrue(d["contest"]["is_enrolled"])
        self.assertEqual(d["contest"]["enrolled_count"], 1)
        self.assertEqual([e["username"] for e in d["enrolled"]], ["Alice"])
        # later pages: Alice's are listed automatically, Bob's are not
        self.resync(cid, {1: ("Alice", 3, 1), 2: ("Bob", 3, 2), 3: ("Alice", 3, 3), 4: ("Bob", 3, 4)})
        self.assertEqual(statuses(cid), {1: "pending_review", 2: "eligible", 3: "pending_review", 4: "eligible"})
        names = [r["username"] for r in self.c.get(f"/api/contests/{cid}").json["leaderboard"]]
        self.assertEqual(names, ["Alice"])
        with A.app.app_context():
            self.assertTrue(AuditLog.query.filter_by(contest_id=cid, action="enroll").count())

    def test_enroll_needs_login_and_the_right_mode(self):
        cid = self.make_contest()
        self.assertEqual(A.app.test_client().post(f"/api/contests/{cid}/enroll").status_code, 401)
        self.assertEqual(self.c.post(f"/api/contests/{cid}/enroll").json["error_code"], "enrollment_closed")

    def test_manual_mode_submit_selected_pages(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Alice", 3, 2), 3: ("Bob", 3, 3)},
                                submission_mode="manual")
        self.assertEqual(statuses(cid), {1: "eligible", 2: "eligible", 3: "eligible"})
        alice = A.app.test_client(); self.login("Alice", alice)
        mine = alice.get(f"/api/contests/{cid}/my-eligible").json["pages"]
        self.assertEqual(sorted(p["page_number"] for p in mine), [1, 2])
        self.assertEqual(alice.get(f"/api/contests/{cid}").json["contest"]["my_eligible_count"], 2)
        with A.app.app_context():
            bob_page = ContestPage.query.filter_by(contest_id=cid, page_number=3).first().id
        # Alice submits page 1 and tries to sneak in Bob's page: only hers is accepted
        r = alice.post(f"/api/contests/{cid}/submit", json={"page_ids": [mine[0]["id"], bob_page]})
        self.assertEqual(r.json["submitted"], 1)
        self.assertEqual(statuses(cid), {1: "pending_review", 2: "eligible", 3: "eligible"})
        self.assertEqual(alice.post(f"/api/contests/{cid}/submit", json={"page_ids": []}).json["error_code"],
                         "nothing_selected")
        # select all
        r = alice.post(f"/api/contests/{cid}/submit", json={"page_ids": [p["id"] for p in mine]})
        self.assertEqual(r.json["submitted"], 1)
        board = {x["username"]: x for x in self.c.get(f"/api/contests/{cid}").json["leaderboard"]}
        self.assertEqual(board["Alice"]["submitted"], 2)
        self.assertNotIn("Bob", board)
        # a sync never takes submitted pages back
        self.resync(cid, {1: ("Alice", 3, 1), 2: ("Alice", 3, 2), 3: ("Bob", 3, 3)})
        self.assertEqual(statuses(cid)[2], "pending_review")
        # unsubmitted pages are not reviewable
        r = self.c.post(f"/api/contests/{cid}/jury/submit", json={"page_id": bob_page, "decision": "accepted"})
        self.assertEqual(r.json["error_code"], "page_not_reviewable")

    def test_manual_submission_closes_after_grace(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)}, submission_mode="manual")
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            c.end_time = utcnow() - timedelta(days=2)
            db.session.commit()
            pid = ContestPage.query.filter_by(contest_id=cid).first().id
        alice = A.app.test_client(); self.login("Alice", alice)
        r = alice.post(f"/api/contests/{cid}/submit", json={"page_ids": [pid]})
        self.assertEqual(r.json["error_code"], "contest_ended")

    def test_removed_book_takes_unsubmitted_pages_with_it(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)}, submission_mode="manual")
        with A.app.app_context():
            bid = ContestPage.query.filter_by(contest_id=cid).first().book_id
        self.c.post(f"/api/contests/{cid}/books/{bid}/remove", json={"mode": "keep_points"})
        alice = A.app.test_client(); self.login("Alice", alice)
        self.assertEqual(alice.get(f"/api/contests/{cid}/my-eligible").json["pages"], [])

    def test_switching_to_auto_lists_waiting_pages(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Bob", 3, 2)}, submission_mode="manual")
        r = self.c.post(f"/api/contests/{cid}/config", json={"submission_mode": "auto"})
        self.assertTrue(r.json["success"])
        self.assertEqual(statuses(cid), {1: "pending_review", 2: "pending_review"})
        self.assertEqual(self.c.post(f"/api/contests/{cid}/config", json={"submission_mode": "x"}).json["error_code"],
                         "invalid_value")


class RevertedPageTests(Base):
    def test_listed_page_set_back_to_not_proofread_stays_with_zero_points(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Bob", 3, 2)})
        with A.app.app_context():
            pid = ContestPage.query.filter_by(contest_id=cid, page_number=1).first().id
        rev = A.app.test_client(); self.login("Rev", rev)
        self.assertTrue(rev.post(f"/api/contests/{cid}/jury/submit", json={"page_id": pid, "decision": "accepted"}).json["success"])
        # someone sets page 1 back to "Not proofread" on the wiki
        A.fetch_live_book_pages_from_wiki = live({1: ("Alice", 1, 10), 2: ("Bob", 3, 2)})
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        with A.app.app_context():
            p = db.session.get(ContestPage, pid)
            self.assertEqual(p.review_status, "reverted")
            self.assertEqual(p.reverted_from, "accepted")
            self.assertEqual(p.revert_reason, "not_proofread")
            self.assertEqual((p.base_points, p.bonus_points), (0, 0))
            self.assertEqual(p.proofreader, "Alice")
            self.assertEqual(AuditLog.query.filter_by(contest_id=cid, action="page_reverted").count(), 1)
        d = self.c.get(f"/api/contests/{cid}").json
        self.assertEqual(d["stats"]["reverted_pages"], 1)
        self.assertEqual(d["stats"]["listed_pages"], 1)
        self.assertEqual([r["page_number"] for r in d["reverted"]], [1])
        board = {x["username"]: x for x in d["leaderboard"]}
        self.assertEqual((board["Alice"]["points"], board["Alice"]["reverted"], board["Alice"]["submitted"]), (0, 1, 0))
        # still on Alice's page list, with 0 points
        up = self.c.get(f"/api/contests/{cid}/user/Alice/pages").json
        self.assertEqual([(p["review_status"], p["total_points"]) for p in up["pages"]], [("reverted", 0)])
        # never offered for review again
        q = rev.get(f"/api/contests/{cid}/jury/next-page?status=all").json
        self.assertNotIn(pid, [x["id"] for x in q["queue"]])
        self.assertEqual(rev.post(f"/api/contests/{cid}/jury/submit", json={"page_id": pid, "decision": "accepted"}).json["error_code"],
                         "page_not_reviewable")
        # proofread again later -> back in the review queue, logged
        A.fetch_live_book_pages_from_wiki = live({1: ("Alice", 3, 11), 2: ("Bob", 3, 2)})
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        with A.app.app_context():
            p = db.session.get(ContestPage, pid)
            self.assertEqual(p.review_status, "pending_review")
            self.assertIsNone(p.reverted_from)
            self.assertEqual(AuditLog.query.filter_by(contest_id=cid, action="page_restored").count(), 1)

    def test_page_missing_from_complete_listing_is_kept_as_reverted(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Bob", 3, 2)})
        A.fetch_live_book_pages_from_wiki = live({1: ("Alice", 3, 1)}, complete=True)
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        self.assertEqual(statuses(cid), {1: "pending_review", 2: "reverted"})

    def test_unsubmitted_page_that_stops_qualifying_just_disappears(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)}, submission_mode="manual")
        A.fetch_live_book_pages_from_wiki = live({1: ("Alice", 1, 5)})
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        self.assertEqual(statuses(cid), {1: "unproofread"})
        with A.app.app_context():
            self.assertEqual(AuditLog.query.filter_by(contest_id=cid, action="page_reverted").count(), 0)

    def test_backup_keeps_reverted_pages_and_enrollments(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Bob", 3, 2)}, submission_mode="enroll")
        alice = A.app.test_client(); self.login("Alice", alice)
        alice.post(f"/api/contests/{cid}/enroll")
        A.fetch_live_book_pages_from_wiki = live({1: ("Alice", 1, 9), 2: ("Bob", 3, 2)})
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        payload = json.loads(self.c.get(f"/api/contests/{cid}/backup.json").data)
        with A.app.app_context():
            Enrollment.query.delete()
            ContestPage.query.delete()
            db.session.commit()
        r = self.c.post(f"/api/contests/{cid}/restore", data=json.dumps(payload), content_type="application/json")
        self.assertTrue(r.json["success"], r.json)
        self.assertEqual(statuses(cid), {1: "reverted", 2: "eligible"})
        with A.app.app_context():
            self.assertEqual([e.username for e in Enrollment.query.filter_by(contest_id=cid)], ["Alice"])


class ToolAdminTests(Base):
    def setUp(self):
        super().setUp()
        A.invalidate_tool_admin_cache()

    def test_root_admin_adds_and_removes_tool_admins(self):
        root = A.app.test_client(); self.login("MS Sakib", root)
        alice = A.app.test_client(); self.login("Alice", alice)
        self.assertEqual(alice.get("/api/admin/snapshots").status_code, 403)
        self.assertEqual(alice.post("/api/admin/tool-admins", json={"username": "Alice"}).status_code, 403)
        r = root.post("/api/admin/tool-admins", json={"username": "alice"})   # canonical form is stored
        self.assertEqual([a["username"] for a in r.json["admins"]], ["Alice"])
        self.assertEqual(r.json["root"], ["MS Sakib"])
        self.assertTrue(alice.get("/api/session").json["is_super_admin"])
        self.assertEqual(alice.get("/api/admin/snapshots").status_code, 200)
        self.assertEqual(root.post("/api/admin/tool-admins", json={"username": "Alice"}).json["error_code"], "already_tool_admin")
        self.assertEqual(root.post("/api/admin/tool-admins", json={"username": "a|b"}).json["error_code"], "invalid_username")
        # an added tool admin can manage the list too, but never remove a root admin
        self.assertEqual(alice.delete("/api/admin/tool-admins/MS Sakib").json["error_code"], "cannot_remove_root_admin")
        r = root.delete("/api/admin/tool-admins/Alice")
        self.assertEqual(r.json["admins"], [])
        self.assertFalse(alice.get("/api/session").json["is_super_admin"])
        with A.app.app_context():
            acts = {a.action for a in AuditLog.query.filter(AuditLog.contest_id.is_(None)).all()}
            self.assertTrue({"tool_admin_add", "tool_admin_remove"} <= acts)

    def test_added_tool_admin_can_delete_contests_on_request(self):
        with A.app.app_context():
            db.session.add(ToolAdmin(username="Helper", added_by="MS Sakib"))
            db.session.commit()
        A.invalidate_tool_admin_cache()
        cid = self.make_contest()
        self.c.post(f"/api/contests/{cid}/delete-request", json={"reason": "test"})
        helper = A.app.test_client(); self.login("Helper", helper)
        self.assertTrue(helper.post(f"/api/contests/{cid}/delete").json["success"])


class CentralPolicyTests(Base):
    def test_central_submission_policy_is_forced_on_projects(self):
        self.login("MS Sakib")
        now = utcnow()
        r = self.c.post("/api/central", json={
            "title": "Indic", "slug": "indic", "submission_policy": "manual",
            "start_time": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            "end_time": (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M"),
            "projects": [{"wiki_domain": "bn.wikisource.org"}, {"wiki_domain": "hi.wikisource.org"}]})
        self.assertTrue(r.json["success"], r.json)
        with A.app.app_context():
            kids = Contest.query.all()
            self.assertEqual({c.submission_mode for c in kids}, {"manual"})
            cid = kids[0].id
        d = self.c.get(f"/api/contests/{cid}").json["contest"]
        self.assertIn("submission_mode", d["locked_fields"])
        self.c.post("/api/central/indic/config", json={"submission_policy": "auto"})
        with A.app.app_context():
            self.assertEqual({c.submission_mode for c in Contest.query.all()}, {"auto"})
        self.assertEqual(self.c.post("/api/central/indic/config", json={"submission_policy": "nope"}).json["error_code"],
                         "invalid_value")


class ProofreadAnyPageTests(Base):
    def test_random_page_keeps_away_from_submitted_pages(self):
        cid = self.make_contest(entries={50: ("Alice", 3, 1)})
        self.c.post(f"/api/contests/{cid}/sync", json={"force": True})
        orig = A.fetch_all_existing_page_levels
        A.fetch_all_existing_page_levels = lambda d, b: {50: 3}
        try:
            got = set()
            for _ in range(12):
                r = self.c.post(f"/api/contests/{cid}/reserve-page", json={})
                self.assertEqual(r.status_code, 200, r.json)
                n = int(r.json["page_title"].rsplit("/", 1)[1])
                if len(got) < 3:                  # room for wide gaps in a 100-page book
                    self.assertTrue(all(abs(n - m) > 10 for m in got | {50}), (n, got))
                self.assertNotIn(n, got)          # a fresh page on every press
                got.add(n)
        finally:
            A.fetch_all_existing_page_levels = orig
        self.assertGreater(len({n // 10 for n in got}), 1)   # spread out, not one fixed page


if __name__ == "__main__":
    unittest.main()


SITEMATRIX = {"sitematrix": {
    "count": 4,
    "0": {"code": "bn", "name": "বাংলা", "localname": "Bangla", "site": [
        {"url": "https://bn.wikipedia.org", "code": "wiki", "sitename": "উইকিপিডিয়া"},
        {"url": "https://bn.wikisource.org", "code": "wikisource", "sitename": "উইকিসংকলন"}]},
    "1": {"code": "en", "name": "English", "localname": "English", "site": [
        {"url": "https://en.wikisource.org", "code": "wikisource", "sitename": "Wikisource"}]},
    "2": {"code": "xx", "name": "Xx", "localname": "Brand new", "site": [
        {"url": "https://xx.wikisource.org", "code": "wikisource", "sitename": "Wikisource"}]},
    "3": {"code": "ang", "name": "Ænglisc", "localname": "Old English", "site": [
        {"url": "https://ang.wikisource.org", "code": "wikisource", "sitename": "Wikisource", "closed": ""}]},
    "4": {"code": "ta", "name": "தமிழ்", "localname": "Tamil", "site": [
        {"url": "https://ta.wikisource.org", "code": "wikisource", "sitename": "விக்கிமூலம்"}]},
    "5": {"code": "hi", "name": "हिन्दी", "localname": "Hindi", "site": [
        {"url": "https://hi.wikisource.org", "code": "wikisource", "sitename": "विकिस्रोत"}]},
    "6": {"code": "fr", "name": "français", "localname": "French", "site": [
        {"url": "https://fr.wikisource.org", "code": "wikisource", "sitename": "Wikisource"},
        {"url": "https://evil.example.org", "code": "wikisource", "sitename": "Fake"}]},
    "specials": [{"url": "https://wikisource.org", "code": "sources", "sitename": "Wikisource"},
                 {"url": "https://commons.wikimedia.org", "code": "commons", "sitename": "Commons"}],
}}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class WikiListTests(Base):
    """The Wikisource list comes from Meta-Wiki's site matrix: a new wiki needs no code change."""

    def setUp(self):
        super().setUp()
        self._get = A.wiki_engine.requests.get
        A.wiki_engine._WIKI_LIST.update(at=None, ok=False, wikis=None)

    def tearDown(self):
        A.wiki_engine.requests.get = self._get
        A.wiki_engine._WIKI_LIST.update(at=None, ok=False, wikis=None)
        super().tearDown()

    def test_live_list_has_new_wikis_and_skips_closed_and_foreign_ones(self):
        calls = []
        A.wiki_engine.requests.get = lambda url, **kw: calls.append(url) or _Resp(SITEMATRIX)
        d = A.app.test_client().get("/api/wikis").json
        self.assertTrue(d["live"])
        by_domain = {w["domain"]: w for w in d["wikis"]}
        self.assertEqual(set(by_domain), {"bn.wikisource.org", "en.wikisource.org", "xx.wikisource.org", "ta.wikisource.org",
                                          "hi.wikisource.org", "fr.wikisource.org", "wikisource.org"})
        self.assertEqual(by_domain["bn.wikisource.org"]["label"], "বাংলা উইকিসংকলন")
        self.assertEqual(by_domain["xx.wikisource.org"]["name_en"], "Brand new")
        self.assertEqual(d["wikis"][-1]["domain"], "wikisource.org")          # multilingual comes last
        self.assertEqual(calls, ["https://meta.wikimedia.org/w/api.php"])
        A.app.test_client().get("/api/wikis")
        self.assertEqual(len(calls), 1)                                        # cached: Meta is asked once a day

    def test_meta_down_serves_the_builtin_list_and_retries_later(self):
        def boom(url, **kw):
            raise RuntimeError("network down")
        A.wiki_engine.requests.get = boom
        d = A.app.test_client().get("/api/wikis").json
        self.assertFalse(d["live"])
        self.assertIn("bn.wikisource.org", [w["domain"] for w in d["wikis"]])
        # once Meta answers again (after the retry delay) the live list replaces the fallback
        A.wiki_engine._WIKI_LIST["at"] -= A.wiki_engine.WIKI_LIST_RETRY + 1
        A.wiki_engine.requests.get = lambda url, **kw: _Resp(SITEMATRIX)
        self.assertTrue(A.app.test_client().get("/api/wikis").json["live"])

    def test_contest_on_an_unlisted_new_wikisource_is_accepted(self):
        self.login("Admin")
        r = self.c.post("/api/contests", json={"title": "New wiki", "wiki_domain": "zz-new.wikisource.org",
                                               "start_time": "2026-01-01T00:00", "end_time": "2026-02-01T00:00"})
        self.assertTrue(r.json["success"])
        self.assertEqual(r.json["lang_code"], "zz-new")


class DeepCheckTests(Base):
    def test_wiki_cannot_change_once_pages_are_recorded(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        r = self.c.post(f"/api/contests/{cid}/config", json={"wiki_domain": "en.wikisource.org"})
        self.assertEqual(r.json["error_code"], "project_locked")      # started: the project is fixed
        with A.app.app_context():
            self.assertEqual(db.session.get(Contest, cid).wiki_domain, "bn.wikisource.org")

    def test_sync_lock_is_taken_atomically(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            c.sync_started_at = utcnow()            # another worker holds the lock
            db.session.commit()
            self.assertEqual(A.sync_contest_books(c), {"skipped": True, "reason": "sync_in_progress"})
            c.sync_started_at = utcnow() - timedelta(seconds=A.SYNC_LOCK_SECONDS + 5)   # a dead worker's lock
            db.session.commit()
            self.assertFalse(A.sync_contest_books(c)["skipped"])
            self.assertIsNone(db.session.get(Contest, cid).sync_started_at)
            self.assertEqual(ContestPage.query.filter_by(contest_id=cid).count(), 1)             # no duplicate rows

    def test_csv_cells_cannot_become_spreadsheet_formulas(self):
        cid = self.make_contest(entries={1: ("=HYPERLINK(1)", 3, 1), 2: ("Bob", 3, 2)})
        body = self.c.get(f"/api/contests/{cid}/export-csv").data.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(1)", body)
        self.assertNotIn("\n1,=HYPERLINK", body)
        pages = self.c.get(f"/api/contests/{cid}/backup-pages.csv").data.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(1)", pages)
        self.assertEqual(A.backup_engine.csv_safe("Bob"), "Bob")


class DataIntegrityTests(Base):
    def test_old_database_with_duplicate_pages_is_cleaned_and_gets_the_unique_index(self):
        from sqlalchemy import text, inspect
        from models import migrate_schema
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        with A.app.app_context():
            page = ContestPage.query.filter_by(contest_id=cid).one()
            page.review_status = "accepted"
            db.session.commit()
            with db.engine.begin() as conn:                      # the state of a database from before the index
                conn.execute(text("DROP INDEX uq_cp_book_page" + ("" if db.engine.dialect.name == "sqlite" else " ON contest_pages")))
                conn.execute(text("INSERT INTO contest_pages (contest_id, book_id, page_number, page_title, review_status) "
                                  "VALUES (:c, :b, 1, 'dup', 'pending_review')"), {"c": cid, "b": page.book_id})
            self.assertEqual(ContestPage.query.filter_by(contest_id=cid).count(), 2)
            migrate_schema()
            rows = ContestPage.query.filter_by(contest_id=cid).all()
            self.assertEqual([r.review_status for r in rows], ["accepted"])      # the reviewed row is the one kept
            self.assertIn("uq_cp_book_page", {i["name"] for i in inspect(db.engine).get_indexes("contest_pages")})
            with self.assertRaises(Exception):
                with db.engine.begin() as conn:
                    conn.execute(text("INSERT INTO contest_pages (contest_id, book_id, page_number, page_title) "
                                      "VALUES (:c, :b, 1, 'again')"), {"c": cid, "b": page.book_id})

    def test_long_history_is_read_to_the_end(self):
        """A page first proofread after its 500th revision used to be judged on a cut-off history."""
        start, end = datetime(2026, 1, 1), datetime(2026, 2, 1)
        raw = lambda lvl, user, body: (f'<noinclude><pagequality level="{lvl}" user="{user}" /></noinclude>'
                                       f'{body}<noinclude></noinclude>')
        revs = [{"revid": i, "user": "Typist", "timestamp": "2025-06-01T00:00:00Z",
                 "slots": {"main": {"*": raw(1, "Typist", f"text {i}")}}} for i in range(1, 701)]
        revs.append({"revid": 701, "user": "Alice", "timestamp": "2026-01-10T00:00:00Z",
                     "slots": {"main": {"*": raw(3, "Alice", "final text")}}})
        calls = []

        def fake_get(url, params=None, **kw):
            off = int(params.get("rvcontinue") or 0)
            calls.append(off)
            chunk = revs[off:off + 50]
            out = {"query": {"pages": {"1": {"revisions": chunk}}}}
            if off + 50 < len(revs):
                out["continue"] = {"rvcontinue": str(off + 50)}
            return _Resp(out)

        real = A.wiki_engine.requests.get
        A.wiki_engine.requests.get = fake_get
        try:
            res = A.wiki_engine.verify_page_in_contest_window("https://bn.wikisource.org/w/api.php", "Page:B/1", start, end)
            self.assertTrue(res["eligible"])
            self.assertEqual(res["proofreader"], "Alice")
            self.assertEqual(len(calls), 15)
            with self.assertRaises(RuntimeError):      # over the cap: an error, never a guess on a cut-off history
                list(A.wiki_engine.iter_revision_history("https://bn.wikisource.org/w/api.php", "Page:B/1", max_revs=600))
        finally:
            A.wiki_engine.requests.get = real

    def test_sync_that_runs_out_of_time_is_continued_by_the_next_call(self):
        import time as _time
        cid = self.make_contest(books="Index:A.pdf\nIndex:B.pdf", entries={1: ("Alice", 3, 1)})
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            res = A.sync_contest_books(c, deadline=_time.monotonic() - 1)      # no time at all
            self.assertTrue(res["partial"])
            self.assertFalse(res["complete"])
            self.assertIsNone(db.session.get(Contest, cid).last_synced_at)     # marked unfinished ...
            self.assertTrue(self.c.get(f"/api/contests/{cid}").json["contest"]["needs_sync"])
            res = A.sync_contest_books(c, deadline=_time.monotonic() + 60)     # ... and the next call finishes it
            self.assertEqual((res["partial"], res["complete"]), (False, True))
            self.assertIsNotNone(db.session.get(Contest, cid).last_synced_at)
            self.assertTrue(all(b.last_synced_at for b in c.books))

    def test_book_lookup_needs_login_and_a_wikisource(self):
        anon = A.app.test_client()
        self.assertEqual(anon.get("/api/wiki/index-search?wiki=bn.wikisource.org&q=abc").status_code, 401)
        self.login("Admin")
        self.assertEqual(self.c.get("/api/wiki/index-search?wiki=evil.example&q=abc").json["error_code"], "invalid_project")
        real_s, real_i = A.wiki_engine.search_index_titles, A.wiki_engine.index_info
        A.wiki_engine.search_index_titles = lambda d, q, limit=10: [f"Index:{q}.pdf"]
        A.wiki_engine.index_info = lambda d, t: {"title": t, "base_name": t, "exists": True, "total_pages": 120}
        try:
            self.assertEqual(self.c.get("/api/wiki/index-search?wiki=bn.wikisource.org&q=Boi").json["titles"], ["Index:Boi.pdf"])
            self.assertEqual(self.c.get("/api/wiki/index-search?wiki=bn.wikisource.org&q=B").json["titles"], [])
            self.assertEqual(self.c.get("/api/wiki/index-info?wiki=bn.wikisource.org&title=Index:Boi.pdf").json["total_pages"], 120)
        finally:
            A.wiki_engine.search_index_titles, A.wiki_engine.index_info = real_s, real_i
