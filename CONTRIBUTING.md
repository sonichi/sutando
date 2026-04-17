# Contributing to Sutando

Thanks for your interest! Sutando is alpha software — the biggest need is **testing and hardening**.

## Quick ways to contribute

### Test a capability
Pick something from the "What's inside" table in [README.md](README.md), try it, and report what breaks.

```bash
# Clone and set up
git clone https://github.com/sonichi/sutando.git
cd sutando
npm install
cp .env.example .env  # add your GEMINI_API_KEY
bash src/startup.sh
```

### Report bugs
[Open an issue](https://github.com/sonichi/sutando/issues) using the bug report template. A good bug report includes:

1. **What happened** — describe the issue clearly
2. **Steps to reproduce** — numbered steps someone else can follow
3. **Expected behavior** — what should have happened
4. **Logs** — paste relevant lines from `logs/*.log`
5. **Environment** — macOS version, Node.js version, Claude Code version

**Bonus (highly valued):**
- A POC script under `scripts/test-*.sh` that reproduces the bug programmatically
- Before/after commit hashes if you can identify when it regressed
- The specific tool call or function that failed (check voice-agent.log for `[Tool]` entries)

### Add a skill
Skills are modular capabilities in `skills/`. Each skill has:
- `SKILL.md` — description and usage instructions
- `scripts/` — the actual code

See existing skills for examples. Install with `bash skills/install.sh`.

## Code style

- **Python**: standard library preferred, no frameworks. Python 3.9+ compatible (avoid `str | None` union syntax — use `Optional[str]`).
- **TypeScript**: ESM modules, strict mode. Run `npx tsc --noEmit` before submitting.
- **Shell**: bash, `set -e`, use `$REPO` for paths
- **web-client.ts**: The entire web UI is an inline HTML template literal. Do NOT use TypeScript-only syntax (like `as Type` casts) inside the embedded `<script>` block — the browser runs it as plain JS.
- All scripts should work from a fresh clone with minimal setup

## Pull requests

- Keep PRs focused — one feature or fix per PR
- Test your changes locally before submitting
- Update README.md if you add user-facing features
- Run `npx tsc --noEmit` to verify TypeScript compiles
- Check for lazy imports if your code reads from `.env` — static ESM imports resolve before module-level code runs

### Review process
PRs are reviewed by one of the Sutando bot instances (MacBook or Mac Mini). Reviews check for:
- Correctness and test coverage
- Import strategy (lazy vs static — avoid breaking env var reads)
- Default-value changes that could affect existing behavior
- Security: no hardcoded credentials, sandbox compliance for non-owner paths
- No unnecessary code — don't add features beyond what was asked

## Architecture

```
Voice (Gemini Live) <-> File Bridge (tasks/results) <-> Claude Code (brain)
                                                         |
                                              8 channels: voice, phone,
                                              Discord, Telegram, context
                                              drop, iMessage, WhatsApp, email
```

Two machines coordinate via Discord:
- **MacBook** — travels with the owner
- **Mac Mini** — always-on at home

See README.md for the full architecture diagram.

## Community

- [Discord](https://discord.gg/uZHWXXmrCS) — real-time dev, PR discussion, live debugging
- [GitHub Issues](https://github.com/sonichi/sutando/issues) — bug reports and feature requests
