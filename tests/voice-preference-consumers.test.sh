#!/bin/bash
# Cross-consumer agreement matrix for the S1 credential-source truth table
# (design 2b; impl plan WS2 Step 6 — "the 2b guardrail test").
#
# One fixture set, four engine-side consumers, one expected column per row:
# the TS resolver, its python twin, startup-runtime.sh's shell gate + launcher
# decision, and health-check.py must all agree on the effective voice
# credential source for every {managed entries x voicePreference x quarantined
# x env key} combination in tests/fixtures/voice-preference-matrix.json.
# Without this, one consumer can honor the preference while another silently
# boots voice off a source the resolver refuses — the exact two-reader
# disagreement (resolver vs. supervisor injection) design 2b calls out.
#
# DESKTOP TWIN: the supervisor half — managedGeminiVoiceKey() spawn-env
# injection and the `requires` gate (impl plan WS2 Step 5) — lives in the
# ag2space-cinny-desktop repo and is asserted by its
# engine/backend-supervisor.voice-preference.test.mjs, which copies the same
# fixture verbatim (the `supervisorInjectedManagedKey` / `voiceEnabled`
# columns). This driver deliberately does NOT reach across repos; the twin
# header comment names this file so the two cannot drift silently.
#
# Isolation mirrors tests/startup-voice-managed-gate.test.sh: the gate
# resolves its workspace through "$REPO"/scripts/sutando-config.sh, so a stub
# repo redirects the lookup at per-row temp workspaces.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE="$REPO/tests/fixtures/voice-preference-matrix.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

PY="$(command -v python3 2>/dev/null)"
[ -x "${PY:-}" ] || PY=/usr/bin/python3
[ -x "$PY" ] || fail "no python3 available to drive the matrix"

# --- materialize every row's workspace ONCE; all consumers read these bytes --
ROWS="$("$PY" - "$FIXTURE" "$TMP" <<'PYEOF'
import json, sys
from pathlib import Path
fixture = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
for i, row in enumerate(fixture["rows"]):
    ws = root / f"row-{i}"
    (ws / "state" / "auth").mkdir(parents=True)
    if row["managedFile"] is not None:
        (ws / "state" / "auth" / "managed-credentials.json").write_text(
            json.dumps(row["managedFile"]))
print(len(fixture["rows"]))
PYEOF
)"
echo "matrix: $ROWS rows materialized under $TMP"

# --- consumer 1: the TS resolver (one tsx pass over all rows) ----------------
TSX="$REPO/node_modules/.bin/tsx"
if [ ! -x "$TSX" ]; then
  fail "node_modules/.bin/tsx missing — run npm ci first (CI installs it in this job)"
fi
# The driver lives in $TMP (never written into the repo) and imports the real
# resolver by absolute file URL, so the repo tree stays pristine mid-run.
cat > "$TMP/resolver-driver.mts" <<'TSEOF'
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [repoRoot, fixturePath, rowsRoot] = process.argv.slice(2);
const resolver = await import(
	pathToFileURL(join(repoRoot, 'src', 'credential-resolver.ts')).href
);
const { credentialSourceLabel, resolveCredential } = resolver;

const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
let failures = 0;
fixture.rows.forEach((row: any, i: number) => {
	delete process.env.GEMINI_VOICE_API_KEY;
	delete process.env.SUTANDO_VOICE_CREDENTIAL_GENERATION;
	if (row.envKeyPresent) process.env.GEMINI_API_KEY = fixture.envKeyValue;
	else delete process.env.GEMINI_API_KEY;
	const managedPath = join(rowsRoot, `row-${i}`, 'state', 'auth', 'managed-credentials.json');
	const got = resolveCredential('gemini-voice', { managedPath });
	const exp = row.expected;
	const gotGen = got.credentialGeneration ?? null;
	const label = credentialSourceLabel(got.source);
	const ok =
		got.source === exp.resolverSource &&
		got.key === exp.resolvedKey &&
		gotGen === exp.credentialGeneration &&
		label === exp.effectiveSourceLabel;
	if (!ok) {
		failures += 1;
		console.error(
			`FAIL ts-resolver ${row.name}: got {source:${got.source}, key:${got.key}, ` +
			`gen:${gotGen}, label:${label}} expected {source:${exp.resolverSource}, ` +
			`key:${exp.resolvedKey}, gen:${exp.credentialGeneration}, label:${exp.effectiveSourceLabel}}`,
		);
	} else {
		console.log(`  ok  ts-resolver   ${row.name} -> ${exp.effectiveSourceLabel}`);
	}
});
process.exit(failures ? 1 : 0);
TSEOF
"$TSX" "$TMP/resolver-driver.mts" "$REPO" "$FIXTURE" "$TMP" \
  || fail "TS resolver disagrees with the matrix"

