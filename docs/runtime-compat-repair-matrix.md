# Core CLI repair matrix

Sutando runs on Claude Code or the Codex CLI. This page lists every state the
CLI can be in, how Sutando detects it, and how to get from that state back to
a running core.

The code is `src/runtime_compat.py`. Both launchers call it before they replace
a core, through `src/agent/runtime-compat-gate.sh`. Startup calls the same gate
before any service starts (`src/startup-runtime.sh`), and its own prerequisite
check and `src/verify-setup.sh` ask `runtime_compat.py which`, so a managed
install off `PATH` counts everywhere
(`test_codex_only_managed_startup_passes_its_cli_checks`,
`test_claude_only_managed_startup_passes_its_cli_checks`,
`test_verify_setup_finds_the_managed_codex_and_its_login`).
`health-check.py` reports the same verdict as `core-cli-compat`.

## Check your install

```bash
python3 src/runtime_compat.py doctor            # both CLIs, with the fix for each
python3 src/runtime_compat.py doctor --json     # the same, for tools
python3 src/runtime_compat.py which --runtime codex --enforce-auth   # the binary a launch would use
```

## How Sutando picks a binary

Sutando looks in this order:

1. every `claude` or `codex` on `PATH`, in `PATH` order;
2. the Sutando-managed install (see below);
3. known install locations: `$HOME/.local/bin`, `$HOME/.claude/local` (Claude),
   `$HOME/.npm-global/bin` (or `$CODEX_INSTALL_BIN`), and the Homebrew bin folders.

Known locations are used only when no `PATH` or managed install works. A
working CLI on your `PATH` is never replaced by one you did not put there.

Each one is launched with `--version` and `--help`. The first one that is
`ok` or `newer-untested` is used. So when the first `claude` on your `PATH`
works, Sutando launches exactly the command it launched before. When it does
not, Sutando launches the next working one by its full path and says so.

## Asking for consent

Sutando never installs anything without your consent. You can give it in
four ways:

- `--yes` on the command line;
- `SUTANDO_RUNTIME_REPAIR_CONSENT=1`;
- "y" at the terminal prompt;
- off a terminal (a core started by the desktop app or a supervisor), your
  reply to Sutando's DM.

Off a terminal, Sutando sends you **one DM per incident**, however often the
launch is retried. It names the problem and what the fix changes. Reply with
the whole message `install compatible claude` (or `install compatible codex`)
to let Sutando install it. Reply `retry now` after you fixed it yourself or
logged in. Sutando records each reply before it acts, then marks it applied,
answered and archived as each step lands, so a crash replays only the steps that
did not finish. An answer already written, even one a bridge has delivered and
archived, is never sent again. A reply that arrives after the launch stopped
waiting is handled by the next launch
(`test_crash_after_*`, `test_answer_delivered_and_archived_by_the_bridge_is_not_resent`,
`test_reply_after_the_wait_window_is_handled_on_the_next_launch`).
Sutando acts on each reply once, answers it in the same
conversation, and the same launch continues. The DM and reply path is the
engine half that the desktop app's repair card calls.

## Which installer the assisted fix uses

| Host has | Claude Code | Codex | What it changes |
|---|---|---|---|
| `npm` | `npm install -g --prefix <managed>/<cli>/... <package>@<major>` | same | only the Sutando-owned folder and the npm cache |
| no `npm`, `curl` and `bash` | the official native installer: `curl -fsSL https://claude.ai/install.sh \| bash -s stable` (no Node needed) | see the next rows | the installer's own folders, `$HOME/.local/bin/claude` and `$HOME/.local/share/claude` (it has no target-folder option), plus one link in the managed folder |
| no `npm`, Homebrew | as above | `brew install --cask codex` | Homebrew's `codex` cask, plus one link in the managed folder |
| neither | user step: install `curl`, then the native installer | user step: `curl -fsSL https://chatgpt.com/codex/install.sh \| sh`, or `brew install --cask codex`, or Node.js 22+ and `npm install -g @openai/codex` | nothing until you run it |

