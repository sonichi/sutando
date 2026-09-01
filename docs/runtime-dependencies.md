# External runtime dependencies

What Sutando needs installed on the host, what only a particular feature needs,
and what to vendor when embedding Sutando in another application.

Every claim below is tied to the line that enforces it, so the list can be
checked rather than trusted. Verify a host with:

```bash
bash src/verify-setup.sh      # per-dependency pass/fail, including CLI auth
python3 src/health-check.py   # service-level health once running
```

## Required to boot

`src/startup.sh` sets `missing=1` and refuses to start for exactly these:

| Dependency | Install | Enforced at |
|---|---|---|
| `node` | `brew install node` | `src/startup.sh:478` |
| `npx` | ships with Node | `src/startup.sh:479` |
| `python3` | `brew install python3` | `src/startup.sh:481` |
| Authenticated `claude` **or** `codex` CLI | whichever `core.runtime` names | `src/startup.sh` selected-core preflight |
| `fswatch` | `brew install fswatch` (startup auto-installs when Homebrew is present) | `src/startup.sh:494,497` |

That is the whole hard gate. Nothing else stops a boot.

### Strongly recommended, but not enforced at boot

`startup.sh` checks the selected agent CLI's presence and authentication before
services launch. It does not check platform or tool versions:

- **macOS 15+** — not checked anywhere.
- **Node.js 22+** — checked by `src/verify-setup.sh:22-26`, which is advisory.
- **A signed-in agent CLI is enforced at boot.** Claude uses the existing
  login-state preflight; Codex exports its configured `CODEX_HOME` and runs
  `codex login status`. `SUTANDO_SKIP_AUTH_PREFLIGHT=1` is a one-run escape
  hatch; the runtime launcher still checks again before replacing the core.

The agent CLI is the one dependency nothing can work around: **Sutando is a
harness around Claude Code or Codex.**

## Required per feature

None of these block startup. Each one's absence disables its own surface and
nothing else.

| Feature | Needs | Without it |
|---|---|---|
| Discord bridge | `pip3 install discord.py` | bridge skipped (gated on the token *and* the import) |
| Slack bridge | `pip3 install slack_bolt` | bridge skipped |
| Telegram bridge | — *(standard library only)* | — |
| Image generation | `pip3 install google-genai Pillow` | `skills/image-generation` unavailable |
| Voice | Gemini API key | text/core paths still work (`src/startup.sh:755`) |
| Phone calls, SMS | Twilio account + ngrok | browser, Telegram and Discord paths still work |
| Recording, subtitle burn, video concat | `ffmpeg` / `ffprobe` | those features unavailable |
| Vault sync, self-upgrade, commit provenance | `git` | those features unavailable; everything else runs |
| Agent-authored PR workflows | `gh` | unavailable |
| OCR on screen captures | `tesseract` | unavailable |
| Sutando.app watcher auto-restart | `tmux` | **core still starts** — `src/agent/claude/cli/start-cli.sh:613` falls back to a bare `exec claude`; only the auto-restart is lost |
| Building `Sutando.app` from source | Xcode Command Line Tools | not needed if a prebuilt binary ships |

Two entries worth calling out, because both have been overstated before:

- **`tmux` is not a boot requirement.** `start-cli.sh:597` auto-installs it via
  Homebrew when available, and `:613` falls back cleanly when it is missing.
- **`python-telegram-bot` is not required.** `src/telegram-bridge.py` imports
  only the standard library (`urllib.request`). It appears in
  `src/migrate.sh:308`'s convenience `pip3 install`, but nothing imports it.

Install only the packages for the surfaces you actually use. A text/core install
needs none of them.

## Not required — macOS ships these

Nothing to install: `osascript`, `open`, `pgrep`, `lsof`, `ps`, `sips`,
`launchctl`, `security`, `screencapture`, `pbcopy`, `pbpaste`, `say`, `which`.

## A note on the Xcode Command Line Tools

On macOS `/usr/bin/{git,python3,swift,swiftc,clang,cc,gcc,make,…}` are **one
inode hardlinked 78 ways** — the CLT *stub*, not those tools
(`ls -li /usr/bin | awk '$3==78'`). The file exists whether or not the tools are
installed; invoking it without them raises a modal "install command line
developer tools" dialog and returns nothing.

Two consequences when packaging, or when adding a call to any of these:

- **Existence is not a usable probe.** `command -v`, `test -x`,
  `shutil.which` and `FileManager.fileExists` all succeed against the stub.
  `xcode-select -p` is the only check that reports CLT status without
  prompting — see `src/migrate.sh:122` for the pattern.
- **The stub cannot be shadowed by an absolute path.** Hardcoding
  `/usr/bin/git` pins the stub, so a user who installs a real git never wins.

`REVIEW.md` lesson 7 carries the review criterion and the machine check. Note
its stated limit: the gate matches explicit `/usr/bin/…` tokens only, so a bare
`git` or `python3` resolved through PATH is invisible to it.

> **Resolver rollout, partially landed.** Shared resolvers prefer a real install
> and degrade instead of prompting, rather than invoking the stub directly.
>
> On `main`: `src/python-binary.ts` (#2475) — used by `skills/zoom/tools.ts` and
> `src/meeting-tools.ts`.
>
> Still under review: `src/git_binary.py` (#2469), `SutandoConfig.resolvePython`
> (#2473), and the runtime-descriptor guard in `scripts/sutando-config.sh`
> (#2478). Until those land, the call sites they cover still invoke the stub
> directly on a host without developer tools.

## Embedding Sutando in another application

`scripts/build-bundle.mjs` compiles the TypeScript entrypoints to `dist/*.js`
(esbuild, ESM, `platform: 'node'`) with only `bufferutil` and `utf-8-validate`
left external. **It vendors no runtimes** — no node, no python, no ffmpeg, no
git. A host application that embeds Sutando supplies them.

To make an embedded install self-contained, vendor:

| Vendor | Notes |
|---|---|
| Node.js | the `dist/*.js` bundles need a node to run |
| `python3` **+ the per-feature packages above** | an interpreter with an empty `site-packages` starts nothing |
| `fswatch` | file watcher |
| `tmux` | optional — buys Sutando.app watcher auto-restart, not core start |
| `ffmpeg` / `ffprobe` | only if recording features are wanted |

Two conventions the code already follows, so a vendored layout is picked up
without further change:

- **Python** — place it at `<engine>/../runtime/python/bin/python3`, or export
  `$SUTANDO_PY`. Both are checked ahead of PATH by
  `scripts/sutando-config.sh:44` and `src/agent/claude/cli/start-cli.sh:30`.
- **ffmpeg** — placed beside the running node binary is found by
  `src/recording-tools.ts`.

`git` is **deliberately not vendored** by this repo. Development runs from a
checkout where git already exists; an embedded install degrades on the
git-backed features above rather than shipping and maintaining a git
distribution.

The agent CLI still has to be installed and authenticated by the host
application — it cannot be vendored transparently.
