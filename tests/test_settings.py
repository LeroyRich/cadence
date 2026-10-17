import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tagger.library import Library
from tagger.server import Handler


class CommitSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.library = Library(self.state_dir, resume_jobs=False)
        self.handler = object.__new__(Handler)
        self.handler.server = SimpleNamespace(app=SimpleNamespace(library=self.library))
        self.handler._session = lambda modify=False: None
        self.handler._send_json = lambda value, status=200: (status, value)
        self.handler.path = "/api/settings"

    def test_confirmation_defaults_on_and_persists_disabled_and_enabled(self):
        self.assertTrue(self.handler._handle("GET")[1]["confirm_commit"])
        for enabled in (False, True):
            self.handler._body = lambda: {"confirm_commit": enabled}
            self.assertEqual(self.handler._handle("POST")[0], 200)
            self.handler.server.app.library = Library(self.state_dir, resume_jobs=False)
            self.assertIs(self.handler._handle("GET")[1]["confirm_commit"], enabled)

    def test_legacy_settings_save_keeps_existing_confirmation_preference(self):
        self.library.set_setting("confirm_commit", "false")
        self.handler._body = lambda: {"contact_email": "test@example.com"}
        self.assertEqual(self.handler._handle("POST")[0], 200)
        self.assertFalse(self.handler._handle("GET")[1]["confirm_commit"])

    def test_invalid_values_cannot_disable_confirmation(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            self.handler._body = lambda: {"confirm_commit": value}
            self.assertEqual(self.handler._handle("POST")[0], 400)
            self.assertTrue(self.handler._handle("GET")[1]["confirm_commit"])
        self.library.set_setting("confirm_commit", "invalid")
        self.assertTrue(self.handler._handle("GET")[1]["confirm_commit"])

    def test_id3_upgrade_defaults_on_and_can_restore_per_edit_choice(self):
        self.assertTrue(self.handler._handle("GET")[1]["auto_id3_upgrade"])
        for enabled in (False, True):
            self.handler._body = lambda enabled=enabled: {"auto_id3_upgrade": enabled}
            self.assertEqual(self.handler._handle("POST")[0], 200)
            self.handler.server.app.library = Library(self.state_dir, resume_jobs=False)
            self.assertIs(self.handler._handle("GET")[1]["auto_id3_upgrade"], enabled)

    def test_invalid_id3_upgrade_setting_is_rejected(self):
        for value in ("false", 0, None, [], {}):
            self.handler._body = lambda value=value: {"auto_id3_upgrade": value}
            self.assertEqual(self.handler._handle("POST")[0], 400)
            self.assertTrue(self.handler._handle("GET")[1]["auto_id3_upgrade"])

    def test_daily_scan_is_opt_in_persistent_and_not_reset_by_older_clients(self):
        self.assertFalse(self.handler._handle("GET")[1]["daily_scan_enabled"])
        self.handler.server.app.scheduler = SimpleNamespace(notify=Mock())
        self.handler._body = lambda: {"daily_scan_enabled": True}
        self.assertEqual(self.handler._handle("POST")[0], 200)
        self.handler.server.app.scheduler.notify.assert_called_once()
        self.handler.server.app.library = Library(self.state_dir, resume_jobs=False)
        self.assertTrue(self.handler._handle("GET")[1]["daily_scan_enabled"])
        self.handler._body = lambda: {"contact_email": "test@example.com"}
        self.assertEqual(self.handler._handle("POST")[0], 200)
        self.assertTrue(self.handler._handle("GET")[1]["daily_scan_enabled"])
        self.handler._body = lambda: {"daily_scan_enabled": False}
        self.assertEqual(self.handler._handle("POST")[0], 200)
        self.assertFalse(self.handler._handle("GET")[1]["daily_scan_enabled"])

    def test_invalid_daily_scan_setting_is_rejected(self):
        for value in ("true", 0, 1, None, [], {}):
            self.handler._body = lambda value=value: {"daily_scan_enabled": value}
            self.assertEqual(self.handler._handle("POST")[0], 400)
            self.assertFalse(self.handler._handle("GET")[1]["daily_scan_enabled"])
