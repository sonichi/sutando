#!/usr/bin/env bash
# Mutation check for the alert-only standby: each named mutant of the supervisor (or of
# result_publish's exit-3 path) must make its suite fail. Mutants are applied to a scratch copy,
# never to this checkout. Exit 0 only if every mutant applies and is killed.
# Run: bash scripts/mutation-check-standby-alert.sh
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR="$(mktemp -d "${TMPDIR:-/tmp}/sut-alert-mut.XXXXXX")"
trap 'rm -rf "$MIRROR"' EXIT
mkdir -p "$MIRROR/tests/skills/worker-pool" "$MIRROR/packages/ag2-sparrow" "$MIRROR/skills"
cp -R "$REPO/src" "$REPO/scripts" "$MIRROR/"
cp -R "$REPO/skills/worker-pool" "$REPO/skills/task-workstream-grouping" "$REPO/skills/schedule-crons" "$MIRROR/skills/"
cp -R "$REPO/tests/_helpers" "$MIRROR/tests/"
cp "$REPO/tests/task-notifier-supervisor-alert-only.test.sh" "$REPO/tests/result-publish.test.py" \
   "$REPO/tests/codex-core-launcher.test.py" "$MIRROR/tests/"
cp "$REPO/tests/skills/worker-pool/pool-remedy-timer-old-alert-settings.test.py" "$MIRROR/tests/skills/worker-pool/"
cp -R "$REPO/packages/ag2-sparrow/ag2_sparrow" "$MIRROR/packages/ag2-sparrow/"
SUP="src/agent/codex/cli/task-notifier-supervisor.sh"
PUB="src/result_publish.py"
CODEX="src/agent/codex/cli/start-cli.sh"
ENSURE="skills/worker-pool/scripts/worker-watcher-supervisor.sh"
ALERT=(env SUT_ALERT_TEST_SKIP_TMUX=1 bash tests/task-notifier-supervisor-alert-only.test.sh)
PUBT=(python3 tests/result-publish.test.py PublisherFailurePaths)
OLDT=(python3 tests/skills/worker-pool/pool-remedy-timer-old-alert-settings.test.py)
CODEXT=(python3 tests/codex-core-launcher.test.py CodexCoreLauncherTests.test_the_alert_only_knobs_are_inert_on_the_codex_standby)
bad=0

run_in_mirror() { (cd "$MIRROR" && "$@") >"$MIRROR/.out" 2>&1; }

mutant() {  # mutant <name> <file> <exact text> <replacement> <suite...>
  local name="$1" file="$MIRROR/$2" old="$3" new="$4"; shift 4
  cp "$file" "$file.orig"
  if ! OLD="$old" NEW="$new" python3 -c '
import os, sys
p = sys.argv[1]; s = open(p).read(); o = os.environ["OLD"]
if s.count(o) != 1:
    sys.exit(1)
open(p, "w").write(s.replace(o, os.environ["NEW"]))' "$file"; then
    echo "  NOT APPLIED $name (its text is no longer in $2)"; bad=1; mv "$file.orig" "$file"; return
  fi
  if run_in_mirror "$@"; then
    echo "  SURVIVED $name"; bad=1
  else
    echo "  killed   $name ($(grep -m1 -E 'FAIL|Error' "$MIRROR/.out" | cut -c1-110))"
  fi
  mv "$file.orig" "$file"
}

echo "alert-only standby mutation check:"
run_in_mirror "${ALERT[@]}" && run_in_mirror "${PUBT[@]}" && run_in_mirror "${OLDT[@]}" && run_in_mirror "${CODEXT[@]}" \
  || { echo "  baseline FAILED in the mirror; nothing to measure"; cat "$MIRROR/.out"; exit 1; }