The consent prompt and the DM both name exactly what the chosen installer
changes.

## The matrix

"Assisted fix" is `python3 src/runtime_compat.py repair --runtime <claude|codex> --yes`,
or a consent given in one of the ways above.

| Class | Detection | User-executable fix | Assisted fix | Verification | Test |
|---|---|---|---|---|---|
| `ok` | `--version` ≥ minimum, every required flag in `--help`, same major as tested | none | none | `doctor` shows `* [ok]` | `test_discovery_reads_version_and_capabilities` |
| `missing` | no binary on `PATH`, in the managed folder, or in a known location. The Codex launcher runs this check before any presence check of its own, so a managed or known install off `PATH` is found, and a missing one can still be installed with consent | Claude: `curl -fsSL https://claude.ai/install.sh \| bash`. Codex: `npm install -g --prefix ~/.local @openai/codex` | yes | `doctor`, then the launch continues | `test_missing_when_nothing_installed`, `test_missing_cli_stops_with_fix_and_resume_command`, `test_no_codex_anywhere_with_consent_installs_and_launches`, `test_working_managed_codex_off_path_is_found_and_launched` |
| `missing`, no Node | as above, and no `npm` on `PATH` | Claude: the native installer. Codex: the standalone installer or Homebrew | Claude: native installer. Codex: Homebrew when present | `doctor` | `test_claude_native_install_without_node`, `test_codex_without_npm_uses_homebrew_when_present`, `test_no_installer_at_all_gives_a_manual_step` |
| `too-old` | version below the minimum (Claude 2.0.0, Codex 0.40.0) | Claude: `<path> update`. npm install: `npm install -g --prefix <its prefix> <package>@<major>` | yes | `doctor` | `test_too_old`, `test_claude_minimum_supported_version_is_ok_and_one_below_is_too_old`, `test_codex_minimum_supported_version_is_ok_and_one_below_is_too_old`, `test_plan_too_old_claude_uses_its_own_updater`, `test_plan_too_old_npm_install_updates_that_prefix` |
| `known-bad` | version is on the spec's known-bad list (empty today) | same as `too-old` | yes; an install that lands on a known-bad version is refused and the old one kept | `doctor` | `test_known_bad`, `test_codex_known_bad`, `test_known_bad_first_on_path_is_skipped_for_a_working_second`, `test_known_bad_alone_blocks_and_plans_a_consented_repair`, `test_repair_that_lands_on_a_known_bad_version_is_not_success` |
| `incompatible` | version reads, but a flag the launcher passes is not in `--help` | same as `too-old` | yes | `doctor` | `test_incompatible_when_flag_missing`, `test_help_failure_is_incompatible` |
| `newer-untested` | major version above the tested one; flags present | none needed; to pin: `npm install -g --prefix ~/.local <package>@<tested major>` | not offered | the core starts with a warning | `test_newer_untested_is_usable_with_warning`, `test_codex_newer_untested_is_usable_with_warning` |
| `broken` | not executable, a dangling link, a partial file, `--version` fails or hangs | reinstall with the `missing` command | yes | `doctor` | `test_broken_*` (7 cases) |
| `broken` (no Node.js) | the CLI exits with `env: node: No such file` | `brew install node` (Node.js 22+), or reinstall the desktop app | not offered | `doctor` | `test_plan_broken_node_missing_points_at_node` |
| several installs | more than one binary found | none; the first working one is used | none | `doctor` marks the chosen one with `*` | `test_multiple_path_installs_first_usable_wins`, `test_codex_multiple_path_installs_first_usable_wins`, `test_multiple_path_installs_keep_path_order_when_both_work`, `test_every_path_install_unusable_names_the_first_and_lists_all`, `test_fallback_install_is_launched_by_path`, `test_two_codex_on_path_the_too_old_first_is_skipped_for_the_second` |
| broken on `PATH`, working elsewhere | the `PATH` binary fails, a known location works | none; the working one is used | none | `doctor` | `test_known_location_used_when_path_install_is_broken`, `test_working_path_install_is_never_overridden_by_a_known_location` |
| logged out (Codex) | `codex login status` exits non-zero | `codex login` (with `CODEX_HOME=<home>` when set), then reply `retry now` off a terminal | not offered | the launch continues after login | `test_codex_logged_out_points_to_login`, `test_codex_logged_out_stops_then_login_lets_it_start`, `test_codex_login_dm_then_retry_now`, `test_codex_expired_login_stops_with_the_login_step_then_retry_continues` |
| logged out (Claude) | `src/auth_preflight.py` finds no account or credentials. The existing startup auth preflight already stops a boot here | run `claude` in Terminal and complete `/login` | not offered | the launch continues; the boot gate re-checks | `test_claude_auth_uses_auth_preflight`, `test_logged_out_advisory_continues` |

