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
    (td / "repo").mkdir()
    (td / "ws").mkdir()
    repo_env = td / "repo" / ".env"
    ws_env = td / "ws" / ".env"

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

    # 2b. every whitespace shape of `export` must yield the key — multi-space/tab
    # separators must not silently drop keys in the OTHER file
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

# 5. an unreadable selected file is UNKNOWN, never absent
# (chmod 000 does not block root, where read_text would succeed; skip there)
import os

if os.geteuid() != 0:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "repo").mkdir()
        (td / "ws").mkdir()
        repo_env = td / "repo" / ".env"
        ws_env = td / "ws" / ".env"
        repo_env.write_text("GEMINI_API_KEY=x\n")
        ws_env.write_text("DISCORD_BOT_TOKEN=v\n")
        repo_env.chmod(0)
        try:
            r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
            check("unreadable selected env warns", r is not None and r["status"], "warn")
            # Read failure is UNKNOWN: the probe must not guess a missing-key
            # list from the readable side — it reports comparison incomplete.
            check("unreadable selected env reports incomplete, not a guessed list",
                  r is not None and "could not be completed" in r["detail"]
                  and "DISCORD_BOT_TOKEN" not in r["detail"], True)
        finally:
            repo_env.chmod(0o600)

# 5b. selection is DELEGATED: when the canonical resolver picks the workspace
# file, the probe must follow it, not re-derive repo-first by hand
import sutando_config
from unittest.mock import patch as _patch

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir()
    (td / "ws").mkdir()
    repo_env = td / "repo" / ".env"
    ws_env = td / "ws" / ".env"
    repo_env.write_text("GEMINI_API_KEY=x\nEXTRA_KEY=y\n")
    ws_env.write_text("GEMINI_API_KEY=x\n")
    with _patch.object(sutando_config, "resolve_dotenv", return_value=ws_env):
        r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
    check("probe follows the resolver's pick",
          r is not None and "workspace .env" in r["detail"]
          and "EXTRA_KEY" in r["detail"], True)

# 5c. a third-tier resolver pick must WARN, not silence
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir()
    (td / "ws").mkdir()
    repo_env = td / "repo" / ".env"
    ws_env = td / "ws" / ".env"
    repo_env.write_text("A=1\n")
    ws_env.write_text("A=1\n")
    third = td / "bundle.env"
    with _patch.object(sutando_config, "resolve_dotenv", return_value=third):
        r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
    check("third-tier pick warns instead of silencing",
          r is not None and r["status"] == "warn"
          and "outside both compared candidates" in r["detail"], True)

# 5d. one legacy candidate + a third-tier pick must still WARN — the
# two-candidate gate must never run before selection.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir()
    (td / "ws").mkdir()
    repo_env = td / "repo" / ".env"
    ws_env = td / "ws" / ".env"          # absent on disk
    repo_env.write_text("LEGACY_ONLY=1\n")
    third = td / "bundle.env"
    third.write_text("SELECTED_ONLY=1\n")
    with _patch.object(sutando_config, "resolve_dotenv", return_value=third):
        r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
    check("one-candidate + third-tier pick still warns",
          r is not None and r["status"] == "warn"
          and "outside both compared candidates" in r["detail"], True)

# 5e. controls: a lone SELECTED candidate stays silent; zero candidates too.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir()
    (td / "ws").mkdir()
    repo_env = td / "repo" / ".env"
    ws_env = td / "ws" / ".env"
    repo_env.write_text("A=1\n")
    with _patch.object(sutando_config, "resolve_dotenv", return_value=repo_env):
        r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
    check("single selected candidate is silent", r, None)
    with _patch.object(sutando_config, "resolve_dotenv",
                       return_value=td / "bundle.env"):
        r = hc.check_env_split(repo_env=td / "no" / ".env",
                               ws_env=td / "no2" / ".env")
    check("zero candidates stay silent", r, None)

