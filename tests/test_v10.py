"""v10: trash, start/end locks, book dates, tool-admin naming, page notices. No network."""
import os
import unittest
from datetime import timedelta

from test_app import A, Base, db, Contest, IndexBook, ContestPage, Hub, AuditLog, utcnow  # noqa: F401


def boss_client(case):
    boss = A.app.test_client()
    case.login("MS Sakib", boss)
    return boss


class TrashTests(Base):
    def _delete(self, boss, cid):
        self.c.post(f"/api/contests/{cid}/delete-request", json={"reason": "t"})
        return boss.post(f"/api/contests/{cid}/delete").json

    def test_deleted_contest_waits_in_trash_for_tool_admin_only(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        boss = boss_client(self)
        r = self._delete(boss, cid)
        self.assertTrue(r["success"])
        self.assertEqual(r["trash_days"], 7)
        # gone for everybody
        self.assertEqual(self.c.get(f"/api/contests/{cid}").status_code, 404)
        self.assertEqual(boss.get(f"/api/contests/{cid}").status_code, 404)
        self.assertEqual([x["id"] for x in self.c.get("/api/contests").json["contests"]], [])
        # the trash is for the tool admin
        self.assertEqual(self.c.get("/api/admin/trash").status_code, 403)
        items = boss.get("/api/admin/trash").json["items"]
        self.assertEqual([i["id"] for i in items], [cid])
        self.assertEqual(items[0]["pages"], 1)

    def test_restore_brings_everything_back(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        boss = boss_client(self)
        self._delete(boss, cid)
        self.assertEqual(self.c.post("/api/admin/trash/restore", json={"type": "contest", "id": cid}).status_code, 403)
        self.assertTrue(boss.post("/api/admin/trash/restore", json={"type": "contest", "id": cid}).json["success"])
        self.assertEqual(self.c.get(f"/api/contests/{cid}").status_code, 200)
        with A.app.app_context():
            self.assertEqual(ContestPage.query.filter_by(contest_id=cid).count(), 1)

    def test_old_items_are_removed_after_a_week_and_trash_can_be_emptied(self):
        a = self.make_contest(entries={1: ("Alice", 3, 1)})
        b = self.make_contest(entries={1: ("Bob", 3, 2)})
        boss = boss_client(self)
        self._delete(boss, a); self._delete(boss, b)
        with A.app.app_context():
            db.session.get(Contest, a).deleted_at = utcnow() - timedelta(days=8)
            db.session.commit()
            self.assertEqual(A.purge_expired_trash(force=True), 1)
            self.assertIsNone(db.session.get(Contest, a))
            self.assertEqual(ContestPage.query.filter_by(contest_id=a).count(), 0)
            self.assertIsNotNone(db.session.get(Contest, b))
        r = boss.post("/api/admin/trash/purge", json={"all": True}).json
        self.assertEqual(r["removed"], 1)
        with A.app.app_context():
            self.assertIsNone(db.session.get(Contest, b))
            self.assertEqual(AuditLog.query.filter_by(contest_id=b, action="contest_purged").count(), 1)

    def test_deleted_slug_stays_reserved_until_purged(self):
        cid = self.make_contest()
        with A.app.app_context():
            slug = db.session.get(Contest, cid).slug
        boss = boss_client(self)
        self._delete(boss, cid)
        now = utcnow()
        r = self.c.post("/api/contests", json={"title": "again", "slug": slug,
                                               "start_time": now.strftime("%Y-%m-%dT%H:%M"),
                                               "end_time": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
        self.assertTrue(r.json.get("success") or r.json.get("error_code"))
        if r.json.get("success"):
            with A.app.app_context():
                self.assertNotEqual(db.session.get(Contest, r.json["contest_id"]).slug, slug)


def _fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


class LockTests(Base):
    def test_started_contest_keeps_its_start_and_project_but_the_end_can_move(self):
        cid = self.make_contest()
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            start, end = c.start_time, c.end_time
        r = self.c.post(f"/api/contests/{cid}/config", json={"start_time": _fmt(start + timedelta(days=1))})
        self.assertEqual(r.json["error_code"], "start_locked")
        r = self.c.post(f"/api/contests/{cid}/config", json={"wiki_domain": "en.wikisource.org"})
        self.assertEqual(r.json["error_code"], "project_locked")
        # the unchanged start (as the form sends it back) and a new end are fine
        r = self.c.post(f"/api/contests/{cid}/config", json={"start_time": _fmt(start), "wiki_domain": "bn.wikisource.org",
                                                              "end_time": _fmt(end + timedelta(days=3))})
        self.assertTrue(r.json["success"], r.json)
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            self.assertEqual(c.start_time, start)
            self.assertEqual(c.end_time, end + timedelta(days=3))

    def test_contest_that_has_not_begun_can_still_move_everything(self):
        now = utcnow()
        cid = self.make_contest(start_time=_fmt(now + timedelta(days=2)), end_time=_fmt(now + timedelta(days=9)))
        r = self.c.post(f"/api/contests/{cid}/config", json={"start_time": _fmt(now + timedelta(days=3)),
                                                              "wiki_domain": "en.wikisource.org"})
        self.assertTrue(r.json["success"], r.json)

    def test_central_start_is_locked_and_projects_cannot_be_removed_after_start(self):
        self.login("MS Sakib")
        now = utcnow()
        body = {"title": "Indic", "slug": "indic2", "start_time": _fmt(now - timedelta(days=1)),
                "end_time": _fmt(now + timedelta(days=9)),
                "projects": [{"wiki_domain": "bn.wikisource.org"}, {"wiki_domain": "hi.wikisource.org"}]}
        self.assertTrue(self.c.post("/api/central", json=body).json["success"])
        r = self.c.post("/api/central/indic2/config", json={"start_time": _fmt(now)})
        self.assertEqual(r.json["error_code"], "start_locked")
        r = self.c.post("/api/central/indic2/config", json={"projects": [{"wiki_domain": "bn.wikisource.org"}]})
        self.assertEqual(r.json["error_code"], "project_locked")
        r = self.c.post("/api/central/indic2/config", json={"end_time": _fmt(now + timedelta(days=20)),
                                                            "projects": [{"wiki_domain": "bn.wikisource.org"},
                                                                         {"wiki_domain": "hi.wikisource.org"},
                                                                         {"wiki_domain": "as.wikisource.org"}]})
        self.assertTrue(r.json["success"], r.json)


class BookDateTests(Base):
    def test_book_dates_must_stay_inside_the_contest(self):
        cid = self.make_contest(books="")
        now = utcnow()
        ok = {"title": "Index:B.pdf", "start_time": _fmt(now - timedelta(days=3)), "end_time": _fmt(now + timedelta(days=1))}
        bad_start = dict(ok, start_time=_fmt(now - timedelta(days=30)))
        bad_end = dict(ok, end_time=_fmt(now + timedelta(days=30)))
        for body in (bad_start, bad_end):
            self.assertEqual(self.c.post(f"/api/contests/{cid}/books", json=body).json["error_code"], "book_dates_outside_contest")
        r = self.c.post(f"/api/contests/{cid}/books", json=ok)
        self.assertTrue(r.json["success"], r.json)
        # editing is checked the same way
        bid = r.json["book_id"]
        r = self.c.post(f"/api/contests/{cid}/books/{bid}/update", json={"end_time": _fmt(now + timedelta(days=30))})
        self.assertEqual(r.json["error_code"], "book_dates_outside_contest")


class NoticeTests(Base):
    def setUp(self):
        super().setUp()
        self.cid = self.make_contest(entries={1: ("Alice", 3, 1), 2: ("Alice", 3, 2), 3: ("Alice", 3, 3), 4: ("Bob", 3, 4)})
        self.sent = []
        A.post_talk_message = lambda domain, target, subject, text, creds=None: (
            self.sent.append((domain, target, subject, text)) or {"saved_on_wiki": True, "revid": 7})
        with A.app.app_context():
            pages = {p.page_number: p for p in ContestPage.query.filter_by(contest_id=self.cid).all()}
            for n, (st, note) in {1: ("needs_fix", "ফুটনোট বাদ"), 2: ("rejected", ""), 3: ("accepted", ""), 4: ("needs_fix", "x")}.items():
                pages[n].review_status, pages[n].jury_note, pages[n].reviewed_at = st, note, utcnow() - timedelta(hours=1)
            db.session.commit()
            self.ids = {n: p.id for n, p in pages.items()}

    def test_draft_lists_only_alices_problem_pages_in_bangla_wikitext(self):
        d = self.c.get(f"/api/contests/{self.cid}/user/Alice/notice").json
        self.assertEqual(d["count"], 2)
        self.assertEqual(set(d["page_ids"]), {self.ids[1], self.ids[2]})
        self.assertIn("সংশোধন প্রয়োজন", d["text"])
        self.assertIn("গৃহীত হয়নি", d["text"])
        self.assertIn("# [[Page:Book.pdf/1|Book.pdf/১]]; ফুটনোট বাদ", d["text"].replace("\n", "\n"))
        self.assertNotIn("Bob", d["text"])

    def test_only_reviewers_and_admins_can_use_it(self):
        other = A.app.test_client(); self.login("Stranger", other)
        self.assertEqual(other.get(f"/api/contests/{self.cid}/user/Alice/notice").status_code, 403)
        self.assertEqual(A.app.test_client().get(f"/api/contests/{self.cid}/user/Alice/notice").status_code, 401)

    def test_sending_posts_to_the_talk_page_and_never_repeats_a_page(self):
        d = self.c.get(f"/api/contests/{self.cid}/user/Alice/notice").json
        r = self.c.post(f"/api/contests/{self.cid}/user/Alice/notice",
                        json={"subject": d["subject"], "text": d["text"] + "\nধন্যবাদ", "page_ids": d["page_ids"]})
        self.assertEqual(r.json["sent"], 2)
        domain, target, subject, text = self.sent[0]
        self.assertEqual((domain, target), ("bn.wikisource.org", "Alice"))
        self.assertTrue(text.rstrip().endswith("~~~~"))          # signed
        self.assertIn("ধন্যবাদ", text)                            # the reviewer's edit went through
        again = self.c.get(f"/api/contests/{self.cid}/user/Alice/notice").json
        self.assertEqual(again["count"], 0)
        self.assertEqual(self.c.post(f"/api/contests/{self.cid}/user/Alice/notice",
                                     json={"subject": "s", "text": "t", "page_ids": d["page_ids"]}).json["error_code"],
                         "notice_no_pages")
        # a new decision on a page makes it eligible again; other pages stay quiet
        with A.app.app_context():
            p = db.session.get(ContestPage, self.ids[1]); p.reviewed_at = utcnow() + timedelta(minutes=1)
            db.session.commit()
        self.assertEqual(self.c.get(f"/api/contests/{self.cid}/user/Alice/notice").json["page_ids"], [self.ids[1]])

    def test_failed_post_marks_nothing(self):
        A.post_talk_message = lambda *a, **k: {"saved_on_wiki": False, "error": "badtoken"}
        d = self.c.get(f"/api/contests/{self.cid}/user/Alice/notice").json
        r = self.c.post(f"/api/contests/{self.cid}/user/Alice/notice",
                        json={"subject": "s", "text": "t", "page_ids": d["page_ids"]})
        self.assertEqual(r.json["error_code"], "notice_failed")
        self.assertEqual(self.c.get(f"/api/contests/{self.cid}/user/Alice/notice").json["count"], 2)
