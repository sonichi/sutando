#!/usr/bin/env bash
# The menu-bar app's Model submenu reads its choices from skills/model-switch/manifest.json
# at open time and switches through scripts/switch-model.sh --confirm; pins wiring CI cannot compile.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; F="$HERE/src/Sutando/main.swift"; M="$HERE/skills/model-switch/manifest.json"; fails=0
ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
grep -q 'NSMenuItem(title: "Model", action: nil' "$F" && ok "1 a Model submenu exists" || fail "1" "no Model item"
! grep -qE '"(opus|sonnet|haiku|claude-fable-5-1\[1m\])"' "$F" && ok "2 no model id is compiled into the app" || fail "2" "a model id literal is in main.swift"
grep -q '"/skills/model-switch/manifest.json"' "$F" && grep -q '"MODEL_SWITCH_CHOICES"' "$F" && ok "2b the app reads the choices from the skill manifest config key" || fail "2b" "manifest path or key not read"
grep -q 'func menuNeedsUpdate' "$F" && grep -q 'modelSubmenu.delegate = self' "$F" && ok "2c ...on every open (menuNeedsUpdate on the submenu delegate)" || fail "2c" "not rebuilt at open"
python3 - "$M" <<'PY' && ok "2d the manifest lists >=2 id=Title choices including default" || fail "2d" "manifest config invalid"
import json, sys
raw = json.load(open(sys.argv[1]))["config"]["MODEL_SWITCH_CHOICES"]
pairs = [e.split("=", 1) for e in raw.split(";")]
assert len(pairs) >= 2 and all(len(p) == 2 and p[0].strip() and p[1].strip() for p in pairs), pairs
assert "default" in [p[0].strip() for p in pairs]
PY
grep -q 'title: "Other model' "$F" && grep -q 'func switchOtherModel' "$F" && ok "2e a free-form Other model… entry exists for ids the list lacks" || fail "2e" "no Other model entry"
grep -q '"/state/quota-state.json"' "$F" && grep -q 'last_request' "$F" && ok "2f the tick reads the LIVE model the proxy saw, not the switch record" || fail "2f" "tick still sourced from model-switch.json"
grep -q 'func sameModel' "$F" && ! grep -q 'c.id == current' "$F" && ok "2g ...compared by family/version, not raw id equality across two vocabularies" || fail "2g" "raw id comparison remains"
grep -q '"/state/model-switch.json"' "$F" && grep -qE '\(root\["accepted"\] as\? Bool\) == true' "$F" && ok "2h ...AND an accepted model-switch.json record, so a just-completed switch is not lost" || fail "2h" "liveModel no longer reads model-switch.json"
grep -q 'func parseTimestamp' "$F" && grep -q 'withFractionalSeconds' "$F" && ok "2i timestamps are PARSED to Date, not compared as raw strings (mixed second/millisecond precision sorts wrong as text)" || fail "2i" "no Date-parsing comparison found"
grep -q 'w >= s ? wireModel : switchModel' "$F" && ok "2j the MORE RECENT of the two timestamped sources wins" || fail "2j" "freshness compare missing/changed shape"
python3 - "$F" <<'PY' && ok "2k enumerated freshness-decision cases (replicated, mirrors 2f/2g's own control since Swift cannot be executed here)" || fail "2k" "freshness derivation diverges from the enumerated cases"
import re, sys, datetime as dt
src = open(sys.argv[1]).read()
assert 'case let (w?, s?): return w >= s ? wireModel : switchModel' in src
assert 'case (nil, _): return switchModel ?? wireModel' in src
assert 'case (_, nil): return wireModel ?? switchModel' in src

def decide(wire_at, switch_at, wire_model="wire", switch_model="switch"):
    if wire_at is not None and switch_at is not None:
        return wire_model if wire_at >= switch_at else switch_model
    if wire_at is None:
        return switch_model or wire_model
    return wire_model or switch_model

t0 = dt.datetime(2026, 9, 12, 8, 22, 35, tzinfo=dt.timezone.utc)
t1 = t0 + dt.timedelta(seconds=1)
cases = [
    # (wire_at, switch_at) -> expected
    ((t0, t1), "switch"),   # switch accepted AFTER the last wire observation -> switch wins (the reproduced bug)
    ((t1, t0), "wire"),     # a newer quota-bearing request supersedes an older switch
    ((t0, t0), "wire"),     # tie -> wire (the >=), matches "no other client" staleness case exactly
    ((None, t0), "switch"), # no wire record at all yet
    ((t0, None), "wire"),   # no accepted switch on record
    ((None, None), None),
]
for (w, s), expect in cases:
    got = decide(w, s, wire_model=("wire" if w is not None else None), switch_model=("switch" if s is not None else None))
    assert got == expect, (w, s, got, expect)
PY
grep -q 'runCoreAction(script: repoRoot + "/scripts/switch-model.sh"' "$F" && ok "3 the handler runs scripts/switch-model.sh through the shared runner" || fail "3" "handler does not call the script"
grep -A2 'scripts/switch-model.sh' "$F" | grep -q '"--confirm"' && ok "4 ...with --confirm (the click is the owner's instruction)" || fail "4" "no --confirm"
grep -A2 'scripts/switch-model.sh' "$F" | grep -q '"--socket", sutandoTmuxSocket' && ok "5 ...on the configured socket" || fail "5" "socket not passed"
! grep -q 'send-keys.*"/model' "$F" && ok "6 the app never types /model itself" || fail "6" "raw /model send-keys in the app"
[ -x "$HERE/scripts/switch-model.sh" ] && ok "7 the script the menu calls exists and is executable" || fail "7" "scripts/switch-model.sh missing"
echo; [ $fails -eq 0 ] && echo "app-model-submenu: all checks pass" || { echo "app-model-submenu: $fails FAILED"; exit 1; }
