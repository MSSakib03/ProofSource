"""
Regression tests for the security audit fixes (no network: every wiki call is mocked).
Run from the repo root:   python3 -m unittest discover -s tests -v
"""
import json
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from test_app import A, Base, db, backup_engine, wiki_engine, Contest, ContestPage, Hub, OAuthLogin  # noqa: F401


class CSRFTests(Base):
    def test_post_without_token_is_refused(self):
        self.login("Admin")
        self.c.send_csrf = False
        r = self.c.post("/api/contests", json={"title": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json["error_code"], "csrf_failed")

    def test_wrong_token_and_cross_site_are_refused(self):
        self.login("Admin")
        self.c.send_csrf = False
        r = self.c.post("/logout", headers={"X-CSRF-Token": "nope"})
        self.assertEqual(r.status_code, 403)
        self.c.send_csrf = True
        r = self.c.post("/api/contests", json={"title": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/contests", json={"title": "x"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_token_is_exposed_to_the_page_and_session_api(self):
        html = self.c.get("/").data.decode()
        self.assertIn('"csrf_token":', html)
        tok = self.c.get("/api/session").json["csrf_token"]
        self.assertTrue(len(tok) > 20)

    def test_logout_issues_a_fresh_token(self):
        self.login("Admin")
        r = self.c.post("/logout")
        self.assertTrue(r.json["success"])
        self.assertTrue(r.json["csrf_token"])
        self.assertFalse(self.c.get("/api/session").json["logged_in"])


class HeaderTests(Base):
    def test_security_headers_and_csp_nonce(self):
        r = self.c.get("/")
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', r.data.decode())
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("cdn.tailwindcss.com", r.data.decode())       # no third-party assets
        self.assertNotIn("fonts.googleapis.com", r.data.decode())

    def test_api_is_not_cached(self):
        self.assertEqual(self.c.get("/api/session").headers["Cache-Control"], "no-store")


class WikiDomainTests(Base):
    def test_get_api_url_only_allows_wikisource(self):
        self.assertEqual(wiki_engine.get_api_url("https://BN.wikisource.org/"), "https://bn.wikisource.org/w/api.php")
        self.assertEqual(wiki_engine.get_api_url("wikisource.org"), "https://wikisource.org/w/api.php")
        for bad in ("evil.com", "bn.wikisource.org.evil.com", "localhost:8080", "bn.wikisource.org/x?",
                    "x\" onmouseover=\"alert(1)", "169.254.169.254", ""):
            with self.assertRaises(ValueError, msg=bad):
                wiki_engine.get_api_url(bad)

    def test_contest_on_foreign_server_is_refused(self):
        self.login("Admin")
        now = datetime.utcnow()
        r = self.c.post("/api/contests", json={"title": "x", "wiki_domain": "attacker.example",
                                               "start_time": now.strftime("%Y-%m-%dT%H:%M"),
                                               "end_time": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json["error_code"], "invalid_project")

    def test_switching_wiki_needs_new_approval(self):
        cid = self.make_contest()
        r = self.c.post(f"/api/contests/{cid}/config", json={"wiki_domain": "evil.example"})
        self.assertEqual(r.json["error_code"], "invalid_project")
        r = self.c.post(f"/api/contests/{cid}/config", json={"wiki_domain": "en.wikisource.org"})
        self.assertTrue(r.json["success"])
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            self.assertEqual(c.wiki_domain, "en.wikisource.org")
            self.assertEqual(c.approval_status, "pending")

    def test_central_with_foreign_project_is_refused(self):
        self.login("MS Sakib")
        r = self.c.post("/api/central", json={"title": "x", "slug": "h", "start_time": "2030-01-01T00:00",
                                              "end_time": "2030-02-01T00:00",
                                              "projects": [{"wiki_domain": "evil.example"}]})
        self.assertEqual(r.json["error_code"], "invalid_project")


class XSSTests(Base):
    def test_oauth_error_is_not_reflected(self):
        r = self.c.get("/auth/callback?error=<script>alert(1)</script>&error_description=<img src=x onerror=alert(1)>")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.mimetype, "text/plain")
        self.assertNotIn(b"<script>", r.data)
        self.assertNotIn(b"onerror", r.data)

    def test_central_link_must_be_http(self):
        self.login("MS Sakib")
        body = {"title": "x", "slug": "h", "start_time": "2030-01-01T00:00", "end_time": "2030-02-01T00:00",
                "projects": [{"wiki_domain": "bn.wikisource.org"}], "link": "javascript:alert(document.cookie)"}
        r = self.c.post("/api/central", json=body)
        self.assertEqual(r.json["error_code"], "invalid_link")
        body["link"] = "https://meta.wikimedia.org/wiki/Event"
        self.assertTrue(self.c.post("/api/central", json=body).json["success"])
        # a dangerous link already stored in the database is never handed to the browser
        with A.app.app_context():
            h = Hub.query.filter_by(slug="h").first()
            h.link = "javascript:alert(1)"
            db.session.commit()
        self.assertEqual(self.c.get("/api/central/h").json["central"]["link"], "")


class RestoreTests(Base):
    def _backup(self, cid):
        return json.loads(self.c.get(f"/api/contests/{cid}/backup.json").data)

    def test_backup_is_signed_and_forged_backup_refused(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        payload = self._backup(cid)
        self.assertIn("signature", payload)
        # a contest admin edits the file and recomputes the (public) checksum
        payload["data"]["pages"][0]["bonus_points"] = 500
        payload["checksum"] = backup_engine._checksum(payload["data"])
        r = self.c.post(f"/api/contests/{cid}/restore", data=json.dumps(payload), content_type="application/json")
        self.assertIn("signature_invalid", r.json["problems"])
        del payload["signature"]
        r = self.c.post(f"/api/contests/{cid}/restore", data=json.dumps(payload), content_type="application/json")
        self.assertIn("signature_invalid", r.json["problems"])

    def test_restore_never_changes_approval_owner_or_wiki(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            c.approval_status = "rejected"
            db.session.commit()
        payload = self._backup(cid)
        payload["data"]["contest"].update({"approval_status": "approved", "approved_by": "Admin",
                                           "created_by": "Mallory", "wiki_domain": "evil.example"})
        payload["checksum"] = backup_engine._checksum(payload["data"])
        payload.pop("signature")
        boss = A.app.test_client(); self.login("MS Sakib", boss)   # tool admin may restore unsigned files
        r = boss.post(f"/api/contests/{cid}/restore", data=json.dumps(payload), content_type="application/json")
        self.assertTrue(r.json["success"], r.json)
        with A.app.app_context():
            c = db.session.get(Contest, cid)
            self.assertEqual(c.approval_status, "rejected")
            self.assertEqual(c.created_by, "Admin")
            self.assertEqual(c.wiki_domain, "bn.wikisource.org")

    def test_non_object_backup_is_rejected_cleanly(self):
        cid = self.make_contest()
        r = self.c.post(f"/api/contests/{cid}/restore/validate", data="[1,2]", content_type="application/json")
        self.assertEqual(r.status_code, 400)


class IdentityTests(Base):
    def test_super_admin_is_matched_exactly(self):
        self.assertTrue(A.is_super_admin("MS Sakib"))
        self.assertTrue(A.is_super_admin("MS_Sakib"))
        self.assertFalse(A.is_super_admin("Ms sakib"))
        self.assertFalse(A.is_super_admin("ms sakib"))
        self.assertFalse(A.is_super_admin("MS SAKIB"))

    def test_expired_login_record_is_not_accepted(self):
        self.login("Admin")
        with A.app.app_context():
            for rec in OAuthLogin.query.all():
                rec.created_at = datetime.utcnow() - timedelta(days=60)
            db.session.commit()
        self.assertFalse(self.c.get("/api/session").json["logged_in"])

    def test_tokens_are_encrypted_at_rest(self):
        r = self.c.get("/login")
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(r.headers["Location"]).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])   # PKCE

        class R:
            def __init__(s, js): s.status_code = 200; s._js = js; s.text = ""
            def json(s): return s._js
        sent = {}

        def fake_post(url, data=None, **k):
            sent.update(data or {})
            return R({"access_token": "SECRET-AT", "refresh_token": "SECRET-RT", "expires_in": 14400})
        with mock.patch.object(A.requests, "post", fake_post), \
             mock.patch.object(A.requests, "get", lambda *a, **k: R({"username": "Some User"})):
            self.c.get(f"/auth/callback?code=x&state={q['state'][0]}")
        self.assertTrue(sent.get("code_verifier"))
        with A.app.app_context():
            rec = OAuthLogin.query.filter_by(username="Some User").first()
            self.assertTrue(rec.access_token.startswith("enc1:"))
            self.assertNotIn("SECRET", rec.access_token + rec.refresh_token)
        with A.app.test_request_context():
            from flask import session
            session["login_id"] = rec.id
            self.assertEqual(A.get_oauth_creds(), {"access_token": "SECRET-AT"})


class AbuseTests(Base):
    def test_redos_page_is_parsed_in_linear_time(self):
        evil = '<noinclude><pagequality level="3" user="x" />' + "</noinclude><noinclude>" * 50000 + "x"
        t = time.monotonic()
        wiki_engine.parse_proofread_page(evil)
        self.assertLess(time.monotonic() - t, 1.0)

    def test_reserving_pages_needs_login(self):
        cid = self.make_contest()
        anon = A.app.test_client()
        r = anon.post(f"/api/contests/{cid}/reserve-page", json={})
        self.assertEqual(r.status_code, 401)

    def test_forced_sync_needs_login_and_is_rate_limited(self):
        cid = self.make_contest()
        calls = A.fetch_live_book_pages_from_wiki.calls
        anon = A.app.test_client()
        before = len(calls)
        anon.post(f"/api/contests/{cid}/sync", json={"force": True})   # anonymous: treated as non-forced
        self.assertEqual(len(calls), before)                           # data is fresh -> skipped
        codes = [self.c.post(f"/api/contests/{cid}/sync", json={"force": True}).status_code for _ in range(12)]
        self.assertIn(429, codes)

    def test_garbage_numbers_are_400_not_500(self):
        cid = self.make_contest()
        r = self.c.post(f"/api/contests/{cid}/config", json={"proofread_points": "lots"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post(f"/api/contests/{cid}/config", json={"jury_mode": "anything"})
        self.assertEqual(r.json["error_code"], "invalid_value")

    def test_reviewer_cannot_override_a_page_locked_by_another(self):
        cid = self.make_contest(entries={1: ("Alice", 3, 1)})
        with A.app.app_context():
            p = ContestPage.query.filter_by(contest_id=cid).first()
            p.locked_by_jury = "Admin"
            p.jury_lock_expires = datetime.utcnow() + timedelta(minutes=5)
            db.session.commit()
            pid = p.id
        rev = A.app.test_client(); self.login("Rev", rev)
        r = rev.post(f"/api/contests/{cid}/jury/submit", json={"page_id": pid, "decision": "rejected"})
        self.assertEqual(r.status_code, 409)
        r = rev.get(f"/api/contests/{cid}/jury/next-page?page_id={pid}")
        with A.app.app_context():
            self.assertEqual(db.session.get(ContestPage, pid).locked_by_jury, "Admin")


if __name__ == "__main__":
    unittest.main()
