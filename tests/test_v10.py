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