# 7. read-failure is UNKNOWN, not a clean "no missing" — WARN on BOTH
# orientations. Chmod 000 is skipped under root (reads regardless of mode).
import os as _os
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir(); (td / "ws").mkdir()
    repo_env = td / "repo" / ".env"
    ws_env = td / "ws" / ".env"
    repo_env.write_text("GEMINI_API_KEY=real\n")
    ws_env.write_text("GEMINI_API_KEY=real\nDISCORD_BOT_TOKEN=old\n")
    if _os.geteuid() != 0:
        # unreadable unselected input stays UNKNOWN, never absent
        ws_env.chmod(0o000)
        try:
            with _patch.object(sutando_config, "resolve_dotenv",
                               return_value=repo_env):
                r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
            check("unreadable unselected WARNs (not silent)",
                  r is not None and r["status"] == "warn"
                  and "could not be completed" in r["detail"], True)
        finally:
            ws_env.chmod(0o600)
        # unreadable SELECTED (opposite orientation)
        repo_env.chmod(0o000)
        try:
            with _patch.object(sutando_config, "resolve_dotenv",
                               return_value=repo_env):
                r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
            check("unreadable selected WARNs (not silent)",
                  r is not None and r["status"] == "warn"
                  and "could not be completed" in r["detail"], True)
        finally:
            repo_env.chmod(0o600)

# 8. a deprecated key present only in OTHER is reported as delete-not-merge,
# never advised as a mergeable missing key. Controls: deprecated-alone + a mix.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir(); (td / "ws").mkdir()
    repo_env = td / "repo" / ".env"
    ws_env = td / "ws" / ".env"
    for dep in sorted(sutando_config.DEPRECATED_ENV_KEYS):
        # deprecated-only diff: must not read as a mergeable missing key
        repo_env.write_text("GEMINI_API_KEY=real\n")
        ws_env.write_text(f"GEMINI_API_KEY=real\n{dep}=/some/path\n")
        with _patch.object(sutando_config, "resolve_dotenv", return_value=repo_env):
            r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
        check("deprecated-only diff never advises a merge",
              r is not None and "missing" not in r["detail"]
              and "do NOT merge" in r["detail"], True)
        check("deprecated key carries its OWN remedy, not blanket delete advice",
              r is not None
              and sutando_config.DEPRECATED_ENV_KEY_REMEDIES[dep] in r["detail"], True)
        check("deprecated key is named as delete-not-merge",
              r is not None and dep in r["detail"], True)
        # mix: a real missing key AND a deprecated one -> both, correctly separated
        ws_env.write_text(f"GEMINI_API_KEY=real\nDISCORD_BOT_TOKEN=old\n{dep}=/p\n")
        with _patch.object(sutando_config, "resolve_dotenv", return_value=repo_env):
            r = hc.check_env_split(repo_env=repo_env, ws_env=ws_env)
        check("mix: real key advised as merge, deprecated as delete",
          r is not None and "DISCORD_BOT_TOKEN" in r["detail"]
          and "missing 1 key" in r["detail"]
          and "do NOT merge" in r["detail"], True)
    check("a value-moving key is never advised as a bare delete",
          "vault.remote_url" in
          sutando_config.DEPRECATED_ENV_KEY_REMEDIES["SUTANDO_VAULT"], True)

# 6. run_all_checks wiring: the call site is separate code from the probe
# (same pattern as health-check-bridge-log-content's integration section).
from unittest.mock import patch

_sentinel = {"name": "env-split", "status": "warn", "detail": "wiring-sentinel"}
with patch.object(hc, "check_env_split", return_value=_sentinel):
    _rows = [c for c in hc.run_all_checks() if c.get("detail") == "wiring-sentinel"]
check("run_all_checks carries the env-split row", len(_rows), 1)

# 7. Multi-assignment lines. `set -a; . f` exports EVERY assignment word, so a
# stranded key on a shared line must not read as absent.
for _label, _line, _want_named in [
    ("one per line", "GEMINI_API_KEY=real\nDISCORD_BOT_TOKEN=old\n", True),
    ("two on one line", "GEMINI_API_KEY=real DISCORD_BOT_TOKEN=old\n", True),
    ("export, two words", "export GEMINI_API_KEY=real DISCORD_BOT_TOKEN=old\n", True),
    ("quoted value is ONE assignment", 'GEMINI_API_KEY="real DISCORD_BOT_TOKEN=old"\n', False),
]:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "repo").mkdir(); (td / "ws").mkdir()
        _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
        _re_.write_text("GEMINI_API_KEY=real\n")
        _we_.write_text(_line)
        with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
            _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
        _named = _r is not None and "DISCORD_BOT_TOKEN" in _r["detail"]
        check(f"stranded key on a shared line is seen: {_label}", _named, _want_named)

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir(); (td / "ws").mkdir()
    _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
    _re_.write_text("GEMINI_API_KEY=real\n")
    _we_.write_text('DISCORD_BOT_TOKEN="unbalanced\n')
    with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
        _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
    check("an unparseable line WARNs as incomplete, never as absent",
          _r is not None and _r["status"] == "warn"
          and "could not be completed" in _r["detail"], True)

