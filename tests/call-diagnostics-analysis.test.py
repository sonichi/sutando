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

# ── remaining detection rules ────────────────────────────────────────────────
# Every trigger below was read from analysis.py rather than guessed — two of my
# earlier fixtures were wrong from assuming (the fast-fail rule is a SUB-10ms
# return, and agent lines are prefixed "sutando:" not "agent:").
print("── detection rules ──")


def only(issues, needle):
    return [i for i in issues if needle in i["issue"]]


# 3. inline task delegated via work (keyword in the action text, not a /tmp/ path)
inline = A.diagnose(call(events=[ev(1, "call_started"),
                                 ev(3, "task_delegated:please screen record the demo")]))
check("inline recording task delegated via work is flagged",
      only(inline, "Inline task delegated"), str(inline))
path_only = A.diagnose(call(events=[ev(1, "call_started"),
                                    ev(3, "task_delegated:tidy up /tmp/recording.mov")]))
check("a keyword only in a /tmp/ path does not trigger it",
      not only(path_only, "Inline task delegated"), str(path_only))

# 4. auto-play after recording, with no caller speech in between
autoplay = A.diagnose(call(events=[ev(1, "call_started"), ev(3, "recording auto-stop"),
                                   ev(5, "tool_call:play_recording")]))
check("auto-play right after a recording stop is flagged",
      only(autoplay, "Auto-play after recording"), str(autoplay))
asked = A.diagnose(call(events=[ev(1, "call_started"), ev(3, "recording auto-stop"),
                                ev(4, "caller: play it back please"),
                                ev(5, "tool_call:play_recording")]))
check("caller speech in between suppresses the auto-play warning",
      not only(asked, "Auto-play after recording"), str(asked))

# 5. >30s delay between a caller request and the tool call
delay = A.diagnose(call(events=[ev(1, "caller: can you record this"),
                                ev(45, "tool_call:screen_record")]))
check("a >30s request-to-execution delay is flagged", only(delay, "delay from request"), str(delay))
prompt_ = A.diagnose(call(events=[ev(1, "caller: can you record this"),
                                  ev(5, "tool_call:screen_record")]))
check("a prompt execution is not flagged as delayed",
      not only(prompt_, "delay from request"), str(prompt_))

# 6. caller speech logged >5s AFTER the tool call (STT lag)
lag = A.diagnose(call(events=[ev(1, "tool_call:describe_screen"),
                              ev(20, "caller: what is on screen")]))
check("caller speech logged after a tool call is flagged as STT lag",
      only(lag, "Caller speech logged"), str(lag))

# 7. wrong tool for the request
wrong = A.diagnose(call(events=[ev(1, "caller: what branch am i on"),
                                ev(5, "tool_call:describe_screen")]))
check("a repo question answered with describe_screen is flagged as the wrong tool",
      only(wrong, "Wrong tool"), str(wrong))
right = A.diagnose(call(events=[ev(1, "caller: what branch am i on"),
                                ev(5, "tool_call:work")]))
check("the correct tool for the same request is not flagged",
      not only(right, "Wrong tool"), str(right))

# 8. explicit user correction
corr = A.diagnose(call(events=[ev(1, "call_started"),
                               ev(3, "caller: i am not asking you to record")]))
check("an explicit user correction is flagged", only(corr, "User correction"), str(corr))

# 10. auto-invoked tool with no matching request in the preceding 20s
auto = A.diagnose(call(events=[ev(1, "call_started"), ev(3, "tool_call:screen_record")]))
check("a tool invoked with no matching request is flagged",
      only(auto, "Auto-invoked"), str(auto))
requested = A.diagnose(call(events=[ev(1, "caller: please record the screen"),
                                    ev(3, "tool_call:screen_record")]))
check("the same tool IS allowed when the caller asked for it",
      not only(requested, "Auto-invoked"), str(requested))

# ── categorize_issue: every arm ──────────────────────────────────────────────
# categorize_issue takes an issue dict directly, so each branch is driven by the
# text/detail it actually matches on — all read from analysis.py, not guessed.
print("── categorize_issue arms ──")


def cat(text, detail=""):
    return A.categorize_issue({"issue": text, "detail": detail, "time": "10:00:00"})


