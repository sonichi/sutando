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

    # 3. selected is a superset -> silent
    repo_env.write_text("GEMINI_API_KEY=x\nEXTRA=y\n")
    check("selected superset stays silent",
          hc.check_env_split(repo_env=repo_env, ws_env=ws_env), None)

    # 4. single file -> silent
    ws_env.unlink()
    check("single .env stays silent",
          hc.check_env_split(repo_env=repo_env, ws_env=ws_env), None)

if fails:
    print(f"FAIL ({len(fails)})")
    sys.exit(1)
print("PASS: env-split probe fixtures")
