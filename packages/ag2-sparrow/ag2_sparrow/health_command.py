"""The core's health row from a command an adapter injects (REMOTE_HEALTH_COMMAND).

Sparrow runs the command with no shell and a timeout, at most once per `ttl` seconds
and off its poll loop, and reads one JSON object (at most MAX_OUTPUT bytes) from its
stdout: the heartbeat's `health` row. The broker
refuses a whole heartbeat whose row is malformed, so a row that does not match its
contract (alive: bool|null; motion: idle|moving|unknown; condition:
healthy|abnormal|unknown; reason: a [a-z0-9-]{1,40} slug or null; since: epoch
seconds or null) is dropped and logged, never sent. Empty stdout means no row.
This module owns that contract for every row the bridge sends; it names no adapter."""
from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

FIELDS = ("alive", "motion", "condition", "reason", "since")
MOTIONS = ("idle", "moving", "unknown")
CONDITIONS = ("healthy", "abnormal", "unknown")
REASON_MAX = 40
REASON_RE = re.compile(f"[a-z0-9-]{{1,{REASON_MAX}}}")
TIMEOUT_S = 10.0
MAX_OUTPUT = 4096


def slug(value) -> "str | None":
    """`value` as a reason REASON_RE accepts, or None when nothing is left."""
    if value is None:
        return None
    text = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    return text[:REASON_MAX].strip("-") or None


def _epoch(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def validate(value) -> "tuple[dict | None, str | None]":
    """(row, None) for a row the broker accepts, else (None, why)."""
    if not isinstance(value, dict):
        return None, "not a JSON object"
    row = {k: value.get(k) for k in FIELDS}
    if row["alive"] is not None and not isinstance(row["alive"], bool):
        return None, "alive must be true, false or null"
    if row["motion"] not in MOTIONS:
        return None, f"motion must be one of {', '.join(MOTIONS)}"
    if row["condition"] not in CONDITIONS:
        return None, f"condition must be one of {', '.join(CONDITIONS)}"
    if row["reason"] is not None and not (isinstance(row["reason"], str)
                                          and REASON_RE.fullmatch(row["reason"])):
        return None, "reason must be a [a-z0-9-]{1,40} slug or null"
    if row["since"] is not None and not _epoch(row["since"]):
        return None, "since must be epoch seconds or null"
    return row, None


def run(command: str, timeout: float = TIMEOUT_S,
        env: "dict[str, str] | None" = None) -> "tuple[dict | None, str | None]":
    """(row, None), (None, None) when the command printed nothing, or (None, why)."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            r = subprocess.run(shlex.split(command), stdin=subprocess.DEVNULL, env=env,
                               stdout=out, stderr=err, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"timed out after {timeout:g}s"
        except (OSError, ValueError) as e:
            return None, f"{type(e).__name__}: {e}"
        out.seek(0)
        err.seek(max(0, err.seek(0, 2) - MAX_OUTPUT))
        data, tail = out.read(MAX_OUTPUT + 1), err.read()
    if r.returncode != 0:
        lines = tail.decode("utf-8", "replace").strip().splitlines()
        return None, f"exited {r.returncode}" + (f": {lines[-1]}" if lines else "")
    if len(data) > MAX_OUTPUT:
        return None, f"stdout is over {MAX_OUTPUT} bytes"
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return None, None
    try:
        value = json.loads(text)
    except ValueError:
        return None, "stdout is not JSON"
    return validate(value)


class HealthCommand:
    """The last row `run(command)` gave, refreshed off the caller's thread at most once
    per `ttl` seconds; logs each new error once. `env`, when given, builds the
    command's whole environment for each run."""

    def __init__(self, command: str, ttl: float, log: Callable[[str], None],
                 env: "Callable[[], dict[str, str]] | None" = None):
        self.command, self.ttl, self.log, self.env = command.strip(), ttl, log, env
        self._lock = threading.Lock()
        self._thread: "threading.Thread | None" = None
        self._at: "float | None" = None
        self._row: "dict | None" = None
        self._error: "str | None" = None

    def __bool__(self) -> bool:
        return bool(self.command)

    def row(self) -> "dict | None":
        """The cached row at once; a due refresh starts in a daemon thread unless one
        is still running. None until the first refresh has finished."""
        if not self.command:
            return None
        with self._lock:
            due = self._at is None or time.monotonic() - self._at >= self.ttl
            if due and not (self._thread is not None and self._thread.is_alive()):
                self._thread = threading.Thread(target=self.refresh, name="health-command",
                                                daemon=True)
                self._thread.start()
            return self._row

    def refresh(self) -> "dict | None":
        """Run the command now and cache what it gave; any error clears the row."""
        try:
            row, error = run(self.command, env=self.env() if self.env else None)
        except Exception as e:  # noqa: BLE001 — a dead refresh would pin the old row
            row, error = None, f"{type(e).__name__}: {e}"
        with self._lock:
            if error and error != self._error:
                self.log(f"health command gave no row: {error}")
            self._at, self._row, self._error = time.monotonic(), row, error
        return row