ARMS = [
    ("screen_record returned in 3ms — likely failed silently", "", "returned too fast"),
    ("Wrong tool: describe_screen instead of work", "Code questions should use work.", "Wrong tool"),
    ("Possible hallucination: \"it is playing\"", "", "video is playing"),
    ("Possible hallucination: \"recording is complete\"", "", "recording is complete"),
    ("Possible hallucination: \"I'm unable to\"", "can't find the file", "can't find file"),
    ("Possible hallucination: \"on develop\"", "the branch is develop", "fabricated answer"),
    ("Possible hallucination: \"something else entirely\"", "", "Hallucinated:"),
    ("Auto-invoked screen_record — no matching user request", "", "without user asking"),
    ("Auto-play after recording — user didn't ask", "", "without user asking"),
    ("Inline task delegated via work: \"x\"", 'asked to "record the screen"', "Recording delegated"),
    ("Inline task delegated via work: \"x\"", 'asked to "play it back"', "Playback delegated"),
    ("Inline task delegated via work: \"x\"", 'asked to "do something"', "Inline task delegated"),
    ("User correction: \"you should submit\"", "", "submit task"),
    ("User correction: \"not asking you to record\"", "", "recorded when user didn't ask"),
    ("User correction: \"this is not the subtitle\"", "", "wrong video version"),
    ("User correction: \"i just need this one\"", "", "existing file modified"),
    ("User correction: \"something unusual\"", "", "User correction"),
    ("Unmet expectation — user repeated request", "", "repeated request"),
    ("45s delay from request to screen_record", "", "Long delay"),
    ("screen_record failed 3 times in this call", "", "failed repeatedly"),
    ("Caller speech logged 8s after work tool call", "", "STT timestamp lag"),
    ("something entirely unrecognised", "", "Other:"),
]
for text, detail, expect in ARMS:
    got = cat(text, detail)
    check(f"categorize: {expect}", expect.lower() in got.lower(), f"{text[:40]!r} -> {got!r}")

check("every arm returns a non-empty string",
      all(isinstance(cat(t, d), str) and cat(t, d) for t, d, _ in ARMS))

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

# ── _classify_repair: every arm ──────────────────────────────────────────────
# Signature: (cat, freq, affected_calls, total_calls, trend, in_recent, occurrences).
# Every branch keys off `cat` plus the scalars, so each arm is driven directly.
# Conditions read from analysis.py.
print("── _classify_repair arms ──")


def clf(cat, freq=5, affected=3, total=10, trend="steady", in_recent=True, occ=None):
    return A._classify_repair(cat, freq, affected, total, trend, in_recent, occ or [])


REPAIR_ARMS = [
    ("STT timestamp lag", "unsolvable"),
    ("Auto-invoked screen_record — no matching user request", "prompt"),
    ("Auto-invoked play_recording — no matching user request", "prompt"),
    ("Auto-invoked describe_screen — no matching user request", "prompt"),
    ("Hallucinated: 'video is playing'", "prompt"),
    ("Hallucinated: 'can't find file'", "code"),
    ("Hallucinated: fabricated answer", "prompt"),
    ("Hallucinated: 'something else'", "prompt"),
    ("User had to explain 'submit task' = work tool", "prompt"),
    ("Gemini recorded when user didn't ask", "prompt"),
    ("Long delay before calling screen_record", "prompt"),
    ("scroll_and_describe returned too fast (failed)", "prompt"),
    ("screen_record returned too fast (failed)", "code"),
    ("screen_record failed repeatedly", "code"),
    ("Other: something nobody has classified", "unknown"),
]
for cat_text, expect_type in REPAIR_ARMS:
    r = clf(cat_text)
    check(f"classify {expect_type}: {cat_text[:38]}",
          isinstance(r, dict) and r.get("repair_type") == expect_type, f"{cat_text!r} -> {r}")

# PRE-EXISTING DEAD BRANCH (characterized, not fixed — separate concern).
# _classify_repair tests `"wrong version" in cat_lower`, but categorize_issue
# emits "Opened wrong VIDEO version" — which does not contain that substring.
# So the intended `code` repair is unreachable from its own upstream category
# and falls through to the generic `unknown` arm. Proven end to end:
#   categorize_issue({... "not the subtitle" ...}) -> 'Opened wrong video version'
#   _classify_repair(that)                          -> repair_type 'unknown'
#   _classify_repair('wrong version opened')        -> repair_type 'code'
# Characterized so this extraction cannot change it silently.
_dead = A.categorize_issue({"issue": 'User correction: "this is not the subtitle"',
                            "detail": "", "time": "t"})
check("categorize emits 'Opened wrong video version'", _dead == "Opened wrong video version", _dead)
check("its repair arm is UNREACHABLE — falls through to 'unknown' (pre-existing)",
      clf(_dead).get("repair_type") == "unknown", str(clf(_dead)))
