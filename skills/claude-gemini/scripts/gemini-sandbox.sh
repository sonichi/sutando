#!/usr/bin/env bash
# Read-only Gemini sandbox for non-owner (team/other) tasks.
#
# Same contract as `codex exec --sandbox read-only -o FILE -- PROMPT`, so the
# bridge's two-stage rulebook needs no other change when sandbox.runtime is
# gemini:
#   - runs headless (no TTY, no prompts, stdin closed)
#   - read-only: --approval-mode plan makes no edits, --sandbox confines the
#     process (macOS seatbelt, Docker or Podman elsewhere)
#   - writes ONLY the final answer to FILE, nothing on failure
#   - exits with gemini's own status. A clean exit with an empty answer writes
#     no file and exits 0, which is the Stage-2 "exited 0 with no output" case.
#
# Usage: gemini-sandbox.sh --cd DIR -o FILE [--model M] -- PROMPT...
#
# Env: GEMINI_API_KEY, GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS: key based
#      auth, and the run gets a fresh empty HOME so no user-level ~/.gemini state
#      reaches a non-owner task. GEMINI_SANDBOX_AUTH_HOME: a directory holding only
#      the CLI's OAuth credentials, used as HOME when there is no key. With neither
#      the script refuses. GEMINI_SANDBOX_HEARTBEAT: seconds between progress lines
#      while the model is quiet, default 10.
#      SEATBELT_PROFILE defaults to restrictive-open on macOS: strict file
#      restrictions, network allowed (the model call needs it). Off macOS the
#      sandbox needs docker or podman, and the script refuses without one.
set -euo pipefail

WORKDIR=""
OUT=""
MODEL=""
usage() {
  sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cd) WORKDIR="${2:-}"; shift 2 ;;
    -o|--output) OUT="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) echo "gemini-sandbox: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
PROMPT="${*:-}"
[[ -n "$WORKDIR" ]] || { echo "gemini-sandbox: --cd DIR is required" >&2; exit 2; }
[[ -n "$OUT" ]] || { echo "gemini-sandbox: -o FILE is required" >&2; exit 2; }
[[ -n "$PROMPT" ]] || { echo "gemini-sandbox: prompt required after --" >&2; exit 2; }
[[ -d "$WORKDIR" ]] || { echo "gemini-sandbox: working directory does not exist: $WORKDIR" >&2; exit 2; }
if ! command -v gemini >/dev/null 2>&1; then
  echo "gemini-sandbox: gemini CLI not found in PATH (npm i -g @google/gemini-cli)" >&2
  exit 127
fi

