"""Regression tests for the multi-worker / proxy hardening (secret key, upload cap, rate-limit keys)."""
import os
import tempfile
import unittest
from unittest import mock

from werkzeug.middleware.proxy_fix import ProxyFix

from test_app import A, Base  # noqa: F401


class SecretKeyTests(unittest.TestCase):
    def _load(self, home):
        env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
        env["HOME"] = home
        with mock.patch.dict(os.environ, env, clear=True):
            return A._load_secret_key()

    def test_workers_share_one_key_and_file_is_private(self):
        home = tempfile.mkdtemp()
        first = self._load(home)
        self.assertEqual(self._load(home), first)
        path = os.path.join(home, ".proofsource_secret_key")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(os.listdir(home), [".proofsource_secret_key"])       # no temp leftovers

    def test_a_worker_that_loses_the_race_adopts_the_winners_key(self):
        home = tempfile.mkdtemp()
        path = os.path.join(home, ".proofsource_secret_key")
        real_link = os.link

        def winner_first(src, dst):          # another worker publishes its key just before ours
            with open(dst, "w") as f:
                f.write("winners-key")
            return real_link(src, dst)       # -> FileExistsError

        with mock.patch("os.link", side_effect=winner_first):
            self.assertEqual(self._load(home), "winners-key")
        self.assertEqual(open(path).read(), "winners-key")


class LimitTests(Base):
    def test_upload_cap_is_modest_by_default(self):
        if "PROOFSOURCE_MAX_UPLOAD_MB" not in os.environ:
            self.assertEqual(A.app.config["MAX_CONTENT_LENGTH"], 32 * 1024 * 1024)

    def test_proxy_headers_are_trusted_for_one_hop(self):
        self.assertIsInstance(A.app.wsgi_app, ProxyFix)

    def test_anonymous_visitors_are_limited_separately(self):
        A.limiter.reset()
        for _ in range(3):
            with A.app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.1"}):
                self.assertIsNone(A.rate_limit("t", 3, 60))
        with A.app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.1"}):
            self.assertIsNotNone(A.rate_limit("t", 3, 60))           # same address: blocked
        with A.app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.2"}):
            self.assertIsNone(A.rate_limit("t", 3, 60))              # somebody else: unaffected


if __name__ == "__main__":
    unittest.main()
