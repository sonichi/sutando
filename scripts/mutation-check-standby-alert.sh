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
cp -R "$REPO/skills/worker-pool" "$MIRROR/skills/"
cp -R "$REPO/tests/_helpers" "$MIRROR/tests/"
cp "$REPO/tests/task-notifier-supervisor-alert-only.test.sh" "$REPO/tests/result-publish.test.py" \
   "$REPO/tests/standby-alert-once.test.py" "$MIRROR/tests/"
cp "$REPO/tests/skills/worker-pool/pool-remedy-timer-retired-env.test.py" "$MIRROR/tests/skills/worker-pool/"
cp -R "$REPO/packages/ag2-sparrow/ag2_sparrow" "$MIRROR/packages/ag2-sparrow/"
SUP="src/agent/codex/cli/task-notifier-supervisor.sh"
ONCE="src/standby_alert_once.py"
PUB="src/result_publish.py"
TIMER="skills/worker-pool/scripts/pool_remedy_timer.py"
ALERT=(env SUT_ALERT_TEST_SKIP_TMUX=1 bash tests/task-notifier-supervisor-alert-only.test.sh)
ONCET=(python3 tests/standby-alert-once.test.py)
PUBT=(python3 tests/result-publish.test.py PublisherFailurePaths)
TIMERT=(python3 tests/skills/worker-pool/pool-remedy-timer-retired-env.test.py)
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
baseline() { run_in_mirror "$@" || { echo "  baseline FAILED in the mirror ($*); nothing to measure"; cat "$MIRROR/.out"; exit 1; }; }
baseline "${ALERT[@]}"; baseline "${ONCET[@]}"; baseline "${PUBT[@]}"; baseline "${TIMERT[@]}"

# One alert per outage: "already published" by the result-side owners, under one lock.
mutant once-no-dedupe "$ONCE" '        if already_published(results, stem):
            return ALREADY' '        if False:
            return ALREADY' "${ALERT[@]}"
mutant once-live-copy-only "$ONCE" '    return find_ready_result(results, stem) is not None or bool(undelivered_quarantine.find_quarantined(results, stem))' '    return (results / f"{stem}.txt").exists()' "${ALERT[@]}"
mutant once-quarantine-ignored "$ONCE" ' or bool(undelivered_quarantine.find_quarantined(results, stem))' '' "${ONCET[@]}"
mutant once-no-lock "$ONCE" '    with locked_file(_state(workspace) / f"{stem}.lock"):
        if already_published' '    if True:
        if already_published' "${ONCET[@]}"
mutant once-unconfirmed-is-failure "$ONCE" '            return UNCONFIRMED' '            return FAILED' "${ONCET[@]}"
mutant once-no-results-is-published "$ONCE" '    if not results.is_dir():
        return FAILED' '    if not results.is_dir():
        return PUBLISHED' "${ALERT[@]}"
mutant name-reminder-slot-dropped "$SUP" ' reminder "$due"; then' ' reminder 1; then' "${ALERT[@]}"
mutant name-per-episode-dropped "$ONCE" '-{int(episode)}" + (f"-r{int(slot)}"' '" + (f"-r{int(slot)}"' "${ALERT[@]}"
# The outage record: shared, refreshed, ended, and over once stale.
mutant episode-not-durable "$ONCE" '        if since is None or now - seen > stale_after or now < since:' '        if True:' "${ALERT[@]}"
mutant episode-never-stale "$ONCE" ' or now - seen > stale_after' '' "${ONCET[@]}"
mutant episode-not-ended "$SUP" '        { [ -n "$since" ] || [ "$ended" = 0 ]; } && episode_end' '        :' "${ALERT[@]}"
mutant notify-claim-kept-after-end "$ONCE" '            if p.suffix != ".lock":' '            if p.suffix != ".lock" and "notified" not in p.name:' "${ONCET[@]}"
mutant notify-every-attempt "$SUP" '  if [ -z "$kind" ] && alert_once claim-notify --inbox "$TASKS_DIR" --episode "$episode" >/dev/null 2>&1; then' '  if [ -z "$kind" ]; then' "${ALERT[@]}"
mutant notify-claim-not-exclusive "$ONCE" '        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))' '        os.close(os.open(marker, os.O_CREAT | os.O_WRONLY, 0o600))' "${ONCET[@]}"
# The retry: only what was not published is retried, with a bounded back-off.
mutant retry-exit3-is-a-failure "$SUP" '[ "$rc" = 0 ] || [ "$rc" = 3 ]' '[ "$rc" = 0 ]' "${ALERT[@]}"
mutant retry-exit1-is-sent "$SUP" '[ "$rc" = 0 ] || [ "$rc" = 3 ]' '[ "$rc" = 0 ] || [ "$rc" = 3 ] || [ "$rc" = 1 ]' "${ALERT[@]}"
mutant retry-already-is-a-failure "$SUP" '    4) supervisor_log "the unwatched-inbox alert $stem was already published; not resent"; return 0 ;;' '' "${ALERT[@]}"
mutant retry-failed-alert-latches "$SUP" '            if unwatched_alert $((now - since)) "$since"; then' '            if unwatched_alert $((now - since)) "$since" || true; then' "${ALERT[@]}"
mutant retry-failed-reminder-latches "$SUP" '              fail_rem=$((fail_rem + 1))' '              fail_rem=$((fail_rem + 1)); slot="$due"' "${ALERT[@]}"
mutant backoff-none "$SUP" '  [ "$w" -gt 300 ] && w=300' '  w=0' "${ALERT[@]}"
mutant backoff-uncapped "$SUP" '[ "$i" -lt "$1" ] && [ "$w" -lt 300 ]' '[ "$i" -lt "$1" ]' "${ALERT[@]}"
mutant backoff-alert-hold-ignored "$SUP" '-ge "$ALERT_GRACE" ] && [ "$now" -ge "$hold_alert" ]' '-ge "$ALERT_GRACE" ]' "${ALERT[@]}"
mutant backoff-reminder-hold-ignored "$SUP" ' && [ "$now" -ge "$hold_rem" ]' '' "${ALERT[@]}"
# Validation of the settings.
mutant validate-no-upper-bound "$SUP" '[ $((10#$v)) -le "$4" ]' 'true' "${ALERT[@]}"
mutant validate-zero-grace "$SUP" 'SUTANDO_NOTIFIER_ALERT_GRACE 300 1 86400' 'SUTANDO_NOTIFIER_ALERT_GRACE 300 0 86400' "${ALERT[@]}"
mutant validate-octal "$SUP" "    printf '%s' \"\$((10#\$v))\"" "    printf '%s' \"\$v\"" "${ALERT[@]}"
mutant validate-keeps-invalid "$SUP" '; using $2"
  printf '"'"'%s'"'"' "$2"' '; using $2"
  printf '"'"'%s'"'"' "$v"' "${ALERT[@]}"
