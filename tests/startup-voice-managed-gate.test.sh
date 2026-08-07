#!/bin/bash
# Startup must start voice for a managed-only install.
#
# Regression test for the gap on #2197: the PR taught resolveCredential() to load
# a managed voice key, but `configure_startup_runtime` still gated on the BYO env
# vars alone. A provisioned managed user with no GEMINI_* env therefore restarted
# into SKIP_VOICE=1 and voice silently stayed offline — the new tier was
# unreachable through the only path that actually boots the product.
#
# Isolation: the gate resolves its workspace through "$REPO"/scripts/sutando-config.sh,
# so pointing REPO at a stub repo redirects the lookup at a temp workspace without
# touching the developer's real one. $SUTANDO_WORKSPACE is deliberately NOT used —
# it stopped being honored for workspace resolution in v0.8 (#1440), so a test
# built on it would pass while proving nothing.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STUB="$TMP/stub-repo"
WS="$TMP/workspace"
mkdir -p "$STUB/scripts" "$WS/state/auth"
cat > "$STUB/scripts/sutando-config.sh" <<STUBEOF
#!/bin/bash
[ "\${1:-}" = "workspace" ] && printf '%s\n' "$WS"
STUBEOF
chmod +x "$STUB/scripts/sutando-config.sh"

# Run the REAL gate with the workspace lookup redirected at the stub.
run_gate() {
  env -i PATH="/usr/bin:/bin" REPO="$STUB" \
    GEMINI_API_KEY="${1:-}" GEMINI_VOICE_API_KEY="${2:-}" \
    bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
    _ "$TMP" "$REPO"
}

write_managed() { printf '%s\n' "$1" > "$WS/state/auth/managed-credentials.json"; }
clear_managed() { rm -f "$WS/state/auth/managed-credentials.json"; }

fail() { echo "FAIL: $1" >&2; exit 1; }

# A fake `python3` that exits non-zero, exactly like the Xcode Command Line
# Tools stub on a Mac with no real Python. Placed FIRST on PATH.
STUBBIN="$TMP/stubbin"; mkdir -p "$STUBBIN"
printf '#!/bin/sh\nexit 1\n' > "$STUBBIN/python3"; chmod +x "$STUBBIN/python3"

# Same as run_gate but with the stub shadowing python3 on PATH.
run_gate_stubbed() {
  env -i PATH="$STUBBIN:/usr/bin:/bin" REPO="$STUB" \
    GEMINI_API_KEY="" GEMINI_VOICE_API_KEY="" \
    bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
    _ "$TMP" "$REPO" 2>/dev/null
}

# Same, but capturing stderr — the loud-vs-silent assertion needs the warning.
run_gate_stubbed_err() {
  env -i PATH="$STUBBIN:/usr/bin:/bin" REPO="$STUB" \
    GEMINI_API_KEY="" GEMINI_VOICE_API_KEY="" \
    bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime' \
    _ "$TMP" "$REPO" 2>&1 >/dev/null
}

# 1. Managed-only install (no env keys at all) MUST start voice. This is the
#    assertion that fails on the unpatched gate.
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-voice-key"}}}'
out="$(run_gate)"
grep -q 'SKIP_VOICE=0' <<<"$out" || fail "managed-only voice key did not start voice: $out"
grep -q 'managed credentials' <<<"$out" || fail "enabled via managed tier but did not say so: $out"

# 2. Voice falls back to the gemini-text slot, matching
#    CAPABILITY_FALLBACKS['gemini-voice'] = ['gemini-voice','gemini-text'].
write_managed '{"capabilities":{"gemini-text":{"key":"managed-text-key"}}}'
grep -q 'SKIP_VOICE=0' <<<"$(run_gate)" || fail "gemini-text slot did not satisfy the voice capability"

# 3. A genuinely credential-free install MUST stay disabled — the control. If this
#    passes unconditionally the suite proves nothing about tier detection.
clear_managed
out="$(run_gate)"
grep -q 'SKIP_VOICE=1' <<<"$out" || fail "credential-free install was not disabled: $out"
grep -q 'managed-credentials.json' <<<"$out" || fail "disabled without naming the managed path: $out"
grep -q 'GEMINI_VOICE_API_KEY' <<<"$out" || fail "disabled without naming the BYO escape hatch: $out"