# 8. Loader-grammar shapes. Each expectation is what BASH itself does after
# `set -a; . file`, not what a convenient parse would give.
for _label, _line, _persists in [
    ("plain two assignments", "GEMINI_API_KEY=real DISCORD_BOT_TOKEN=old", True),
    ("literal hash inside a word", "GEMINI_API_KEY=a#b DISCORD_BOT_TOKEN=old", True),
    ("real trailing comment", "GEMINI_API_KEY=real # DISCORD_BOT_TOKEN=old", False),
    ("bare export argument", "export GEMINI_API_KEY DISCORD_BOT_TOKEN=old", True),
    ("command prefix does not persist", "GEMINI_API_KEY=r DISCORD_BOT_TOKEN=e true", False),
    ("assignment to the export command", "GEMINI_API_KEY=r export DISCORD_BOT_TOKEN=o", True),
    # List operators. `;` and `&&` both reach the second assignment, so the
    # stranded key must still be named; a QUOTED operator is one value.
    ("semicolon list", "GEMINI_API_KEY=other;DISCORD_BOT_TOKEN=old", True),
    ("and-list", "GEMINI_API_KEY=other&&DISCORD_BOT_TOKEN=old", True),
    ("quoted operator is a value, not a split",
     "GEMINI_API_KEY='other;DISCORD_BOT_TOKEN=old'", False),
    ("export segment then command prefix",
     "export GEMINI_API_KEY=other ; DISCORD_BOT_TOKEN=ephemeral true", False),
]:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "repo").mkdir(); (td / "ws").mkdir()
        _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
        _re_.write_text("GEMINI_API_KEY=real\n")
        _we_.write_text(_line + "\n")
        with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
            _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
    _named = _r is not None and "DISCORD_BOT_TOKEN" in _r["detail"]
    check(f"loader grammar: {_label}", _named, _persists)

# Forms bash persists (measured under `set -e; set -a; source`) must be named,
# and forms whose effect is NOT proven must be UNKNOWN — never "absent".
for _label, _line, _want in [
    ("successful command then &&: exit status unmodelled -> UNKNOWN",
     "true && DISCORD_BOT_TOKEN=old", "unmodelled-command"),
    ("pure assignment then && (control): named", "GEMINI_API_KEY=o && DISCORD_BOT_TOKEN=old", "named"),
    ("append-assignment persists", "DISCORD_BOT_TOKEN+=old", "named"),
    ("assignment with a redirection persists", "DISCORD_BOT_TOKEN=old >/dev/null", "named"),
    ("fd redirection before the assignment persists", "2>/dev/null DISCORD_BOT_TOKEN=old", "named"),
    ("export with a redirection persists", "export DISCORD_BOT_TOKEN=old >/dev/null", "named"),
    ("invalid identifier aborts the load -> UNKNOWN, never a merge target", "1BAD=old", "invalid-identifier"),
    ("leading-digit-free identifier (control): named", "DISCORD_BOT_TOKEN1=old", "named1"),
    ("unmodelled redirection operator -> UNKNOWN", "DISCORD_BOT_TOKEN=old &>/dev/null", "unparseable"),
]:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "repo").mkdir(); (td / "ws").mkdir()
        _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
        _re_.write_text("GEMINI_API_KEY=real\n")
        _we_.write_text(_line + "\n")
        with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
            _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
    _d = "" if _r is None else _r["detail"]
    if _want.startswith("named"):
        _key = "DISCORD_BOT_TOKEN1" if _want == "named1" else "DISCORD_BOT_TOKEN"
        check(f"loader grammar: {_label}", _key in _d and "is missing" in _d, True)
    else:
        check(f"loader grammar: {_label} (reason)", _want in _d, True)
        check(f"loader grammar: {_label} (not absence)", "is missing" in _d, False)
        check(f"loader grammar: {_label} (no merge advice for the bad name)", "1BAD" in _d and "merged" in _d, False)