mutant validate-silent "$SUP" '  supervisor_log "ignoring $1=${v:0:40}: not a whole number of seconds from $3 to $4; using $2"' '  :' "${ALERT[@]}"
mutant default-reminder-on "$SUP" 'SUTANDO_NOTIFIER_ALERT_REPEAT 0 0 86400' 'SUTANDO_NOTIFIER_ALERT_REPEAT 1800 0 86400' "${ALERT[@]}"
mutant wording-promises "$SUP" ' Restarting that session brings its watcher back."' ' Restarting that session brings its watcher back. This is the last message about it."' "${ALERT[@]}"
# result_publish: a failure after the rename is exit 3, one before it never is.
mutant publish-no-unconfirmed-error "$PUB" '            raise PublishedUnconfirmed(exc.errno, f"published {target}, but syncing its directory failed: {exc}") from exc' '            raise' "${PUBT[@]}"
mutant publish-unconfirmed-not-exit3 "$PUB" '        return EXIT_PUBLISHED_UNCONFIRMED' '        raise' "${PUBT[@]}"
mutant publish-any-oserror-is-exit3 "$PUB" '    except PublishedUnconfirmed as exc:' '    except OSError as exc:' "${PUBT[@]}"
# The remedy timer's one-time removal of the retired alert settings.
mutant timer-keys-kept-in-plist "$TIMER" '        job["EnvironmentVariables"] = {k: v for k, v in env.items() if k not in RETIRED_ENV}' '        job["EnvironmentVariables"] = dict(env)' "${TIMERT[@]}"
mutant timer-other-entries-dropped "$TIMER" '{k: v for k, v in env.items() if k not in RETIRED_ENV}' '{k: v for k, v in env.items() if k == "PATH"}' "${TIMERT[@]}"
mutant timer-loaded-job-not-checked "$TIMER" '    in_loaded = _loaded_retired_keys(target, runner)' '    in_loaded = [] if _is_loaded(target, runner) else None' "${TIMERT[@]}"
mutant timer-no-reload "$TIMER" '    if in_job:
        # Booting' '    if True:
        # Booting' "${TIMERT[@]}"
mutant timer-reloads-inside-its-job "$TIMER" '    if in_job:
        # Booting' '    if False:
        # Booting' "${TIMERT[@]}"
mutant timer-no-plist-rollback "$TIMER" '            tmp.write_bytes(old_bytes)
            os.replace(tmp, dest)
        except OSError as restore_error:
            errors.append(str(restore_error))
        if not _is_loaded' '            pass
        except OSError as restore_error:
            errors.append(str(restore_error))
        if not _is_loaded' "${TIMERT[@]}"
mutant timer-no-job-rollback "$TIMER" '        if not _is_loaded(target, runner):
            restored' '        if False:
            restored' "${TIMERT[@]}"
mutant timer-legacy-skipped "$TIMER" '        targets.append((LABEL, legacy_plist_path(launch_agents)))' '        pass' "${TIMERT[@]}"
mutant timer-legacy-any-workspace "$TIMER" '    if legacy.get("workspace") and Path(legacy["workspace"]).resolve() == Path(workspace).resolve():' '    if legacy.get("installed"):' "${TIMERT[@]}"
mutant timer-not-run-on-spawn skills/worker-pool/scripts/spawn_worker.py '            for m in prt.migrate_retired_env(workspace, launch_agents=launch_agents, runner=runner):' '            for m in []:' "${TIMERT[@]}"

if [ "$bad" = 0 ]; then echo "  ok  every mutant killed"; else echo "  FAILED: a mutant survived or no longer applies"; fi
exit "$bad"