# 4. Malformed / empty / wrong-shape managed files skip the tier rather than
#    throwing, mirroring readManaged()'s try/catch contract. Each must land
#    disabled, not crash the gate under `set -e`.
for bad in 'not json at all' '{}' '{"capabilities":[]}' '{"capabilities":{"gemini-voice":{}}}' \
           '{"capabilities":{"gemini-voice":{"key":""}}}' '{"capabilities":{"gemini-voice":{"key":123}}}'; do
  write_managed "$bad"
  got="$(run_gate)"
  grep -q 'SKIP_VOICE=1' <<<"$got" || fail "malformed managed file was treated as a credential: $bad -> $got"
done

# 5. BYO env still works with no managed file — the pre-existing path must not
#    regress, including the dedicated voice var.
clear_managed
grep -q 'SKIP_VOICE=0' <<<"$(run_gate byo-text-key)"  || fail "GEMINI_API_KEY regressed"
grep -q 'SKIP_VOICE=0' <<<"$(run_gate '' byo-voice-key)" || fail "GEMINI_VOICE_API_KEY regressed"

# 6. An unreadable managed file must not wedge startup (permissions can be wrong
#    on a half-provisioned install).
write_managed '{"capabilities":{"gemini-voice":{"key":"k"}}}'
chmod 000 "$WS/state/auth/managed-credentials.json"
if [ "$(id -u)" -ne 0 ]; then  # root ignores the mode bits
  grep -q 'SKIP_VOICE=1' <<<"$(run_gate)" || fail "unreadable managed file did not fail closed"
fi
chmod 644 "$WS/state/auth/managed-credentials.json"

# 7. CROSS-PATH: managed key + INHERITED SKIP_VOICE=1 — the launcher and the health
#    resolver must agree. This is the composition neither single-path test could
#    catch: cases 1-6 never set SKIP_VOICE, and the python suite never runs the real
#    launcher. The launcher UNSETS an inherited SKIP_VOICE when a managed credential
#    exists, so health must report ENABLED for the same workspace. When they
#    disagree the product ships a running voice agent behind a green "disabled".
#    (#2197 review blocker, john-the-dev 2026-07-31T05:37.)
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-voice-key"}}}'

# real launcher, with SKIP_VOICE=1 inherited from the parent environment
startup_out="$(env -i PATH="/usr/bin:/bin" REPO="$STUB" SKIP_VOICE=1 \
  bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
  _ "$TMP" "$REPO")"

# real health resolver, pointed at the SAME workspace
health_out="$(python3 -c '
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("hc", sys.argv[1] + "/src/health-check.py")
hc = importlib.util.module_from_spec(spec); spec.loader.exec_module(hc)
hc.WORKSPACE_DIR = Path(sys.argv[2])
cfg = hc.resolve_voice_health_config(env={"SKIP_VOICE": "1"},
                                     env_path=Path(sys.argv[2]) / "no-such-dotenv")
print("enabled" if cfg["enabled"] else "disabled", "|", cfg.get("detail", cfg.get("error","")))
' "$REPO" "$WS")"

grep -q 'SKIP_VOICE=0' <<<"$startup_out" \
  || fail "launcher did not unset inherited SKIP_VOICE for a managed credential: $startup_out"
grep -q '^enabled' <<<"$health_out" \
  || fail "CROSS-PATH DISAGREEMENT — launcher starts voice, health says: $health_out"

# control: with NO credential in any tier, BOTH must say disabled. Without this the
# assertion above could pass by making health ignore SKIP_VOICE unconditionally.
clear_managed
startup_none="$(env -i PATH="/usr/bin:/bin" REPO="$STUB" SKIP_VOICE=1 \
  bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
  _ "$TMP" "$REPO")"
health_none="$(python3 -c '
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("hc", sys.argv[1] + "/src/health-check.py")
hc = importlib.util.module_from_spec(spec); spec.loader.exec_module(hc)
hc.WORKSPACE_DIR = Path(sys.argv[2])
cfg = hc.resolve_voice_health_config(env={"SKIP_VOICE": "1"},
                                     env_path=Path(sys.argv[2]) / "no-such-dotenv")