## What can go wrong during an assisted fix

| Failure | Detection | What Sutando does | What you do | Test |
|---|---|---|---|---|
| disk full | free space under the managed folder is below 512 MB, checked before any download; or the installer reports `ENOSPC` | stops at once, no retry, old version kept | free the space it names, then rerun the assisted fix | `test_disk_full_checked_before_download`, `test_disk_full_during_download_is_not_retried`, `test_disk_full_checked_before_the_native_installer`, `test_disk_full_during_the_native_install_is_not_retried`, `test_disk_full_reply_is_answered_then_a_later_reply_installs_and_continues` |
| blocked download | installer output names a network, proxy, certificate or `curl` connection error | retries 3 times with 2 s and 4 s backoff, old version kept | check network, proxy or firewall; npm must reach `registry.npmjs.org` (the native installers their vendor's site); rerun | `test_blocked_download_retries_with_backoff_then_guides`, `test_blocked_download_recovers_on_retry`, `test_native_install_blocked_download`, `test_codex_homebrew_blocked_download_retries_then_guides`, `test_blocked_download_reply_keeps_the_old_version_then_a_later_reply_installs` |
| interrupted update | npm killed mid-install, or a leftover `.staging-*` folder whose process is gone | the half-written install never replaces the old one; the leftover is reported and removed by the next repair | nothing | `test_interrupted_update_keeps_old_version`, `test_leftover_staging_is_reported_then_cleaned` |
| new binary will not run | the staged or switched binary fails the same `--version` and `--help` probe | reports failure; the link is switched back to the previous version | use the manual command | `test_unusable_download_keeps_old_version`, `test_failed_post_swap_verify_restores_the_previous_link`, `test_link_that_will_not_launch_is_not_success`, `test_native_install_that_does_not_run_is_not_success` |
| same version reinstalled | that version's folder already exists | a working copy is reused untouched; a broken one is replaced, never the one in use | nothing | `test_reinstall_of_same_version_keeps_the_live_target`, `test_reinstall_replaces_a_broken_copy_of_the_same_version` |
| fault while publishing | an error or crash after staging, after the old folder is moved aside, while renaming the new folder in, while swapping the link, or in the live probe | a journal (`<root>/<cli>/.swap-*.json`) is written first. Any failure puts the old folder and the link back; the backup is deleted only after the new version runs. A process killed midway is undone (or, if it had committed, finished) by the next run | nothing | `test_fault_after_staging`, `test_fault_after_the_old_folder_is_moved_aside`, `test_fault_renaming_the_new_folder_into_place`, `test_fault_after_the_new_folder_is_in_place`, `test_fault_swapping_the_link`, `test_fault_in_the_live_probe`, `test_live_probe_fails_restores_old_folder_and_link`, `test_process_killed_mid_swap_is_undone_by_the_next_run`, `test_committed_swap_left_by_a_killed_process_is_finished` |
| killed during a rollback | the rollback itself is killed at any step: after it records the rollback phase, after the link is restored, after the new folder is removed, or after the old folder is back but before the journal is deleted | the journal (atomic, fsync'd) records the phase and the inode of the new folder. A repeated rollback removes a folder only when it is the new install, so the restored original is never deleted, however often recovery runs | nothing | `test_owner_repro_undo_killed_before_the_journal_delete_then_two_recoveries`, `test_undo_killed_after_the_phase_is_written_then_recovered_twice`, `test_undo_killed_after_the_link_is_restored_then_recovered_twice`, `test_undo_killed_after_the_new_folder_is_removed_then_recovered_twice`, `test_undo_killed_after_the_old_folder_is_restored_then_recovered_twice` |
| two repairs at once | two launchers repair the same CLI | a lock on `<root>/<cli>` runs them one at a time; the second one finds the first one's result and uses it | nothing | `test_two_racing_repairs_install_once_and_both_see_the_result`, `test_concurrent_installs_are_serialized` |
| no installer | no `npm`, and no native installer route for this CLI | reports it with the user step | follow the "neither" row above | `test_npm_missing` |

Success is reported only after the switched binary has been launched with
`--version` and `--help` (`test_assisted_install_launches_before_reporting_success`).

## Resuming the same journey

The launcher does not restart onboarding after a fix:

- A restart with an unusable CLI stops before it touches the running core
  (`test_restart_with_unusable_cli_kills_nothing`).
- The launcher waits up to 10 minutes for the fix, checking every 5 seconds,
  then continues the same launch. At a terminal the fix is your own command or
  login (`test_waits_for_the_user_fix_then_continues`). Off a terminal it is
  your reply to the DM (`test_reply_during_the_wait_continues_the_same_launch`).
- A reply that arrives after the wait is picked up by the next launch attempt,
  which then continues
  (`test_off_tty_broken_cli_asks_the_owner_then_their_reply_repairs_and_launches`,
  through the real Codex launcher).
- With consent, the assisted fix runs and the same launch continues
  (`test_consent_runs_the_repair_and_the_launch_continues`; through the real Claude
  launcher, `test_consent_repair_launches_the_managed_claude`).
- Otherwise it prints the fix and the exact command to run again:
  `bash src/agent/start-cli.sh`.

## Where the managed install lives

`$SUTANDO_RUNTIMES_DIR`, else `$XDG_DATA_HOME/sutando/runtimes`, else
`$HOME/.local/share/sutando/runtimes`. Layout:

```
<root>/bin/<cli>                   link to the version in use (switched atomically)
<root>/<cli>/versions/<version>/   one npm prefix per installed version
<root>/<cli>/.staging-<pid>-<ts>/  an install in progress
<root>/<cli>/.lock                 held while a repair runs
```

Older versions stay in place, so going back is one link away. The incident
record for the owner DM is `<workspace>/state/runtime-compat-<cli>.json`.

## Settings

| Variable | Effect |
|---|---|
| `SUTANDO_SKIP_RUNTIME_COMPAT=1` | skip the check for this launch |
| `SUTANDO_RUNTIME_REPAIR_CONSENT=1` | consent to the assisted fix without a prompt |
| `SUTANDO_RUNTIME_WAIT_S` | seconds to wait for a fix or a reply; default 600 |
| `SUTANDO_RUNTIMES_DIR` | the managed install root |
| `SUTANDO_RUNTIME_SYSTEM_DIRS` | `:`-separated folders searched instead of the Homebrew bin folders |

## Not covered yet

- The desktop installer's own flow (slice 2, in the desktop repo).
- A Claude token that has expired but is still on disk passes the static
  `auth_preflight` check, so this gate does not catch it; only the live probe
  (`auth_preflight.py --live`, one model call) does.
- When a known-bad version is the newest of its major, the assisted fix refuses
  it and keeps the old version, but cannot yet pin an earlier good one.
- `src/owner_reply.py` holds the DM and reply plumbing. The provider-limit
  hold gate (#5670) has its own copy; moving it onto this module is a
  follow-up once that PR lands.
