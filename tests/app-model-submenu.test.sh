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
grep -q '"/state/model-switch.json"' "$F" && grep -q 'it.state = (c.id == current)' "$F" && ok "2f the recorded model is check-marked" || fail "2f" "no current-model mark"
grep -q 'runCoreAction(script: repoRoot + "/scripts/switch-model.sh"' "$F" && ok "3 the handler runs scripts/switch-model.sh through the shared runner" || fail "3" "handler does not call the script"
grep -A2 'scripts/switch-model.sh' "$F" | grep -q '"--confirm"' && ok "4 ...with --confirm (the click is the owner's instruction)" || fail "4" "no --confirm"
grep -A2 'scripts/switch-model.sh' "$F" | grep -q '"--socket", sutandoTmuxSocket' && ok "5 ...on the configured socket" || fail "5" "socket not passed"
! grep -q 'send-keys.*"/model' "$F" && ok "6 the app never types /model itself" || fail "6" "raw /model send-keys in the app"
[ -x "$HERE/scripts/switch-model.sh" ] && ok "7 the script the menu calls exists and is executable" || fail "7" "scripts/switch-model.sh missing"
echo; [ $fails -eq 0 ] && echo "app-model-submenu: all checks pass" || { echo "app-model-submenu: $fails FAILED"; exit 1; }
