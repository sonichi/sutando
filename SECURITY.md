# Security Policy

Sutando is a personal AI agent with deep access to your computer — file system, screen, keyboard, browser, email, phone, and messaging. This power comes with real security risks that you should understand before deploying.

## Threat Model

Sutando's primary attack surface is **unauthorized access through its communication channels**. An attacker who can interact with Sutando through any channel may attempt to escalate privileges and execute actions on your computer.

### Attack Channels

#### Phone Calls (Highest Risk)

Sutando accepts inbound phone calls via Twilio. Anyone who knows your Twilio number can call.

**3-tier access control:**

| Tier | Who | Capabilities |
|------|-----|-------------|
| **Owner** | `OWNER_NUMBER` in `.env` | Full computer access via `work` tool: file system, screen control, keyboard, browser, email, phone calls, app switching |
| **Verified** | Numbers in `VERIFIED_CALLERS` | Configurable subset of tools (default: volume, brightness, time, meeting ID lookup) |
| **Unverified** | Everyone else | Volume, brightness, current time only. No file access, no screen control, no task delegation |

**Risks:**
- **Caller ID spoofing**: Phone numbers can be spoofed. An attacker who spoofs the owner's number gets full computer access. Twilio provides some spoofing protection via STIR/SHAKEN, but it is not foolproof.
- **Social engineering**: The AI may be manipulated through conversation to perform unintended actions, even within its authorized tool set.
- **Prompt injection via voice**: An attacker could attempt to inject instructions through speech that override the system prompt.

**Mitigations:**
- Keep your Twilio number private
- Use `VERIFIED_CALLERS` to explicitly allowlist trusted numbers (don't leave it empty — an empty set allows all callers)
- Monitor call transcripts in `results/calls/calls.jsonl`
- The `work` tool delegates to Claude Code, which has its own safety boundaries

#### Zoom/Google Meet (Medium Risk)

Sutando can join meetings in two modes:

1. **Via voice agent** (`summon`, `join_zoom`, `join_gmeet`): Joins with the owner's computer audio. Only the owner can trigger this.
2. **Via phone agent** (concurrent call to meeting dial-in): Joins as a phone participant. The meeting must be in `VERIFIED_MEETINGS` for Sutando to get task capabilities; otherwise it is notes-only.

**Risks:**
- In verified meetings, other participants can ask Sutando to perform actions (make calls, look things up, take screenshots)
- Meeting participants may attempt prompt injection through speech
- An unverified meeting attendee cannot directly control Sutando, but the meeting audio is processed by Gemini

**Mitigations:**
- Only add trusted meetings to `VERIFIED_MEETINGS`
- Unverified meetings default to notes-only mode (no `work` tool)
- Use the `/meeting` API endpoint's approve flow for ad-hoc verification

#### Discord (Medium Risk)

The Discord bridge accepts DMs and channel mentions.

**Access control:**
- **Owner** (`allowFrom` in `~/.claude/channels/discord/access.json`): Full task delegation to Claude Code
- **Team** (channel-specific allowlists): Sandboxed execution via `codex exec --sandbox read-only` — read-only, no system mutations
- **Other**: Sandboxed, information-only responses about Sutando

**Risks:**
- Anyone who can DM the bot or mention it in a configured channel can interact with it
- Owner-tier Discord users get full Claude Code capabilities — equivalent to phone owner access
- Bot token compromise gives full access to all Discord interactions

**Mitigations:**
- Use `"dmPolicy": "allowlist"` (not `"pairing"`) in production
- Keep `allowFrom` list minimal
- Protect the bot token in `~/.claude/channels/discord/.env`

#### Telegram (Medium Risk)

Same architecture as Discord — polling-based bridge with allowlist access control.

**Risks:**
- Same as Discord: owner-tier users get full task delegation
- Bot token compromise exposes all interactions

**Mitigations:**
- Configure allowlist in `~/.claude/channels/telegram/.env`
- Keep bot token secure

#### Web Client (Low Risk)

The web client connects to the voice agent on `localhost:8080`. It is local-only by default.

**Risks:**
- If exposed to the network (e.g., via ngrok or port forwarding), anyone with the URL gets owner-level voice access
- No authentication on the WebSocket connection

**Mitigations:**
- Keep the web client on localhost
- Do not expose port 8080 to the internet

### General Risks

- **`--dangerously-skip-permissions`**: The default startup uses this flag, giving Claude Code unrestricted tool access. This is convenient but means any successful attack gets full system access.
- **File system access**: Owner-tier callers can read, write, and delete files on your computer.
- **Screen and keyboard control**: Owner-tier callers can see your screen, type text, switch apps, and open URLs.
- **Email and messaging**: Owner-tier callers can send emails and messages on your behalf.
- **Phone calls**: Owner-tier callers can initiate outbound phone calls from your Twilio number.

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it privately:

1. **Email**: Open a GitHub issue marked `[SECURITY]` with a brief description (no exploit details)
2. **Discord**: DM a maintainer directly

Do not post exploit details publicly.

## Recommendations

1. **Treat your Twilio number like a password** — don't share it publicly
2. **Keep `VERIFIED_CALLERS` explicit** — never leave it empty in production
3. **Use allowlists for Discord/Telegram** — not open pairing mode
4. **Monitor transcripts** — review `results/calls/calls.jsonl` and `conversation.log` periodically
5. **Don't expose local services** — keep web client and voice agent on localhost
6. **Consider the blast radius** — Sutando with `--dangerously-skip-permissions` has full access to your computer. An attacker who gains owner access gains everything.
