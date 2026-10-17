"""Opt-in daily library scans at 03:00 in the NAS local time zone."""

import logging
import threading
import time
from datetime import datetime, time as clock_time, timedelta


def next_scan_time(now):
    day = datetime.fromtimestamp(now).date()
    candidate = datetime.combine(day, clock_time(3)).timestamp()
    if candidate < now:
        candidate = datetime.combine(day + timedelta(days=1), clock_time(3)).timestamp()
    return candidate


class DailyScanScheduler:
    def __init__(self, library):
        self.library = library
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._enabled = None
        self._next_due = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="cadence-daily-scan", daemon=True)
            self._thread.start()

    def notify(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def check(self, now):
        enabled = self.library.get_setting("daily_scan_enabled", "false") == "true"
        if enabled != self._enabled:
            self._enabled = enabled
            self._next_due = next_scan_time(now) if enabled else None
        if enabled and now >= self._next_due:
            day = datetime.fromtimestamp(self._next_due).date().isoformat()
            job_id = self.library.start_scheduled_scan(day)
            self._next_due = next_scan_time(now + 1)
            return job_id
        return None

    def _run(self):
        while not self._stop.is_set():
            try:
                self.check(time.time())
                delay = 60 if self._next_due is None else max(0.1, min(60, self._next_due - time.time()))
            except Exception:
                logging.getLogger("cadence").exception("Scheduled library scan check failed")
                delay = 60
            self._wake.wait(delay)
            self._wake.clear()
