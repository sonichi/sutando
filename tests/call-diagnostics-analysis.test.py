#!/usr/bin/env python3
"""Direct coverage for skills/call-diagnostics/scripts/analysis.py.

The engine is pure, so every case here is an in-memory call dict — no SQLite,
no JSONL, no CLI, no workspace resolution. The import itself is part of the
contract: if analysis.py ever starts resolving a workspace or reading argv,
`test_import_is_pure` fails.
"""
import importlib.util
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO / "skills" / "call-diagnostics" / "scripts" / "analysis.py"

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


# ── the import must be side-effect free ──────────────────────────────────────
print("── purity ──")
_argv, sys.argv = sys.argv, ["analysis-purity-probe", "--verbose", "--context"]
buf_out, buf_err = io.StringIO(), io.StringIO()
try:
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        spec = importlib.util.spec_from_file_location("cd_analysis", ENGINE)
        A = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(A)
finally:
    sys.argv = _argv
check("importing the engine prints nothing", buf_out.getvalue() == "" and buf_err.getvalue() == "",
      f"stdout={buf_out.getvalue()!r} stderr={buf_err.getvalue()!r}")
src_full = ENGINE.read_text()
# Strip the module docstring: it legitimately NAMES the things the engine
# avoids ("does NOT ... read sys.argv ..."), which would false-positive.
src = src_full.split('"""', 2)[-1]
for forbidden, why in (("sqlite3", "opens a database"),
                       ("sys.argv", "reads CLI args"),
                       ("resolve_workspace", "resolves a workspace"),
                       ("<html", "generates HTML")):
    check(f"engine does not {why}", forbidden not in src, f"found {forbidden!r}")
check("engine exposes the documented API",
      all(hasattr(A, n) for n in ("merge_timeline", "diagnose", "categorize_issue",
                                  "analyze_patterns_and_repair")))


def ev(t, name):
    return {"timestamp": f"2026-08-01T10:00:{t:02d}Z", "event": name}


def tc(t, name, ms):
    return {"timestamp": f"2026-08-01T10:00:{t:02d}Z", "name": name, "durationMs": ms}


def call(sid="c", events=None, tools=None, dur=60000):
    return {"callSid": sid, "sessionId": sid, "timestamp": "2026-08-01T10:00:00Z",
            "durationMs": dur, "events": events if events is not None else [],
            "toolCalls": tools if tools is not None else []}


# ── timeline ─────────────────────────────────────────────────────────────────
print("── merge_timeline ──")
tl = A.merge_timeline(call(events=[ev(9, "b"), ev(1, "a")], tools=[tc(5, "t", 250)]))
check("merges events and tool calls", len(tl) == 3, str(len(tl)))
check("sorted by timestamp", [i["ts"] for i in tl] == sorted(i["ts"] for i in tl), str(tl))
check("event shape preserved",
      tl[0]["type"] == "event" and tl[0]["detail"] == "a", str(tl[0]))
tool_item = [i for i in tl if i["type"] == "toolCall"][0]
check("tool detail keeps the name (duration) format",
      tool_item["detail"] == "t (250ms)", tool_item["detail"])
check("tool item retains name and durationMs",
      tool_item["name"] == "t" and tool_item["durationMs"] == 250, str(tool_item))
check("empty call yields an empty timeline", A.merge_timeline(call()) == [])
check("duplicate events are NOT de-duplicated",
      len(A.merge_timeline(call(events=[ev(1, "x"), ev(1, "x")]))) == 2)

print("── parse_ts ──")
check("parses ISO with Z", A.parse_ts("2026-08-01T10:00:00Z") is not None)
check("parses ISO with offset", A.parse_ts("2026-08-01T10:00:00+00:00") is not None)
check("invalid timestamp returns None, does not raise", A.parse_ts("bogus") is None)
check("empty timestamp returns None", A.parse_ts("") is None)

# ── detections ───────────────────────────────────────────────────────────────
print("── diagnose ──")
clean = A.diagnose(call(events=[ev(1, "call_started"), ev(30, "call_ended")]))
check("a clean call is diagnosable without raising", isinstance(clean, list), str(type(clean)))

halluc = A.diagnose(call(events=[ev(1, "call_started"),
                                 ev(3, "sutando: I'm recording"),
                                 ev(30, "call_ended")]))
check("a hallucination phrase is detected", len(halluc) >= 1, str(halluc))
check("issues carry the documented keys",
      all({"time", "issue", "detail"} <= set(i) for i in halluc), str(halluc))

# The rule is a suspiciously FAST return (<10ms = likely a silent early error),
# not a slow one; `work` and `hang_up` are exempt.
fast = A.diagnose(call(events=[ev(1, "call_started"), ev(50, "call_ended")],
                       tools=[tc(2, "screen_record", 5)], dur=52000))
check("a sub-10ms tool return is flagged as a silent failure", len(fast) >= 1, str(fast))
exempt = A.diagnose(call(events=[ev(1, "call_started")], tools=[tc(2, "hang_up", 5)]))
check("hang_up is exempt from the fast-return rule", len(exempt) == 0, str(exempt))

check("every detected issue categorizes to a string",
      all(isinstance(A.categorize_issue(i), str) and A.categorize_issue(i)
          for i in halluc + fast), "empty or non-string category")
check("categorize_issue is deterministic",
      [A.categorize_issue(i) for i in halluc] == [A.categorize_issue(i) for i in halluc])

# ── repairs ──────────────────────────────────────────────────────────────────
print("── analyze_patterns_and_repair ──")
many = [call(f"c{n}", events=[ev(1, "call_started"),
                              ev(3, "sutando: I'm recording"),
                              ev(30, "call_ended")]) for n in range(6)]
reps = A.analyze_patterns_and_repair(many)
check("a repeated pattern produces at least one repair", len(reps) >= 1, str(len(reps)))
check("repairs are dicts", all(isinstance(r, dict) for r in reps))
check("no calls → no repairs", A.analyze_patterns_and_repair([]) == [])
check("repair output is deterministic",
      A.analyze_patterns_and_repair(many) == reps)

# ── characterized pre-existing defect ────────────────────────────────────────
print("── known defect (characterized, NOT fixed here) ──")
# `call.get("events", [{}])[0]` defaults only on a MISSING key, so a present-
# but-empty list indexes out of range. Pre-existing on main; recorded here so
# this extraction cannot silently change it. Fixing it is a separate PR.
raised = False
try:
    A.analyze_patterns_and_repair([call("empty", events=[])])
except IndexError:
    raised = True
check("empty events[] still raises IndexError (pre-existing, unchanged)", raised)
check("a call with NO events key does not raise",
      A.analyze_patterns_and_repair([{"callSid": "x", "durationMs": 1, "toolCalls": []}]) is not None)

# ── the renderer must delegate, not duplicate ────────────────────────────────
print("── delegation ──")
diag = (REPO / "skills" / "call-diagnostics" / "scripts" / "diagnose.py").read_text()
check("diagnose.py imports the engine", "from analysis import" in diag)
for dup in ("def diagnose(", "def categorize_issue(", "def analyze_patterns_and_repair(",
            "def merge_timeline("):
    check(f"diagnose.py does not redefine {dup.split('(')[0][4:]}", dup not in diag)
check("HALLUCINATION_PHRASES is not re-declared in the renderer",
      "HALLUCINATION_PHRASES = [" not in diag)

print()
if FAILS:
    print(f"FAIL — {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("PASS — call-diagnostics analysis engine")