# The Gemini CLI reads user-level state from ~/.gemini (GEMINI.md context, history,
# projects, extensions) whatever the working directory is, and it does so before the
# sandbox confines anything. A non-owner task must not see any of that.
#   - key based auth (GEMINI_API_KEY, GOOGLE_API_KEY, GOOGLE_APPLICATION_CREDENTIALS)
#     needs nothing from HOME, so the child gets a fresh, empty HOME.
#   - OAuth lives in ~/.gemini, so it needs a HOME. GEMINI_SANDBOX_AUTH_HOME names a
#     directory holding only the credentials for this purpose, and it is used as HOME.
#   - with neither, this refuses. A warning is not isolation.
if [[ -n "${GEMINI_API_KEY:-}" || -n "${GOOGLE_API_KEY:-}" || -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
  SCRUB_HOME="$(mktemp -d "${TMPDIR:-/tmp}/gemini-sandbox-home.XXXXXX")"
  trap 'rm -rf "$SCRUB_HOME"' EXIT
  export HOME="$SCRUB_HOME"
elif [[ -n "${GEMINI_SANDBOX_AUTH_HOME:-}" && -d "${GEMINI_SANDBOX_AUTH_HOME}" ]]; then
  export HOME="$GEMINI_SANDBOX_AUTH_HOME"
  echo "gemini-sandbox: using GEMINI_SANDBOX_AUTH_HOME as HOME for the CLI's own auth" >&2
else
  echo "gemini-sandbox: no key based auth in the environment (GEMINI_API_KEY, GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS) and no GEMINI_SANDBOX_AUTH_HOME; refusing to run a non-owner task with the owner's HOME" >&2
  exit 2
fi

# Headless runs have nobody to answer the folder-trust prompt. Trust here means the
# CLI may load context files from the working directory: /tmp for other-tier tasks
# (nothing there) and the owner's own workspace for team-tier tasks. It does not
# widen what the sandbox may read, which the workspace boundary and the sandbox
# profile decide.
export GEMINI_CLI_TRUST_WORKSPACE=true

# The sandbox must actually engage. On macOS --sandbox is seatbelt, always present.
# Elsewhere it needs Docker or Podman, and without either this refuses rather than
# letting a non-owner task run unconfined.
if [[ "$(uname -s)" == "Darwin" ]]; then
  export SEATBELT_PROFILE="${SEATBELT_PROFILE:-restrictive-open}"
elif ! command -v docker >/dev/null 2>&1 && ! command -v podman >/dev/null 2>&1; then
  echo "gemini-sandbox: --sandbox needs docker or podman on this platform and neither is on PATH; refusing to run unconfined" >&2
  exit 2
fi

# stream-json, not json, plus a heartbeat. The bounded runner that wraps this kills a
# command that emits nothing for 45 seconds, on the reasoning that a working sandbox
# streams as it goes. In plain json mode the CLI prints one object at the end and
# nothing before, so a healthy run looked wedged and was killed: 8 of 36 scenarios came
# back as "Sandbox unavailable (gemini exit 125)", the watchdog's own kill code.
# Streaming gives the guard real events, and the gaps between events can still reach
# tens of seconds while the model thinks, so the filter below also writes a heartbeat
# to stderr whenever nothing has arrived for GEMINI_SANDBOX_HEARTBEAT seconds (10 by
# default). For this runtime the stall guard therefore cannot fire on a quiet but live
# process; the bounded runner's --max cap is the backstop, and the docs say so.
cmd=(gemini --prompt "$PROMPT" --approval-mode plan --sandbox --output-format stream-json)
[[ -n "$MODEL" ]] && cmd+=(--model "$MODEL")

set +e
raw="$(cd "$WORKDIR" && "${cmd[@]}" < /dev/null | GEMINI_SANDBOX_HEARTBEAT="${GEMINI_SANDBOX_HEARTBEAT:-10}" python3 -u -c '
import json, os, select, sys, time
parts, status, err = [], None, None
beat = float(os.environ.get("GEMINI_SANDBOX_HEARTBEAT") or 10)
waited = 0.0
while True:
    ready, _, _ = select.select([sys.stdin], [], [], beat)
    if not ready:
        waited += beat
        sys.stderr.write("gemini-sandbox: waiting, %.0fs without an event\n" % waited)
        sys.stderr.flush()
        continue
    line = sys.stdin.readline()
    if not line:
        break
    waited = 0.0
    line = line.strip()
    if not line:
        continue
    # One progress line per event on stderr: this is what the stall watchdog watches.
    try:
        ev = json.loads(line)
    except Exception:
        sys.stderr.write("gemini-sandbox: unparsable event\n")
        continue
    kind = ev.get("type")
    sys.stderr.write("gemini-sandbox: %s\n" % kind)
    sys.stderr.flush()
    if kind == "message" and ev.get("role") == "assistant":
        parts.append(str(ev.get("content") or ""))
    elif kind == "error":
        err = ev.get("error") or ev
    elif kind == "result":
        status = ev.get("status")
        if ev.get("error"):
            err = ev["error"]
if err is not None:
    sys.stderr.write("gemini-sandbox: gemini reported an error: %s\n" % (err,))
    sys.exit(1)
if status not in (None, "success"):
    sys.stderr.write("gemini-sandbox: gemini finished with status %s\n" % (status,))
    sys.exit(1)
sys.stdout.write("".join(parts))
')"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  echo "gemini-sandbox: gemini exited $rc" >&2
  exit "$rc"
fi

response="$raw"

if [[ -z "${response//[[:space:]]/}" ]]; then
  echo "gemini-sandbox: gemini exited 0 with no output" >&2
  exit 0
fi
printf '%s\n' "$response" > "$OUT"
