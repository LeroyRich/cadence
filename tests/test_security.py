import json
import logging
import os
import hashlib
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tagger.artwork import fetch_public_response
from tagger.library import LibraryError
from tagger.server import Application, Handler, REMEMBER_LIFETIME, TooManyRequests, main


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.app = Application(self.state_dir, resume_jobs=False)
        self.app.set_password("test-password-123")

    def test_password_change_requires_current_password_and_revokes_sessions(self):
        token, csrf = self.app.login("test-password-123")
        remembered, remembered_csrf = self.app.login("test-password-123", remember_me=True)
        with self.assertRaises(PermissionError):
            self.app.change_password("wrong", "replacement-password-123")
        self.assertEqual(self.app.session(token), csrf)
        self.assertEqual(self.app.session(remembered), remembered_csrf)
        self.app.change_password("test-password-123", "replacement-password-123")
        self.assertIsNone(self.app.session(token))
        self.assertIsNone(Application(self.state_dir, resume_jobs=False).session(remembered))
        with self.assertRaises(LibraryError):
            self.app.login("test-password-123")
        self.assertTrue(self.app.login("replacement-password-123")[0])

    def test_remembered_session_survives_restart_and_logout_revokes_it(self):
        ordinary, _ = self.app.login("test-password-123")
        remembered, csrf = self.app.login("test-password-123", remember_me=True)
        with self.app.library._db() as db:
            rows = db.execute("SELECT token_hash,csrf,created,expires FROM remembered_sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_hash"], hashlib.sha256(remembered.encode("ascii")).hexdigest())
        self.assertEqual(rows[0]["csrf"], csrf)
        self.assertAlmostEqual(rows[0]["expires"] - rows[0]["created"], REMEMBER_LIFETIME)
        reopened = Application(self.state_dir, resume_jobs=False)
        self.assertIsNone(reopened.session(ordinary))
        self.assertEqual(reopened.session(remembered), csrf)
        reopened.logout(remembered)
        self.assertIsNone(self.app.session(remembered))
        self.assertIsNone(Application(self.state_dir, resume_jobs=False).session(remembered))

    def test_expired_and_invalid_remembered_sessions_are_rejected(self):
        remembered, _ = self.app.login("test-password-123", remember_me=True)
        self.assertIsNone(self.app.session("not-a-session-token"))
        with self.app.library._db() as db:
            db.execute("UPDATE remembered_sessions SET expires=0")
        self.assertIsNone(self.app.session(remembered))
        with self.app.library._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM remembered_sessions").fetchone()[0], 0)

    def test_remembered_session_keeps_csrf_protection_after_restart(self):
        token, csrf = self.app.login("test-password-123", remember_me=True)
        reopened = Application(self.state_dir, resume_jobs=False)
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=reopened)
        handler.headers = {"Cookie": "tagger_session={}".format(token)}
        handler.path = "/api/logout"
        handler._body = lambda: {}
        captured = {}
        handler._send_json = lambda value, status=200, cookie=None: captured.update(
            value=value, status=status, cookie=cookie)
        with self.assertRaisesRegex(PermissionError, "Invalid request token"):
            handler._session(modify=True)
        handler.headers["X-CSRF-Token"] = csrf
        handler._handle("POST")
        self.assertEqual(captured["value"], {"ok": True})
        self.assertIn("Max-Age=0", captured["cookie"])
        self.assertIsNone(reopened.session(token))

    def test_failed_login_and_invalid_remember_option_do_not_create_session(self):
        with self.assertRaises(LibraryError):
            self.app.login("wrong", remember_me=True)
        with self.assertRaisesRegex(LibraryError, "Remember me"):
            self.app.login("test-password-123", remember_me="true")
        with self.app.library._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM remembered_sessions").fetchone()[0], 0)

    def test_password_reset_revokes_remembered_sessions(self):
        ordinary, _ = self.app.login("test-password-123")
        remembered, _ = self.app.login("test-password-123", remember_me=True)
        Application(self.state_dir, resume_jobs=False).set_password("another-password-123")
        self.assertIsNone(self.app.session(ordinary))
        self.assertIsNone(Application(self.state_dir, resume_jobs=False).session(remembered))

    def test_failed_logins_throttle_without_proxy_ip_lockout(self):
        for _ in range(20):
            with self.assertRaises(LibraryError):
                self.app.login("wrong", "127.0.0.1")
        with self.assertRaises(TooManyRequests):
            self.app.login("test-password-123", "127.0.0.1")
        with self.app.lock:
            self.app.login_failures.clear()
        self.assertTrue(self.app.login("test-password-123", "127.0.0.1")[0])

    def test_cookie_modes_and_browser_headers(self):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=self.app)
        handler.headers = {}
        handler.path = "/api/login"
        handler._body = lambda: {"password": "test-password-123"}
        captured = {}
        handler._send_json = lambda value, status=200, cookie=None: captured.update(
            status=status, cookie=cookie)
        handler._handle("POST")
        self.assertIn("tagger_session=", captured["cookie"])
        self.assertNotIn("; Secure", captured["cookie"])
        self.assertNotIn("Max-Age", captured["cookie"])
        handler._body = lambda: {"password": "test-password-123", "remember_me": True}
        handler._handle("POST")
        self.assertIn("Max-Age={}".format(REMEMBER_LIFETIME), captured["cookie"])
        self.assertIn("HttpOnly; SameSite=Strict; Path=/", captured["cookie"])
        self.app.secure_cookies = True
        handler._handle("POST")
        self.assertIn("__Host-cadence_session=", captured["cookie"])
        self.assertIn("; Secure", captured["cookie"])
        handler.send_header = lambda name, value: captured.__setitem__(name, value)
        handler._security_headers()
        self.assertEqual(captured["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", captured["Content-Security-Policy"])

    def test_body_limits_and_content_type(self):
        handler = object.__new__(Handler)
        handler.path = "/api/login"
        handler.headers = {"Content-Type": "application/json", "Content-Length": "8193"}
        handler.rfile = BytesIO()
        with self.assertRaisesRegex(LibraryError, "too large"):
            handler._body()
        payload = json.dumps({"password": "test-password-123"}).encode()
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
        handler.rfile = BytesIO(payload)
        self.assertEqual(handler._body()["password"], "test-password-123")
        handler.headers["Content-Type"] = "text/plain"
        with self.assertRaisesRegex(LibraryError, "application/json"):
            handler._body()

    def test_state_permissions(self):
        if not hasattr(os, "geteuid"):
            self.skipTest("POSIX permissions needed")
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state_dir / "library.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_https_proxy_startup_reaches_server(self):
        logger = logging.getLogger("cadence")
        existing = set(logger.handlers)

        def close_new_handlers():
            for handler in list(logger.handlers):
                if handler not in existing:
                    logger.removeHandler(handler)
                    handler.close()

        self.addCleanup(close_new_handlers)
        with patch("sys.argv", ["cadence", "--state-dir", str(self.state_dir), "--https-proxy"]), patch(
                "tagger.server.BoundedHTTPServer") as server:
            main()
        self.assertTrue(server.return_value.app.secure_cookies)
        server.return_value.serve_forever.assert_called_once_with()

    def test_metadata_download_rejects_private_redirect_and_large_response(self):
        class Response:
            status = 302

            def getheader(self, name):
                return "http://127.0.0.1/private" if name == "Location" else None

        class Connection:
            def request(self, method, target, headers):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with patch("tagger.artwork._public_connection", side_effect=[
                (Connection(), "/metadata"), ValueError("Only public artwork URLs are allowed")]):
            with self.assertRaisesRegex(ValueError, "public artwork"):
                fetch_public_response("https://musicbrainz.org/metadata", 1024)

        class OversizedResponse(Response):
            status = 200

            def getheader(self, name):
                return "2048" if name == "Content-Length" else None

        oversized = Connection()
        oversized.getresponse = lambda: OversizedResponse()
        with patch("tagger.artwork._public_connection", return_value=(oversized, "/metadata")):
            with self.assertRaisesRegex(ValueError, "larger"):
                fetch_public_response("https://musicbrainz.org/metadata", 1024)


if __name__ == "__main__":
    unittest.main()
