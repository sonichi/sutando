---
name: account-switch
description: "The core's account card in the owner's DM: which account the core (Codex or Claude) is signed in to, a switch to an account saved on this Mac (Codex core; restarts the core and confirms), and a pointer to Settings to add another. Owner-only, DM-only; a sign-in code never travels as a chat message."
user-invocable: true
---

# account-switch

Use it when the **owner, in their DM**, asks to switch, change or add the core's account, or asks
which account the core is on and wants to change it. Anyone else, or any other room: say the
account is switched from the owner's DM and do nothing.

```bash
python3 skills/account-switch/scripts/account_card.py post --room <owner DM> [--reply-to <event>]
python3 skills/account-switch/scripts/account_card.py post --room <owner DM> --print   # the card, not sent
python3 skills/account-switch/scripts/account_card.py status                          # JSON: in use + saved
```

The Codex app-server seat runs `account_card.py post --signed-out` itself (no `--room`: the core's checked
owner DM) when the core's login is refused even after it restarted the app-server on the login on disk;
that card leads with "Codex isn't signed in anymore." and offers the same switch.

`post --owner-dm --lead <words>` posts the same card, unasked, to the core's checked owner DM with
`<words>` opening it: the core's quota notice (`src/core_quota_notice.py`) sends it once per usage
limit, from the Codex seat and the Claude Code core's monitor, with `--retry` adding a Retry button
that sends the owner's "retry now" (the Claude core's limit hold acts on it; a Codex core's card has none). Never post it that way yourself.
`--owner-dm` and leaving out `--room` go to the same DM; with `--signed-out`, `--lead` replaces the
"isn't signed in anymore" line.

Never start a sign-in yourself, and never ask for, accept or repeat a sign-in code, link code or
token in chat. If the owner pastes one, do not use it; tell them to paste it in Settings, in the
app's own box.

## The card

- **In use.** A Codex core (TUI or app-server seat) is read by `src/codex_account.py`; a Claude core
  by `claude auth status --json` under the core's config dir. Addresses are masked (first letter and
  domain) on the card.
- **Saved accounts (Codex core only).** The desktop keeps a copy of each ChatGPT sign-in the core has
  had in `<app support>/accounts/codex/<key>.json` (ag2space-cinny-desktop#1264; `<app support>` is
  `$SUTANDO_APP_SUPPORT`, else the folder of the desktop's `runtime.json`). `post` saves the
  signed-in account there first, in the desktop's own format, so a later sign-in never loses it. Up to
  five other saved accounts get a "Use <account>" button. The key on a button is 16 hex of a hash of
  Codex's account id; it names no account and carries no credential.
- **Add another account.** Answers with where to sign in: Settings → Connections → the Sutando (on this
  computer) row → "Switch account". The app's sign-in terminal shows the link and takes the code in its
  own box. Then the owner asks for the card again.

A Claude core has no saved accounts (its sign-in lives in the Keychain), so its card offers only
"Add another account".

## A tap

`src/agent_card.py click` routes a tap to `account_card.py reply` (the recorded handler). Only the
owner's tap, in the DM the card was posted in, on a card this agent posted, acts
(`agent_card.click_verdict`). A "Use" tap acts only for a key that card offered and the store still
holds; a tap on the account already in use changes nothing; a second switch while one is under way is
refused. The tap is the same from any client the owner uses (the desktop app, the web, a phone): it
carries only the choice, and the saved sign-in never leaves this computer. A tap message acts once (a
resent one starts no second switch), and each tap's outcome is one line in the owner-action audit
(`state/recovery/audit.jsonl`), ids only. Adding an account needs the computer that runs the core.

A "Use" tap answers at once ("Switching the core to X. It restarts once this turn has finished…") and
starts `account_card.py switch` detached, in its own session, because the core restarts. `switch`:

1. refuses, changing nothing, when the current `auth.json` exists but cannot be read or cannot be saved
   to switch back to (an API key, a broken file); otherwise keeps it in memory and saves it in the store;
2. writes the saved one as the core's `<CODEX_HOME>/auth.json` (0600);
3. runs `src/agent/graceful-restart.sh` (it waits for the core's turn to end, then
   `start-cli.sh --restart`; a seat core is replaced as a seat);
4. waits for a fresh heartbeat from the core session (its session and tmux socket), asks Codex's own
   `account/read` which account the saved sign-in is, and posts the outcome in the same DM: **"I asked
   the core to restart with X's sign-in, and its heartbeat came back after the request (or: I haven't
   seen a heartbeat from the core session since). Saved sign-in verified: Codex confirms it is X. The
   running core's own account isn't confirmed."**, or why the saved sign-in is not X. It never says
   the core restarted: a heartbeat shows the session alive, not which process or account answers in it
   (a known limit).
   When the restart definitely did not happen (exit 3 not ready, 4 put off, 5 rehearsal), the previous
   state is put back, absence included; after any other failure the new sign-in stays and the reply
   says the outcome is unknown. One switch runs at a time.

Its log is `<workspace>/logs/account-card.log`; nothing in it, in the cards or in the JSON output is a
token or a code.