print("enabled" if cfg["enabled"] else "disabled")
' "$REPO" "$WS")"
grep -q 'SKIP_VOICE=1' <<<"$startup_none" || fail "control: credential-free launcher should disable: $startup_none"
grep -q '^disabled' <<<"$health_none"     || fail "control: credential-free health should disable: $health_none"

echo "PASS: startup voice gate recognizes the managed tier"

# --- stub-shadowing: a broken `python3` on PATH must not be mistaken for -------
#     "no managed credential". The gate returns 1 on ANY failure, so a stub and
#     an absent credential are the same signal to the caller — a silent wrong
#     answer, not a crash: startup proceeds BYO-only while a managed credential
#     sits on disk. The fix resolves an absolute interpreter first and PROBES it,
#     so the stub cannot shadow a real Python.
#
#     This used to branch on a HOST-level probe:
#
#         if [ -x /opt/homebrew/bin/python3 ] || [ -x /usr/local/bin/python3 ]
#
#     evaluated in the OUTER shell, while the assertion runs inside
#     `env -i PATH="$STUBBIN:/usr/bin:/bin"`. Those are different worlds, and on a
#     Homebrew Mac they disagree: the outer test sees /opt/homebrew/bin/python3 and
#     takes the "a real interpreter is reachable" branch, but the inner env has no
#     brew on PATH at all (`command -v brew` -> nothing, measured), so the gate's
#     Homebrew tier cannot fire and the assertion fails. It failed identically on
#     the parent commit — the guard never held on that platform, and CI (Linux, no
#     Homebrew) structurally could not see it. Reported by sonichi on #2197.
#
#     The branch is gone rather than repaired. Inside this hermetic env NO tier is
#     reachable by construction — $SUTANDO_PY is cleared by `env -i`, the bundled
#     runtime does not exist under the fixture repo, PATH's python3 IS the stub,
#     and brew is not on PATH — so "a real interpreter is found despite the stub"
#     was unassertable here on ANY platform, not just where it happened to fail.
#     That case is covered properly below, by injecting a reachable interpreter
#     into the same inner env instead of asking the host about one.
#
#     What remains is the invariant this env CAN express: with nothing usable to
#     fall back to, returning 1 is correct — but it must be LOUD. Silence is the
#     whole defect, because the caller cannot then distinguish "no usable python"
#     from "no managed credential" and picks the wrong one.
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-voice-key"}}}'
err="$(run_gate_stubbed_err)"
case "$err" in
  *"no usable python3"*) echo "  ok  unusable python3 is reported, not silently read as 'absent'" ;;
  *) fail "stub python3 produced NO warning — indistinguishable from an absent credential (stderr: ${err:-<empty>})" ;;
esac

# And it must still DISABLE voice in that state rather than proceeding as if a
# credential had been read — the warning and the outcome are separate claims.
out="$(run_gate_stubbed)"
case "$out" in
  *SKIP_VOICE=1*) echo "  ok  ...and voice is disabled, not left enabled on an unread credential" ;;
  *) fail "no usable python yet voice stayed enabled (got: $out)" ;;
esac
clear_managed

# --- $SUTANDO_PY / bundled-runtime precedence (review blocker, 2026-08-02) -----
#     The stub test above falls back to a HOMEBREW python, which is exactly the
#     tier the buggy gate already had — so it passed while the real defect was
#     live. The untested case is a bundled install: a broken `python3` first on
#     PATH and a perfectly good $SUTANDO_PY. There the gate answered "no usable
#     python3" and left voice disabled with a valid managed credential on disk.
#
#     No host-dependent skip here: $SUTANDO_PY is pointed at whatever interpreter
#     is running this suite, so the case is exercised on every machine. That is
#     the point — a guard that only runs where Homebrew happens to exist is how
#     this bug reached review.
run_gate_sutando_py() {
  env -i PATH="$STUBBIN:/usr/bin:/bin" REPO="$STUB" \
    SUTANDO_PY="$REAL_PY" \
    GEMINI_API_KEY="" GEMINI_VOICE_API_KEY="" \
    bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
    _ "$TMP" "$REPO" 2>/dev/null
}

