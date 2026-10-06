#!/usr/bin/env python3
"""Redeem one earned Codex reset when the weekly allowance is exhausted early."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


WEEKLY_MINUTES = 7 * 24 * 60
MIN_SECONDS_TO_RESET = 24 * 60 * 60
USED_THRESHOLD = 99.9
RPC_TIMEOUT_SECONDS = 15
LOCK_TIMEOUT_SECONDS = 5
MAX_RPC_LINE_BYTES = 1024 * 1024
ENABLE_ENV = "SUTANDO_CODEX_AUTO_RESET_ENABLED"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifest.json"
TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


class AutoResetError(Exception):
    pass


class NoCreditError(AutoResetError):
    """The account has no eligible earned reset to redeem."""


class UnsupportedAccountError(AutoResetError):
    """The authenticated Codex account does not have ChatGPT rate limits."""


class AccountIdentityUnavailable(AutoResetError):
    """The Codex CLI did not provide a safe ChatGPT account identity."""


def _number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AutoResetError(f"invalid {name}")
    return value


def _reject_nonfinite(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def enabled(environ: Mapping[str, str] | None = None,
            manifest_path: Path = MANIFEST_PATH) -> bool:
    env = os.environ if environ is None else environ
    raw = env.get(ENABLE_ENV)
    if raw is None:
        try:
            config = json.loads(manifest_path.read_text(encoding="utf-8")).get("config")
            raw = config.get(ENABLE_ENV) if isinstance(config, dict) else None
        except (OSError, ValueError, TypeError, AttributeError):
            return False
    if not isinstance(raw, str):
        return False
    normalized = raw.strip().lower()
    return normalized in TRUE_VALUES and normalized not in FALSE_VALUES


class AppServer:
    def __init__(self, codex_bin: str, codex_home: Path, workspace: Path):
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        self.process = subprocess.Popen(
            [codex_bin, "app-server"], cwd=workspace, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.buffer = bytearray()
        self.next_id = 1
        self.selector = selectors.DefaultSelector()
        assert self.process.stdout is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def __enter__(self) -> AppServer:
        try:
            self.call("initialize", {"clientInfo": {"name": "sutando-codex-auto-reset",
                                                    "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.notify("initialized")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                pipe.close()

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AutoResetError("Codex App Server pipe closed") from exc

    def _line(self, deadline: float) -> dict[str, Any]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                if len(raw) > MAX_RPC_LINE_BYTES:
                    raise AutoResetError("Codex App Server response too large")
                try:
                    return _json_object(json.loads(raw, parse_constant=_reject_nonfinite), "App Server response")
                except (UnicodeError, ValueError) as exc:
                    raise AutoResetError("invalid Codex App Server JSON") from exc
            if len(self.buffer) > MAX_RPC_LINE_BYTES:
                raise AutoResetError("Codex App Server response too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AutoResetError("Codex App Server request timed out")
            if not self.selector.select(remaining):
                raise AutoResetError("Codex App Server request timed out")
            assert self.process.stdout is not None
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise AutoResetError("Codex App Server closed before responding")
            self.buffer.extend(chunk)

    def notify(self, method: str) -> None:
        self._send({"method": method})

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
        for _ in range(100):
            response = self._line(deadline)
            if response.get("id") != request_id:
                if "id" in response:
                    raise AutoResetError("unexpected Codex App Server request ID")
                continue
            if "error" in response:
                raise AutoResetError(f"Codex App Server rejected {method}")
            return _json_object(response.get("result"), f"{method} result")
        raise AutoResetError("too many Codex App Server notifications")


def _account_id(account_result: dict[str, Any], limits: dict[str, Any]) -> str:
    account = _json_object(account_result.get("account"), "Codex account")
    if account.get("type") != "chatgpt":
        raise UnsupportedAccountError("Codex account is not ChatGPT")
    route = account_result.get("workspaceRouting")
    routed_id = route.get("chatgptAccountId") if isinstance(route, dict) else None
    usage_id = limits.get("accountId")
    if routed_id is not None and (not isinstance(routed_id, str) or not routed_id):
        raise AutoResetError("invalid routed account ID")
    if usage_id is not None and (not isinstance(usage_id, str) or not usage_id):
        raise AutoResetError("invalid usage account ID")
    if not routed_id:
        raise AccountIdentityUnavailable("routed Codex account identity unavailable")
    if usage_id and routed_id != usage_id:
        raise AutoResetError("Codex account identity changed")
    return routed_id


def _weekly_window(limits: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    buckets = limits.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and "codex" in buckets:
        bucket = _json_object(buckets["codex"], "Codex rate limit")
    else:
        bucket = _json_object(limits.get("rateLimits"), "Codex rate limit")
        if bucket.get("limitId") != "codex":
            raise AutoResetError("Codex rate limit unavailable")
    if bucket.get("limitId") not in (None, "codex"):
        raise AutoResetError("Codex rate limit ID mismatch")
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        used = window.get("usedPercent")
        resets_at = window.get("resetsAt")
        if (not _integer(duration) or duration != WEEKLY_MINUTES
                or not _number(used) or not 0 <= used <= 100
                or not _integer(resets_at) or resets_at <= 0):
            continue
        candidates.append((duration, float(used), window))
    if not candidates:
        raise AutoResetError("valid weekly Codex window unavailable")
    selected = max(candidates, key=lambda row: (row[0], row[1]))[2]
    return "codex", selected


def _credit_id(limits: dict[str, Any], now: float) -> str | None:
    summary = _json_object(limits.get("rateLimitResetCredits"), "reset credits")
    count = summary.get("availableCount")
    if not _integer(count) or count < 0:
        raise AutoResetError("invalid reset credit count")
    if count == 0:
        raise NoCreditError("no reset credit available")
    rows = summary.get("credits")
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise AutoResetError("invalid reset credit details")
    eligible: list[tuple[float, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AutoResetError("invalid reset credit detail")
        expiry = row.get("expiresAt")
        if (row.get("status") != "available" or row.get("resetType") != "codexRateLimits"
                or not isinstance(row.get("id"), str) or not row["id"]
                or (expiry is not None and (not _integer(expiry) or expiry <= now))):
            continue
        eligible.append((float(expiry) if expiry is not None else float("inf"), row["id"]))
    if not eligible:
        raise NoCreditError("no eligible Codex reset credit detail")
    return min(eligible)[1]


def _window_key(limit_id: str, window: dict[str, Any]) -> str:
    return f"{limit_id}:{window['windowDurationMins']}:{window['resetsAt']}"


def _state_path(workspace: Path, account_id: str) -> Path:
    digest = hashlib.sha256(account_id.encode()).hexdigest()
    return workspace / "state" / "codex-auto-reset" / f"{digest}.json"


@contextlib.contextmanager
def _locked_state(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AutoResetError("another Codex reset check holds the lock")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def _read_state(path: Path, account_id: str) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "accountId": account_id, "pending": None,
                "blockedWindow": None, "armed": False}
    except (OSError, ValueError) as exc:
        raise AutoResetError("Codex reset state unreadable") from exc
    if (not isinstance(state, dict) or not _integer(state.get("version"))
            or state["version"] != 1
            or state.get("accountId") != account_id
            or not {"pending", "blockedWindow", "armed"}.issubset(state)
            or state.get("pending") is not None and not isinstance(state.get("pending"), dict)
            or state.get("blockedWindow") is not None and not isinstance(state.get("blockedWindow"), str)
            or not isinstance(state.get("armed"), bool)):
        raise AutoResetError("Codex reset state invalid")
    pending = state["pending"]
    if pending is not None:
        key = pending.get("key")
        if not isinstance(key, str):
            raise AutoResetError("Codex reset attempt key invalid")
        try:
            uuid.UUID(key)
        except ValueError as exc:
            raise AutoResetError("Codex reset attempt key invalid") from exc
        if (not isinstance(pending.get("window"), str) or not pending["window"]
                or pending.get("creditId") is not None
                and (not isinstance(pending.get("creditId"), str) or not pending["creditId"])):
            raise AutoResetError("Codex reset attempt invalid")
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".codex-reset-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(state, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise AutoResetError("Codex reset state could not be saved") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _limits(server: AppServer) -> dict[str, Any]:
    return server.call("account/rateLimits/read", {"excludeResetCreditDetails": False})


def tick(server: AppServer, workspace: Path, now: float | None = None) -> dict[str, Any]:
    if not enabled():
        return {"status": "disabled"}
    account = server.call("account/read", {"refreshToken": False})
    first = _limits(server)
    try:
        account_id = _account_id(account, first)
    except UnsupportedAccountError:
        return {"status": "unsupported-account"}
    except AccountIdentityUnavailable:
        return {"status": "unsupported-codex-cli"}
    path = _state_path(workspace, account_id)
    with _locked_state(path):
        account = server.call("account/read", {"refreshToken": False})
        limits = _limits(server)
        if _account_id(account, limits) != account_id:
            raise AutoResetError("Codex account identity changed")
        observed_at = time.time() if now is None else now
        limit_id, window = _weekly_window(limits)
        window_key = _window_key(limit_id, window)
        used = float(window["usedPercent"])
        state = _read_state(path, account_id)
        pending = state["pending"]
        if pending is not None and used < USED_THRESHOLD:
            state["pending"] = None
            state["blockedWindow"] = window_key
            state["armed"] = False
            _write_state(path, state)
            return {"status": "reconciled", "usedPercent": used}
        if pending is not None and pending["window"] != window_key:
            return {"status": "awaiting-lower-sample", "usedPercent": used}
        if state["blockedWindow"] is not None and not state["armed"]:
            if used < USED_THRESHOLD:
                state["blockedWindow"] = window_key
                state["armed"] = True
                _write_state(path, state)
                return {"status": "rearmed", "usedPercent": used}
            return {"status": "awaiting-lower-sample", "usedPercent": used}
        if used < USED_THRESHOLD:
            return {"status": "above-reserve", "usedPercent": used}
        seconds_left = int(window["resetsAt"] - observed_at)
        if seconds_left < MIN_SECONDS_TO_RESET:
            return {"status": "weekly-reset-soon", "secondsToReset": seconds_left}
        created = False
        if pending is None:
            try:
                credit_id = _credit_id(limits, observed_at)
            except NoCreditError:
                return {"status": "no-credit"}
            pending = {"key": str(uuid.uuid4()), "window": window_key,
                       "creditId": credit_id}
            state["pending"] = pending
            _write_state(path, state)
            created = True
        active_account = server.call("account/read", {"refreshToken": False})
        if _account_id(active_account, limits) != account_id:
            raise AutoResetError("Codex account identity changed")
        seconds_left = int(window["resetsAt"] - (time.time() if now is None else now))
        if seconds_left < MIN_SECONDS_TO_RESET:
            if created:
                state["pending"] = None
                _write_state(path, state)
            return {"status": "weekly-reset-soon", "secondsToReset": seconds_left}
        params: dict[str, Any] = {"idempotencyKey": pending["key"]}
        if pending["creditId"] is not None:
            params["creditId"] = pending["creditId"]
        result = server.call("account/rateLimitResetCredit/consume", params)
        outcome = result.get("outcome")
        if outcome in ("reset", "alreadyRedeemed", "nothingToReset"):
            state["pending"] = None
            state["blockedWindow"] = window_key
            state["armed"] = False
            _write_state(path, state)
            try:
                post = _limits(server)
                if _account_id(account, post) != account_id:
                    raise AutoResetError("Codex account identity changed")
                _, post_window = _weekly_window(post)
                post_used = float(post_window["usedPercent"])
            except AutoResetError:
                post_used = None
            return {"status": outcome, "usedPercent": used,
                    "postResetUsedPercent": post_used}
        if outcome == "noCredit":
            state["pending"] = None
            _write_state(path, state)
            return {"status": "no-credit"}
        raise AutoResetError("unknown Codex reset outcome")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if not enabled():
            print(json.dumps({"status": "disabled"}) if args.json else "disabled")
            return 0
        workspace = args.workspace.expanduser().resolve(strict=True)
        codex_home = args.codex_home.expanduser().resolve(strict=True)
        if not workspace.is_dir() or not codex_home.is_dir():
            raise AutoResetError("workspace and Codex home must be directories")
        with AppServer(args.codex_bin, codex_home, workspace) as server:
            result = tick(server, workspace)
    except (AutoResetError, OSError, subprocess.SubprocessError) as exc:
        result = {"status": "error", "reason": str(exc)}
    print(json.dumps(result, sort_keys=True) if args.json else result["status"])
    return 2 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
