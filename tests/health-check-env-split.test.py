#!/usr/bin/env python3
"""env-split probe: the selected .env missing keys the other carries must WARN
by key NAME (never value); superset/single-file layouts stay silent."""
import importlib.util
import sys
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "hc", Path(__file__).resolve().parent.parent / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r} want {want!r}")
        print(f"  FAIL: {name}: got {got!r} want {want!r}")
    else:
        print(f"  OK: {name}")


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    repo_env = td / "repo.env"
    ws_env = td / "ws.env"

    # 1. stub selected + full other -> warn naming the missing keys, not values
    repo_env.write_text("GEMINI_API_KEY=stub-value\n")
    ws_env.write_text("GEMINI_API_KEY=real\nDISCORD_BOT_TOKEN=sekret-value\n"
                      "export SLACK_BOT_TOKEN=also-sekret\n")
    r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
    check("stub repo env warns", r is not None and r["status"], "warn")
    check("missing key names listed",
          r is not None and "DISCORD_BOT_TOKEN" in r["detail"]
          and "SLACK_BOT_TOKEN" in r["detail"], True)
    check("values never leak into the message",
          r is not None and ("sekret-value" in r["detail"]
                             or "also-sekret" in r["detail"]), False)

    # 2. identical key sets -> silent
    ws_env.write_text("GEMINI_API_KEY=other-value\n")
    check("identical key sets stay silent",
          hc.check_env_split(repo_env=repo_env, ws_env=ws_env), None)

    # 2b. every whitespace shape of `export` must yield the key (PR block:
    # multi-space/tab separators silently dropped keys in the OTHER file)
    repo_env.write_text("GEMINI_API_KEY=stub\n")
    for label, content in [
        ("multi-space", "export   DISCORD_BOT_TOKEN=v\n"),
        ("space-tab", "export \t DISCORD_BOT_TOKEN=v\n"),
        ("tab-separator", "export\tDISCORD_BOT_TOKEN=v\n"),
        ("bare-export-line", "export\nDISCORD_BOT_TOKEN=v\n"),
    ]:
        ws_env.write_text(content)
        r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
        check(f"export {label} still warns",
              r is not None and "DISCORD_BOT_TOKEN" in r["detail"], True)
    ws_env.write_text("#DISCORD_BOT_TOKEN=v\n")
    check("commented key stays silent (negative control)",
          hc.check_env_split(repo_env=repo_env, ws_env=ws_env), None)
    repo_env.write_text("GEMINI_API_KEY=x\n")
    ws_env.write_text("GEMINI_API_KEY=other-value\n")

    # 3. selected is a superset -> silent
    repo_env.write_text("GEMINI_API_KEY=x\nEXTRA=y\n")
    check("selected superset stays silent",
          hc.check_env_split(repo_env=repo_env, ws_env=ws_env), None)

    # 4. single file -> silent
    ws_env.unlink()
    check("single .env stays silent",
          hc.check_env_split(repo_env=repo_env, ws_env=ws_env), None)

# 5. unreadable selected file -> reads as empty, so the other's keys all warn
# (chmod 000 does not block root, where read_text would succeed; skip there)
import os

if os.geteuid() != 0:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        repo_env = td / "repo.env"
        ws_env = td / "ws.env"
        repo_env.write_text("GEMINI_API_KEY=x\n")
        ws_env.write_text("DISCORD_BOT_TOKEN=v\n")
        repo_env.chmod(0)
        try:
            r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
            check("unreadable selected env warns", r is not None and r["status"], "warn")
            check("unreadable selected env lists the other's keys",
                  r is not None and "DISCORD_BOT_TOKEN" in r["detail"], True)
        finally:
            repo_env.chmod(0o600)

# 6. run_all_checks wiring: the call site is separate code from the probe
# (same pattern as health-check-bridge-log-content's integration section).
from unittest.mock import patch

_sentinel = {"name": "env-split", "status": "warn", "detail": "wiring-sentinel"}
with patch.object(hc, "check_env_split", return_value=_sentinel):
    _rows = [c for c in hc.run_all_checks() if c.get("detail") == "wiring-sentinel"]
check("run_all_checks carries the env-split row", len(_rows), 1)

if fails:
    print(f"FAIL ({len(fails)})")
    sys.exit(1)
print("PASS: env-split probe fixtures")
