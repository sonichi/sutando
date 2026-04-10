# Contributing to Sutando

Thanks for your interest! Sutando is alpha software — the biggest need is **testing**.

## Quick ways to contribute

### Test a capability
Pick something from the "What's inside" table in [README.md](README.md), try it, and report what breaks.

```bash
# Clone and set up
git clone https://github.com/liususan091219/sutando.git
cd sutando
npm install
cp .env.example .env  # add your GEMINI_API_KEY
bash src/startup.sh
```

### Report bugs
[Open an issue](https://github.com/liususan091219/sutando/issues) with:
- What you tried
- What happened
- What you expected
- Logs from `logs/*.log` if relevant

### Add a skill
Skills are modular capabilities in `skills/`. Each skill has:
- `SKILL.md` — description and usage instructions
- `scripts/` — the actual code

See existing skills for examples. Install with `bash skills/install.sh`.

### Improve the phone conversation
The phone skill (`skills/phone-conversation/`) uses Twilio Media Streams + Gemini Live. Areas to improve:
- Multi-language STT support
- Translation during calls
- Inbound call handling

## Code style

- **Python**: standard library preferred, no frameworks
- **TypeScript**: ESM modules, strict mode
- **Shell**: bash, `set -e`, use `$REPO` for paths
- All scripts should work from a fresh clone with minimal setup

## Pull requests

- Keep PRs focused — one feature or fix per PR
- Test your changes locally before submitting
- Update README.md if you add user-facing features
- Run `npx tsc --noEmit` to verify TypeScript compiles

## Architecture

```
Voice (Gemini Live) ←→ File Bridge (tasks/results) ←→ Claude Code (brain)
```

See README.md for the full architecture diagram.