REAL_PY="$(command -v python3 2>/dev/null)"
[ -x "$REAL_PY" ] || REAL_PY=/usr/bin/python3
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-voice-key"}}}'
out="$(run_gate_sutando_py)"
case "$out" in
  *SKIP_VOICE=0*) echo "  ok  a stub python3 cannot disable voice when \$SUTANDO_PY is valid" ;;
  *) fail "gate ignored \$SUTANDO_PY — managed credential read as absent (got: $out)" ;;
esac

# CALIBRATION. The assertion above is satisfied by a gate that never disables
# voice at all, which would be a worse bug in the other direction. Same stub
# PATH, same valid $SUTANDO_PY, but NO managed credential: voice must go off.
clear_managed
out="$(run_gate_sutando_py)"
case "$out" in
  *SKIP_VOICE=1*) echo "  ok  ...and with no credential it still disables (gate can say NO)" ;;
  *) fail "gate enabled voice with no credential at all — the check is vacuous (got: $out)" ;;
esac

# The single source of truth itself: `sutando-config.sh python-bin` must PREFER
# $SUTANDO_PY. If this drifts, the gate above silently falls back to a later
# tier and the regression returns without either test failing.
got="$(SUTANDO_PY="$REAL_PY" bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"
[ "$got" = "$REAL_PY" ] \
  && echo "  ok  sutando-config.sh python-bin honours \$SUTANDO_PY" \
  || fail "python-bin ignored \$SUTANDO_PY (wanted $REAL_PY, got ${got:-<empty>})"


# --- tier 2: the BUNDLE-VENDORED runtime, with $SUTANDO_PY unset --------------
#     The guard above covers tier 1 ($SUTANDO_PY), which is the case the review
#     reproduced. It does NOT cover tier 2, and tier 2 is the one that matters on
#     a real bundled install: measured on this host, $SUTANDO_PY is UNSET while
#     <engine>/runtime/python/bin/python3 exists. If the system python3 there were
#     the CLT stub, tier 2 is the only thing standing between a managed user and a
#     silent voice outage — so leaving it untested would leave the actual
#     production shape unguarded while the suite looked thorough.
#
#     Builds the real layout (<engine>/sutando/scripts + <engine>/runtime/python)
#     and symlinks a WORKING interpreter into it, so this runs anywhere including
#     CI, where no bundle exists.
BUNDLE="$TMP/engine"
mkdir -p "$BUNDLE/sutando/scripts" "$BUNDLE/runtime/python/bin"
cp "$REPO/scripts/sutando-config.sh" "$BUNDLE/sutando/scripts/sutando-config.sh"
# sutando-config.sh sources this; without it the bundle fixture dies on the
# `.` and the tier-2 assertions below never run (CI, #2599).
cp "$REPO/scripts/python-binary.sh" "$BUNDLE/sutando/scripts/python-binary.sh"
ln -sf "$REAL_PY" "$BUNDLE/runtime/python/bin/python3"

got="$(env -i PATH="$STUBBIN:/usr/bin:/bin" \
       bash "$BUNDLE/sutando/scripts/sutando-config.sh" python-bin 2>/dev/null)"
case "$got" in
  */runtime/python/bin/python3)
    echo "  ok  python-bin picks the bundled runtime when \$SUTANDO_PY is unset" ;;
  *)
    fail "python-bin skipped the bundled runtime (got: ${got:-<empty>})" ;;
esac

# It must also RUN — resolving a path proves precedence, not usability, and the
# whole defect class here is an interpreter that exists but does not execute.
"$got" -c 'import json' 2>/dev/null \
  && echo "  ok  ...and the bundled interpreter actually executes" \
  || fail "python-bin returned a bundled path that does not run: $got"

# CONTROL: remove the bundled runtime; python-bin must not keep claiming it.
# A loud failure is expected when no verified later tier exists.
rm -f "$BUNDLE/runtime/python/bin/python3"
got_none="$(env -i PATH="$STUBBIN:/usr/bin:/bin" \
            bash "$BUNDLE/sutando/scripts/sutando-config.sh" python-bin 2>/dev/null)" || true
