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
python3 "$M" find "lead enrichment"              # search (add --kind skill|cloud_tool)
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
   credits: get the owner's explicit OK first. Exit code `0` means it's free:
   go ahead. Never pass `--yes` to `uninstall` without the owner's OK.
4. **Apply.** Re-run the same command with `--yes`. One failed item doesn't
   stop the rest.
5. **Report.** Say what's ready and what failed, using the reasons the script
   prints (not enough credits, plan too low, not found…).
6. **Restart, once, at the end.** If the output says `RESTART REQUIRED`
   (`restart_required: true` in JSON), tell the owner exactly once, after
   everything else:

   > New cloud tools are active on your account, but I can only use them after
   > a core restart: open **Settings → Agent → Restart Core** (also under
   > Settings → Services, or the restart button in the Agent Console).
   > Restarting ends my current session; your tasks and files are kept.

   Never restart the core yourself. Installed **skills** need no restart; they
   are usable right away.

## "Are my skills up to date?"

Run `status`. Skills show `ok`, `missing` (owned but not on this machine),
`outdated` (local version behind the marketplace), `disabled`, or `bundled`
(ships with Sutando). If anything is missing or outdated, offer `update --yes`.
Updating never charges.

## Things to know

- **Connectors** (Gmail, Slack, …) need a browser sign-in. They're skipped with
  a note; send the owner to the Marketplace for those.
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
