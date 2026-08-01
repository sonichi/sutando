# External runtime dependencies

What Sutando needs installed on the host, what it only needs for particular
features, and what it never needs because macOS already ships it.

Two audiences: someone setting up a checkout (see [Quick start](../README.md#quick-start)
first — this page is the complete list behind it), and anyone **embedding
Sutando in another application**, who needs to know exactly what to vendor.

Verify a host with:

```bash
bash src/verify-setup.sh      # per-dependency pass/fail
python3 src/health-check.py   # service-level health once running
```

## Required — the core will not start without these

`src/startup.sh` refuses to boot and prints `✗ …` for each of these.

| Dependency | Install | Enforced at |
|---|---|---|
| macOS 15+ | — | — |
| Node.js 22+ | `brew install node` | `src/startup.sh:478` |
| `npx` | ships with Node | `src/startup.sh:479` |
| `python3` | `brew install python3` | `src/startup.sh:481` |
| `claude` **or** `codex` CLI | [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started) / [Codex CLI](https://developers.openai.com/codex/cli/) — whichever `core.runtime` names, **signed in** | `src/startup.sh:483` |
| `fswatch` | `brew install fswatch` (startup auto-installs when Homebrew is present) | `src/startup.sh:497` |
| `tmux` | `brew install tmux` (auto-installed by `start-cli.sh:597`) | not in the startup check, but the core runs inside a tmux session and Sutando.app's watcher-auto-restart depends on it |

The agent CLI is the one dependency that cannot be worked around: **Sutando is a
harness around Claude Code or Codex.** It must be installed *and authenticated* —
`verify-setup.sh` checks authentication separately from presence, because an
unauthenticated CLI passes a `command -v` test and then fails at runtime.

### Python packages

A bare interpreter is not enough — the bridges import third-party packages and
exit without them.

```bash
pip3 install google-genai discord.py python-telegram-bot slack_bolt Pillow
```

Also imported by parts of the tree: `anthropic`, `aiohttp`, `python-dotenv`.
`detect-secrets` is a **test-only** dependency (the vault soft-imports it and
degrades when absent), so it is not needed at runtime.

If you vendor an interpreter, these must be installed **into that interpreter**.
A vendored python with an empty `site-packages` starts nothing.

## Required per feature — absent means that feature degrades

None of these block startup. Each one's absence disables its own surface and
nothing else.

| Dependency | Unlocks | Without it |
|---|---|---|
| `ffmpeg` / `ffprobe` | recording, subtitle burn, video concat | recording features unavailable |
| `git` | vault sync (`sync-workspace.sh`), self-upgrade, commit provenance in dashboard/activity | those features unavailable; everything else runs |
| `gh` | agent-authored PR workflows | PR flows unavailable |
| `tesseract` | OCR on screen captures | OCR unavailable |
| `ngrok` / `tailscale` | inbound phone calls, remote access | local-only |
| Swift toolchain (Xcode CLT) | **building** `Sutando.app` from source | not needed if a prebuilt binary ships |
| Gemini API key | voice | text/core paths still work |
| Twilio account | phone calls, SMS | browser + Telegram + Discord paths still work |

### A note on the Xcode Command Line Tools

On macOS, `/usr/bin/{git,python3,swift,swiftc,clang,cc,gcc,make,…}` are **one
inode hardlinked 78 ways** — the CLT *stub*, not those tools. The file exists
whether or not the tools are installed; invoking it without them raises a modal
"install command line developer tools" dialog and returns nothing.

Two consequences worth knowing when packaging:

- **Existence is not a usable probe.** `command -v`, `test -x`,
  `shutil.which` and `FileManager.fileExists` all succeed against the stub.
  `xcode-select -p` is the only check that does not prompt.
- **Sutando does not require the CLT.** Interpreter and git lookup go through
  `src/git_binary.py`, `src/python-binary.ts` and `SutandoConfig.resolvePython`,
  which prefer a real install and degrade rather than prompt. See `REVIEW.md`
  lesson 7 before adding a call to any of those tools.

## Not required — macOS ships these

Nothing to install: `osascript`, `open`, `pgrep`, `lsof`, `ps`, `sips`,
`launchctl`, `security`, `screencapture`, `pbcopy`, `pbpaste`, `say`, `which`.

## Embedding Sutando in another application

`scripts/build-bundle.mjs` compiles the TypeScript entrypoints to `dist/*.js`
(esbuild, ESM, `platform: 'node'`) with only `bufferutil` and `utf-8-validate`
left external. **It vendors no runtimes** — no node, no python, no ffmpeg, no
git. A host application that embeds Sutando is responsible for providing them.

To make an embedded install self-contained, vendor:

| Vendor | Notes |
|---|---|
| Node.js | the `dist/*.js` bundles need a node to run |
| `python3` **+ the packages above** | the interpreter alone is not sufficient |
| `fswatch` | file watcher |
| `tmux` | core session and watcher-auto-restart |
| `ffmpeg` / `ffprobe` | only if recording features are wanted |

Two conventions the code already follows, so a vendored layout is picked up
with no further change:

- **Python** — place it at `<engine>/../runtime/python/bin/python3`, or export
  `$SUTANDO_PY` pointing at it. Both are checked ahead of anything on PATH by
  `scripts/sutando-config.sh`, `src/agent/claude/cli/start-cli.sh`,
  `src/python-binary.ts` and `SutandoConfig.resolvePython`.
- **ffmpeg** — placed beside the running node binary is found by
  `src/recording-tools.ts`.

`git` is **deliberately not vendored**. Development use runs from a checkout
where git already exists; an embedded install degrades on the git-backed
features listed above rather than shipping and maintaining a git distribution.

The agent CLI still has to be installed and authenticated by the host
application — it is not something that can be vendored transparently.