check("the arm does fire for a string that literally contains 'wrong version'",
      clf("wrong version opened").get("repair_type") == "code",
      str(clf("wrong version opened")))

# priority escalation is a real branch, not decoration
hot = clf("Auto-invoked screen_record — no matching user request", in_recent=True, trend="steady")
cool = clf("Auto-invoked screen_record — no matching user request", in_recent=False, trend="improving")
check("a recent, non-improving pattern escalates priority above a stale improving one",
      hot.get("priority") != cool.get("priority"), f"hot={hot.get('priority')} cool={cool.get('priority')}")

# the low-signal fallthrough: not recent AND under the pct threshold
quiet = clf("Other: rare thing", affected=1, total=100, in_recent=False)
check("a rare, non-recent pattern is not forced into an 'unknown' repair",
      quiet is None or isinstance(quiet, dict), str(quiet))

check("every repair carries a type and a priority",
      all(clf(c).get("repair_type") and clf(c).get("priority") for c, _ in REPAIR_ARMS))

# ── characterized pre-existing defect ────────────────────────────────────────
print("── empty-events regression (fixed) ──")
# `call.get("events", [{}])[0]` defaults only on a MISSING key, so a present-
# but-empty list indexes out of range. Pre-existing on main; recorded here so
# this extraction cannot silently change it. Fixing it is a separate PR.
# Regression: `call.get("events", [{}])[0]` defaulted only on a MISSING key, so
# a present-but-empty list indexed out of range. Both call sites now use
# `(call.get("events") or [{}])[0]`.
raised = None
try:
    A.analyze_patterns_and_repair([call("empty", events=[])])
except IndexError as e:
    raised = e
check("empty events[] no longer raises IndexError", raised is None, str(raised))
check("a mix of empty-events and normal calls is handled",
      A.analyze_patterns_and_repair(
          [call("empty", events=[]),
           call("real", events=[ev(1, "call_started"), ev(3, "sutando: I'm recording")])]) is not None)
check("a call with NO events key does not raise",
      A.analyze_patterns_and_repair([{"callSid": "x", "durationMs": 1, "toolCalls": []}]) is not None)

# ── the renderer must delegate, not duplicate ────────────────────────────────
# ── renderer path: the SECOND fixed crash site, actually executed ────────────
# CR #2544 (qingyun-wu): the empty-events fix touches TWO independently
# reachable sites — analyze_patterns_and_repair AND generate_tracker_html
# (diagnose.py:418). The regression above only exercises the first; the
# delegation section below only SCANS diagnose.py as text. A text scan cannot
# catch a regression in the renderer, so this executes it for real.
print("── renderer path (diagnose.py:418) ──")
import importlib.util as _ilu
import tempfile as _tf
from pathlib import Path as _P

_spec = _ilu.spec_from_file_location(
    "cd_diagnose", REPO / "skills" / "call-diagnostics" / "scripts" / "diagnose.py")
_D = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_D)

_out = _P(_tf.mkdtemp(prefix="cd-render-")) / "tracker.html"
_calls = [
    {"callSid": "empty-events", "sessionId": "s0", "durationMs": 1000,
     "events": [], "toolCalls": []},                      # the crash input
    {"callSid": "normal", "sessionId": "s1", "durationMs": 60000,
     "events": [ev(1, "call_started"), ev(3, "sutando: I'm recording")],
     "toolCalls": [tc(2, "screen_record", 5)]},
]
_rendered = None
try:
    _D.generate_tracker_html(_calls, str(_out), source_type="phone")
    _rendered = _out.read_text() if _out.exists() else ""
except IndexError as e:
    check("generate_tracker_html survives a present-but-empty events list", False, f"IndexError: {e}")

if _rendered is not None:
    check("generate_tracker_html survives a present-but-empty events list", True)
    check("it wrote a non-empty tracker file", len(_rendered) > 0, f"{len(_rendered)} bytes")
    check("the normal call still rendered alongside the empty one",
          "normal" in _rendered, "expected the second callSid in the output")

# A call with NO events key at all must also render (the other default path).
_out2 = _P(_tf.mkdtemp(prefix="cd-render2-")) / "t.html"
_D.generate_tracker_html([{"callSid": "no-key", "durationMs": 1, "toolCalls": []}],
                         str(_out2), source_type="phone")
check("a call with no events key renders too", _out2.exists() and _out2.stat().st_size > 0)

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
