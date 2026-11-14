"""
Tests for submission modes (auto / enrol / manual), reverted pages that stay listed,
tool admins managed from the UI and the central submission policy.
Run from the repo root:   python3 -m unittest discover -s tests -v
"""
import json
import unittest
from datetime import datetime, timedelta

from test_app import A, Base, db, live, Contest, ContestPage, AuditLog  # noqa: F401
from models import Enrollment, ToolAdmin


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
            c.end_time = datetime.utcnow() - timedelta(days=2)
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
        now = datetime.utcnow()
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