case "$got_none" in
  */runtime/python/bin/python3)
    fail "python-bin still claims the bundled runtime after it was removed (got: $got_none)" ;;
  *)
    echo "  ok  control: with no bundled runtime it never claims it" ;;
esac

# Summary last: printing PASS before the tier-2 assertions below would have
# announced success for checks that had not run yet.
echo "PASS: interpreter precedence (SUTANDO_PY / bundled) is honoured by the gate"

# --- S1 truth table: voicePreference / quarantined (design 2b, WS2 Step 4) ----
#     The launcher implements the SHARED credential-source table (amendment S1)
#     alongside the TS/python resolvers, health-check.py and the desktop
#     supervisor's spawn-env gate; tests/voice-preference-consumers.test.sh
#     drives all the engine-side consumers over one fixture matrix — these
#     cases pin the launcher's own decision + its user-facing reasons.

# 8. BYOK preference: managed entries must NOT enable voice (the resolver would
#    refuse them, so booting off them here would resurrect the disagreement
#    this whole suite exists to prevent).
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-v"},"gemini-text":{"key":"managed-t"}},"voicePreference":"byok"}'
out="$(run_gate)"
grep -q 'SKIP_VOICE=1' <<<"$out" || fail "byok preference still booted voice off a managed entry: $out"
grep -q 'BYOK preference set (managed entries ignored)' <<<"$out" \
  || fail "byok-with-no-env-key must read as disabled WITH the preference named: $out"
grep -q 'GEMINI_VOICE_API_KEY' <<<"$out" || fail "byok-disabled message must name the env escape hatch: $out"

# 9. BYOK preference + env key -> enabled (env is the only satisfying source).
out="$(run_gate byo-main-key)"
grep -q 'SKIP_VOICE=0' <<<"$out" || fail "byok preference + env key should enable voice: $out"

# 10. Quarantined entries are absent in EVERY mode (signed-out quarantine):
#     no preference marker, quarantined file, no env key -> disabled.
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-v"}},"quarantined":true}'
out="$(run_gate)"
grep -q 'SKIP_VOICE=1' <<<"$out" || fail "quarantined managed entry still booted voice: $out"

# 11. ...but quarantine only hides the MANAGED tier: an env key still enables
#     under an unset preference (legacy walk).
grep -q 'SKIP_VOICE=0' <<<"$(run_gate byo-main-key)" \
  || fail "quarantine must not disable a BYO env key under an unset preference"

# 12. Managed preference + usable managed entry -> enabled via the managed tier.
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-v"}},"voicePreference":"managed"}'
out="$(run_gate)"
grep -q 'SKIP_VOICE=0' <<<"$out" || fail "managed preference + managed entry did not enable voice: $out"
grep -q 'managed credentials' <<<"$out" || fail "managed-preference enable must credit the managed tier: $out"

# 13. S1's load-bearing row: managed preference + env key + QUARANTINED managed
#     entries -> voice stays OFF. A present env key must not silently satisfy a
#     managed preference — that is the logout-quarantine bypass.
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-v"}},"voicePreference":"managed","quarantined":true}'
out="$(run_gate byo-main-key byo-voice-key)"
grep -q 'SKIP_VOICE=1' <<<"$out" \
  || fail "S1 VIOLATION: env key satisfied a managed preference with quarantined entries: $out"
grep -q 'voicePreference=managed' <<<"$out" \
  || fail "managed-preference disable must name the preference: $out"

# 14. Same with the managed entries MISSING outright.
write_managed '{"capabilities":{},"voicePreference":"managed"}'
out="$(run_gate byo-main-key)"
grep -q 'SKIP_VOICE=1' <<<"$out" \
  || fail "S1 VIOLATION: env key satisfied a managed preference with no managed entry: $out"

# 15. Marker-free file (with only the R15 revision fields) -> byte-identical
#     legacy behavior: the managed entry boots voice. Regression pin.
write_managed '{"capabilities":{"gemini-voice":{"key":"managed-v"}},"preferenceRevision":7,"sessionRevision":3}'
out="$(run_gate)"
grep -q 'SKIP_VOICE=0' <<<"$out" || fail "marker-free managed file regressed (revisions must be ignored): $out"

echo "PASS: launcher honours the S1 voicePreference/quarantine truth table"
