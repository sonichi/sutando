#!/usr/bin/env python3
"""PreToolUse native-pim-guard: Bash commands that script or launch the native
macOS Calendar/Reminders/Contacts apps are denied unless the owner consented
(hooks/native-pim-guard.py).

Behavioral: drives the real hook via stdin exactly the way Claude Code invokes
it, asserting on the emitted decision JSON + exit code. Every run points the
hook at a throwaway workspace (SUTANDO_TEST_MODE + SUTANDO_WORKSPACE), so a
real host's consent or denial markers never leak into the assertions.

Run:  python3 tests/native-pim-guard.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = str(REPO / "hooks" / "native-pim-guard.py")
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


WS = Path(tempfile.mkdtemp(prefix="native-pim-guard-"))


def run(payload, env_extra=None, ws=None):
    stdin = json.dumps(payload) if not isinstance(payload, str) else payload
    env = dict(os.environ)
    env.pop("SUTANDO_ALLOW_NATIVE_PIM", None)
    env["SUTANDO_TEST_MODE"] = "1"
    env["SUTANDO_WORKSPACE"] = str(ws or WS)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(PYBASE + [HOOK], input=stdin,
                          capture_output=True, text=True, timeout=20, env=env)


def bash(command, env_extra=None, ws=None):
    return run({"tool_name": "Bash", "tool_input": {"command": command}}, env_extra, ws)


def reason_of(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def decision(r):
    try:
        return json.loads(r.stdout).get("hookSpecificOutput", {}).get("permissionDecision")
    except Exception:
        return None


# ── Every app form is denied: AppleScript, JXA, open -a/-ga/-gja, bundle id, .app path ──
DENIED = [
    """osascript -e 'tell application "Calendar" to get every calendar'""",
    """osascript -e 'tell app "iCal" to get name of every calendar'""",
    """osascript -e 'tell application "Reminders" to get name of every list'""",
    """osascript -e 'tell application "Contacts" to get name of every person'""",
    """osascript -e 'tell application "Address Book" to get every person'""",
    """osascript -l JavaScript -e 'Application("Calendar").calendars()'""",
    """osascript -l JavaScript -e "Application('Reminders').lists()" """,
    "osascript -e 'using terms from application \"Calendar\"' -e 'return 1'",
    "open -a Calendar",
    "open -ga Reminders",
    "open -gja Calendar && sleep 3",
    "open -a Contacts",
    "open -a 'Address Book'",
    "open -b com.apple.iCal",
    "open -b com.apple.reminders",
    "open -b com.apple.AddressBook",
    "open /System/Applications/Calendar.app",
    "open /System/Applications/Reminders.app",
    "OTHER=1 osascript -e 'tell application \"Calendar\" to launch'",
    # bundle ids inside the script, URL schemes, and the opaque runners
    """osascript -e 'tell application id "com.apple.iCal" to get every calendar'""",
    """osascript -l JavaScript -e 'Application("com.apple.reminders").lists()'""",
    """osascript -l JavaScript -e "Application('com.apple.AddressBook').people()" """,
    "open x-apple-reminderkit://",
    "open 'x-apple-reminderkit://REMCDReminder/abc'",
    "shortcuts run 'Add to Reminders'",
    "shortcuts run MyShortcut --input-path x.txt",
    "osascript ~/scripts/dump-calendar.scpt",
    "osascript /tmp/contacts-export.applescript",
    # command position is what counts: wrappers, substitutions, subshells, later segments
    "bash -c 'open -a Calendar'",
    "sh -c \"osascript -e 'tell application \\\"Contacts\\\" to get every person'\"",
    "grep -l x *.txt | xargs open -a Reminders",
    "echo $(osascript -e 'tell application \"Calendar\" to launch')",
    "sudo open -a Contacts",
    "(open -a Calendar)",
    "{ open -a Calendar; }",
    "grep foo bar; open -a Calendar",
    "osascript -l JavaScript -e 'var a = 1; Application(\"Calendar\").calendars()'",
    "osascript <<'EOF'\ntell application \"Calendar\"\nget every calendar\nend tell\nEOF",
    "/usr/bin/osascript -e 'tell application \"Reminders\" to get every list'",
]
for cmd in DENIED:
    r = bash(cmd)
    check(f"deny: {cmd[:60]}", r.returncode == 0 and decision(r) == "deny", r.stdout[:120])

# An opaque osascript file is read: a script that scripts one of the apps is denied,
# one that does not passes, and a missing file is not an error.
SCRIPTS = WS / "scripts"
SCRIPTS.mkdir(parents=True)
(SCRIPTS / "agenda.scpt").write_text('tell application "Calendar"\n  get every calendar\nend tell\n')
(SCRIPTS / "jxa.js").write_text("Application('com.apple.reminders').lists()")
(SCRIPTS / "notify.applescript").write_text('display notification "done" with title "Sutando"')
r = bash(f"osascript {SCRIPTS / 'agenda.scpt'}")
check("script file that tells Calendar is denied", decision(r) == "deny", r.stdout[:120])
r = bash(f"osascript -l JavaScript '{SCRIPTS / 'jxa.js'}'")
check("JXA file naming a bundle id is denied", decision(r) == "deny", r.stdout[:120])
r = bash(f"osascript {SCRIPTS / 'notify.applescript'}")
check("script file that does not touch the apps is allowed", decision(r) is None and not r.stdout.strip(), r.stdout[:120])
r = bash(f"osascript {SCRIPTS / 'missing.scpt'}")
check("missing script file is allowed (nothing to inspect)", decision(r) is None and not r.stdout.strip(), r.stdout[:120])

# Reason carries the order the model must follow and the never-re-prompt rule.
r = bash("open -a Calendar")
reason = reason_of(r)
check("reason names the Station connector first", "composio_find" in reason and "composio_exec" in reason)
check("reason names the owner's own MCP calendar tools second", "mcp__claude_ai_Google_Calendar__" in reason)
check("reason says to ask the owner third", "ask the owner" in reason)
check("reason forbids re-prompting after a denial", "never re-prompt" in reason)
check("reason names the escape hatch", "SUTANDO_ALLOW_NATIVE_PIM=1" in reason)
check("reason names the owner's grant command", "native_pim_consent.py grant" in reason)

# ── Unrelated osascript / open commands pass through ──────────────────────────
ALLOWED = [
    """osascript -e 'display notification "Calendar synced" with title "Sutando"'""",
    """osascript -e 'tell application "System Events" to get name of every process'""",
    """osascript -e 'tell application "Finder" to get name of every window'""",
    "osascript -e 'do shell script \"defaults read /Library/Preferences/com.apple.timezone\"'",
    "open -a Safari https://example.com",
    "open -a 'Google Chrome'",
    "open ./notes/calendar-plan.md",
    "python3 skills/macos-tools/scripts/calendar-reader.py 1 text --owner-asked",
    "grep -rn Calendar docs/",
    "echo 'tell application \"Calendar\"'",
    "shortcuts list",
    "osascript -e 'tell application id \"com.apple.Safari\" to activate'",
    "python3 skills/macos-tools/scripts/native_pim_consent.py status",
    # Read-only commands that quote an app command are reads, not launches.
    'grep -rn "open -gja Calendar" src/',
    "grep -n osascript f | grep 'application \"Calendar\"'",
    'git log -S "open -a Contacts" --oneline',
    "cat hooks/native-pim-guard.py",
    "rg 'tell application \"Reminders\"' skills/",
    "echo 'open -a Calendar' > notes.txt",
]
for cmd in ALLOWED:
    r = bash(cmd)
    check(f"allow: {cmd[:60]}",
          r.returncode == 0 and decision(r) is None and not r.stdout.strip(), r.stdout[:120])

# ── Non-Bash tools are untouched (safe under a broad matcher) ─────────────────
for t in ["Read", "mcp__sutando-station__composio_exec", "mcp__claude_ai_Google_Calendar__list_events"]:
    r = run({"tool_name": t, "tool_input": {"command": "open -a Calendar"}})
    check(f"allow non-bash: {t}", r.returncode == 0 and decision(r) is None and not r.stdout.strip())

# ── The file tools cannot write the consent record either (Write/Edit/MultiEdit/NotebookEdit) ──
ws_file = Path(tempfile.mkdtemp(prefix="native-pim-file-"))
(ws_file / "state").mkdir()
ws_link = Path(tempfile.mkdtemp(prefix="native-pim-link-")) / "ws"
ws_link.symlink_to(ws_file)  # the workspace reached through a symlink resolves to the same markers


def file_tool(tool, path, ws=ws_file, key="file_path"):
    payload = {"tool_name": tool, "tool_input": {key: str(path), "content": "owner"}}
    if tool == "Edit":
        payload["tool_input"].update({"old_string": "", "new_string": "owner"})
    if tool == "MultiEdit":
        payload["tool_input"].update({"edits": [{"old_string": "", "new_string": "owner"}]})
    return run(payload, ws=ws)


MARKERS = ["native-pim-consent", "calendar-automation-denied",
           "reminders-automation-denied", "contacts-automation-denied"]
for tool in ["Write", "Edit", "MultiEdit"]:
    for name in MARKERS:
        r = file_tool(tool, ws_file / "state" / name)
        check(f"{tool} on {name} is denied", decision(r) == "deny" and "own terminal" in reason_of(r), r.stdout[:120])
    r = file_tool(tool, ws_link / "state" / "native-pim-consent")
    check(f"{tool} through a symlinked workspace path is denied", decision(r) == "deny", r.stdout[:120])
    r = file_tool(tool, f"{ws_file}/state/../state/native-pim-consent")
    check(f"{tool} through a ../ path is denied", decision(r) == "deny", r.stdout[:120])
    for path in [ws_file / "state" / "core-status.json", ws_file / "state" / "native-pim-consent.md",
                 ws_file / "notes" / "native-pim-consent", "src/native-pim-consent.ts",
                 ws_file / "state" / "bindings" / "active-execution.json"]:
        r = file_tool(tool, path)
        check(f"{tool} on {Path(path).name} ({Path(path).parent.name}) is allowed",
              r.returncode == 0 and decision(r) is None and not r.stdout.strip(), r.stdout[:120])
r = file_tool("NotebookEdit", ws_file / "state" / "native-pim-consent", key="notebook_path")
check("NotebookEdit on the marker (notebook_path) is denied", decision(r) == "deny", r.stdout[:120])
r = file_tool("NotebookEdit", ws_file / "state" / "notes.ipynb", key="notebook_path")
check("NotebookEdit on a sibling notebook is allowed", decision(r) is None and not r.stdout.strip())
r = run({"tool_name": "Read", "tool_input": {"file_path": str(ws_file / "state" / "native-pim-consent")}}, ws=ws_file)
check("Read of the marker is allowed", decision(r) is None and not r.stdout.strip())
r = run({"tool_name": "Write", "tool_input": {"content": "x"}}, ws=ws_file)
check("Write without a path fails open", r.returncode == 0 and decision(r) is None)
check("no marker was written by the file-tool probes", not (ws_file / "state" / "native-pim-consent").exists())

# ── Escape hatch: the env prefix on the command, or the hook's own environment ──
for cmd in [
    "SUTANDO_ALLOW_NATIVE_PIM=1 osascript -e 'tell application \"Calendar\" to get every calendar'",
    "SUTANDO_ALLOW_NATIVE_PIM=1 open -ga Reminders",
    "cd /tmp && SUTANDO_ALLOW_NATIVE_PIM=1 python3 x.py; open -a Contacts",
]:
    r = bash(cmd)
    check(f"prefix lifts the guard: {cmd[:60]}", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])

r = bash("open -a Calendar", env_extra={"SUTANDO_ALLOW_NATIVE_PIM": "1"})
check("SUTANDO_ALLOW_NATIVE_PIM=1 in the hook env lifts the guard",
      r.returncode == 0 and not r.stdout.strip())

# A look-alike prefix does not count, nor does the token anywhere but command position.
for cmd in ["SUTANDO_ALLOW_NATIVE_PIM=10 open -a Calendar",
            "SUTANDO_ALLOW_NATIVE_PIM=0 open -a Calendar",
            "echo SUTANDO_ALLOW_NATIVE_PIM=1; open -a Calendar",
            "echo SUTANDO_ALLOW_NATIVE_PIM=1 ; open -a Calendar",
            "grep SUTANDO_ALLOW_NATIVE_PIM=1 .env && open -a Calendar"]:
    r = bash(cmd)
    check(f"look-alike prefix still denied: {cmd[:60]}", decision(r) == "deny", r.stdout[:120])

# ── The owner's persisted host opt-in (state/native-pim-consent) lifts the guard ──
ws_marker = Path(tempfile.mkdtemp(prefix="native-pim-consent-"))
(ws_marker / "state").mkdir()
(ws_marker / "state" / "native-pim-consent").write_text("owner")
r = bash("open -a Calendar", ws=ws_marker)
check("consent marker lifts the guard", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])
r = bash("open -a Calendar")
check("no marker in the workspace: still denied", decision(r) == "deny")

# The agent may never write that marker itself — `grant` is the owner's own command.
for cmd in ["python3 skills/macos-tools/scripts/native_pim_consent.py grant",
            "SUTANDO_ALLOW_NATIVE_PIM=1 python3 native_pim_consent.py grant",
            "cd skills/macos-tools/scripts && python3 native_pim_consent.py grant"]:
    r = bash(cmd, ws=ws_marker)
    check(f"grant is denied to the agent: {cmd[:60]}",
          decision(r) == "deny" and "own terminal" in reason_of(r), r.stdout[:120])

# …and no other spelling of "write the consent record" gets past either: the marker
# path, its denial-marker siblings, and any Python that drives the policy module.
MARKER = WS / "state" / "native-pim-consent"
for cmd in [f"touch {MARKER}",
            f"echo owner > {MARKER}",
            "printf owner >> state/native-pim-consent",
            f"cd {WS}/state && touch native-pim-consent",
            f"echo owner | tee {MARKER}",
            f"cp /dev/null {MARKER}",
            f"mkdir -p {WS}/state && date > {WS}/state/native-pim-consent",
            f"python3 -c \"open('{MARKER}', 'w').close()\"",
            "python3 -c 'import native_pim_consent as c; c.grant()'",
            "python3 -c 'import sys; sys.path.insert(0, \"skills/macos-tools/scripts\"); import native_pim_consent; native_pim_consent.grant()'",
            "python3 - <<'EOF'\nimport native_pim_consent\nnative_pim_consent.grant()\nEOF",
            "python3 -m native_pim_consent grant",
            "python3 skills/macos-tools/scripts/native_pim_consent.py check Calendar",
            f"rm {WS}/state/calendar-automation-denied",
            "rm -f state/contacts-automation-denied state/reminders-automation-denied",
            f"cat /dev/null > {WS}/state/reminders-automation-denied",
            "find . -name native-pim-consent -delete",
            f"sed -i '' d {MARKER}",
            f"ls; touch {MARKER}"]:
    r = bash(cmd, ws=ws_marker)
    check(f"consent record write is denied: {cmd[:60]!r}",
          decision(r) == "deny" and "own terminal" in reason_of(r), r.stdout[:120])
check("no marker was written by the probes themselves", not MARKER.exists())

# Reading the record, or the sources that mention it, is not writing it.
for cmd in [f"cat {MARKER}",
            f"ls -la {WS}/state/",
            f"test -f {MARKER} && echo present",
            "grep -rn native-pim-consent src/ hooks/",
            "git grep -n native_pim_consent",
            "cat src/native-pim-consent.ts",
            "grep -rn automation-denied src/morning-briefing.py",
            "python3 skills/macos-tools/scripts/native_pim_consent.py revoke",
            f"python3 skills/macos-tools/scripts/native_pim_consent.py status --workspace {WS}",
            f"python3 skills/macos-tools/scripts/native_pim_consent.py --workspace {WS} revoke",
            "python3 tests/native-pim-guard.test.py",
            "npx tsx --test tests/call-contact-consent.test.ts",
            "cat state/calendar-automation-denied"]:
    r = bash(cmd, ws=ws_marker)
    check(f"consent record read is allowed: {cmd[:60]}",
          r.returncode == 0 and decision(r) is None and not r.stdout.strip(), r.stdout[:120])

# ── Consent counts only on the owner's own task (state/bindings → tasks/<id>.txt) ──
def _bind(ws, tier, task_id="task-42"):
    (ws / "state" / "bindings").mkdir(parents=True, exist_ok=True)
    (ws / "tasks").mkdir(exist_ok=True)
    (ws / "state" / "bindings" / "active-execution.json").write_text(json.dumps({"task_id": task_id}))
    body = f"source: discord\nuser_id: 1\naccess_tier: {tier}\ntask: open my calendar\n" if tier else \
        "source: discord\ntask: open my calendar\n"
    (ws / "tasks" / f"{task_id}.txt").write_text(body)


ws_owner = Path(tempfile.mkdtemp(prefix="native-pim-owner-"))
_bind(ws_owner, "owner")
r = bash("SUTANDO_ALLOW_NATIVE_PIM=1 open -a Calendar", ws=ws_owner)
check("owner-tier task: prefix lifts the guard", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])

ws_team = Path(tempfile.mkdtemp(prefix="native-pim-team-"))
_bind(ws_team, "team")
for lift in ({"cmd": "SUTANDO_ALLOW_NATIVE_PIM=1 open -a Calendar"},
             {"cmd": "open -a Calendar", "env": {"SUTANDO_ALLOW_NATIVE_PIM": "1"}}):
    r = bash(lift["cmd"], env_extra=lift.get("env"), ws=ws_team)
    check(f"team-tier task: consent ignored ({'env' if 'env' in lift else 'prefix'})",
          decision(r) == "deny" and "owner's own task" in reason_of(r), r.stdout[:120])
(ws_team / "state" / "native-pim-consent").write_text("owner")
r = bash("open -a Calendar", ws=ws_team)
check("team-tier task: the persisted marker is ignored too", decision(r) == "deny", r.stdout[:120])
r = bash("open -a Safari", ws=ws_team)
check("team-tier task: unrelated commands are untouched", decision(r) is None and not r.stdout.strip())

ws_guest = Path(tempfile.mkdtemp(prefix="native-pim-guest-"))
_bind(ws_guest, "guest")
r = bash("SUTANDO_ALLOW_NATIVE_PIM=1 osascript -e 'tell application \"Contacts\" to get every person'", ws=ws_guest)
check("guest-tier task: consent ignored", decision(r) == "deny", r.stdout[:120])

ws_legacy = Path(tempfile.mkdtemp(prefix="native-pim-legacy-"))
_bind(ws_legacy, None)
r = bash("SUTANDO_ALLOW_NATIVE_PIM=1 open -a Calendar", ws=ws_legacy)
check("task without a tier line resolves to owner: prefix lifts", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])

ws_dangling = Path(tempfile.mkdtemp(prefix="native-pim-dangling-"))
(ws_dangling / "state" / "bindings").mkdir(parents=True)
(ws_dangling / "state" / "bindings" / "active-execution.json").write_text(json.dumps({"task_id": "gone"}))
r = bash("SUTANDO_ALLOW_NATIVE_PIM=1 open -a Calendar", ws=ws_dangling)
check("binding to a missing task file: self-attested prefix still lifts", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])
(ws_dangling / "state" / "bindings" / "active-execution.json").write_text("{not json")
r = bash("SUTANDO_ALLOW_NATIVE_PIM=1 open -a Calendar", ws=ws_dangling)
check("unreadable binding: self-attested prefix still lifts", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])
(ws_dangling / "state" / "bindings" / "active-execution.json").write_text(json.dumps({"task_id": "../etc/passwd"}))
r = bash("SUTANDO_ALLOW_NATIVE_PIM=1 open -a Calendar", ws=ws_dangling)
check("binding with a path-shaped task id is ignored", r.returncode == 0 and not r.stdout.strip(), r.stdout[:120])

# ── Unit: decide() in-process; the policy is the consent module's, not a copy ──
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("native_pim_guard", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
STATE = WS / "state"
check("decide: unrelated command allowed", guard.decide("ls -la", STATE) is None)
check("decide: grant denied before anything else", guard.decide("python3 native_pim_consent.py grant", STATE) == guard.REASON_GRANT)
check("decide: marker write denied before anything else", guard.decide("touch state/native-pim-consent", STATE) == guard.REASON_MARKER)
os.environ.pop("SUTANDO_ALLOW_NATIVE_PIM", None)
check("decide: app command without consent denied", guard.decide("open -a Reminders", STATE) == guard.REASON)
check("decide: unresolvable workspace = no consent evidence → still denied", guard.decide("open -a Reminders", None) == guard.REASON)
check("decide: unresolvable workspace leaves unrelated commands alone", guard.decide("ls", None) is None)
check("no private workspace fallback in the hook", not hasattr(guard, "_workspace") and "sutando-workspace" not in Path(HOOK).read_text())
check("marker names and the bound tier come from native_pim_consent",
      guard.consent.__name__ == "native_pim_consent" and not hasattr(guard, "bound_task_tier")
      and guard.MARKER_FILE.search("state/" + guard.consent.CONSENT_MARKER) is not None
      and guard.MARKER_FILE.search("src/native-pim-consent.ts") is None)
check("tier of the bound team task is read through the consent module",
      guard.consent.bound_task_tier(ws_team / "state") == "team")
check("decide_file: unknown state falls back to the marker name",
      guard.decide_file("/anywhere/native-pim-consent", None) == guard.REASON_MARKER
      and guard.decide_file("/anywhere/native-pim-consent.ts", None) is None
      and guard.decide_file("", STATE) is None)
check("segments: env prefix skipped, wrapper and read-only words recognised",
      [w for w, _ in guard.segments("X=1 osascript -e 'a'; grep b | xargs open")] == ["osascript", "grep", "xargs"])

# ── Bad input: fail-OPEN (exit 0, no deny) — never wedge the core ─────────────
r = run("this is not json")
check("malformed stdin fails open", r.returncode == 0 and decision(r) is None)
r = run({"tool_name": "Bash", "tool_input": "not an object"})
check("non-object tool_input fails open", r.returncode == 0 and decision(r) is None)
r = run({"tool_name": "Bash"})
check("missing tool_input allows", r.returncode == 0 and decision(r) is None)

if failures:
    print(f"\nFAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("\nPASS — native-pim-guard tests")