# The retry: only a DM that was certainly not published (exit 1 or 2, or no results/) is retried,
# with a bounded back-off; every other outcome, a kill included, counts as sent.
mutant retry-unknown-outcome "$SUP" '  [ "$rc" != 1 ] && [ "$rc" != 2 ]' '  [ "$rc" = 0 ] || [ "$rc" = 3 ]' "${ALERT[@]}"
mutant retry-exit3 "$SUP" '  [ "$rc" != 1 ] && [ "$rc" != 2 ]' '  [ "$rc" != 1 ] && [ "$rc" != 2 ] && [ "$rc" != 3 ]' "${ALERT[@]}"
mutant retry-exit1-is-sent "$SUP" '  [ "$rc" != 1 ] && [ "$rc" != 2 ]' '  [ "$rc" != 2 ]' "${ALERT[@]}"
mutant retry-exit2-is-sent "$SUP" '  [ "$rc" != 1 ] && [ "$rc" != 2 ]' '  [ "$rc" != 1 ]' "${ALERT[@]}"
mutant unknown-not-logged "$SUP" '    *) supervisor_log "the unwatched-inbox alert'"'"'s publisher ended with exit $rc, outcome unknown; counted as sent, not resent" ;;' '    *) ;;' "${ALERT[@]}"
mutant retry-no-results-dir-is-sent "$SUP" '    supervisor_log "no results folder at the seat'"'"'s workspace; the unwatched-inbox alert will be retried"' '    rc=0' "${ALERT[@]}"
mutant retry-failed-alert-latches "$SUP" '            if unwatched_alert $((now - since)); then' '            if unwatched_alert $((now - since)) || true; then' "${ALERT[@]}"
mutant retry-failed-reminder-latches "$SUP" '            fail_rem=$((fail_rem + 1))' '            fail_rem=$((fail_rem + 1)); last="$now"' "${ALERT[@]}"
mutant backoff-none "$SUP" '  [ "$w" -gt 300 ] && w=300' '  w=0' "${ALERT[@]}"
mutant backoff-uncapped "$SUP" '[ "$i" -lt "$1" ] && [ "$w" -lt 300 ]' '[ "$i" -lt "$1" ]' "${ALERT[@]}"
mutant backoff-alert-hold-ignored "$SUP" '-ge "$ALERT_GRACE" ] && [ "$now" -ge "$hold_alert" ]' '-ge "$ALERT_GRACE" ]' "${ALERT[@]}"
mutant backoff-reminder-hold-ignored "$SUP" ' && [ "$now" -ge "$hold_rem" ]' '' "${ALERT[@]}"
# The macOS notification: once per outage, on the first attempt.
mutant notify-every-attempt "$SUP" '  if [ "$alert_notified" = "0" ]; then' '  if true; then' "${ALERT[@]}"
mutant notify-never-reset "$SUP" '        alerted=0
        alert_notified=0' '        alerted=0' "${ALERT[@]}"
# Validation of the settings.
mutant validate-no-upper-bound "$SUP" '[ $((10#$v)) -le "$4" ]' 'true' "${ALERT[@]}"
mutant validate-zero-grace "$SUP" 'SUTANDO_NOTIFIER_ALERT_GRACE 300 1 86400' 'SUTANDO_NOTIFIER_ALERT_GRACE 300 0 86400' "${ALERT[@]}"
mutant validate-octal "$SUP" "    printf '%s' \"\$((10#\$v))\"" "    printf '%s' \"\$v\"" "${ALERT[@]}"
mutant validate-keeps-invalid "$SUP" '; using $2"
  printf '"'"'%s'"'"' "$2"' '; using $2"
  printf '"'"'%s'"'"' "$v"' "${ALERT[@]}"
mutant validate-silent "$SUP" '  supervisor_log "ignoring $1=${v:0:40}: not a whole number of seconds from $3 to $4; using $2"' '  :' "${ALERT[@]}"
mutant default-reminder-on "$SUP" 'SUTANDO_NOTIFIER_ALERT_REPEAT 0 0 86400' 'SUTANDO_NOTIFIER_ALERT_REPEAT 1800 0 86400' "${ALERT[@]}"
# The message and its file.
mutant wording-promises "$SUP" ' Restarting that session brings its watcher back."' ' Restarting that session brings its watcher back. This is the last message about it."' "${ALERT[@]}"
mutant filename-shared "$SUP" '-$$-${alert_seq}.txt"' '-$$.txt"' "${ALERT[@]}"
# result_publish: a failure after the rename is exit 3, one before it never is.
mutant publish-no-unconfirmed-error "$PUB" '            raise PublishedUnconfirmed(exc.errno, f"published {target}, but syncing its directory failed: {exc}") from exc' '            raise' "${PUBT[@]}"
mutant publish-unconfirmed-not-exit3 "$PUB" '        return EXIT_PUBLISHED_UNCONFIRMED' '        raise' "${PUBT[@]}"
mutant publish-any-oserror-is-exit3 "$PUB" '    except PublishedUnconfirmed as exc:' '    except OSError as exc:' "${PUBT[@]}"

