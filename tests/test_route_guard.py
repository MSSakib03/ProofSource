"""Security regression: no state-changing route may forget to look at who is calling."""
import inspect
import unittest

from test_app import A

# a handler counts as guarded when its source consults one of these (or a helper that does)
CALLER_CHECKS = ("get_current_user", "is_super_admin", "is_contest_admin", "is_central_admin", "is_reviewer",
                 "is_listed_admin", "can_delete", "_notice_access", "_wiki_lookup_guard", "login_required")
# public on purpose: logging out only drops the caller's own session
PUBLIC_UNSAFE = {"/logout"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class RouteGuardTests(unittest.TestCase):
    def rules(self):
        return [r for r in A.app.url_map.iter_rules() if r.endpoint != "static" and UNSAFE & set(r.methods or ())]

    def test_every_state_changing_route_checks_the_caller(self):
        unguarded = []
        for rule in self.rules():
            if rule.rule in PUBLIC_UNSAFE:
                continue
            src = inspect.getsource(A.app.view_functions[rule.endpoint])
            if not any(marker in src for marker in CALLER_CHECKS):
                unguarded.append(f"{sorted(rule.methods & UNSAFE)} {rule.rule}")
        self.assertEqual(unguarded, [], "these routes never look at who is calling")

    def test_the_scan_sees_the_routes(self):
        paths = {r.rule for r in self.rules()}
        for expected in ("/api/contests", "/api/admin/trash/purge", "/api/contests/<int:cid>/restore"):
            self.assertIn(expected, paths)
        self.assertGreater(len(paths), 30)

    def test_anonymous_callers_are_refused_on_admin_routes(self):
        c = A.app.test_client()
        for path in ("/api/admin/trash/purge", "/api/admin/tool-admins", "/api/admin/snapshots"):
            r = c.post(path, json={})
            self.assertIn(r.status_code, (401, 403), path)
