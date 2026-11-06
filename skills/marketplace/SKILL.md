---
name: marketplace
description: Install marketplace skills, activate cloud tools, check whether installed skills are up to date, or remove them — without the owner opening the Marketplace. Use when the owner asks to install/activate/enable/remove a skill or cloud tool, asks "are my skills up to date?", or hands you a use-case doc and asks you to set up what it needs.
---

# Marketplace

The owner is already signed in to Sutando Cloud (desktop onboarding), so
everything the Marketplace UI does can be done from here: activate a cloud
tool, install a local skill, update skills, remove them.

The script lives next to this file. Run it with `python3`:

```bash
M="<this skill's directory>/scripts/marketplace.py"
python3 "$M" find "lead enrichment"              # search (add --kind skill|cloud_tool|connector)
python3 "$M" status                              # owned vs on this machine
python3 "$M" install intent-leads campaign-runner   # PLAN only — writes nothing
python3 "$M" install intent-leads campaign-runner --yes
python3 "$M" update                              # list missing / outdated skills
python3 "$M" update --yes                        # re-fetch them (never charges)
python3 "$M" uninstall live-preview              # PLAN only
python3 "$M" uninstall live-preview --yes
```

Add `--json` for machine-readable output. One `install` call takes skills and
cloud tools together; the script works out which is which.

## How to run a setup

1. **Collect the slugs.** From the owner's request, or from a use-case doc
   (read it; docs list what they need under a `Requires:` heading). If a name
   isn't an exact slug, run `find` first.
2. **Plan.** Run `install <all slugs>` without `--yes`. Show the owner the plan
   in a sentence or two (what gets installed or activated, what's already set
   up, what's skipped and why).
3. **Confirm when it costs something.** Exit code `3` means the plan spends
   credits once, or activates a cloud tool that charges per use (the plan says
   "leads charges per use: 5 credits per result"): tell the owner the price and
   get their explicit OK first. Exit code `0` means it's free: go ahead. Never
   pass `--yes` to `uninstall` without the owner's OK.
4. **Apply.** Re-run the same command with `--yes`. One failed item doesn't
   stop the rest.
5. **Use it now; restart only when told.** A newly activated cloud tool is
   **usable at once through `station_find` / `station_call`** (the output says
   "usable now through station_call", `usable_now` in JSON): go on with the
   owner's request in the same turn. **Only if the output says `RESTART REQUIRED`**
   (`restart_required: true`) did the running engine start without the Station,
   or for another account; then tell the owner exactly once, after everything else:

   > New cloud tools are active on your account, but I can only use them after
   > an engine restart: open **Agent settings** (the bot icon, bottom left),
   > scroll down to **Runtime**, and click **Restart engine**. Restarting ends
   > my current session; your tasks and files are kept.

   When the output says the desktop app hasn't connected the engine to AG2 Cloud
   yet (`restart_after_sign_in: true`), the tools need that same one restart
   once the owner signs in; re-check with `status` then.

   **Never restart the core yourself, never ask for a restart the output did not
   name, and never send the owner to a dashboard or Station page to activate:
   this script is the activation.** Installed **skills** need no restart; they
   are usable right away.
6. **Report.** Say what's ready and what failed, using the reasons the script
   prints (not enough credits, plan too low, not found…).
7. **Price before the first paid call.** `station_find` marks a metered tool
   `confirm_before_call: true`. Before the first `station_call` to such a tool
   in a conversation, state its price from `pricing` ("5 credits per result")
   and wait for the owner's OK.

## "Are my skills up to date?"

Run `status`. Skills show `ok`, `missing` (owned but not on this machine),
`outdated` (local version behind the marketplace), `disabled`, or `bundled`
(ships with Sutando). If anything is missing or outdated, offer `update --yes`.
Updating never charges.

## Things to know

- **Connectors** (Gmail, Google Calendar, Slack, …) are not installed here:
  `install` skips them with a note, and `find` / `install` match them by exact
  slug. Connect them from chat with the `connect-apps` skill, which sends the
  owner a Connect card and picks the request up once they sign in.
- **Paid skills** charge once. Uninstalling one and reinstalling it charges again.
- Skills install as real directories under `$CLAUDE_CONFIG_DIR/skills/<slug>/`,
  verified against the marketplace's sha256. Unsigned bundles are refused.
  Skills bundled with Sutando are never replaced.
- **Errors:**
  - "not signed in" / "session expired": the owner signs in again from the desktop app.
  - Exit `2`: a setup problem to relay, not something to retry.

## Writing a use-case doc that Sutando can set up

List the slugs under a `Requires:` heading:

```markdown
## Requires
- Cloud tools: icp-mapper, intent-leads, lead-enricher, email-drafter
- Skills: campaign-runner, campaign-responder
```