# The SELECTED side obeys the same grammar: a command there makes the selected
# key set unproven, so the probe WARNs UNKNOWN rather than claiming a diff.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir(); (td / "ws").mkdir()
    _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
    _re_.write_text("GEMINI_API_KEY=sel DISCORD_BOT_TOKEN=ephemeral true\n")
    _we_.write_text("GEMINI_API_KEY=sel\nDISCORD_BOT_TOKEN=persistent\n")
    with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
        _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
    check("command prefix in the SELECTED file is UNKNOWN, never silent",
          _r is not None and "could not be completed" in _r["detail"]
          and "selected repo" in _r["detail"]
          and "unmodelled-command" in _r["detail"], True)

# 10. A command segment is UNKNOWN at `;` and end-of-line, not an empty parse.
# Expectations come from bash itself (`set -e; set -a; .` inside a function).
import shutil as _shutil
import subprocess as _sp


def _bash_load(path):
    """(rc, DISCORD_BOT_TOKEN set?) after the loader-shaped source of path."""
    _p = _sp.run(
        ["bash", "-c",
         'trap \'echo "rc=$? set=${DISCORD_BOT_TOKEN+yes}"\' EXIT; '
         'f(){ set -e; set -a; . "$1"; }; f "$1"', "_", str(path)],
        capture_output=True, text=True)
    _rc, _set = _p.stdout.split()
    return int(_rc[3:]), _set == "set=yes"


for _label, _line, _bash, _want in [
    ("plain assignment (control)", "DISCORD_BOT_TOKEN=old", (0, True), "named"),
    ("persistent builtin at end-of-line", "readonly DISCORD_BOT_TOKEN=old",
     (0, True), "unmodelled-command"),
    ("failed command then semicolon", "false; DISCORD_BOT_TOKEN=old",
     (1, False), "unmodelled-command"),
]:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "repo").mkdir(); (td / "ws").mkdir()
        _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
        _re_.write_text("GEMINI_API_KEY=selected\n")
        _we_.write_text("GEMINI_API_KEY=other\n" + _line + "\n")
        if _shutil.which("bash"):
            check(f"bash oracle: {_label}", _bash_load(_we_), _bash)
        with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
            _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
    _d = "" if _r is None else _r["detail"]
    if _want == "named":
        check(f"command segment: {_label}",
              "DISCORD_BOT_TOKEN" in _d and "is missing" in _d, True)
    else:
        check(f"command segment: {_label} (UNKNOWN, not silent)",
              _r is not None and _want in _d, True)
        check(f"command segment: {_label} (no invented key)",
              "DISCORD_BOT_TOKEN" in _d or "is missing" in _d, False)

# 9. `||` short-circuits after a SUCCESSFUL assignment, so bash never reaches
# the right side. Rather than model exit status, the probe answers UNKNOWN.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir(); (td / "ws").mkdir()
    _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
    _re_.write_text("GEMINI_API_KEY=real\n")
    _we_.write_text("GEMINI_API_KEY=other || DISCORD_BOT_TOKEN=old\n")
    with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
        _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
    check("unmodelled operator warns UNKNOWN rather than inventing a key",
          _r is not None and "unparseable" in _r["detail"], True)
    check("...and does not name a key bash never sets",
          _r is not None and "DISCORD_BOT_TOKEN" in _r["detail"], False)

# A parse failure must not be reported as a permissions problem — the reader
# would go fix file modes for a quoting bug.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "repo").mkdir(); (td / "ws").mkdir()
    _re_, _we_ = td / "repo" / ".env", td / "ws" / ".env"
    _re_.write_text("GEMINI_API_KEY=real\n")
    _we_.write_text("GEMINI_API_KEY='unbalanced\n")
    with _patch.object(sutando_config, "resolve_dotenv", return_value=_re_):
        _r = hc.check_env_split(repo_env=_re_, ws_env=_we_)
    check("unbalanced quote names the parse cause",
          _r is not None and "unparseable" in _r["detail"], True)
    check("unbalanced quote does NOT advise fixing permissions",
          _r is not None and "file permissions" in _r["detail"], False)

if fails:
    print(f"FAIL ({len(fails)})")
    sys.exit(1)
print("PASS: env-split probe fixtures")
