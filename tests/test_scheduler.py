import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tagger.library import Library
from tagger.scheduler import DailyScanScheduler, next_scan_time


class DailyScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "music"
        self.root.mkdir()
        self.library = Library(self.base / "state", allowed_volume=self.base, resume_jobs=False)

    def test_next_scan_uses_local_three_am_and_next_calendar_day(self):
        day = datetime(2026, 1, 15)
        target = day.replace(hour=3).timestamp()
        self.assertEqual(next_scan_time(day.replace(hour=2, minute=59).timestamp()), target)
        self.assertEqual(next_scan_time(target), target)
        self.assertEqual(next_scan_time(target + 1), (day + timedelta(days=1)).replace(hour=3).timestamp())

    def test_scheduler_runs_once_at_due_time_and_does_not_catch_up_after_restart(self):
        self.library.set_setting("daily_scan_enabled", "true")
        day = datetime(2026, 1, 15)
        before = day.replace(hour=2, minute=59).timestamp()
        due = day.replace(hour=3).timestamp()
        following = (day + timedelta(days=1)).replace(hour=3).timestamp()
        scheduler = DailyScanScheduler(self.library)
        with patch.object(self.library, "start_scheduled_scan", return_value="job") as claim:
            self.assertIsNone(scheduler.check(before))
            self.assertEqual(scheduler.check(due), "job")
            self.assertIsNone(scheduler.check(due + 3600))
            self.assertEqual(scheduler.check(following), "job")
            self.assertEqual([call.args[0] for call in claim.call_args_list], ["2026-01-15", "2026-01-16"])
            restarted = DailyScanScheduler(self.library)
            self.assertIsNone(restarted.check(due + 3600))
            claim.assert_called_with("2026-01-16")

    def test_enabling_after_three_am_waits_until_tomorrow(self):
        day = datetime(2026, 1, 15)
        scheduler = DailyScanScheduler(self.library)
        self.assertIsNone(scheduler.check(day.replace(hour=2).timestamp()))
        self.library.set_setting("daily_scan_enabled", "true")
        with patch.object(self.library, "start_scheduled_scan", return_value="job") as claim:
            self.assertIsNone(scheduler.check(day.replace(hour=4).timestamp()))
            self.assertEqual(scheduler.check((day + timedelta(days=1)).replace(hour=3).timestamp()), "job")
            claim.assert_called_once_with("2026-01-16")

    def test_disabling_before_due_cancels_the_future_run(self):
        day = datetime(2026, 1, 15)
        self.library.set_setting("daily_scan_enabled", "true")
        scheduler = DailyScanScheduler(self.library)
        self.assertIsNone(scheduler.check(day.replace(hour=2).timestamp()))
        self.library.set_setting("daily_scan_enabled", "false")
        with patch.object(self.library, "start_scheduled_scan") as claim:
            self.assertIsNone(scheduler.check(day.replace(hour=2, minute=30).timestamp()))
            self.assertIsNone(scheduler.check(day.replace(hour=3).timestamp()))
            claim.assert_not_called()

    def test_claim_is_persistent_and_reuses_an_active_manual_scan(self):
        self.library.set_setting("daily_scan_enabled", "true")
        with patch.object(self.library, "_launch_next_job"):
            self.assertIsNone(self.library.start_scheduled_scan("2026-01-15"))
            self.library.add_root(str(self.root))
            manual = self.library.start_scan()
            self.assertEqual(self.library.start_scheduled_scan("2026-01-16"), manual)
            self.assertIsNone(self.library.start_scheduled_scan("2026-01-16"))
            with self.library._db() as db:
                jobs = db.execute("SELECT kind FROM jobs").fetchall()
            self.assertEqual([row[0] for row in jobs], ["scan"])
        self.assertEqual(self.library.get_setting("daily_scan_last_date"), "2026-01-16")

    def test_scheduled_job_survives_restart_and_runs_through_scan_pipeline(self):
        self.library.add_root(str(self.root))
        self.library.set_setting("daily_scan_enabled", "true")
        with patch.object(self.library, "_launch_next_job"):
            job = self.library.start_scheduled_scan("2026-01-15")
        self.assertEqual(self.library.job(job)["kind"], "scheduled_scan")
        restarted = Library(self.base / "state", allowed_volume=self.base)
        deadline = time.monotonic() + 5
        while restarted.job(job)["state"] not in ("complete", "partial", "failed") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(restarted.job(job)["state"], "complete", restarted.job(job)["errors"])
        self.assertIsNone(restarted.start_scheduled_scan("2026-01-15"))
        with patch.object(restarted, "_launch_next_job"):
            following = restarted.start_scheduled_scan("2026-01-16")
        self.assertNotEqual(following, job)
        self.assertEqual(restarted.job(following)["kind"], "scheduled_scan")


if __name__ == "__main__":
    unittest.main()
