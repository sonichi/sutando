# Contributing to Sutando

Thanks for your interest! Sutando is alpha software — the biggest need is **testing and hardening**.

## Contributor License Agreement (CLA)

Before your first contribution can be merged, you'll be asked to sign the project's CLA — a one-time, web-based "I agree" via the [CLA Assistant](https://cla-assistant.io) bot. The bot will comment on your PR with a link; just click through and sign. The CLA text is in [`CLA.md`](CLA.md). Subsequent PRs are auto-recognized.

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

## Before opening any PR or issue

Six checks save a lot of churn for both sides:

### 1. Search for existing PRs / issues first

The same fix has been opened in 10+ different PRs before (e.g. the bare-except narrow in `skills/quota-tracker/scripts/read-quota.py` had 10 attempts across multiple contributors before one landed). Before you open:

```bash
# Open + recently closed PRs that closed/referenced the same issue
gh pr list --repo sonichi/sutando --state all --limit 30 --search "closes #N"

# Open issues with related keywords
gh issue list --repo sonichi/sutando --state open --search "your-keyword"
```

If someone else's PR is already in flight and CLA-blocked or just stale, prefer pinging them or rebasing their branch over opening a parallel one.

### 2. CLA + git author email

CLA-Assistant maps your commits to a GitHub user via the author email. Two pitfalls trip almost every new contributor:

```bash
# Check your most recent commit's author email
git log -1 --format='%ae'

# If it shows something like:
#   user@Hostname.local                ← macOS hostname auto-fill
#   noreply@anthropic.com              ← Claude Code default for bot commits
# CLA-Assistant CANNOT map it to your GitHub account → check stays PENDING forever.

# Fix it before pushing:
git config user.email YOUR_GH_MAPPED_EMAIL
git commit --amend --reset-author --no-edit   # rewrites only the latest commit
# or for a fuller rewrite:
git rebase -i origin/main   # mark each as 'edit', then --reset-author + continue
```

If you're running a Sutando bot that commits on your behalf, set the bot's `git config user.email` to your CLA-signed email locally (don't share the keychain — just configure git).

### 3. Single concern per PR

A PR that fixes "X" should not also bundle "while I was here, I cleaned up Y / refactored Z / added a new feature W". Split those:

- One concern → one PR → one closes-link
- Mixing concerns triples the review burden, increases revert blast radius, and makes merge conflicts harder
- "Drive-by" cleanup that happens to land in the same hunk is fine; net-new scope is not

### 4. Confirm the bug exists on `upstream/main`

Before adding a fix:

```bash
git fetch upstream main
git show upstream/main:path/to/file.py | grep -n "the buggy line"
```

If the bug is already fixed upstream, the PR is unnecessary. Save yourself + reviewer time.

### 5. Respect the V1-workspace hold list

The V1 workspace contract migration (3-space Code / Workspace / Memory model) is in design. PRs that touch the following are currently held — please don't open new ones in these areas until the contract is finalized:

- `src/workspace_default.py` / `src/workspace_default.ts` resolution logic
- `scripts/sync-memory.sh` path probing (`SUTANDO_MEMORY_DIR` / `SUTANDO_PRIVATE_DIR`)
- new `claude_home_path()` helpers or similar path-derivation utilities
- migration scaffolding in `skills/agent-registry/` paths

If unsure, ask in #design before opening.

### 6. After `update-branch`, CLA-Assistant may not auto-rerun

If your PR was BEHIND main and you click "Update branch" (or `gh pr update-branch`), the new HEAD commit may show `license/cla` PENDING and never resolve. Known issue. In most cases `.github/workflows/cla-recheck-on-push.yml` will auto-fire the recheck comment for you on every push, but if the workflow is disabled or fails, the manual workarounds are (in order):

1. Wait — sometimes the bot catches up in ~10 minutes
2. Comment `@cla-assistant check` on the PR
3. Close and reopen the PR (forces a `pull_request.reopened` webhook)
4. Ask a maintainer to admin-merge if you've verified the underlying CLA is signed

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
