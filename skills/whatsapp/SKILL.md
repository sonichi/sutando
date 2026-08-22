---
name: whatsapp
description: Send WhatsApp messages, list chats, and search history via wacli (local CLI backed by a synced store at ~/.wacli). Unpaired? Run the guided connect flow (scripts/guided_connect.py) from chat — no terminal needed.
---

# WhatsApp (wacli)

Send messages, list chats, and search history using [wacli](https://github.com/openclaw/wacli) — a local CLI that syncs a WhatsApp Web session into `~/.wacli/`. No third-party service, no API key; the auth flow is a QR-code scan from the user's phone.

## When to use

- The user asks to send a WhatsApp message, list chats, or search WhatsApp history.
- The user asks "did X message me on WhatsApp?" or similar history-search questions.
- Unpaired Mac? Probe with `wacli chats list --limit 1`; if it errors "not authenticated," run the guided connect flow (`python3 scripts/guided_connect.py --phone <number>`) and relay its protocol lines into chat — the user never opens a terminal. Manual `wacli auth` in a terminal is the explicit fallback only.

## Install + auth

```bash
brew install openclaw/tap/wacli       # one-time install (3rd-party tap, NOT Homebrew-core)
wacli auth                            # opens a QR for the user to scan from WhatsApp → Linked Devices
```

`openclaw/tap` is a third-party Homebrew tap (not Homebrew-core), and wacli stores a WhatsApp Web session locally at `~/.wacli/`. Review the tap source before installing if security-sensitive.

The session lives at `~/.wacli/`. Stays signed in across reboots until the user revokes the linked device from their phone.

Optional `.env` settings (label shown in WhatsApp's Linked Devices screen on the user's phone):

```bash
WACLI_DEVICE_LABEL=Sutando            # any string; appears next to the device in WhatsApp → Linked Devices
WACLI_DEVICE_PLATFORM=CHROME          # default per wacli is DESKTOP; CHROME is the fallback if an invalid value is set.
                                      # See wacli's docs (https://github.com/openclaw/wacli) for the full platform-string list.
```

## Guided connect (zero-terminal pairing)

When the user asks to connect WhatsApp, do NOT tell them to open a terminal.
Run the orchestrator and relay its line protocol into the chat:

```bash
python3 skills/whatsapp/scripts/guided_connect.py --phone "+14155551234"   # preferred
python3 skills/whatsapp/scripts/guided_connect.py                          # QR fallback
```

- **Prefer `--phone`** whenever the user's number is known or can be asked for:
  it emits `PAIR_CODE: XXXX-XXXX`, which you relay as text — the user types it
  under WhatsApp → Linked devices → "Link with phone number". A QR posted into
  the chat cannot be scanned when the chat is open on the same phone.
- QR mode emits `QR_PNG: <path>` — attach it to your reply via `[file: <path>]`.
  Codes rotate; each new `QR_PNG:` line supersedes the previous image, so
  re-post it. `QR_TEXT: <payload>` appears instead if the qrcode lib is absent.
- `ALREADY_CONNECTED` / `CONNECTED` both mean the session is live and the
  chats probe passed — confirm to the user and offer a closed-loop test send
  to a recipient THEY name (wacli refuses self-sends by design).
- `ERROR: <reason>` is terminal; relay it honestly. Passkey-gated accounts are
  a documented wacli limitation.

## Commands

```bash
wacli send text --to "+14155551234" --message "Hello!"     # send
wacli send text --to "+14155551234" --message "$(cat draft.txt)"  # multi-line via stdin / file
wacli chats list --limit 20                                # recent chats
wacli messages search "keyword" --limit 10                 # search history
wacli messages list --chat "+14155551234" --limit 20       # read a thread
```

Phone numbers are in E.164 (`+countrycode...`). For groups, pass the JID returned by `wacli chats list` instead of a phone number.

## Conventions

- **Always confirm message content with the user before sending.** Matches the iMessage / SMS / X-post pattern in CLAUDE.md. **The confirmation flow lives in the agent / bridge that calls `wacli send` — this skill itself has no confirm step.** Treating the SKILL.md as the confirmation surface would skip the check entirely on direct CLI invocations.
- For multi-line / long messages, write to a `/tmp/wa-*.txt` and pass via `--message "$(cat /tmp/wa-X.txt)"` — avoids shell-escape issues with quotes and emoji.
- WhatsApp delivers messages best-effort; `wacli send text` returning success means the message hit WhatsApp's servers, not that the recipient has read it.

## Failure modes

- `wacli: not authenticated` → run the guided connect flow (`scripts/guided_connect.py`) and relay its lines; terminal `wacli auth` only as an explicit fallback. One-time per Mac.
- `wacli: linked device revoked` → user revoked the session from their phone. Re-run the guided connect flow.
- `wacli: rate limited` → WhatsApp is throttling. Back off ~30s and retry.
- Phone-number invalid → confirm the user provided E.164 format with a `+` prefix.

## Origin

Synthesizes the proposal in [PR #180](https://github.com/sonichi/sutando/pull/180) by @priyansh4320 (stale + conflicts, never landed) and the wacli reference already documented in CLAUDE.md's "Built-in capabilities" section. Issue #179 (asking for WhatsApp support) was closed 2026-05-16 noting the capability had landed as inline doc.