# The retired pile-up setting is read by nothing; the Codex standby takes no alert setting at all;
# settings an earlier timer install saved stay in force until a reinstall writes the job without them.
mutant pileup-as-repeat-default "$SUP" 'ALERT_REPEAT="$(alert_setting SUTANDO_NOTIFIER_ALERT_REPEAT 0 0 86400)"' 'ALERT_REPEAT="$(alert_setting SUTANDO_NOTIFIER_ALERT_REPEAT "${SUTANDO_NOTIFIER_ALERT_PILEUP:-0}" 0 86400)"' "${ALERT[@]}"
mutant pileup-as-grace-default "$SUP" 'ALERT_GRACE="$(alert_setting SUTANDO_NOTIFIER_ALERT_GRACE 300 1 86400)"' 'ALERT_GRACE="$(alert_setting SUTANDO_NOTIFIER_ALERT_GRACE "${SUTANDO_NOTIFIER_ALERT_PILEUP:-300}" 1 86400)"' "${ALERT[@]}"
mutant pileup-forwarded-by-worker-ensure "$ENSURE" 'SUTANDO_NOTIFIER_GRACE_PERIOD SUTANDO_NOTIFIER_ROLE_POLL; do' 'SUTANDO_NOTIFIER_GRACE_PERIOD SUTANDO_NOTIFIER_ROLE_POLL SUTANDO_NOTIFIER_ALERT_PILEUP; do' "${OLDT[@]}"
mutant saved-settings-dropped-by-worker-ensure "$ENSURE" '-e "SUTANDO_NOTIFIER_ALERT_REPEAT=${SUTANDO_NOTIFIER_ALERT_REPEAT:-}"' '-e "SUTANDO_NOTIFIER_ALERT_REPEAT="' "${OLDT[@]}"
mutant spawn-rewrites-the-timer skills/worker-pool/scripts/spawn_worker.py '            if st.get("loaded") and (want is None or st.get("stop_interrupt") == want):' '            if False:' "${OLDT[@]}"
mutant reinstall-saves-alert-settings skills/worker-pool/scripts/pool_remedy_timer.py '"EnvironmentVariables": {"PATH": path, **({STOP_INTERRUPT: stop_interrupt} if stop_interrupt else {})},' '"EnvironmentVariables": {"PATH": path, **({STOP_INTERRUPT: stop_interrupt} if stop_interrupt else {}), **{k: v for k, v in os.environ.items() if k.startswith("SUTANDO_NOTIFIER_ALERT_")}},' "${OLDT[@]}"
mutant codex-forwards-alert-settings "$CODEX" '  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_STANDBY_MODE=type")' '  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_STANDBY_MODE=type" -e "SUTANDO_NOTIFIER_ALERT_REPEAT=${SUTANDO_NOTIFIER_ALERT_REPEAT:-}")' "${CODEXT[@]}"
mutant codex-identity-has-alert-settings "$CODEX" '-mtype"' '-mtype-r${SUTANDO_NOTIFIER_ALERT_REPEAT:-}"' "${CODEXT[@]}"
mutant publish-cli-exit3-lost "$PUB" '        return EXIT_PUBLISHED_UNCONFIRMED' '        return 1' "${PUBT[@]}"

if [ "$bad" = 0 ]; then echo "  ok  every mutant killed"; else echo "  FAILED: a mutant survived or no longer applies"; fi
exit "$bad"