# --- consumer 2: the python resolver twin ------------------------------------
"$PY" - "$FIXTURE" "$TMP" "$REPO" <<'PYEOF' || fail "python resolver twin disagrees with the matrix"
import json, os, sys
from pathlib import Path
repo = sys.argv[3]
sys.path.insert(0, str(Path(repo) / "src"))
from credential_resolver import credential_source_label, resolve_credential
fixture = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
failures = 0
for i, row in enumerate(fixture["rows"]):
    os.environ.pop("GEMINI_VOICE_API_KEY", None)
    os.environ.pop("SUTANDO_VOICE_CREDENTIAL_GENERATION", None)
    if row["envKeyPresent"]:
        os.environ["GEMINI_API_KEY"] = fixture["envKeyValue"]
    else:
        os.environ.pop("GEMINI_API_KEY", None)
    managed = root / f"row-{i}" / "state" / "auth" / "managed-credentials.json"
    got = resolve_credential("gemini-voice", str(managed))
    exp = row["expected"]
    label = credential_source_label(got.source)
    ok = (got.source == exp["resolverSource"] and got.key == exp["resolvedKey"]
          and got.credential_generation == exp["credentialGeneration"]
          and label == exp["effectiveSourceLabel"])
    if not ok:
        failures += 1
        print(f"FAIL py-resolver {row['name']}: got {tuple(got)} label={label} expected {exp}",
              file=sys.stderr)
    else:
        print(f"  ok  py-resolver   {row['name']} -> {exp['effectiveSourceLabel']}")
sys.exit(1 if failures else 0)
PYEOF

# --- consumer 3: health-check.py (gate + composed voice-enabled answer) ------
"$PY" - "$FIXTURE" "$TMP" "$REPO" <<'PYEOF' || fail "health-check.py disagrees with the matrix"
import importlib.util, json, sys, tempfile
from pathlib import Path
repo = sys.argv[3]
spec = importlib.util.spec_from_file_location("hc", str(Path(repo) / "src" / "health-check.py"))
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)
fixture = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
missing = Path(tempfile.gettempdir()) / "voice-matrix-missing-dotenv"
missing.unlink(missing_ok=True)
failures = 0
for i, row in enumerate(fixture["rows"]):
    hc.WORKSPACE_DIR = root / f"row-{i}"
    exp = row["expected"]
    env = {"GEMINI_API_KEY": fixture["envKeyValue"]} if row["envKeyPresent"] else {}
    gate = hc.managed_voice_credential_present()
    cfg = hc.resolve_voice_health_config(env=env, env_path=missing)
    ok = gate == exp["managedGatePresent"] and cfg["enabled"] == exp["voiceEnabled"]
    if not ok:
        failures += 1
        print(f"FAIL health-check {row['name']}: gate={gate} enabled={cfg['enabled']} "
              f"expected gate={exp['managedGatePresent']} enabled={exp['voiceEnabled']} "
              f"({cfg.get('detail', cfg.get('error', ''))})", file=sys.stderr)
    else:
        print(f"  ok  health-check  {row['name']} -> gate={gate} enabled={cfg['enabled']}")
sys.exit(1 if failures else 0)
PYEOF

# --- consumer 4: startup-runtime.sh (gate function + launcher decision) ------
# Stub repo redirecting the workspace lookup; the workspace itself comes from
# $VOICE_MATRIX_WS so one stub serves every row. python-bin deliberately
# answers nothing (exit 1) so _voice_gate_python falls through to the real
# /usr/bin/python3 inside the hermetic env — same shape as the managed-gate
# suite's stub.
STUB="$TMP/stub-repo"
mkdir -p "$STUB/scripts"
cat > "$STUB/scripts/sutando-config.sh" <<'STUBEOF'
#!/bin/bash
[ "${1:-}" = "workspace" ] && printf '%s\n' "$VOICE_MATRIX_WS"
STUBEOF
chmod +x "$STUB/scripts/sutando-config.sh"

i=0
while [ "$i" -lt "$ROWS" ]; do
  name="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["rows"][int(sys.argv[2])]["name"])' "$FIXTURE" "$i")"
  env_present="$("$PY" -c 'import json,sys; print("1" if json.load(open(sys.argv[1]))["rows"][int(sys.argv[2])]["envKeyPresent"] else "")' "$FIXTURE" "$i")"
  gate_expected="$("$PY" -c 'import json,sys; print("1" if json.load(open(sys.argv[1]))["rows"][int(sys.argv[2])]["expected"]["managedGatePresent"] else "")' "$FIXTURE" "$i")"
  enabled_expected="$("$PY" -c 'import json,sys; print("0" if json.load(open(sys.argv[1]))["rows"][int(sys.argv[2])]["expected"]["voiceEnabled"] else "1")' "$FIXTURE" "$i")"
  ws="$TMP/row-$i"

  # 4a. the gate function itself: true iff a managed entry may satisfy voice.
  if env -i PATH="/usr/bin:/bin" REPO="$STUB" VOICE_MATRIX_WS="$ws" \
      bash -c 'source "$1/src/startup-runtime.sh"; _managed_voice_credential_present' _ "$REPO" 2>/dev/null; then
    gate_got="1"
  else
    gate_got=""
  fi
  [ "$gate_got" = "$gate_expected" ] \
    || fail "shell gate disagrees on $name: managed-present=${gate_got:-0} expected ${gate_expected:-0}"

  # 4b. the launcher decision (SKIP_VOICE after configure_startup_runtime).
  out="$(env -i PATH="/usr/bin:/bin" REPO="$STUB" VOICE_MATRIX_WS="$ws" \
      GEMINI_API_KEY="${env_present:+byo-mk}" GEMINI_VOICE_API_KEY="" \
      bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
      _ "$TMP" "$REPO")"
  grep -q "SKIP_VOICE=$enabled_expected" <<<"$out" \
    || fail "launcher disagrees on $name: $out (expected SKIP_VOICE=$enabled_expected)"

  echo "  ok  shell-gate    $name -> gate=${gate_got:-0} SKIP_VOICE=$enabled_expected"
  i=$((i + 1))
done

echo "PASS: resolver (TS+py), shell gate, launcher and health check agree on every S1 matrix row"
