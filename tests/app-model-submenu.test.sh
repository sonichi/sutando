#!/usr/bin/env bash
# The menu-bar app's Model submenu switches through scripts/switch-model.sh --confirm,
# never by typing /model itself; this pins the wiring CI cannot compile.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; F="$HERE/src/Sutando/main.swift"; fails=0
ok(){ echo "  ok   $1"; }; fail(){ echo "  FAIL $1 — $2"; fails=$((fails+1)); }
grep -q 'NSMenuItem(title: "Model", action: nil' "$F" && ok "1 a Model submenu exists" || fail "1" "no Model item"
for m in default opus sonnet haiku 'claude-fable-5-1\[1m\]'; do grep -q "\"$m\")" "$F" && ok "2 model id $m is offered" || fail "2 $m" "missing"; done
grep -q 'runCoreAction(script: repoRoot + "/scripts/switch-model.sh"' "$F" && ok "3 the handler runs scripts/switch-model.sh through the shared runner" || fail "3" "handler does not call the script"
grep -A2 'scripts/switch-model.sh' "$F" | grep -q '"--confirm"' && ok "4 ...with --confirm (the click is the owner's instruction)" || fail "4" "no --confirm"
grep -A2 'scripts/switch-model.sh' "$F" | grep -q '"--socket", sutandoTmuxSocket' && ok "5 ...on the configured socket" || fail "5" "socket not passed"
! grep -q 'send-keys.*"/model' "$F" && ok "6 the app never types /model itself" || fail "6" "raw /model send-keys in the app"
[ -x "$HERE/scripts/switch-model.sh" ] && ok "7 the script the menu calls exists and is executable" || fail "7" "scripts/switch-model.sh missing"
echo; [ $fails -eq 0 ] && echo "app-model-submenu: all checks pass" || { echo "app-model-submenu: $fails FAILED"; exit 1; }
