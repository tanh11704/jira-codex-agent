"""Small macOS integrations that let the daemon run while nobody is at the machine."""
from __future__ import annotations

import json
import logging
import os
import subprocess
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def __call__(self, title: str, message: str) -> None:
        if not self.enabled:
            return
        # json.dumps yields a valid AppleScript string literal for quotes/backslashes.
        script = f"display notification {json.dumps(message[:240])} with title {json.dumps(title)}"
        try:
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            log.warning("could not post notification")


class SleepGuard:
    """Hold a `caffeinate -i` idle-sleep assertion while work is in progress.

    Re-entrant: the assertion is released when the outermost hold ends. It is tied to the
    daemon PID, so it also disappears if the daemon crashes.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._depth = 0
        self._process: subprocess.Popen | None = None

    @contextmanager
    def hold(self) -> Iterator[None]:
        self._depth += 1
        if self._depth == 1 and self.enabled:
            try:
                self._process = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                log.warning("caffeinate unavailable; the Mac may sleep during work")
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and self._process:
                self._process.terminate()
                self._process.wait()
                self._process = None
