#!/usr/bin/env python3
"""
Sutando health check — verifies all components are running correctly.

Usage:
  python3 src/health-check.py                  # full check, human-readable
  python3 src/health-check.py --json           # machine-readable output
  python3 src/health-check.py --fix            # attempt to fix issues
  python3 src/health-check.py --emit-task      # write tasks/task-health-*.txt on failure
  python3 src/health-check.py --notify-on-fail # macOS notification on failure
  python3 src/health-check.py --notify-slack   # DM the owner on Slack on failure (remote, core-independent)
  python3 src/health-check.py --notify-gateway # DM the owner on ag2.space/gateway on failure (remote, core-independent)
  python3 src/health-check.py --recover-core   # auto-restart the core when alive-but-wedged (guarded)

Checks:
  - macOS TCC Documents-folder access (when repo is under ~/Documents)
  - Voice agent (port 9900), web client, agent API, dashboard
  - Critical files (CLAUDE.md, build_log.md, ACTIVITY.md)
  - Memory system (MEMORY.md index, key memory files)
  - Notes directory
"""

import functools
import hashlib
import fnmatch
import json
import os
import re
import shlex
import statistics
import shutil
import tempfile
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

try:
    import fcntl  # POSIX file locking for the recovery critical section
except ImportError:  # non-POSIX (e.g. Windows) — the lock degrades to a no-op
    fcntl = None

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
# Kept as two lines on purpose: tests/git-binary-resolution.test.py:109 asserts
# the literal `from git_binary import git_argv` to prove each call site imports
# the resolver instead of hardcoding a git path. Merging these into one import
# breaks that substring check — which guards 27 other call sites, so the import
# bends here rather than the guard.
from git_binary import git_argv  # noqa: E402
from git_binary import GitUnavailable  # noqa: E402
from git_binary import developer_tools_installed  # noqa: E402
from channel_token import token_from_vault  # noqa: E402
from util_paths import _host_label, channel_access_path, claude_home_path, claude_project_slug, legacy_dotted_workspace, shared_personal_path  # noqa: E402
import slack_access  # noqa: E402
from workspace_default import resolve_workspace, status_read_path  # noqa: E402
from workspace_layout import inspect_layout  # noqa: E402
from sutando_config import resolve_core_runtime, resolve_down_bridge_action  # noqa: E402
from cron_entry_digest import digest_map, drifted  # noqa: E402
from task_archive import find_task_file  # noqa: E402

# Workspace = runtime-state root (tasks/, results/, state/). REPO_DIR stays the
# source-code root (src/, skills/, logs/, .env, build_log.md). Before PR #762's
# resolver existed, every consumer hardcoded REPO_DIR / "tasks" — so when the
# owner set $SUTANDO_WORKSPACE to a non-repo location, health-check kept
# writing alerts to <repo>/tasks/ while the watcher was reading from
# $SUTANDO_WORKSPACE/tasks/. Three task-health alerts on 2026-05-16 landed in
# the wrong dir before this fix; same drift class as src/watch-tasks-stream.sh
# pre-#736 and skills/self-diagnose pre-#769.
WORKSPACE_DIR = resolve_workspace()

# Sentinel key in the failure-alert dedup state files (health-last-alerted /
# -notified / -slacked .json) that stores the most-recently-alerted
# failure-set hash, distinct from the per-hash timestamp entries used for
# 24h pruning. sha256 hex digests are [0-9a-f]-only, so this can never
# collide with a real hash_key.
_LAST_HASH_KEY = "_last_hash"

def _default_memory_dir() -> str:
    """Claude Code memory dir under the workspace claude-home.

    Mirrors how Claude Code itself resolves memory: <claude-home>/projects/
    <slug>/memory. Pre-#1454 this hardcoded ~/.claude/projects/<slug>/memory,
    which ignored the workspace-scoped CLAUDE_CONFIG_DIR — so on a migrated
    install the probe read an empty/stale ~/.claude path instead of the
    workspace memory dir (where Claude Code actually writes and the vault
    syncs), which forced a SUTANDO_MEMORY_DIR override to compensate.
    claude_home_path() honors CLAUDE_CONFIG_DIR, falling back to ~/.claude
    only when it is unset (preserving the old path for ad-hoc launches).
    """
    repo = Path(__file__).parent.parent.resolve()
    slug = claude_project_slug(repo)
    return str(Path(claude_home_path()) / "projects" / slug / "memory")

# SUTANDO_MEMORY_DIR stays authoritative here, same as everywhere else that
# resolves core memory (src/voice-agent.ts, src/voice-context.ts, and
# CLAUDE.md/AGENTS.md all honor it). An earlier version of this fix made
# ONLY this check ignore the override, on the theory that it was purely a
# stale pre-#1454 workaround (see _default_memory_dir()'s docstring) — but
# that broke the invariant that this check reports on the SAME directory the
# rest of the runtime actually reads/writes, which is a worse failure mode
# than the one being fixed (a health check silently diverging from ground
# truth). If SUTANDO_MEMORY_DIR is a genuine leftover from that era, the
# memory-dir-override check below flags the divergence instead of silently
# redirecting.
MEMORY_DIR = Path(os.environ.get("SUTANDO_MEMORY_DIR", _default_memory_dir()))

# How much of MEMORY.md a session actually loads. These are the RUNTIME's
# documented numbers, not this repo's guess:
#
#   "The first 200 lines of MEMORY.md, or the first 25KB, whichever comes
#    first" — and ONLY MEMORY.md. Content beyond it is dropped; the prefix still
#    loads. YAML frontmatter and block-level HTML comments are stripped before
#    those limits are measured.
#   https://code.claude.com/docs/en/memory#how-it-works
#
# Earlier revisions of this check encoded a measured-by-eye ~24KB cutoff and
# claimed the WHOLE index stops loading past it. Both were wrong (john-the-dev,
# #2449): the limit is line-OR-byte, and truncation is a suffix drop, not total
# loss. They are plain constants rather than env knobs on purpose — they mirror
# a documented external contract, so a deployment that "tunes" them is just
# lying to itself about what its runtime does. Undeclared env vars are also
# forbidden by AGENTS.md (qingyun-wu, #2449).
MEMORY_INDEX_LOAD_LINES = 200
# 25 KB DECIMAL (25_000), not 25 KiB. The docs say "the first 25KB"; encoding it
# as 25 * 1024 made this check 600 B more generous than the runtime, so a file
# between 25_000 and 25_600 reports healthy while its tail is already dropped.
#
# The runtime settles it: its own over-limit warning prints the limit as
# "24.4KB", and 25_000 / 1024 = 24.41 — a 25_600 limit would print "25.0KB".
# Observed on this repo 2026-08-02, where MEMORY.md was truncated at session
# start while this check called it under the limit.
#
# NB this is a UNITS fix, not a return to the "measured-by-eye ~24KB" cutoff
# that #2449 rightly rejected — that guess also claimed the whole index stops
# loading, which remains wrong: truncation is a suffix drop.
MEMORY_INDEX_LOAD_BYTES = 25_000
# Warn while there is still room to compact deliberately rather than in a panic.
MEMORY_INDEX_NEAR_LIMIT = 0.9


def _index_effective_text(text: str) -> str:
    """Drop what the runtime strips BEFORE it measures the 200-line/25KB limits.

    Counting raw bytes over-reports: a file whose bulk is frontmatter or a block
    HTML comment measures large here but small to the runtime. That produced a
    false `fail` on a 25.6KB fixture whose visible content was one line
    (john-the-dev, #2449).
    """
    if text.startswith("---"):
        m = re.match(r"^---\r?\n.*?\r?\n---[ \t]*\r?\n?", text, re.DOTALL)
        if m:
            text = text[m.end():]

    # Block-level HTML comments only, and NOT inside a fenced code block: the
    # runtime strips block comments but PRESERVES them inside code fences, so a
    # regex that ignores fence state under-reports. A 28KB comment wrapped in
    # ```html measured as 37 bytes here and returned a false `ok` while the
    # entry after it was genuinely past the cut (john-the-dev, #2449).
    #
    # Line-oriented rather than one regex, because fence state and multi-line
    # comment state both have to be tracked, and a regex cannot carry either.
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    in_comment = False
    def _block_indent_ok(raw: str) -> bool:
        # CommonMark bounds a block-level marker to at most THREE columns of
        # indentation; at four the line is indented CODE, not a marker. This
        # gates BOTH markers we track:
        #   fence  — a 4-space ```html is not a fence, so the comment inside it
        #            must be stripped, not preserved (false `fail`; qingyun-wu).
        #   <!--   — a 4-space or TAB indented comment is code CONTENT and must
        #            count toward the 25KB prefix, not be stripped (false `ok`
        #            on a 30KB fixture that measured 18 bytes; qingyun-wu).
        # Counted in COLUMNS, not characters: a bare lstrip(" ") ignored tabs, and
        # one tab already reaches the 4-column stop. Applies to a fence closer
        # too — an over-indented closer must not close a real fence.
        n = 0
        for ch in raw:
            if ch == " ":
                n += 1
            elif ch == "\t":
                n += 4 - (n % 4)          # advance to the next 4-column tab stop
            else:
                break
            if n >= 4:
                return False
        return True

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        indent_ok = _block_indent_ok(line)
        if in_fence:
            out.append(line)
            # CommonMark: a fence closes only on the SAME character, repeated at
            # least as many times as the opener, alone on its line. Truncating the
            # marker to three characters let an inner ``` line close a ````
            # fence early — the comment after it then fell outside any fence, was
            # stripped, and a 28KB file measured 39 bytes: false `ok`
            # (rui-sutando-codex, #2449).
            if (indent_ok and stripped[:1] == fence_char
                    and re.fullmatch(re.escape(fence_char) + "{%d,}" % fence_len,
                                     stripped.rstrip())):
                in_fence = False
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        m = re.match(r"(`{3,}|~{3,})", stripped) if indent_ok else None
        if m and m.group(1)[0] == "`" and "`" in stripped[m.end():]:
            # CommonMark: a BACKTICK fence's info string may not contain a
            # backtick (it would be ambiguous with inline code). ```bad`info is
            # therefore an ordinary paragraph line, not an opener. Accepting it
            # opened a phantom fence, so the block comment that followed was
            # PRESERVED and a 31KB fixture measured 31KB: a false `fail` telling
            # the operator to compact an index that loads fine (john-the-dev,
            # #2449). Tilde fences have no such rule — ~~~a`b IS a valid opener.
            out.append(line)
            continue
        if m:
            in_fence = True
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))     # FULL length — see the close check above
            out.append(line)
            continue
        if indent_ok and stripped.startswith("<!--"):
            if "-->" not in line:
                in_comment = True
            continue                      # whole-line block comment: dropped
        out.append(line)
    return "".join(out)


def _index_loaded_prefix(text: str) -> "tuple[str, int, int]":
    """Return (prefix_that_loads, bytes_loaded, lines_loaded) per the contract.

    Whichever of the two limits is reached first stops the read. A line that
    would straddle the byte limit is treated as not loaded — the conservative
    reading, and the one that matters here since a half-read index line cannot
    be relied on to name its memory file.
    """
    kept: list[str] = []
    total = 0
    for i, line in enumerate(text.splitlines(keepends=True)):
        if i >= MEMORY_INDEX_LOAD_LINES:
            break
        nbytes = len(line.encode("utf-8"))
        if total + nbytes > MEMORY_INDEX_LOAD_BYTES:
            # The limit is a BYTE prefix, not a line count — the session reads
            # the bytes up to the cut, so a line that STARTS inside the budget
            # is partially read. Dropping it whole marked an entry whose
            # filename sat comfortably before the cut as unreadable, failing an
            # index that loads fine (qingyun-wu, #2449). Keep the bytes that fit.
            room = MEMORY_INDEX_LOAD_BYTES - total
            if room > 0:
                # errors="ignore" so a cut through a multi-byte character
                # yields the readable prefix instead of raising.
                kept.append(line.encode("utf-8")[:room].decode("utf-8", errors="ignore"))
                total += room
            break
        kept.append(line)
        total += nbytes
    return "".join(kept), total, len(kept)


def _resolve_dotenv() -> Path:
    """Resolve the `.env` path via the canonical resolver.

    The 2-tier fallback (repo root -> workspace, #1871) lives in
    `sutando_config.py` — the canonical resolver — so this consumer never
    inlines the path. (The #1973 Sutando.app bundle tier is deferred pending the
    app-bundle install-location decision — see sutando_config.resolve_dotenv.)
    """
    from sutando_config import resolve_dotenv  # noqa: PLC0415
    return resolve_dotenv(REPO_DIR, WORKSPACE_DIR)


_VOICE_ENV_KEYS = ("SKIP_VOICE", "GEMINI_VOICE_API_KEY", "GEMINI_API_KEY")


def managed_voice_credential_present(path: Optional[Path] = None) -> bool:
    """True when the managed-credentials file carries a usable voice key.

    Mirrors `_managed_voice_credential_present` in startup-runtime.sh, including
    its fallback order (`CAPABILITY_FALLBACKS['gemini-voice']`), its
    malformed-file contract — an unreadable or malformed file SKIPS the tier
    rather than raising, matching readManaged()'s try/catch — and the S1 truth
    table (design 2b): a `quarantined: true` file's entries are ABSENT in every
    mode, and an explicit `voicePreference: "byok"` skips the managed tier
    entirely. Guards identical to the resolvers and the shell gate;
    tests/voice-preference-consumers.test.sh pins the agreement.

    Deliberately NOT fail-closed, unlike the dotenv parsing above. The two cases
    differ: a malformed SKIP_VOICE means someone configured voice and we cannot
    tell how, so hiding it would mask an outage. A malformed managed file means
    the managed tier is unusable, so startup will not boot voice either — and
    reporting "enabled" there would invent an outage that cannot exist. Match
    the launcher, because the whole bug was the two disagreeing.
    """
    if path is None:
        path = WORKSPACE_DIR / "state" / "auth" / "managed-credentials.json"
    try:
        doc = json.loads(Path(path).read_text()) or {}
        caps = doc.get("capabilities") or {}
        if not isinstance(caps, dict):
            return False
    except Exception:
        return False
    if doc.get("quarantined") is True or doc.get("voicePreference") == "byok":
        return False
    for slot in ("gemini-voice", "gemini-text"):
        entry = caps.get(slot)
        key = entry.get("key") if isinstance(entry, dict) else None
        if isinstance(key, str) and key:
            return True
    return False


def managed_voice_preference(path: Optional[Path] = None) -> str:
    """The committed voice credential-source preference: managed | byok | unset.

    Twin of `_voice_credential_preference` in startup-runtime.sh. Every failure
    mode — missing file, malformed JSON, out-of-vocabulary value — reads as
    "unset": the legacy resolution every pre-preference install runs under.
    The byok/quarantine enforcement never rides on this helper alone;
    managed_voice_credential_present re-checks the marker fields directly.
    """
    if path is None:
        path = WORKSPACE_DIR / "state" / "auth" / "managed-credentials.json"
    try:
        doc = json.loads(Path(path).read_text())
        pref = doc.get("voicePreference") if isinstance(doc, dict) else None
    except Exception:
        pref = None
    return pref if pref in ("managed", "byok") else "unset"


def resolve_voice_health_config(
    env: Optional[dict] = None,
    env_path: Optional[Path] = None,
) -> dict:
    """Resolve whether voice health checks are required.

    Process environment values win over the canonical dotenv file, matching
    the already-running service configuration that health-check observes.
    Missing configuration is a supported text-only mode. A present but
    unreadable or malformed relevant value is an error: failing closed avoids
    hiding a configured voice outage behind an accidental "disabled" result.
    """
    env = os.environ if env is None else env
    env_path = _resolve_dotenv() if env_path is None else env_path
    file_values = {}
    if env_path.exists():
        try:
            lines = env_path.read_text().splitlines()
        except OSError as exc:
            return {"enabled": True, "error": f"{env_path.name} unreadable ({exc})"}
        for line_no, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            assignment = line
            if assignment.startswith("export "):
                assignment = assignment[len("export "):].lstrip()
            if "=" not in assignment:
                if assignment in _VOICE_ENV_KEYS:
                    return {
                        "enabled": True,
                        "error": f"{env_path.name}:{line_no} malformed {assignment} assignment",
                    }
                continue
            key, value = assignment.split("=", 1)
            key = key.strip()
            if key not in _VOICE_ENV_KEYS:
                continue
            try:
                parsed = shlex.split(value, comments=True, posix=True)
            except ValueError as exc:
                return {
                    "enabled": True,
                    "error": f"{env_path.name}:{line_no} malformed {key} value ({exc})",
                }
            if len(parsed) > 1:
                return {
                    "enabled": True,
                    "error": f"{env_path.name}:{line_no} malformed {key} value",
                }
            file_values[key] = parsed[0] if parsed else ""

    def effective(key: str) -> str:
        value = env[key] if key in env else file_values.get(key, "")
        return str(value).strip()

    skip_voice = effective("SKIP_VOICE")
    # S1 truth table (design 2b): an explicit `voicePreference: managed` is
    # decided by the managed gate ALONE — a present env key must NOT silently
    # satisfy a managed preference (that is the logout-quarantine bypass:
    # quarantined managed entries + a leftover BYO env key would otherwise
    # report voice enabled off a source the resolver refuses). Same order as
    # configure_startup_runtime; tests/voice-preference-consumers.test.sh pins
    # launcher/health/resolver agreement per matrix row.
    preference = managed_voice_preference()
    if preference == "managed":
        if managed_voice_credential_present():
            return {"enabled": True, "detail": "managed voice credential configured"}
        return {
            "enabled": False,
            "detail": (
                "disabled (voicePreference=managed but no usable managed credential"
                " — quarantined or missing; env keys do not satisfy a managed"
                " preference; sign in to renew or switch to BYOK)"
            ),
        }
    if effective("GEMINI_VOICE_API_KEY") or effective("GEMINI_API_KEY"):
        if preference == "byok":
            return {
                "enabled": True,
                "detail": "Gemini voice credential configured (BYOK preference)",
            }
        return {"enabled": True, "detail": "Gemini voice credential configured"}
    if skip_voice not in ("", "0", "1"):
        return {"enabled": True, "error": f"invalid SKIP_VOICE={skip_voice!r}"}
    # The MANAGED tier, checked in the same order startup-runtime.sh uses: BYO env
    # first, then managed, and only then SKIP_VOICE. Without this the two disagree —
    # startup-runtime.sh:52-58 boots voice on a managed credential while this
    # returned "disabled", so all four voice checks reported `ok — disabled` over a
    # running-and-broken voice agent. A health check that reports "disabled" about a
    # service that is actually running is worse than no check: it converts an outage
    # into a green light. (#2197 review blocker, john-the-dev 2026-07-30T01:53.)
    #
    # This check MUST sit above the SKIP_VOICE=1 return, not below it. Placing it
    # below narrowed the bug without resolving it: the launcher *unsets* an inherited
    # SKIP_VOICE when a managed credential exists, so the composition "managed key +
    # inherited SKIP_VOICE=1" still had startup booting voice while health reported
    # disabled. The managed-only test could not catch it because it omits SKIP_VOICE.
    # (#2197 review blocker, john-the-dev 2026-07-31T05:37.)
    # (Under `byok` or quarantine the gate is False by construction, so this
    # branch cannot re-enable a source the resolver refuses.)
    if managed_voice_credential_present():
        return {"enabled": True, "detail": "managed voice credential configured"}
    if skip_voice == "1":
        return {"enabled": False, "detail": "disabled by SKIP_VOICE=1"}
    if preference == "byok":
        # Named reason, not a generic "no credential": a BYOK preference with
        # managed entries on disk must read as *disabled with a reason*, never
        # as "managed credential configured" (impl plan WS2 Step 4).
        return {
            "enabled": False,
            "detail": (
                "disabled (BYOK preference set (managed entries ignored); set"
                " GEMINI_VOICE_API_KEY or GEMINI_API_KEY)"
            ),
        }
    return {"enabled": False, "detail": "disabled (no Gemini voice credential configured)"}


def resolve_web_client_port(
    env: Optional[dict] = None,
    env_path: Optional[Path] = None,
) -> dict:
    """Resolve CLIENT_PORT with the same sourced-dotenv precedence as startup."""
    env = os.environ if env is None else env
    env_path = _resolve_dotenv() if env_path is None else env_path
    file_value: Optional[str] = None
    file_has_value = False

    if env_path.exists():
        try:
            lines = env_path.read_text().splitlines()
        except OSError as exc:
            return {"error": f"{env_path.name} unreadable ({exc})"}
        for line_no, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            assignment = line
            if assignment.startswith("export "):
                assignment = assignment[len("export "):].lstrip()
            if not assignment.startswith("CLIENT_PORT"):
                continue
            if "=" not in assignment:
                return {"error": f"{env_path.name}:{line_no} malformed CLIENT_PORT assignment"}
            key, value = assignment.split("=", 1)
            if key.strip() != "CLIENT_PORT":
                continue
            try:
                parsed = shlex.split(value, comments=True, posix=True)
            except ValueError as exc:
                return {"error": f"{env_path.name}:{line_no} malformed CLIENT_PORT value ({exc})"}
            if len(parsed) > 1:
                return {"error": f"{env_path.name}:{line_no} malformed CLIENT_PORT value"}
            file_value = parsed[0] if parsed else ""
            file_has_value = True

    configured = file_value if file_has_value else env.get("CLIENT_PORT")
    value = str(configured or "8080").strip()
    try:
        port = int(value)
    except ValueError:
        return {"error": f"invalid CLIENT_PORT={value!r}"}
    if not 1 <= port <= 65535:
        return {"error": f"invalid CLIENT_PORT={value!r}"}
    return {"port": port}


def check_voice_stack(
    env: Optional[dict] = None,
    env_path: Optional[Path] = None,
) -> list[dict]:
    """Return config-aware voice-agent, watcher, and transport checks."""
    config = resolve_voice_health_config(env=env, env_path=env_path)
    if config.get("error"):
        config_check = {
            "name": "voice-config",
            "status": "down",
            "detail": config["error"],
        }
    else:
        config_check = None

    if not config["enabled"]:
        detail = config["detail"]
        return [
            {"name": "voice-agent", "status": "ok", "detail": detail},
            {"name": "voice-watchers", "status": "ok", "detail": detail},
            {"name": "voice-transport", "status": "ok", "detail": detail},
            {"name": "bodhi-dist", "status": "ok", "detail": detail},
        ]

    voice_check = check_port(9900, "voice-agent", probe=True)
    if voice_check["status"] == "ok":
        mark_stale_if_outdated(
            voice_check,
            REPO_DIR / "src" / "voice-agent.ts",
            "voice-agent.ts",
        )
    checks = [
        voice_check,
        check_voice_watchers(voice_check),
        check_voice_transport(voice_check),
        check_bodhi_dist(),
    ]
    if config_check is not None:
        checks.insert(0, config_check)
    return checks


def _resolved_vault() -> dict:
    """Return the resolved vault config subtree via the canonical resolver
    (`sutando_config.resolve_vault`) — the SINGLE source of truth for
    `vault.enabled` and `vault.remote_url`.

    Augments the resolver's dict with `_explicit_disable`: True only when the
    config file actually carries `vault.enabled=false` (a deliberate opt-out),
    as opposed to the resolver's default-False for a host with no vault block
    at all. This lets check_memory_sync distinguish "opted out on purpose" from
    "never configured" without re-reading config.

    Best-effort: on any error (resolver import failure, malformed config) return
    safe defaults ({"enabled": False, "remote_url": ""}) so a config-helper
    hiccup never masks a real check. Mirrors resolve_vault's own defaults.
    """
    try:
        from sutando_config import resolve_vault, load_config  # noqa: PLC0415
        vault = dict(resolve_vault(repo_root=REPO_DIR))
        raw_vault = load_config(repo_root=REPO_DIR).get("vault") or {}
        vault["_explicit_disable"] = raw_vault.get("enabled") is False
        return vault
    except Exception:
        return {"enabled": False, "remote_url": "", "_explicit_disable": False}


def _vault_remote_url(vault: "dict | None" = None) -> str:
    """Vault remote URL as the sync writer's CRON path resolves it: config
    `vault.remote_url`, then the deprecated `.env` SUTANDO_MEMORY_REPO alias."""
    resolved = _resolved_vault() if vault is None else vault
    url = resolved.get("remote_url") or ""
    if url:
        return url
    env_path = _resolve_dotenv()
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("SUTANDO_MEMORY_REPO="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def twilio_configured(env_content: str) -> bool:
    """True only when .env has an ACTIVE TWILIO_ACCOUNT_SID with a value.

    A plain substring test also matched the commented placeholder shipped in
    the .env template (`# TWILIO_ACCOUNT_SID=ACxxxxxxxxx`), so hosts that
    never configured Twilio still ran the conversation-server + tunnel
    checks — and startup.sh's matching gate kept a public ngrok tunnel open
    to a port with nothing behind it (caught 2026-07-02). startup.sh's
    phone block carries the anchored-grep equivalent of this test.
    """
    for line in env_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("TWILIO_ACCOUNT_SID=") and stripped.split("=", 1)[1].strip():
            return True
    return False


def resolve_node_runtime(env: Optional[dict] = None, which=shutil.which) -> dict:
    """G1.5 node-bundle: resolve the Node executable the engine's JS services
    would use, in the same precedence as `sutando-config.sh node-bin`:

      1. $SUTANDO_NODE — exact executable exported by the desktop app.
      2. $SUTANDO_APP_NODE_DIR/node (or its default app-support home) — the
         bundled runtime found at rest (launchd jobs without the env var).
      3. `node` on PATH — dev/OSS hosts.

    Returns {"source": "bundled"|"app-bundle"|"system"|"none", "path": str|None}.
    Pure over (env, which) for testability.
    """
    env = os.environ if env is None else env
    explicit = env.get("SUTANDO_NODE", "")
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return {"source": "bundled", "path": explicit}
        # Owner review P1-1: SUTANDO_NODE is the desktop's explicit
        # declaration — set-but-invalid is a packaging error, NOT a case to
        # silently rescue via PATH. Surface it as its own failure source.
        return {"source": "invalid-explicit", "path": explicit}
    app_dir = env.get("SUTANDO_APP_NODE_DIR", "") or os.path.expanduser(
        "~/Library/Application Support/space.ag2.app/engine/runtime/node/bin"
    )
    app_node = os.path.join(app_dir, "node")
    if os.path.isfile(app_node) and os.access(app_node, os.X_OK):
        return {"source": "app-bundle", "path": app_node}
    on_path = which("node")
    if on_path:
        # Desktop-managed installs should never end up here (bundled runtime
        # dir exists but its node is broken/missing) — flag it so the probe
        # can degrade instead of reporting a false green (owner review).
        if os.path.isdir(app_dir):
            return {"source": "system-degraded", "path": on_path}
        return {"source": "system", "path": on_path}
    return {"source": "none", "path": None}


def check_node_runtime() -> dict:
    """Surface WHICH node the JS services resolve to — or a loud red line when
    none exists. The 2026-07-13 outage class: an interactive terminal finding
    node does NOT mean launchd-/app-spawned services can (credential-proxy sat
    dead for days on this failure). "none" is a real issue, not a warn: every
    JS service (voice, phone, proxy, web-client) silently fails to start.
    """
    resolved = resolve_node_runtime()
    if resolved["source"] == "none":
        return {
            "name": "node-runtime",
            "status": "down",
            "detail": "no node found (no SUTANDO_NODE, no app bundle, none on PATH) — JS services cannot start",
        }
    if resolved["source"] == "invalid-explicit":
        return {
            "name": "node-runtime",
            "status": "down",
            "detail": f"SUTANDO_NODE set but not executable: {resolved['path']} — desktop packaging error (fail-closed, no PATH fallback)",
        }
    if resolved["source"] == "system-degraded":
        return {
            "name": "node-runtime",
            "status": "warn",
            "detail": f"bundled runtime dir present but its node is unusable — running on system node {resolved['path']} (pinned-runtime guarantee NOT in effect)",
        }
    # Executable permission alone doesn't prove the runtime can launch the
    # bundled services (Codex re-review F3): a --version that errors or fails
    # to run means every JS service dies at spawn — that is DOWN, not ok.
    try:
        out = subprocess.run(
            [resolved["path"], "--version"], capture_output=True, text=True, timeout=5
        )
        if out.returncode != 0:
            return {
                "name": "node-runtime",
                "status": "down",
                "detail": f"node at {resolved['path']} failed --version (rc={out.returncode}) — runtime not runnable",
            }
        version = out.stdout.strip()
    except Exception as exc:
        return {
            "name": "node-runtime",
            "status": "down",
            "detail": f"node at {resolved['path']} could not be executed ({exc}) — runtime not runnable",
        }
    # Version floor (external review on #2182): bundled services use node:sqlite,
    # which needs Node >= 22.5 — an older node passes every other probe here but
    # crashes those services at import. Unparseable versions degrade to warn
    # (never block on a formatting surprise), too-old is DOWN with the fix named.
    floor = (22, 5)
    parsed = re.match(r"v?(\d+)\.(\d+)", version or "")
    if parsed is None:
        return {
            "name": "node-runtime",
            "status": "warn",
            "detail": f"node at {resolved['path']} reported unparseable version {version!r} — cannot verify the >=22.5 floor (node:sqlite)",
        }
    if (int(parsed.group(1)), int(parsed.group(2))) < floor:
        return {
            "name": "node-runtime",
            "status": "down",
            "detail": f"{version} via {resolved['source']} is below the 22.5 floor (node:sqlite) — bundled services will crash; upgrade the runtime",
        }
    return {
        "name": "node-runtime",
        "status": "ok",
        "detail": f"{version} via {resolved['source']} ({resolved['path']})",
    }


def check_port(port: int, name: str, probe: bool = False) -> dict:
    """Check if a port is listening, optionally probing for a live response.

    A wedged server can keep its listen socket open while never answering
    (2026-06-10: voice-agent accepted TCP for 26h with a dead event loop, so
    the dashboard's WS connect hung forever). With probe=True, send a minimal
    HTTP GET and require *any* response bytes — a healthy HTTP server replies
    with a status line and a healthy WS server replies 400/426 to a plain GET,
    while a wedged one sends nothing.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            result = s.connect_ex(("127.0.0.1", port))
            up = result == 0
            if up and probe:
                try:
                    # 10s: dashboard.py takes ~3.5s to first byte (collects
                    # data via subprocesses before responding). Wedged servers
                    # never send anything, so the verdict is still decisive.
                    s.settimeout(10)
                    # Probe an unknown path, NOT "/": dashboard's "/" collects
                    # data including a health-check.py subprocess — probing it
                    # from health-check recursed (probe → render → health-check
                    # → probe …) and amplified into a request storm. A 404 is
                    # still response bytes, which is all liveness needs.
                    s.sendall(f"GET /__liveness_probe__ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
                    if not s.recv(1):
                        raise TimeoutError("no response bytes")
                    # Drain the rest of the response before close. Closing
                    # with unread bytes sends RST, so the probed server's
                    # response write fails mid-flight — one BrokenPipeError
                    # traceback in ITS log per health run. Connection: close
                    # means a healthy server EOFs right after the response;
                    # cap time and bytes so a misbehaving one can't stall us.
                    # The verdict is already decided by the first byte, so
                    # drain failures are ignored rather than marked wedged.
                    s.settimeout(2)
                    drained = 0
                    try:
                        while drained < 65536:  # pragma: no cover — socket recv timing makes this hard to instrument in CI
                            chunk = s.recv(4096)
                            if not chunk:
                                break
                            drained += len(chunk)
                    except OSError:  # pragma: no cover — only fires on recv error mid-drain; not triggered in tests
                        pass
                except Exception:
                    return {
                        "name": name,
                        "status": "wedged",
                        "detail": f"port {port} listening but unresponsive — restart needed",
                    }
        return {"name": name, "status": "ok" if up else "down", "detail": f"port {port}"}
    except Exception as e:
        return {"name": name, "status": "error", "detail": str(e)}


def check_launchd(label: str) -> dict:
    """Check if a launchd job is loaded and running."""
    try:
        result = subprocess.run(
            ["/bin/launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.split("\n"):
            if label in line:
                parts = line.split("\t")
                pid = parts[0].strip() if len(parts) > 0 else "-"
                exit_code = parts[1].strip() if len(parts) > 1 else "?"
                running = pid != "-" and pid != ""
                status = "ok" if running or exit_code == "0" else "stopped"
                return {"name": label, "status": status, "detail": f"pid={pid} exit={exit_code}"}
        return {"name": label, "status": "not_loaded", "detail": "not found in launchctl list"}
    except Exception as e:
        return {"name": label, "status": "error", "detail": str(e)}


def check_cron_runner(
    workspace_dir: Optional[Path] = None,
    host_label: Optional[str] = None,
    runtime: Optional[str] = None,
    launchd_check=None,
    now: Optional[float] = None,
) -> dict:
    """Detect configured schedules that have no durable Codex owner."""
    workspace = Path(workspace_dir or WORKSPACE_DIR)
    host = host_label or _host_label()
    crons_file = workspace / "hosts" / host / "crons.json"
    name = "cron-runner"
    try:
        crons = json.loads(crons_file.read_text())
    except FileNotFoundError:
        return {"name": name, "status": "ok", "detail": "no schedules configured"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": name, "status": "fail", "detail": f"cannot read crons.json ({exc})"}
    if not isinstance(crons, list):
        return {"name": name, "status": "fail", "detail": "crons.json is not a list"}

    # One owner: the reconciler and the Codex scheduler bind this same predicate.
    from cron_execution_form import launchd_eligible as eligible  # noqa: PLC0415

    launchd_count = sum(
        1 for entry in crons if isinstance(entry, dict) and entry.get("launchd") is True
    )
    runtime = runtime or resolve_core_runtime(repo_root=REPO_DIR)
    orphaned = sum(1 for entry in crons if isinstance(entry, dict) and eligible(entry))
    if runtime == "codex" and orphaned:
        return {
            "name": name,
            "status": "down",
            "detail": f"{orphaned} configured schedule(s) have no durable Codex owner",
        }
    if not launchd_count:
        return {"name": name, "status": "ok", "detail": "no launchd-owned schedules"}

    probe = (launchd_check or check_launchd)("com.sutando.cron-runner")
    state_file = workspace / "state" / "cron-runner-state.json"
    try:
        age = (float(time.time() if now is None else now) - state_file.stat().st_mtime)
        age_err = None
        # A negative age is a clock step or a bad write, never freshness. Discard
        # it here so no branch below can read it as proof that work is happening.
        if age < 0:
            age, age_err = None, f"state is future-dated by {int(-age)}s"
    except FileNotFoundError:
        age, age_err = None, "state file is missing"
    except OSError as exc:
        age, age_err = None, f"state unreadable ({exc})"

    if probe["status"] != "ok":
        # `check_launchd` reports the LAST invocation. A killed one with a fresh
        # state file means later ones complete, so the schedules still fire.
        if age is not None and age <= 180:
            return {
                "name": name,
                "status": "warn",
                "detail": (f"{launchd_count} schedule(s); launchd reports {probe['status']} "
                           f"({probe.get('detail', '')}) but the runner wrote state "
                           f"{int(age)}s ago — invocations are being killed and "
                           "relaunched, schedules still fire"),
            }
        # Name why freshness could not vouch for it; a clock step here is exactly
        # the neighbour of whatever else has gone wrong.
        return {
            "name": name,
            "status": "down",
            "detail": (f"{launchd_count} schedule(s) configured but launchd is "
                       f"{probe['status']}" + (f"; {age_err}" if age_err else "")),
        }

    if age_err is not None:
        return {"name": name, "status": "down", "detail": f"runner loaded but {age_err}"}
    if age > 180:
        return {
            "name": name,
            "status": "down",
            "detail": f"runner state is stale ({int(age)}s; expected <=180s)",
        }
    return {
        "name": name,
        "status": "ok",
        "detail": f"{launchd_count} durable schedule(s), state {int(age)}s old",
    }


_MONTH_MAX_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                   7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _cron_field_values(field: str, lo: int, hi: int) -> "set[int] | None":
    """Expand one numeric cron field to its value set, or None if not understood.

    None means "do not reason about this expression" — every caller below treats
    an unparseable field as schedulable, so a parser gap can never invent a
    never-fires verdict.
    """
    out: "set[int]" = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                return None
            step = int(raw_step)
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            start_s, _, end_s = part.partition("-")
            if not (start_s.isdigit() and end_s.isdigit()):
                return None
            start, end = int(start_s), int(end_s)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None  # names (JAN/MON), L, W, # — not our business
        if start > end or start < lo or end > hi:
            return None
        out.update(range(start, end + 1, step))
    return out or None


def _cron_can_never_fire(expr: str) -> bool:
    """True only when a 5-field cron's day-of-month/month pair is impossible.

    A deliberately-parked entry is the real case: this host carries
    `wire-newsroom-nightly-DISABLED-...` at `0 0 31 2 *` — February 31st, which
    is syntactically valid and can never occur. /schedule-crons cannot register
    it, so it was counted as expected-but-missing and the session-crons guard
    warned on every run, permanently. A guard that always warns is a guard
    nobody reads, and this one exists to catch a SILENT failure (a peer
    instance registered 2/18 with no error) — so a standing false positive
    disables exactly the signal it was built to raise.

    Deliberately conservative: day-of-week must be unrestricted, because cron
    ORs day-of-month with day-of-week — `0 0 31 2 MON` still fires on Mondays.
    Anything unparseable returns False (schedulable).
    """
    parts = expr.split()
    if len(parts) != 5:
        return False
    _, _, dom_f, month_f, dow_f = parts
    if dow_f.strip() not in ("*", "?"):
        return False  # OR-semantics with day-of-week — it can still fire
    doms = _cron_field_values(dom_f, 1, 31)
    months = _cron_field_values(month_f, 1, 12)
    if doms is None or months is None:
        return False
    return not any(d <= _MONTH_MAX_DAYS[m] for m in months for d in doms)


def _entry_marked_parked(entry: dict) -> bool:
    """True when the entry carries an explicit "deliberately disabled" signal.

    An impossible date alone must NOT qualify. `0 0 31 2 *` is equally the
    signature of a parked job and of an active typo — someone meaning "the 31st,
    monthly" and writing February. Excluding on the date alone would let a
    mistyped ACTIVE schedule vanish from `expected`, so CronCreate silently
    omits it and this guard reports green forever: the precise silent-miss class
    the check exists to surface.

    Two accepted signals: an explicit `disabled: true` field, and the
    established convention of DISABLED in the entry name (used by this host's
    `wire-newsroom-nightly-DISABLED-2026-06-09-...`, which also records why).
    """
    if entry.get("disabled") is True:
        return True
    name = entry.get("name")
    return isinstance(name, str) and "DISABLED" in name


def check_session_cron_registration(
    workspace_dir: Optional[Path] = None,
    host_label: Optional[str] = None,
    runtime: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """Warn when session-owned crons were never (re-)registered for this core boot.

    CronCreate registrations are session-only: they die with the session and
    only exist if /schedule-crons completed after the core booted. The failure
    is silent (config intact on disk, zero live crons, no error) — observed
    2026-07-23 on a peer instance as 2/18 registered. The script-visible signal:
    /schedule-crons writes
    `hosts/<hostname>/schedule-crons-stamp.json` when it finishes; if that
    host-owned stamp predates the CURRENT SESSION'S LAUNCH
    (`_last_core_launch_at`, from `state/session-starts.log`), the current
    session never completed registration. Stamp AGE alone is deliberately not
    used — long-lived sessions would false-warn.

    The boundary is deliberately NOT `.alive.started_at`: that field tracks the
    heartbeat writer, which is retained across launches, so restarting the
    heartbeat under a live session made this probe report every still-registered
    cron as gone. Same field, same mistake, same fix as #2446.
    """
    workspace = Path(workspace_dir or WORKSPACE_DIR)
    host = host_label or _host_label()
    name = "session-crons"
    runtime = runtime or resolve_core_runtime(repo_root=REPO_DIR)

    crons_file = workspace / "hosts" / host / "crons.json"
    try:
        crons = json.loads(crons_file.read_text())
    except FileNotFoundError:
        return {"name": name, "status": "ok", "detail": "no schedules configured"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": name, "status": "warn", "detail": f"cannot read crons.json ({exc})"}
    if not isinstance(crons, list):
        return {"name": name, "status": "warn", "detail": "crons.json is not a list"}

    def session_owned(entry: dict) -> bool:
        if entry.get("launchd") is True or entry.get("execution") == "codex-task":
            return False
        # Same decision /schedule-crons makes, from the shared policy rather
        # than a copy: a form the session cannot run is not an expected cron.
        try:
            from cron_execution_form import (  # noqa: PLC0415
                EXECUTOR_FORMS, MALFORMED, select_for_executor)
            if select_for_executor(entry, EXECUTOR_FORMS["session"])[0] == MALFORMED:
                return False
        except Exception:
            pass  # policy unavailable — fall back to the structural checks
        cron_expr = entry.get("cron")
        if entry.get("loop") == "dynamic" or not cron_expr:
            return False
        if (
            _entry_marked_parked(entry)
            and isinstance(cron_expr, str)
            and _cron_can_never_fire(cron_expr)
        ):
            # BOTH signals required. Marked-disabled AND unregistrable (e.g.
            # `0 0 31 2 *`): /schedule-crons cannot register it, so counting it
            # as expected warns forever. An impossible date WITHOUT a disabled
            # marker is an active typo and must still warn.
            return False
        return True

    expected = sum(1 for e in crons if isinstance(e, dict) and session_owned(e))
    if runtime == "codex":
        # codex has no session CronCreate surface (check_cron_runner owns that
        # story), so nothing here applies regardless of the counts.
        return {"name": name, "status": "ok", "detail": "no session-owned schedules expected"}

    # `expected == 0` DELIBERATELY does not short-circuit here (@john-the-dev on
    # #2654). It used to, and that made the complete 1→0 transition the one case
    # the surplus check below could never see: move a host's last session cron to
    # `launchd: true` / `execution: codex-task`, park it, or delete it, and
    # `expected` reaches 0 while the job registered under the old config is still
    # firing. The probe then said `ok — no session-owned schedules expected`,
    # which is the WORST form of this failure: every session job can be stale and
    # health explicitly reports that none is expected.
    #
    # Zero-expected is not by itself evidence of health; it is only healthy when
    # nothing was registered either. That question is answered by the stamp, so
    # the decision moves BELOW the stamp read — the never-had-session-crons host
    # exits at the no-stamp branch, and a host that DID register something falls
    # through to the surplus check like any other.

    # The SESSION boundary, not the heartbeat's age. `.alive.started_at` is
    # `core_heartbeat._STARTED_AT`, stamped once at module load, and both launch
    # paths RETAIN an existing heartbeat process — so it tracks the heartbeat
    # writer, not the session that owns the crons. #2446 established exactly that
    # for `_marker_predates_running_core`, and `_last_core_launch_at` is the
    # boundary it introduced; this probe was still comparing against the field
    # that PR ruled out, for the same staleness question.
    #
    # Observed on Chis-Mac-mini 2026-08-04T03:0xZ, all nine expected crons live:
    #     core      pid 30961  started 11:32:30   <- .alive "pid" (the core)
    #     heartbeat pid 72981  started 16:13:56   <- .alive "heartbeat_pid"
    #     .alive started_at    16:13:56           <- tracks the WRITER
    #     stamp ts             11:37:21           <- 5 min after the core booted
    # The stamp was written for this very boot and got reported as predating it
    # by 16595s, telling the operator to re-run /schedule-crons against a session
    # whose crons were all present.
    #
    # None keeps its meaning from #2446: "no evidence", never "stale" — so a host
    # with no session-starts.log falls through to the stamp-only checks below
    # rather than warning.
    launch = _last_core_launch_at(workspace)
    started_at = launch[0] if launch else None

    stamp_file = workspace / "hosts" / host / "schedule-crons-stamp.json"
    try:
        stamp = json.loads(stamp_file.read_text())
    except FileNotFoundError:
        if expected == 0:
            # THE genuine never-had-session-crons host: nothing expected AND
            # nothing ever registered. This is the case the old `expected == 0`
            # short-circuit was really protecting, and it still exits healthy.
            return {"name": name, "status": "ok", "detail": "no session-owned schedules expected"}
        return {
            "name": name,
            "status": "warn",
            "detail": f"{expected} session cron(s) expected but /schedule-crons has never stamped completion — run /schedule-crons",
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": name, "status": "warn", "detail": f"stamp unreadable ({exc})"}

    if not isinstance(stamp, dict):
        return {"name": name, "status": "warn", "detail": "stamp malformed (expected an object)"}

    stamp_ts = stamp.get("ts")
    if isinstance(stamp_ts, bool) or not isinstance(stamp_ts, (int, float)):
        return {"name": name, "status": "warn", "detail": "stamp malformed (missing numeric ts)"}
    if started_at is not None and stamp_ts < started_at:
        if expected == 0:
            # The stamp is from a PREVIOUS session, and session crons die with
            # their session — so nothing it registered is still firing, and
            # nothing is expected now. Telling the operator to re-run
            # /schedule-crons here would be advice with no subject.
            return {"name": name, "status": "ok", "detail": "no session-owned schedules expected"}
        return {
            "name": name,
            "status": "warn",
            "detail": (
                f"stamp predates this session's launch ({int(started_at - stamp_ts)}s older) — "
                f"session crons are gone with the old session; re-run /schedule-crons"
            ),
        }

    registered = stamp.get("registered")
    if isinstance(registered, bool) or not isinstance(registered, int) or registered < 0:
        return {
            "name": name,
            "status": "warn",
            "detail": "stamp malformed (missing non-negative registered count)",
        }
    if registered < expected:
        return {
            "name": name,
            "status": "warn",
            "detail": f"only {registered}/{expected} session cron(s) registered at last /schedule-crons",
        }

    # OWNERSHIP TRANSITION (@john-the-dev on #2654). The check above runs one
    # way only, and the opposite inequality is a real failure it cannot see:
    # edit a registered entry to `launchd: true` or `execution: codex-task` and
    # it leaves `expected`, while the session job registered under the old
    # config KEEPS FIRING. Counts then read registered=2, expected=1, and
    # `2 < 1` is false — green. The digest cannot see it either, deliberately:
    # entry_digest ignores the ownership fields precisely so that an entry which
    # correctly stopped being registered is not reported as edited.
    #
    # So the signal is the surplus itself. It also covers an entry deleted from
    # crons.json whose job was never de-registered — same disruption, same remedy.
    #
    # There is deliberately NO allowance for step 4's bootstrap fallback (which
    # registers /proactive-loop when the config lacks it, legitimately putting
    # registered one above expected). Subtracting one for it was the first
    # attempt and it is wrong: from counts alone a benign fallback and a single
    # real transition are the SAME surplus, so the allowance silently absorbs
    # exactly one transition — an amnesty, not a filter. A host whose crons.json
    # carries no proactive-loop entry has a config gap worth surfacing anyway,
    # so the honest move is to warn either way and name both causes.
    surplus = registered - expected
    if surplus > 0:
        fallback_armed = not any(
            isinstance(e, dict) and session_owned(e)
            and (e.get("prompt_skill") == "proactive-loop"
                 or "proactive-loop" in str(e.get("prompt") or ""))
            for e in crons
        )
        note = (
            " (crons.json carries no proactive-loop entry, so step 4's bootstrap "
            "fallback plausibly accounts for one of these — add an explicit entry "
            "to tell the two apart)"
            if fallback_armed else ""
        )
        return {
            "name": name,
            "status": "warn",
            "detail": (
                f"{registered} session cron(s) were registered but only {expected} are "
                f"session-owned now — {surplus} moved to launchd/codex ownership, were "
                f"parked, or were deleted since registration, so a job registered under the "
                f"OLD config was never de-registered and MAY still be firing; re-run "
                f"/schedule-crons to clear it{note}"
            ),
        }

    # CONFIG DRIFT. Everything above answers "was a registration completed for
    # this boot?" — a count, and a count cannot see an entry whose PROMPT was
    # edited after it was registered. That drift is silent by construction: the
    # config is right, the script is right, and only the stale registration is
    # wrong (#2653, where a `--stand` flag added four days into a session kept
    # not firing, and the field it populates read null on all 27 PRs).
    #
    # #2653 makes /schedule-crons re-register rather than skip, so the drift
    # self-heals on the next run. This makes an UNHEALED one visible in between,
    # because until the next run the only other observation point is a fire.
    #
    # Restricted to `session_owned` names: an edit to a launchd- or codex-owned
    # entry is not a session-cron problem and must not warn as one. A stamp
    # written before this field existed simply skips the check — an old stamp
    # must not manufacture a warning it has no data for.
    stamped_digests = stamp.get("config_digests")
    if isinstance(stamped_digests, dict):
        session_names = [
            e.get("name") for e in crons if isinstance(e, dict) and session_owned(e)
        ]
        moved = drifted(stamped_digests, digest_map(crons), names=session_names)
        if moved:
            shown = ", ".join(moved[:4]) + ("…" if len(moved) > 4 else "")
            return {
                "name": name,
                "status": "warn",
                "detail": (
                    f"{len(moved)} session cron(s) edited in crons.json since they were "
                    f"registered ({shown}) — the running job still fires the OLD prompt; "
                    f"re-run /schedule-crons"
                ),
            }

    return {"name": name, "status": "ok", "detail": f"{expected} session cron(s) stamped registered this boot"}


def check_file(path: Path, name: str) -> dict:
    """Check if a file exists and is non-empty."""
    if not path.exists():
        return {"name": name, "status": "missing", "detail": str(path)}
    size = path.stat().st_size
    if size == 0:
        return {"name": name, "status": "empty", "detail": str(path)}
    return {"name": name, "status": "ok", "detail": f"{size} bytes"}


def check_directory(path: Path, name: str) -> dict:
    """Check if a directory exists and has files.

    The count names its directory: both callers (memory-dir, notes-dir) sit on
    paths with a live twin, so a bare count reads as a claim about the wrong one.
    """
    if not path.exists():
        return {"name": name, "status": "missing", "detail": str(path)}
    count = len(list(path.glob("*.md")))
    return {"name": name, "status": "ok", "detail": f"{count} .md files in {path}"}


def check_memory_dir_override() -> "dict | None":
    """Flag a SUTANDO_MEMORY_DIR that diverges from the computed default.

    The var is authoritative for MEMORY_DIR (matching src/voice-agent.ts and
    src/voice-context.ts, which also honor it) — so a genuine current use
    keeps working consistently everywhere. But a leftover pre-#1454 value
    would silently point every consumer at a stale directory instead of the
    actively-synced one. Warn on divergence rather than silently redirecting
    just this check, so the user can judge whether the override is still
    intentional. Returns None when the var is unset or matches the default.
    """
    override = os.environ.get("SUTANDO_MEMORY_DIR")
    if not override:
        return None
    default = Path(_default_memory_dir())
    if Path(override).resolve() == default.resolve():
        return None
    return {
        "name": "memory-dir-override",
        "status": "warn",
        "detail": (
            f"SUTANDO_MEMORY_DIR={override} differs from the computed "
            f"default ({default}) — verify this is still intentional, not "
            "a stale pre-#1454 leftover"
        ),
    }


def _slug_derivation_key(name: str) -> str:
    """Collapse a Claude project slug to a derivation-INDEPENDENT key.

    Claude Code slugifies a filesystem path, and the derivations differ only in
    how they map ``.``, spaces and repeated separators. So two slugs describing
    the SAME path agree once every run of non-alphanumerics is collapsed to one
    ``-`` and case is folded, while an unrelated project does not collide:

        -Users-me-Library-Application-Support-space.ag2.app-engine-sutando
        -Users-me-Library-Application-Support-space-ag2-app-engine-sutando
            -> users-me-library-application-support-space-ag2-app-engine-sutando   (same)
        -Users-me-Documents-unrelated-repo
            -> users-me-documents-unrelated-repo                                    (different)
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")



#: Files the workspace-root contract sanctions. Everything else at the root is
#: drift. Kept next to the probe so adding a legitimate root file is a one-line
#: edit in the same place a reader looks for the rule.
WORKSPACE_ROOT_ALLOWED = frozenset({
    "build_log.md",          # CLAUDE.md names it a workspace-root artifact
    "pending-questions.md",  # same
    "session-state.md",      # written by src/session-handoff.sh on compaction
    ".gitkeep",              # git placeholder, not state
    ".env",                  # sutando_config.resolve_dotenv's 2nd tier (#1871)
    # The two lock guards that legitimately sit at the ROOT, by name. Exempt
    # until they migrate to state/locks/ the way workspace_lock.py already
    # writes <workspace>/state/locks/<role>.lock.guard.
    ".voice-agent.lock.guard",
    ".backend-supervisor.lock.guard",
})

#: Why those two guards are named rather than matched by `*.lock.guard`:
#: `state/locks/<role>.lock.guard` (workspace_lock.py) is where this artifact
#: type belongs, and this probe only lists ROOT files — so a role guard that
#: appears at the root is a workspace-resolution bug, exactly the class the
#: probe exists to catch. A glob would have hidden it.
#:
#: `.voice-agent.lock.guard` stays because six sites resolve it at the root
#: (voice-lock.ts, startup-runtime.sh, restart.sh, restart-voice-agent.sh,
#: voice-agent.ts, tests/voice-lock.test.py) and a half-applied move leaves two
#: processes disagreeing about where the lock is — a double-started voice agent.
#: `.backend-supervisor.lock.guard` is written by the desktop app, whose source
#: is not in this repo (`backend-supervisor.lock.guard`: 0 hits here).
#:
#: `.voice-agent.pid` is deliberately NOT exempt: state/locks/ is a real
#: destination that exists in-tree, so its warn names somewhere to go (#2722).

#: Migration sentinels are production-owned and DELIBERATELY retained at the
#: workspace root — `workspace_default.py` writes `.notes-migrated`,
#: `.build_log-migrated`, `.status-migrated` and `.conversation-log-migrated`
#: there for O(1) re-entry, and says so explicitly ("kept at the workspace root
#: for consistency with the existing ...").
#:
#: Matched by PATTERN, not by four literal names, because that is how the family
#: is already defined elsewhere: `scripts/sutando-migrate.sh` finds them with
#: `-name ".*-migrated*"`. Reusing the existing definition means a sentinel added
#: later is exempt automatically.
#:
#: This is the same mistake this probe exists to catch, made one layer up: the
#: first version of it shipped a hardcoded allowlist that missed a whole
#: documented family, exactly as `_STATUS_FILES` does. @qingyun-wu and
#: @john-the-dev caught it before merge — a permanent WARN on every upgraded
#: install would have trained operators to ignore the detector.
#: `.legacy-notice-printed` is a LITERAL because this family has no shared name
#: shape: written once, read AT the root by init.sh, never unlinked, no destination.
WORKSPACE_ROOT_SENTINEL_GLOBS = (".*-migrated*", ".legacy-notice-printed")

#: `personal_path()` resolves these at the workspace root and never under `state/`,
#: so the probe's "state belongs under state/" remedy would break the reader.
WORKSPACE_ROOT_PERSONAL_ASSETS = frozenset({
    "PERSONAL_CLAUDE.md",
    "stand-identity.json",
    "stand-avatar.png",
    "voice-context-active",
})


def check_workspace_wiring() -> "dict | None":
    """Report-only mirror of the spawn-time guard: `materialized-with-data` FAILS
    (auto-heal would orphan its files — needs a merge); other broken states warn."""
    report = inspect_layout(REPO_DIR)
    state = report["state"]
    if state == "ok":
        return None
    status = "fail" if state == "materialized-with-data" else "warn"
    fix = (
        "merge stray files into the durable workspace, then rerun "
        "`python3 src/workspace_layout.py --ensure`"
        if status == "fail"
        else "`python3 src/workspace_layout.py --ensure` (or restart the core)"
    )
    return {
        "name": "workspace-wiring",
        "status": status,
        "detail": f"{state}: {report['detail']} — {fix}",
    }


def check_workspace_root_tidy() -> "dict | None":
    """Flag loose FILES at the workspace root — state that escaped `state/`.

    CLAUDE.md: "Loose status/state .json files (...) live under `state/`; the
    workspace root holds only the top-level directories."

    That contract already has a MIGRATOR — `workspace_default._migrate_root_status`
    — but it sweeps a hardcoded five-name list (`core-status.json`,
    `voice-state.json`, `contextual-chips.json`, `dynamic-content.json`,
    `quota-state.json`). An allowlist is the right shape for a migrator: its job is
    relocating files it knows about. It is the wrong shape for enforcement, because
    anything added afterwards is exempt by construction and nothing reports it.

    So the contract had no detector at all, and drifted. Found on Chis-Mac-mini:
    `.last-pq-notify` (written by check-pending-questions.py) and `.voice-agent.pid`
    (voice-agent.ts) had accumulated at the root, plus two stray capture scripts —
    none of them in the migrator's list, none of them flagged by any of the 23
    existing probes.

    WARN, never fail. This is drift, not breakage: the files work where they are,
    and a fail-level probe would go red on every host that already has some, for
    state nobody chose. A warn keeps it visible without gating anything.

    Returns None when the root is clean, so a healthy install gains no line.
    """
    if not WORKSPACE_DIR.is_dir():
        return None
    try:
        loose = sorted(
            p.name for p in WORKSPACE_DIR.iterdir()
            if p.is_file()
            and p.name not in WORKSPACE_ROOT_ALLOWED
            and p.name not in WORKSPACE_ROOT_PERSONAL_ASSETS
            and not any(fnmatch.fnmatch(p.name, g)
                        for g in WORKSPACE_ROOT_SENTINEL_GLOBS)
        )
    except OSError:
        return None                      # unreadable workspace is another probe's job
    if not loose:
        return None

    # Name the writer where a cheap grep finds one — "who put this here" is the
    # first question, and answering it turns a nag into an action.
    writers = {}
    src = REPO_DIR / "src"
    for name in loose:
        try:
            hits = sorted(
                f.name for f in src.iterdir()
                if f.is_file() and f.suffix in (".py", ".ts", ".sh")
                and name in f.read_text(errors="replace")
            )
        except OSError:
            hits = []
        # Only attribute when EXACTLY one source file mentions the name. Taking
        # hits[0] named whichever file happened to sort first — including a test
        # that merely contains the string — and a confidently wrong writer is
        # worse than none, because it sends the reader to the wrong file.
        if len(hits) == 1:
            writers[name] = hits[0]

    shown = ", ".join(f"{n} (written by {writers[n]})" if n in writers else n
                      for n in loose)
    return {
        "name": "workspace-root-tidy",
        "status": "warn",
        "detail": (
            f"{len(loose)} loose file(s) at the workspace root, which the contract "
            f"reserves for top-level directories: {shown}. State belongs under "
            f"state/. `_migrate_root_status` only sweeps its five hardcoded names, "
            f"so anything added since is exempt by construction — this probe is the "
            f"detector that was missing, not a new rule."
        ),
    }

def check_memory_dir_siblings() -> "dict | None":
    """Flag a populated memory corpus sitting under a DIFFERENT project slug.

    check_memory_dir_override() above catches only one of the two ways this
    install can end up reading a directory the agent is not writing: an explicit
    SUTANDO_MEMORY_DIR pointing somewhere stale. It returns None when the var is
    unset — and the other failure mode needs no env var at all.

    Claude Code derives a project slug from a path. Two different derivations
    (repo path vs app-support path, or differing rules for spaces and dots)
    produce two sibling project dirs under the same claude-home, each with its
    own memory/. Everything that resolves by repo slug then reports on one
    corpus while the session reads and writes the other. Field-observed on two
    hosts, once per mechanism: one had the env override (caught), one had the
    slug split (silent — memory-dir reported "ok, 66 .md files" about a corpus
    the agent had never written to, while its live corpus held 42).

    Deliberately diagnostic only. Which slug *should* be canonical is an open
    architectural decision; picking one here would answer it in code and, worse,
    hide the divergence behind a green check. So: report, never redirect.

    Symlinked twins are NOT a split — resolve before comparing. Two slug strings
    frequently point at one inode (a compatibility symlink bridging two
    derivation rules), and reporting that as a divergence would make this check
    noise on a healthy install.
    """
    projects = Path(claude_home_path()) / "projects"
    if not projects.is_dir():
        return None

    live = MEMORY_DIR.resolve() if MEMORY_DIR.exists() else MEMORY_DIR
    # Only ALTERNATE DERIVATIONS OF THIS PROJECT are candidates. Warning on any
    # populated corpus would fire on every normal multi-project home — a
    # permanent false warning that teaches people to ignore the health signal,
    # which costs more than the split it is trying to surface (#2353 review).
    live_key = _slug_derivation_key(MEMORY_DIR.parent.name)
    seen: "dict[str, tuple[str, int]]" = {}
    for entry in sorted(projects.iterdir()):
        mem = entry / "memory"
        if not mem.is_dir():
            continue
        if _slug_derivation_key(entry.name) != live_key:
            continue  # unrelated project, not a slug split
        count = len(list(mem.glob("*.md")))
        if count == 0:
            continue
        key = str(mem.resolve())  # collapse symlinked twins onto one entry
        if key not in seen or count > seen[key][1]:
            seen[key] = (entry.name, count)

    others = {k: v for k, v in seen.items() if k != str(live)}
    if not others:
        return None

    live_count = len(list(MEMORY_DIR.glob("*.md"))) if MEMORY_DIR.is_dir() else 0
    listed = ", ".join(f"{name} ({n} .md)" for name, n in sorted(others.values(), key=lambda t: -t[1]))
    return {
        "name": "memory-dir-siblings",
        "status": "warn",
        "detail": (
            f"{len(others)} other populated memory corpus/corpora exist under "
            f"{projects}: {listed}. This check reports on {MEMORY_DIR.name}'s parent "
            f"({live_count} .md) — if the session actually writes one of the others, "
            "its memories are invisible to every path-derived consumer. Diagnostic "
            "only; which slug is canonical is an open decision."
        ),
    }


def _carrier_representatives(workspace: Path, entry: str) -> "list[Path]":
    """EVERY existing concrete path a carrier-set entry matches.

    Entries are gitignore-style and may be globs (`hosts/*/`), directories
    (`notes/`) or plain files (`state/current-track.md`).

    Deliberately ALL matches, not the first. Sampling one is sound for the stale
    branch (which asks "is the entry this host configured actually in effect" —
    one witness settles it), but not for judging whether a *local* rule covers a
    *shipped* one: a narrower local rule can cover one child of a shipped
    wildcard while leaving its siblings ignored, and a first-hit check that
    happened to land on the covered child would report the vault healthy while a
    whole host subtree went unbacked (qingyun-wu P1 on #2572).

    Enumeration is bounded by construction: glob matches are the entry's own
    direct matches (`hosts/*/` -> one path per host), never a recursive walk of
    their contents.
    """
    rel = entry.strip().lstrip("/").rstrip("/")
    if not rel:
        return []
    if any(ch in rel for ch in "*?["):
        matches = sorted(workspace.glob(rel))
    else:
        candidate = workspace / rel
        matches = [candidate] if candidate.exists() else []
    return [m for m in matches if not _reaches_through_symlink(workspace, m)]


def _reaches_through_symlink(workspace: Path, p: Path) -> bool:
    """True when every file the probe would derive from `p` is "beyond a
    symbolic link" to git: `p` itself is a symlinked DIRECTORY, or any
    component between `workspace` and `p` is a symlink.

    Found live 2026-08-05: a compat alias symlink under
    `.claude-sutando/projects/` (a space-slug project dir pointing at its
    dash-slug twin) was matched by the memory glob, `check-ignore` rejected
    every pathspec under it with exit 128 ("beyond a symbolic link"), and the
    whole entry read UNMEASURED every health pass — while the content was
    backed up the entire time at its real path, which the same entry probes
    via the twin's own materialization.

    Git never stores content past a symlink — at most the link entry itself —
    so such a materialization is outside what the vault could ever carry, and
    probing it can only produce 128s (or, if the crossing link itself were
    probed instead, a false `dropped`: dir-only un-ignore patterns like
    `!projects/*/` cannot match a symlink, measured on the live host). The
    content's real path is the one that answers the backup question, and
    when it lies inside the workspace the same probe measures it directly.

    A symlink to a FILE with a real parent chain is deliberately NOT
    filtered: git accepts that pathspec (nothing is *beyond* the link) and
    file rules match it, so the existing behavior of probing it stands.
    """
    ancestors = []
    cur = p
    while cur != workspace and cur != cur.parent:
        ancestors.append(cur)
        cur = cur.parent
    for c in ancestors[1:]:  # components strictly between workspace and p
        if c.is_symlink():
            return True
    return p.is_symlink() and p.is_dir()


def _carrier_probe_files(rep: "Path") -> "list[Path]":
    """Every concrete file a materialized representative stands for.

    A file represents itself; a DIRECTORY is represented by every file beneath
    it, sorted for determinism.

    Deliberately EXHAUSTIVE. The first cut sampled the first 25 and said so in a
    comment — "past the cap the probe can UNDER-report" — which treated a stated
    caveat as an acceptable conclusion rather than a defect. john-the-dev built
    the obvious counterexample on that head: 26 files with only the 26th ignored
    read `ok`, so a stale exclude left a real carrier file unbacked while health
    certified the subtree. In a probe whose entire purpose is catching silent
    non-backup, a sample is not proof of coverage.

    Cost is bounded by asking git ONCE for the whole list (`check-ignore
    --stdin`) rather than once per file, so exhaustiveness costs a filesystem
    walk plus a single process — not N processes. See `_carrier_target_verdict`.
    """
    if rep.is_file():
        return [rep]
    if not rep.is_dir():
        return []
    return sorted(f for f in rep.rglob("*") if f.is_file())


# The one rule file the carrier set is written to. `check-ignore -v` reports
# the deciding rule's SOURCE, and only rules from here speak to whether the
# vault is carrying a path.
CARRIER_EXCLUDE_SOURCE = ".git/info/exclude"


def _carrier_carveout_patterns() -> "set[str]":
    """Every gitignore pattern the generator emits that is SUPPOSED to ignore a
    file inside a carried parent.

    Two sources, both from `sync-workspace.sh:_compose_exclude_content`:

      * `vault.sync.exclude` -> `_emit_exclude_lines` (`:371`): a trailing-slash
        entry emits `path/` AND `path/**`; anything else is emitted verbatim.
      * the unconditional hard-deny block (`:461-482`) — credentials, transient
        state and secret material are denied "regardless of carrier set", so a
        file caught by one of those is correctly not backed up.

    Mirrored rather than parsed out of the live `.git/info/exclude`, because the
    file on disk is the very artifact under test: reading the rules from it
    would make any stale or hand-edited rule self-justifying, which is the
    failure mode #2566 was opened for.
    """
    patterns = {
        # Hard-deny block — keep in step with sync-workspace.sh:461-482.
        ".env*", "*.heartbeat", "*.alive", "*.sentinel", "*.pid",
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
        "*.pem", "*.key", "*.p12", "*.pfx", "*.ppk", "*.keystore", "*.jks",
    }
    for entry in ((_resolved_vault().get("sync") or {}).get("exclude") or []):
        if not isinstance(entry, str) or not entry.strip():
            continue
        e = entry.strip()
        if e.endswith("/"):
            patterns.add(e)
            patterns.add(e + "**")
        else:
            patterns.add(e)
    return patterns


def _carrier_target_verdict(workspace: Path, rep: Path) -> str:
    """`"carried"` | `"dropped"` | `"unmeasured"` for ONE materialized path.

    A DIRECTORY and the files inside it are different questions for git, and the
    first cut asked the wrong one. `*` + `!hosts/` + `!hosts/*/` un-ignores the
    directories while every file beneath stays ignored — `!hosts/**` is what
    carries the contents. So `check-ignore` on `hosts/a` answered "not ignored"
    while `hosts/a/current-track.md` was ignored and unbacked, and the probe
    reported the vault healthy with the very file it exists to protect not being
    backed up. Reproduced independently by john-the-dev and qingyun-wu on
    a958d06f, and confirmed here before changing anything.

    So a directory is probed through EVERY file beneath it, and any ignored one
    condemns the entry. Exhaustive rather than sampled: a 26-file directory whose
    26th file alone was ignored read `ok` under the old 25-file cap
    (john-the-dev, #2572), which is the exact silent non-backup this exists for.

    `--no-index` stays load-bearing and the instrument stays `check-ignore` for
    a reason worth recording: the obvious alternative,
    `ls-files --others --ignored`, is index-AWARE, so it calls a tracked file
    carried. That silently reverses a documented behavior —
    `test_a_TRACKED_file_with_a_stale_exclude_is_still_reported` exists because a
    host that carried a file once and then let its exclude go stale read healthy
    forever while nothing NEW under that entry was being carried. I wrote the
    ls-files version first and that test failed; it was right and I was wrong.

    Used by BOTH the stale and dropped branches. The stale branch probes a
    directory representative too and had the same blindness; fixing only the
    branch that was reported would have left the identical defect one step to
    the left, which is how this class of bug has survived three rounds here.
    """
    targets = _carrier_probe_files(rep)
    if not targets:
        # A materialized directory with no files under it yet. Nothing to
        # measure — treated as carried so an empty `hosts/<label>/` cannot
        # manufacture a failure; the "nothing on disk" case is handled by the
        # callers, which report an entry with no representatives at all.
        return "carried"

    # NUL-delimited, not newline. A filename may CONTAIN a newline, and
    # `"\n".join(...)` then splits one real path into two bogus ones — both of
    # which are typically un-ignored, so the genuinely ignored carrier file reads
    # as carried. Reproduced on a real tree (john-the-dev, #2572):
    #
    #     notes/z\nx.md  check-ignore -q  -> 0  (IGNORED, not backed up)
    #     split halves   notes/z, x.md    -> 1, 1 (both un-ignored)
    #     newline-joined batch            -> 1  -> reads CARRIED, false green
    #     NUL-joined with -z              -> 0  -> dropped, correct
    #
    # `-z` makes git read NUL-separated input, which is the only delimiter a
    # POSIX filename cannot contain.
    rels = "\0".join(str(t.relative_to(workspace)) for t in targets)
    try:
        proc = subprocess.run(
            git_argv("-C", str(workspace), "check-ignore", "--no-index", "-v", "-z", "--stdin"),
            input=rels, capture_output=True, text=True, timeout=60,
        )
    except (GitUnavailable, OSError, subprocess.SubprocessError):
        return "unmeasured"
    # `-v` names the RULE that decided each path, and that is the whole point:
    # "some file under here is ignored" is NOT the same question as "is this
    # entry being carried". `vault.sync.exclude` deliberately ignores files
    # INSIDE an included parent (`_emit_exclude_lines`, sync-workspace.sh:371),
    # so on any real workspace the carve-outs fire constantly:
    #
    #     notes/    9970 of 11369 files ignored  (*.mp4, .DS_Store, node_modules/)
    #     hosts/       9 of    67 files ignored  (data/)
    #
    # The pre-`-v` version condemned an entry when ANY file beneath it was
    # ignored, so both read `dropped` while the vault was demonstrably backing
    # them up (1400 and 58 files tracked, synced minutes earlier, zero diff).
    # It survived because the one entry with no carve-out beneath it —
    # `.claude-sutando/projects/*/memory/`, 1322 files, 0 ignored — passed, so
    # the probe looked like it discriminated. The prescribed remedy made it
    # permanent: `--force-gitignore` regenerates a byte-identical file (eight
    # consecutive syncs logged `existing exclude matches; no-op`), so the
    # failure could never clear no matter how often it was obeyed.
    #
    # NOTE `-v` also CHANGES THE EXIT-CODE CONTRACT the old code relied on: a
    # path matching a negation (`!notes/**`, i.e. carried) is still reported and
    # still exits 0. Verified before depending on it:
    #
    #     notes/a.md   -> !notes/**  exit 0   (NOT ignored)
    #     notes/b.mp4  -> *.mp4      exit 0   (ignored, deliberately)
    #     other/c.txt  -> *          exit 0   (ignored — genuinely dropped)
    #
    # So the verdict now comes from the parsed patterns; only a hard failure
    # (neither 0 nor 1) is still read off the exit code.
    if proc.returncode not in (0, 1):
        return "unmeasured"
    allowed = _carrier_carveout_patterns()
    fields = proc.stdout.split("\0")
    # Records are 4 NUL-separated fields: source, linenum, pattern, pathname.
    for i in range(0, len(fields) - 3, 4):
        source, pattern = fields[i], fields[i + 2]
        if pattern.startswith("!"):
            continue          # un-ignored by the carrier rule — carried
        if source != CARRIER_EXCLUDE_SOURCE:
            # A nested `.gitignore` committed inside a carried tree, or a global
            # core.excludesFile. Not the vault carrier mechanism, so not this
            # probe's question — and condemning on it would make every vendored
            # project a permanent failure. Real instance: a Remotion app under
            # `notes/` ignores its own `out/` build dir, which is correct.
            continue
        if pattern in allowed:
            continue          # a configured carve-out — deliberate, not a defect
        # Ignored by `.git/info/exclude` via something that is NOT a carve-out:
        # either the whitelist catch-all `*` (this entry's un-ignore never took
        # effect) or an operator-authored rule that `generate_exclude`'s refusal
        # guard left standing because `_enforce_carrier_set_pre` swallows the
        # refusal. Both mean the entry is genuinely not being backed up.
        return "dropped"
    return "carried"


def _carrier_representative(workspace: Path, entry: str) -> "Path | None":
    """One existing concrete path for a carrier-set entry, or None.

    The single-witness form, kept for the stale branch: that branch asks whether
    the entry THIS host configured is in effect, and one materialized path
    answers it. An entry with nothing on disk yet is not evidence of anything,
    so it is skipped rather than reported. Use `_carrier_representatives` when
    the question is coverage of a *different* (shipped) entry — see its
    docstring for why one witness is not enough there.
    """
    reps = _carrier_representatives(workspace, entry)
    return reps[0] if reps else None


def check_carrier_set_enforced(workspace_dir=None) -> "dict | None":
    """A configured carrier-set entry that git still ignores is not being backed up.

    `sync-workspace.sh:generate_exclude` REFUSES to rewrite an existing
    `.git/info/exclude` that differs from the generated content (the #1445 guard
    against clobbering operator-authored rules), and `_enforce_carrier_set_pre`
    (`:613`) swallows that refusal so the tick continues. The sync then pushes and
    reports success while the carrier set is silently stale — observed on two
    hosts independently (65 refusals / 63 followed by `pushed to` on one, 4/4 on
    the other; see #2565). Every existing check reads healthy: the config IS
    correct, the checkout IS current, and only the generated artifact is stale.

    Two distinct causes produce the identical symptom and need different remedies,
    so they are reported separately rather than collapsed:

      1. STALE EXCLUDE — the resolved config lists an entry that git still
         ignores. Remedy: `bash scripts/sync-workspace.sh --force-gitignore`.
      2. DROPPED BY OVERRIDE — a local `vault.sync.include` REPLACES the shipped
         default rather than merging into it (#2531), so shipped entries added
         later never reach this host. Remedy: add them to the local list. Here
         the on-disk exclude legitimately MATCHES the resolved config, so cause 1
         is dormant and a resolved-vs-disk comparison alone reads healthy.

    Asking git directly (`check-ignore`) rather than regenerating the exclude and
    diffing is deliberate: it tests the OUTCOME, cannot drift as the generator's
    formatting changes, and is immune to the two legitimate no-diff branches
    (`:562` no-op when already matching, `:570` safe legacy `hosts/<label>/` ->
    `hosts/*/` widening) — after either, the paths ARE un-ignored, so there is
    nothing to false-positive on.

    Returns None when the workspace is not a git repo (vault never initialized),
    which is a valid unconfigured state and not a defect.
    """
    workspace = Path(workspace_dir or WORKSPACE_DIR)
    name = "carrier-set"
    if not (workspace / ".git").exists():
        return None

    vault = _resolved_vault()
    resolved = [e for e in (vault.get("sync") or {}).get("include") or [] if isinstance(e, str)]
    if not resolved:
        return None

    stale: "list[str]" = []
    unmeasured: "list[str]" = []
    for entry in resolved:
        # EVERY materialized match, not one. `_carrier_representative()` (singular)
        # collapses `hosts/*/` to its first match, so a two-host workspace whose
        # exclude carries host A but not host B reported `ok` while B's subtree
        # was ignored and unbacked — the same false green the dropped branch was
        # fixed for, on the OTHER axis.
        #
        # The previous round shared the directory-vs-file instrument across both
        # branches and stopped there. Coverage has two independent axes — WHICH
        # paths you probe, and WHAT you ask about each — and generalizing one of
        # them left the other singular here. Both branches now enumerate.
        # (john-the-dev and qingyun-wu, independently, on cf059ca8.)
        reps = _carrier_representatives(workspace, entry)
        if not reps:
            continue
        # `git_argv` rather than a bare "git": on macOS the stock /usr/bin/git is
        # an Xcode-CLT shim that pops an install dialog when the tools are absent,
        # and this probe runs from a BACKGROUND health check where nobody is there
        # to dismiss it. The resolver refuses that shim instead of spawning it.
        #
        # `--no-index` is LOAD-BEARING on the file path (sync-workspace.sh says so
        # at its own check-ignore call): without it, git reports an ALREADY-TRACKED
        # file as not-ignored regardless of the rules, so a host whose file was
        # carried once and whose exclude later went stale reads healthy forever.
        # Caught by restoring this host's real pre-fix exclude and watching the
        # probe still say OK. Both that and the directory/contents distinction now
        # live in `_carrier_target_verdict` so the two branches cannot drift.
        verdict = "carried"
        for rep in reps:
            got = _carrier_target_verdict(workspace, rep)
            if got != "carried":
                verdict = got
                break
        if verdict == "dropped":
            stale.append(entry)
        elif verdict == "unmeasured":
            # Could not run the measurement at all. NOT the same as "carried".
            unmeasured.append(entry)

    # Read the SHIPPED file directly rather than through load_config(), which
    # deep-merges the local override and would therefore return `resolved` —
    # the very list we are testing against. There is no shipped-only public
    # loader, and inventing one behind a broad `except` would make this branch a
    # silent no-op the day the name was wrong.
    dropped: "list[str]" = []
    try:
        shipped_cfg = json.loads((Path(REPO_DIR) / "sutando.config.json").read_text())
        shipped = ((shipped_cfg.get("vault") or {}).get("sync") or {}).get("include")
    except (OSError, json.JSONDecodeError, AttributeError):
        # The shipped config itself is unreadable, so there is no comparison to
        # make. This except must stay narrow and must NOT wrap the measurement
        # loop below: the first cut did, so one entry's git timeout jumped here
        # and reset `dropped = []`, erasing findings already collected — a
        # fail-OPEN that turned a failed measurement into a green report, three
        # lines under a comment promising the opposite (qingyun-wu P1 on #2572).
        shipped = None

    if isinstance(shipped, list):
        missing = [e for e in shipped if isinstance(e, str) and e not in resolved]
        # String inequality is not absence of coverage. A local entry can be
        # strictly BROADER than the shipped one it replaces — `hosts/` covers
        # everything `hosts/*/` does — and the whole point of this probe is to
        # judge by outcome, not by config text. The `stale` branch above already
        # asks git; this branch used to revert to string equality, so it reported
        # a fully-carried subtree as missing and advised "add them to the local
        # list", i.e. told the operator to NARROW a config that was already
        # correct (#2571, reported against #2566 by Sutando-Pro).
        #
        # EVERY materialized match must be covered, not one sampled witness. A
        # narrower local rule (`hosts/a/`) covers one child of a shipped wildcard
        # (`hosts/*/`) while its siblings stay ignored, and first-hit sampling
        # that landed on the covered child reported the vault healthy with a host
        # subtree silently unbacked.
        #
        # An entry with nothing materialized yet stays reported: there is no
        # outcome to measure, but the divergence is real and becomes silent data
        # loss the moment the first file lands under it.
        for entry in missing:
            reps = _carrier_representatives(workspace, entry)
            if not reps:
                dropped.append(entry)
                continue
            verdict = "covered"
            for rep in reps:
                # Same instrument as the stale branch, deliberately shared: a
                # DIRECTORY representative and the files under it are different
                # questions for git, and asking about the directory reported a
                # subtree healthy while its carrier file was ignored.
                got = _carrier_target_verdict(workspace, rep)
                if got != "carried":
                    verdict = got
                    break
            if verdict == "dropped":
                dropped.append(entry)
            elif verdict == "unmeasured":
                unmeasured.append(entry)

    # A shipped entry can be one this host must NOT carry. `state/current-track.md`
    # is per-host state that #2534 added at a flat, SHARED vault path, so two cores
    # write the same file and each host's sync resolves the resulting conflict in its
    # own favour — a peer's anchor lands in your working copy, and neither side's
    # merge keeps the other's.
    #
    # This comment previously said that "overwrote a peer's 1056-line anchor". It did
    # not, and the correction matters because I wrote the original from a misread of
    # the sync code rather than from the vault. The vault uses PER-HOST branches
    # (`host/<host>/<wsid>`); a host only ever merges a peer INTO its own branch and
    # never writes to the peer's. Checked afterwards: both branches were byte-identical
    # and this host's index referenced 263 memory files against the discarded copy's
    # 262 — a strict superset, nothing missing. Chi corrected the claim; Sutando-Pro
    # independently confirmed it from `state/current-track.md`'s own two-commit history.
    #
    # The guidance is unchanged — do not re-add it — but the reason is cross-host
    # CONTENT DELIVERY on a shared path, not data loss. Telling an operator to re-add
    # walks them back into that, so the REMEDY is split rather than the entry hidden;
    # silently filtering it would suppress a real divergence. Delete this list once the
    # host-qualified migration (#2567/#2568) lands: the flat path stops being shipped
    # and stops appearing here.
    UNSAFE_TO_READD = ("state/current-track.md",)
    unsafe = [e for e in dropped if e in UNSAFE_TO_READD]
    safe = [e for e in dropped if e not in UNSAFE_TO_READD]

    # The same carve-out has to reach the STALE branch, and there it inverts the
    # verdict rather than just softening the wording. A path that must not be
    # carried and is NOT being carried is in its CORRECT state — reporting that as
    # a failure whose remedy is `--force-gitignore` would walk the operator into
    # re-carrying it, which is the cross-host overwrite this carve-out exists to
    # prevent. Found by running the merged probe on a live host that had
    # deliberately un-carried the path: it said `fail` and named the exact command
    # that resumes the incident.
    stale_expected = [e for e in stale if e in UNSAFE_TO_READD]
    stale = [e for e in stale if e not in UNSAFE_TO_READD]

    if not stale and not dropped and not unmeasured:
        if not stale_expected:
            return {
                "name": name,
                "status": "ok",
                "detail": f"all {len(resolved)} configured carrier path(s) un-ignored in the vault",
            }
        # NOT `ok`: this host is in the correct state, but it got there by DIVERGING
        # from the shipped default, which still ships the flat path in
        # `vault.sync.include` and still re-carries it on `--force-gitignore` or in a
        # fresh workspace. Reporting `ok` would claim the default is safe when only
        # this host is (qingyun-wu on #2570).
        #
        # `warn` + an EXPLICIT `alerting: False`. The first cut set `warn` alone and
        # claimed in this comment that it "never alerts" because `main()` excludes
        # warn from `issues` — which was simply false. `issues` is a local list used
        # to print a summary; `--emit-task`, `--notify-on-fail` and `--notify-slack`
        # each consume the FULL checks list and treat a plain warn as a failure, so
        # this state still fired a task and a notification on first transition
        # (qingyun-wu again, with a one-check witness). The claim was tested against
        # the list I happened to have named rather than the surfaces that enforce.
        #
        # Suppression is right HERE specifically: the owner cannot act on it. Phase 2
        # of #2567 is gated on migration evidence, so paging about it would repeat a
        # non-actionable message until that lands, and this branch stops being
        # reachable once the flat path leaves the shipped default.
        return {
            "name": name,
            "status": "warn",
            "alerting": False,
            "detail": (
                f"{len(resolved) - len(stale_expected)} configured carrier path(s) un-ignored; "
                f"{', '.join(stale_expected)} correctly NOT carried on this host — but the shipped "
                f"default still lists it in `vault.sync.include`, so a fresh workspace or "
                f"`--force-gitignore` re-carries it at the flat, SHARED path and can resume the "
                f"cross-host overwrite. This host is right and the default is not; do not "
                f"`--force-gitignore` it back. Clears when phase 2 of #2567 drops the entry"
            ),
        }

    parts = []
    if unmeasured:
        parts.append(
            f"could NOT measure {len(unmeasured)} carrier path(s) "
            f"({', '.join(unmeasured[:4])}{'…' if len(unmeasured) > 4 else ''}) — git was "
            f"unavailable or check-ignore failed, so whether the vault is backing them up is "
            f"UNKNOWN, not fine"
        )
    if stale:
        parts.append(
            f"{len(stale)} configured carrier path(s) are STILL GIT-IGNORED so the vault is not "
            f"backing them up ({', '.join(stale[:4])}{'…' if len(stale) > 4 else ''}) — the exclude "
            f"file is stale and sync refused to regenerate it; fix with "
            f"`bash scripts/sync-workspace.sh --force-gitignore` (diff it first)"
        )
    if safe:
        parts.append(
            f"{len(safe)} shipped carrier path(s) are missing from this host's resolved include "
            f"({', '.join(safe[:4])}{'…' if len(safe) > 4 else ''}) — a local "
            f"`vault.sync.include` REPLACES the shipped default (#2531), so upstream additions never "
            f"arrive; add them to the local list"
        )
    if unsafe:
        parts.append(
            f"{len(unsafe)} shipped carrier path(s) are missing here and must NOT be re-added "
            f"({', '.join(unsafe)}) — per-host state carried at a flat, SHARED vault path, so "
            f"re-adding puts two hosts on one file: a peer's copy lands in yours and each "
            f"sync keeps its own side (#2567). Leave them out until the host-qualified "
            f"migration lands"
        )
    return {"name": name, "status": "fail", "detail": "; ".join(parts)}


# Said instead of "" when the trend cannot be computed: silence made "no history
# to read" indistinguishable from "read it, nothing to report".
_TREND_UNAVAILABLE = "; growth trend unavailable (no readable index history on this host)"


def _index_growth_note(index: Path, effective_bytes: int) -> str:
    """A trend for the memory-index warning, or "" when it cannot be measured.

    The level alone reads as scenery. This probe warned "approaching the session
    read limit" for hours on 2026-08-04 and was correctly ignored by the agent it
    was warning, because "approaching" says nothing about whether that is a week
    away or an hour. The history says which:

        08-03 14:04   24,988 B   headroom     12 B   <- survived by 12 bytes
        08-03 17:36   23,567 B                       <- compacted back
        08-03 22:07   24,238 B   headroom    762 B   <- climbing again

    Two hosts write this file through the vault, so neither one's own edits
    account for the curve. A warning that cannot say "it nearly truncated today"
    is a warning that invites exactly the shrug it got.

    Fail-open by construction: no git, not a repo, no history for the path, or
    an unparsable blob all yield "" and leave the existing message untouched. A
    trend is a nicety; suppressing the level would be a regression.
    """
    try:
        rel = index.name
        repo = index.parent
        proc = subprocess.run(
            git_argv("-C", str(repo), "log", "--format=%H %at", "-n", "12", "--", rel),
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return _TREND_UNAVAILABLE
        # One `cat-file --batch` instead of a `git show` per commit. The first
        # draft spawned 1 + N processes (13 here) on a path that runs EVERY
        # proactive pass for as long as the warning stands — 658 ms on
        # Sutando-Pro's host, 122 ms on mine (wall time is host-dependent, the
        # spawn count is not).
        #
        # `--batch`, not `--batch-check`: the sizes have to be measured in the
        # SAME units as the live level, and that means having the CONTENT.
        # `effective_bytes` comes from `_index_effective_text`, which strips
        # frontmatter and whole-line HTML comments before the runtime measures
        # its limit. Comparing a raw blob length against it is a unit mismatch:
        # a revision whose bulk sits inside `<!-- ... -->` is huge raw and tiny
        # effective, and the first draft of this helper reported
        # "ALREADY EXCEEDED ... entries were dropped then" for exactly that
        # shape — a FALSE claim of proven loss, in a note whose entire purpose
        # is that the reported number describes the artifact.
        # (john-the-dev and qingyun-wu each reproduced it independently on #2610.)
        stamps: "list[tuple[str, int]]" = []
        for line in proc.stdout.splitlines():
            sha, _, at = line.partition(" ")
            if not sha or not at.strip().isdigit():
                continue
            stamps.append((sha, int(at)))
        if not stamps:
            return _TREND_UNAVAILABLE
        batch = subprocess.run(
            git_argv("-C", str(repo), "cat-file", "--batch"),
            input="".join(f"{sha}:./{rel}\n" for sha, _ in stamps).encode(),
            capture_output=True, timeout=20,
        )
        if batch.returncode != 0:
            return _TREND_UNAVAILABLE
        points: "list[tuple[int, int]]" = []
        buf, idx = batch.stdout, 0
        for sha, at in stamps:
            nl = buf.find(b"\n", idx)
            if nl < 0:
                break
            header = buf[idx:nl].decode("utf-8", "ignore").split()
            idx = nl + 1
            # a bad spec prints "<spec> missing" and no body
            if len(header) < 3 or not header[-1].isdigit():
                continue
            size = int(header[-1])
            body, idx = buf[idx:idx + size], idx + size + 1   # +1 trailing \n
            # SAME decode + strip the live path uses (read_text(errors="ignore")
            # then _index_effective_text), then encoded bytes — so peak and rate
            # are in the units the cap is defined on.
            eff = _index_effective_text(body.decode("utf-8", "ignore"))
            points.append((at, len(eff.encode("utf-8"))))
        if len(points) < 2:
            return _TREND_UNAVAILABLE
        points.sort()
        # Closest the index has come to the cut in the recorded window. This is
        # the number that makes the warning land, and no point reading has it.
        peak = max(sz for _, sz in points)
        # FASTEST sustained climb to now, over every start point: a single anchor
        # can be undercut by a compaction or by 1-byte jitter, a max cannot.
        newest_at, _ = points[-1]
        hours = grew = 0.0
        best_rate = 0.0
        for at, sz in points:
            span = (newest_at - at) / 3600.0
            gain = effective_bytes - sz
            if span < 0.5 or gain <= 0:
                continue
            if gain / span > best_rate:
                best_rate, hours, grew = gain / span, span, gain
        grew = int(grew)
        note = ""
        if peak > MEMORY_INDEX_LOAD_BYTES:
            # Not "nearly" — it was OVER, so the tail was genuinely unread for as
            # long as that revision stood. The first draft of this note printed
            # "came within -156 B of the cut", which is how a sign error ships as
            # reassurance: the one history that proves real loss rendered as the
            # calmest wording in the message.
            note += (f"; it has ALREADY EXCEEDED the cut in this history, by "
                     f"{peak - MEMORY_INDEX_LOAD_BYTES:,} B — entries were dropped then, "
                     f"and only a compaction brought it back")
        elif peak > MEMORY_INDEX_LOAD_BYTES * 0.97:
            note += (f"; it came within {MEMORY_INDEX_LOAD_BYTES - peak:,} B of the cut "
                     f"inside this history and was pulled back by a compaction, not by headroom")
        if hours >= 0.5 and grew > 0:
            rate = grew / hours
            left = MEMORY_INDEX_LOAD_BYTES - effective_bytes
            note += (f"; +{grew:,} B over the last {hours:.1f}h"
                     + (f", which is ~{left / rate:.1f}h of remaining headroom at that rate"
                        if rate > 0 and left > 0 else ""))
            # The max window is the worst case, so `gain <= 0` skips every flat one
            # and a quoted deadline outlives its regime. Report the recent window too.
            recent = next(((at, sz) for at, sz in reversed(points)
                           if (newest_at - at) / 3600.0 >= 0.5), None)
            if recent is not None:
                r_span = (newest_at - recent[0]) / 3600.0
                r_gain = effective_bytes - recent[1]
                if r_gain <= 0:
                    note += (f"; but the last {r_span:.1f}h show {r_gain:+,} B — flat or "
                             f"shrinking, so the figure above spans a change in write rate "
                             f"and its deadline is stale; re-measure before acting on it")
        return note
    except (GitUnavailable, OSError, subprocess.SubprocessError, ValueError):
        return _TREND_UNAVAILABLE


def check_memory_index_integrity() -> "dict | None":
    """Catch memories that exist on disk but will never load into a session.

    A memory only loads if it is (a) present in the LIVE memory dir and (b)
    referenced in that dir's MEMORY.md index. Two silent-loss modes have bitten
    us (recurring field report 64340119): a memory file written to the live dir
    but never added to MEMORY.md, and a hard-won capability memory stranded in a
    ``*-BACKUP`` tree (created by scripts/sutando-migrate.sh) that never made it
    into the live index — so the rule it carried was written yet never recalled.

    A third mode has the same consequence: the entry EXISTS but sits past the
    point a session stops reading (200 lines or 25KB of post-strip content,
    whichever comes first — see MEMORY_INDEX_LOAD_*). Truncation drops the
    suffix, not the file, so this is measured by asking whether each memory is
    named in the prefix that actually loads.

    Not every absence from MEMORY.md is a loss, though: overflow entries live in
    sibling HUB indexes (``MEMORY-reference.md``, ``MEMORY-wire.md``, …) which
    MEMORY.md links to. Those are grep-reachable by design and are counted
    separately — lumping them in produced a 1010-file warn on this host whose
    real content was 906, and a warn nobody can read is a warn nobody reads.

    Fails only when a memory is demonstrably lost that way; warns for orphaned/
    stranded files and for an index merely approaching the limit. Returns None
    on a clean index or when the memory dir does not exist yet.
    """
    if not MEMORY_DIR.exists():
        return None
    index = MEMORY_DIR / "MEMORY.md"
    index_text = index.read_text(errors="ignore") if index.exists() else ""

    # What the session actually sees: strip what the runtime strips, then keep
    # only the prefix that fits inside 200 lines / 25KB.
    effective_text = _index_effective_text(index_text)
    loaded_text, loaded_bytes, loaded_lines = _index_loaded_prefix(effective_text)
    truncated = len(loaded_text) < len(effective_text)

    # An index that outgrows the load budget compacts its entries to prefix
    # abbreviations (`f:` for feedback_, `r:` for reference_, `p:` for project_).
    _INDEX_ABBREV = {"feedback_": "f:", "reference_": "r:", "project_": "p:"}

    def _referenced_in(hay: str, name: str) -> bool:
        stem = name[:-3] if name.endswith(".md") else name
        if name in hay or stem in hay:
            return True
        # Without this the probe calls an indexed file unindexed, and the
        # "fix" it invites — expanding the index — is what blows the read limit.
        for full, short in _INDEX_ABBREV.items():
            if not stem.startswith(full):
                continue
            token = short + stem[len(full):]
            # Trailing boundary so `f:foo` is not satisfied by `f:foobar`.
            if re.search(re.escape(token) + r"(?![\w-])", hay):
                return True
        return False

    # MEMORY.md is not the only index. Once a corpus outgrows the load budget the
    # overflow moves to sibling HUB indexes (MEMORY-reference.md, MEMORY-wire.md,
    # …). A hub entry deliberately does not auto-load — it only has to be findable
    # — so it is not a loss.
    #
    # But a hub is only findable if the LOADED prefix of MEMORY.md links to it.
    # Trusting every MEMORY*.md glob match instead lets an unlinked file (a stale
    # copy, a backup, an ordinary memory that happens to match the glob) launder
    # itself AND every filename it mentions into a false green — inside the one
    # probe that exists to prevent silent loss. A hub linked only PAST the cut is
    # equally unreachable, so `loaded_text` is the correct gate, not `index_text`.
    # (john-the-dev, #2483: an earlier revision of this change trusted the glob
    # and its test suite explicitly blessed the unlinked case.)
    #
    # An untrusted MEMORY*.md is therefore not an index at all: it falls through
    # to the classification below and is reported like any other unindexed file.
    index_names = {"MEMORY.md"}
    hub_text = ""
    for hub in sorted(MEMORY_DIR.glob("MEMORY*.md")):
        if hub.name == "MEMORY.md" or not _referenced_in(loaded_text, hub.name):
            continue
        index_names.add(hub.name)
        hub_text += "\n" + hub.read_text(errors="ignore")

    # (a) live memory files not referenced anywhere in MEMORY.md → won't load.
    # (c) referenced, but ONLY beyond the load cut → equally won't load. Same
    #     consequence, different cause, so they are found the same way: ask the
    #     prefix that actually loads, not the whole file. Testing the whole file
    #     reported an index entry parked on line 201 as healthy (john-the-dev,
    #     #2449) because the bytes were on disk — just never read.
    # (d) reachable only from a sibling hub index → by design, NOT a loss. Kept
    #     separate so the genuinely-lost names stay readable: on this host 104
    #     by-design entries were mixed into a single 1010-file warn, which is
    #     unreadable and therefore unread — 8 truly dark memories sat inside it
    #     for months while the probe "warned" about them on every run.
    unindexed: list[str] = []
    beyond_cut: list[str] = []
    hub_only: list[str] = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name in index_names:  # an index is not a memory it indexes
            continue
        if _referenced_in(loaded_text, p.name):
            continue
        if _referenced_in(effective_text, p.name):
            beyond_cut.append(p.name)
        elif _referenced_in(hub_text, p.name):
            hub_only.append(p.name)
        else:
            unindexed.append(p.name)

    # (b) memories stranded in a sibling *-BACKUP tree, absent from the live dir.
    stranded: list[str] = []
    try:
        claude_home = MEMORY_DIR.parent.parent.parent  # memory -> <slug> -> projects -> claude-home
        slug = MEMORY_DIR.parent.name
        for backup in claude_home.parent.glob(claude_home.name + "*BACKUP*"):
            bmem = backup / "projects" / slug / "memory"
            if bmem.is_dir():
                stranded += [
                    mp.name for mp in bmem.glob("*.md")
                    if mp.name != "MEMORY.md" and not (MEMORY_DIR / mp.name).exists()
                ]
    except Exception:  # pragma: no cover — best-effort backup scan; never break the health check
        pass

    # (c) the index outgrowing what a session reads. Truncation is a SUFFIX
    # drop, so the damage is exactly the entries past the cut — reported above
    # as `beyond_cut`. What is left to say here is whether the index is close
    # enough to the cut to be worth compacting before entries start falling off.
    effective_bytes = len(effective_text.encode("utf-8"))
    effective_lines = len(effective_text.splitlines())
    near_limit = (
        effective_bytes >= MEMORY_INDEX_LOAD_BYTES * MEMORY_INDEX_NEAR_LIMIT
        or effective_lines >= MEMORY_INDEX_LOAD_LINES * MEMORY_INDEX_NEAR_LIMIT
    )

    def _size_note() -> str:
        return (f"{effective_bytes / 1024:.1f}KB / {effective_lines} lines of loadable "
                f"content vs the {MEMORY_INDEX_LOAD_BYTES / 1000:.0f}KB "
                f"({MEMORY_INDEX_LOAD_BYTES:,} B) / "
                f"{MEMORY_INDEX_LOAD_LINES}-line session read limit")

    def _corpus_note() -> str:
        """Name the corpus measured: "compact it now" is destructive advice about
        one specific file, and SUTANDO_MEMORY_DIR can point this at another."""
        try:
            default = Path(_default_memory_dir())
        except Exception:  # pragma: no cover - defensive; never break the check
            return f" [corpus: {MEMORY_DIR}]"
        if default.resolve() != MEMORY_DIR.resolve():
            return (f" [corpus: {MEMORY_DIR} — set by SUTANDO_MEMORY_DIR, NOT the "
                    f"workspace default {default}; if sessions load the default, "
                    f"this verdict is about a corpus they never read]")
        return f" [corpus: {MEMORY_DIR}]"

    def _hub_note() -> str:
        return (f"; {len(hub_only)} reachable via a sibling hub index "
                f"({', '.join(sorted(index_names - {'MEMORY.md'}))}) — by design, not loaded"
                if hub_only else "")

    if not unindexed and not stranded and not beyond_cut and not near_limit:
        return {"name": "memory-index", "status": "ok",
                "detail": (f"all memory files reachable from the loaded MEMORY.md index"
                           f"{_hub_note()} ({_size_note()}){_corpus_note()}")}
    parts = []
    if beyond_cut:
        parts.append(
            f"{len(beyond_cut)} memory file(s) ARE indexed but sit past the load cut "
            f"(index stops at line {loaded_lines} / {loaded_bytes / 1024:.1f}KB), so the "
            f"session never reads their entry: {', '.join(beyond_cut[:6])}"
            f"{'…' if len(beyond_cut) > 6 else ''}. Compact the index — the prefix still "
            f"loads, only the tail is dropped ({_size_note()})"
        )
    elif near_limit:
        parts.append(
            f"MEMORY.md is approaching the session read limit ({_size_note()})"
            + (" and is already truncated" if truncated else "")
            + _index_growth_note(index, effective_bytes)
            + " — compact it now; entries past the cut are dropped silently while "
              "every memory file still looks fine on disk"
        )
    if unindexed:
        parts.append(
            f"{len(unindexed)} memory file(s) in NO index — neither MEMORY.md nor any "
            f"sibling hub — so they never load and cannot be found: "
            + ", ".join(unindexed[:6]) + ("…" if len(unindexed) > 6 else "")
            + _hub_note()
        )
    if stranded:
        parts.append(
            f"{len(stranded)} memory file(s) stranded in a *-BACKUP tree, absent from the live dir: "
            + ", ".join(sorted(set(stranded))[:6]) + ("…" if len(set(stranded)) > 6 else "")
        )
    # `fail` only for demonstrated loss: named memory files whose index entry is
    # past the cut and therefore never read. Everything else — orphans, strays,
    # merely approaching the limit — is degradation, not loss, so it warns.
    # (Earlier revisions failed on a raw-byte threshold, which fired on indexes
    # that still loaded fine once frontmatter/comments were excluded.)
    return {"name": "memory-index",
            "status": "fail" if beyond_cut else "warn",
            "detail": "; ".join(parts) + _corpus_note()}


def check_memory_sync() -> dict:
    """Verify memory sync is configured and has run recently.

    Cross-machine sync is OPT-IN. When it's deliberately disabled
    (vault.enabled=false) or simply not configured, that's a valid
    single-machine choice — report it as informational (ok), NOT a recurring
    warn (owner ask 2026-07-10 — the confusing memory-var nag). A
    configured-but-stale sync still warns; that's a real problem.

    The vault remote is read from the CANONICAL config (vault.remote_url via
    sutando_config.resolve_vault) with the deprecated SUTANDO_MEMORY_REPO in
    .env kept as a backward-compat fallback (#1446 window).
    """
    name = "memory-sync"
    vault = _resolved_vault()
    # Deliberate config opt-out (vault.enabled=false) → informational, never a
    # nag (#2069).
    if vault.get("_explicit_disable"):
        return {"name": name, "status": "ok", "detail": "cross-machine sync disabled (config opt-out)"}
    repo_url = _vault_remote_url(vault)
    if not repo_url:
        # Not configured anywhere → single-machine mode is a valid choice, not
        # a warn (#2069).
        return {"name": name, "status": "ok", "detail": "cross-machine sync not configured (single-machine mode)"}
    # Current model (sync-workspace.sh): the workspace ITSELF is a git repo with
    # the vault as a remote — sync = git fetch/merge/push on the workspace, no
    # separate clone dir. So the freshness signal is the workspace's own
    # .git/FETCH_HEAD. Prefer this whenever the workspace is a git repo; the
    # legacy ~/.sutando/memory-sync clone (sync-memory.sh, deprecated) often
    # lingers on disk abandoned and would otherwise read as permanently stale.
    ws_git_fetch = WORKSPACE_DIR / ".git" / "FETCH_HEAD"
    if (WORKSPACE_DIR / ".git").exists():
        if ws_git_fetch.exists():
            age_h = (time.time() - ws_git_fetch.stat().st_mtime) / 3600
            if age_h > 48:
                return {"name": name, "status": "warn",
                        "detail": f"workspace vault last fetched {age_h:.0f}h ago (stale)"}
            return {"name": name, "status": "ok",
                    "detail": f"workspace vault last fetched {age_h:.1f}h ago"}
        return {"name": name, "status": "ok", "detail": "workspace git repo, never fetched"}
    # Legacy memory-sync clone dir: PR #764 renamed legacy ~/.sutando-memory-sync/
    # → ~/.sutando/memory-sync/. Check new path first; fall back to legacy
    # for installs that haven't migrated yet (sync-memory.sh auto-migrates
    # on next run when env is unset).
    sync_dir_new = Path.home() / ".sutando" / "memory-sync"
    sync_dir_legacy = Path.home() / ".sutando-memory-sync"
    if sync_dir_new.exists():
        sync_dir = sync_dir_new
    elif sync_dir_legacy.exists():
        sync_dir = sync_dir_legacy
    else:
        return {"name": name, "status": "warn", "detail": "repo configured but never synced — run bash scripts/sync-workspace.sh"}
    git_dir = sync_dir / ".git" / "FETCH_HEAD"
    if git_dir.exists():
        age_h = (time.time() - git_dir.stat().st_mtime) / 3600
        if age_h > 48:
            return {"name": name, "status": "warn",
                    "detail": f"legacy memory-sync clone last fetched {age_h:.0f}h ago (stale)"}
        return {"name": name, "status": "ok",
                "detail": f"legacy memory-sync clone last fetched {age_h:.1f}h ago"}
    return {"name": name, "status": "ok",
            "detail": "legacy memory-sync clone initialized, never fetched"}


def _age_phrase(age_s) -> str:
    """None means the writer gave no usable timestamp — say so rather than
    printing a number a reader cannot tell apart from a real age."""
    return "age unknown" if age_s is None else f"{age_s}s ago"


def _norm_remote(u: str) -> str:
    """FETCH_HEAD drops a trailing `.git` that `remote get-url` keeps."""
    u = u.strip().rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def _last_fetch_age_s(repo, git_bin: str, expected: str) -> "int | None":
    """Seconds since this checkout last fetched `expected` from a remote, or None.

    Scoped to `origin/<expected>`, the exact comparison target: FETCH_HEAD's mtime is
    the last fetch of ANYTHING, so a PR ref — or `main` from a second remote — would
    otherwise date a stale origin/main.
    """
    try:
        out = subprocess.run([git_bin, "-C", str(repo), "rev-parse", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        common = Path(out.stdout.strip())
        if not common.is_absolute():
            common = Path(repo) / common
        fetch_head = common / "FETCH_HEAD"
        st = fetch_head.stat()
        url = subprocess.run([git_bin, "-C", str(repo), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
        if url.returncode != 0:
            return None
        # `remote get-url` keeps a trailing `.git` that FETCH_HEAD drops, so an exact
        # compare never matches on a real clone; normalise both ends before comparing.
        want = _norm_remote(url.stdout)
        marker = f"branch '{expected}' of "
        for line in fetch_head.read_text(errors="replace").splitlines():
            i = line.find(marker)
            if i >= 0 and _norm_remote(line[i + len(marker):]) == want:
                return max(int(time.time() - st.st_mtime), 0)
        return None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _fetch_age_phrase(age_s) -> str:
    """A currency claim with no fetch behind it is worth naming, not omitting."""
    if age_s is None:
        return "no fetch of that branch recorded"
    return f"a fetch {age_s // 3600}h{age_s % 3600 // 60}m ago"


def check_onboarding_status() -> "dict | None":
    """Read the desktop checklist's agent surface (onboarding v2 spec,
    ag2space-cinny-desktop#165 S4).

    The desktop Console mirrors its setup-checklist row states into
    `<workspace>/state/onboarding-status.json` (written by console_status,
    write-on-change). This check is the core-side half: surface rows the USER
    still needs (or that regressed from green) so the proactive loop can run
    the self-heal ladder and, failing that, tell the owner — instead of the
    Console being the only place onboarding failures are visible.

    Absent file → None (CLI installs and pre-S1 desktop builds have no
    checklist; nothing to report). Rows in "todo" → warn with the row names.
    Read-only; never raises.
    """
    name = "onboarding-status"
    path = WORKSPACE_DIR / "state" / "onboarding-status.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        # Shape-guard (Codex P1): a frontend bug could write [] or {"rows": []}
        # — non-dict shapes must degrade to 'unreadable', never raise.
        if not isinstance(data, dict) or not isinstance(data.get("rows"), dict):
            return {"name": name, "status": "warn", "detail": "onboarding-status.json unreadable"}
        rows = data["rows"]
        # Carry each row's own detail: "gateway" alone cannot distinguish "not
        # running" from a reconnect. `str()` because a separate repo writes this.
        todo_keys = [k for k, v in sorted(rows.items())
                     if isinstance(v, dict) and v.get("state") == "todo"]
        # A heartbeat refutes only the runtime-DOWN row; the writer also emits
        # "core running, Claude sign-in required", a real gap on a running core.
        _core = rows.get("core") if isinstance(rows.get("core"), dict) else {}
        _down = (_core.get("claude_authed") is not False
                 and "not running" in str(_core.get("detail") or "").lower())
        stale_core = "core" in todo_keys and _down and _fresh_local_core_record() is not None
        if stale_core:
            todo_keys.remove("core")
        todo = [f"{k} ({d})" if (d := str(rows[k].get("detail") or "").strip()[:120]) else k
                for k in todo_keys]
        # Absent/null/0 updated_at is UNKNOWN, not the epoch — int(None or 0)
        # rendered the whole unix time as an age (~56 years) on both lines.
        _updated = int(data.get("updated_at", 0) or 0)
        age_s = max(0, int(time.time()) - _updated) if _updated > 0 else None
    except (ValueError, OSError, TypeError):
        return {"name": name, "status": "warn", "detail": "onboarding-status.json unreadable"}
    if stale_core:
        rest = f"; still todo: {', '.join(todo)}" if todo else ""
        return {
            "name": name,
            "status": "warn",
            "detail": (f"Console mirror is stale — its core row says 'not running' but this "
                       f"host's heartbeat is live; mirror last written {_age_phrase(age_s)}"
                       f"{rest}"),
        }
    if todo:
        return {
            "name": name,
            "status": "warn",
            "detail": f"user-facing setup incomplete: {', '.join(todo)} ({_age_phrase(age_s)})",
        }
    return {"name": name, "status": "ok",
            "detail": f"all checklist rows satisfied ({_age_phrase(age_s)})"}


def check_host_subtrees() -> dict:
    """Surface per-host subtrees (hosts/<host>/) that have stopped syncing.

    Under the hosts/<hostname>/ convention each host writes only its own
    subtree, so the newest file mtime in a PEER's subtree is when sync last
    carried that host's writes here. A peer not updated in
    SUTANDO_STALE_HOST_DAYS days went quiet (crashed, decommissioned, or sync
    broke) — surface it rather than letting it silently rot. Read-only.

    THIS host's own subtree is excluded, because its mtime measures local
    writes, not sync: the core rewrites `hosts/<self>/current-track.md`,
    `pending-questions.md` and `crons.json` continuously, so it is fresh even
    where sync has never run once. Counting it made the verdict read
    "all synced <7d" on a single-machine install with `vault.enabled=false` —
    an assurance about the one thing the mtime cannot witness.
    """
    name = "host-subtrees"
    hosts_dir = WORKSPACE_DIR / "hosts"
    if not hosts_dir.is_dir():
        return {"name": name, "status": "ok", "detail": "no hosts/ subtree yet"}
    try:
        stale_days = float(os.environ.get("SUTANDO_STALE_HOST_DAYS", "7"))
    except ValueError:
        stale_days = 7.0
    subtrees = [d for d in sorted(hosts_dir.iterdir()) if d.is_dir()]
    if not subtrees:
        return {"name": name, "status": "ok", "detail": "hosts/ present, no host subtrees"}
    now = time.time()
    # Union of every label a launcher here could have written, and on failure the
    # empty set — which restores the old behaviour rather than excluding a peer we
    # could not identify. Under-claiming sync evidence beats asserting it.
    try:
        local = _local_host_labels()
        label_known = True
    except Exception:                     # noqa: BLE001 — a probe must not raise
        local, label_known = set(), False
    stale, fresh, own = [], 0, []
    for d in subtrees:
        newest = 0.0
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
        if newest == 0.0:
            continue  # empty subtree — nothing to age
        if d.name in local:
            own.append(d.name)
            continue
        age_d = (now - newest) / 86400
        if age_d > stale_days:
            stale.append(f"{d.name} ({age_d:.0f}d)")
        else:
            fresh += 1
    suffix = "" if label_known else " (could not identify this host's own label)"
    if stale:
        return {"name": name, "status": "warn",
                "detail": f"{len(stale)} peer subtree(s) stale (>{stale_days:.0f}d): "
                          f"{', '.join(stale)} — host stopped syncing?{suffix}"}
    if fresh:
        return {"name": name, "status": "ok",
                "detail": f"{fresh} peer subtree(s), all synced <{stale_days:.0f}d"
                          f"{f'; own subtree {own[0]} not aged (local writes)' if own else ''}{suffix}"}
    return {"name": name, "status": "ok",
            "detail": ("no peer subtree(s) to age — "
                       + (f"only this host's own ({', '.join(own)}), whose mtime is local writes, "
                          "not sync" if own else "hosts/ holds no datable subtree")
                       + suffix)}


def _durable_access_bytes(raw: bytes) -> "bytes | None":
    """Normalized, comparable view of an access.json for backup-drift detection.

    Drops the volatile ``pending`` block — short-lived pairing codes created on
    any non-owner DM that expire ~1h later (see #2260). Live grows and ages
    these out constantly, so a byte-for-byte compare would flag a perfectly
    healthy backup as "stale" on nearly every run. Comparing only the durable
    keys (``allowFrom`` / ``tierMap`` / …) makes the probe fire on real
    allowlist/tier drift, not pairing-code churn.

    Returns stable-sorted JSON bytes of the durable keys, or ``None`` when the
    payload is not parseable JSON — the caller then falls back to a raw byte
    compare (can't normalize, so stay conservative and flag any difference).
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k != "pending"}
    return json.dumps(data, sort_keys=True).encode()


def check_per_host_config_backup() -> dict:
    """Warn when a channel's access.json vault backup has drifted from live.

    sync-workspace's `_snapshot_per_host_config` copies each live
    <claude-home>/channels/<svc>/access.json into
    hosts/<host>/channels/<svc>/access.json — a pure backup carried by the vault
    (nothing reads the carrier live, so there's no read/write skew). If that
    snapshot silently stops refreshing, the vault copy drifts stale: the owner's
    allowlist LOOKS synced but recent changes never reach the vault. Observed
    2026-07-22 — a discord access.json backup 6 weeks stale while live had
    changed, so the owner asking "is my access.json synced?" got a misleading
    "there's a committed copy" when that copy was long out of date. This probe
    makes the drift a first-class, glanceable signal. Read-only: content compare,
    warn on divergence; never mutates either file.
    """
    name = "per-host-config-backup"
    # No vault URL → `_snapshot_per_host_config` is unreachable (it runs only
    # inside sync-workspace's push path, which exits early without one).
    if not _vault_remote_url():
        return {"name": name, "status": "ok",
                "detail": "no vault configured — nothing carries a backup (single-machine mode)"}
    try:
        # Resolve the live channels source from the SAME canonical resolver the
        # snapshot WRITER uses — sync-workspace's `_snapshot_per_host_config`
        # reads `sutando-config.sh claude-sutando-config-dir`, i.e.
        # resolve_claude_sutando_config_dir(). The old claude_home_path() fell
        # back to ~/.claude whenever CLAUDE_CONFIG_DIR was unset, but Sutando.app's
        # runHealthCheck() subprocess and the fallback launchd plist don't inject
        # it — so the probe read a DIFFERENT tree than the writer and false-greened
        # "no channels" on exactly the app/launchd paths this check exists to
        # cover (qingyun, #2277 review). The canonical resolver honors deliberate
        # config-based overrides (core_config_dirs) that the writer also respects.
        from sutando_config import resolve_claude_sutando_config_dir  # noqa: PLC0415
        channels_dir = resolve_claude_sutando_config_dir() / "channels"
    except Exception:
        return {"name": name, "status": "ok", "detail": "no channels dir resolvable"}
    if not channels_dir.is_dir():
        return {"name": name, "status": "ok", "detail": "no channels configured"}
    carrier_base = WORKSPACE_DIR / "hosts" / _host_label() / "channels"
    drift, checked = [], 0
    for live in sorted(channels_dir.glob("*/access.json")):
        svc = live.parent.name
        try:
            live_bytes = live.read_bytes()
        except OSError:
            # An unreadable LIVE access.json is the exact failure this probe
            # exists to surface — never silently skip it. Skipping let a lone
            # unreadable live file fall through to checked==0 → a false
            # "no channel access.json to back up" all-clear (qingyun, #2277
            # review). Count + flag it; the probe stays non-fatal (warn).
            drift.append(f"{svc} (live unreadable)")
            checked += 1
            continue
        checked += 1
        carrier = carrier_base / svc / "access.json"
        if not carrier.exists():
            drift.append(f"{svc} (no backup)")
            continue
        try:
            carrier_bytes = carrier.read_bytes()
        except OSError:
            drift.append(f"{svc} (unreadable backup)")
            continue
        # Compare only the durable config — the volatile `pending` pairing-code
        # block churns ~hourly and would otherwise flag every healthy backup
        # (john, #2277 review). Raw-byte fallback when either side is malformed.
        live_norm = _durable_access_bytes(live_bytes)
        carrier_norm = _durable_access_bytes(carrier_bytes)
        if live_norm is None or carrier_norm is None:
            if carrier_bytes != live_bytes:
                drift.append(f"{svc} (stale)")
        elif live_norm != carrier_norm:
            drift.append(f"{svc} (stale)")
        # else: durable config matches — any pending-only diff is healthy churn.
    if checked == 0:
        return {"name": name, "status": "ok", "detail": "no channel access.json to back up"}
    if drift:
        return {"name": name, "status": "warn",
                "detail": f"{len(drift)} channel access.json backup(s) drifted from live: "
                          f"{', '.join(drift)} — vault copy stale; a full sync-workspace "
                          f"(_snapshot_per_host_config) should refresh it"}
    return {"name": name, "status": "ok", "detail": f"{checked} channel access.json backup(s) current"}


# How far behind the expected branch before the checkout is worth nagging about.
# Deliberately not 1: `main` on this repo moves several times a day, so warning on
# any non-zero delta would fire constantly and train the reader to ignore it —
# the failure mode that makes a health check worthless.
_BEHIND_WARN_DEFAULT = 10


def _behind_warn_threshold(repo: "Path") -> int:
    """Positive commits-behind threshold from the durable core config.

    Read through the SAME `core` config path as `expected_branch` below — no
    ad-hoc env var (repo rule: config belongs in the declared config block, not
    in an invented environment variable).

    Read LAZILY and validated, because the first cut of this did neither: it
    ran `int(os.environ[...])` at import time, so
    `SUTANDO_CHECKOUT_BEHIND_WARN=not-a-number` raised ValueError before the
    module finished loading and took down the ENTIRE health check — a probe
    meant to reveal stale guards instead reporting no health at all. A zero
    was equally bad: it warned on an exactly-current checkout. So every
    invalid class — unreadable config, non-integer, zero, negative — falls
    back to the default rather than crashing or crying wolf.
    """
    try:
        from sutando_config import load_config  # noqa: PLC0415
        raw = (load_config(repo_root=repo).get("core") or {}).get("checkout_behind_warn")
    except Exception:
        return _BEHIND_WARN_DEFAULT          # config unreadable/malformed
    # `bool` is a subclass of `int`, so `int(True)` is 1 and a plausible config
    # typo — `"checkout_behind_warn": true` — would silently warn on every
    # one-commit drift. That is precisely the alert fatigue the default of 10
    # exists to prevent, and the probe would train users to ignore it. Accept a
    # REAL integer only; the schema declares this key as an integer, so every
    # other type (bool, float, numeric string) is invalid config and falls back.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return _BEHIND_WARN_DEFAULT          # absent, bool, or non-integer
    return raw if raw > 0 else _BEHIND_WARN_DEFAULT   # zero/negative would false-alarm


def _behind_commits_changing(repo: "Path", branch: str, prefix: "str | list[str]",
                             git_bin: str = "git") -> "list[str]":
    """Subjects of not-yet-pulled commits that EFFECTIVELY change ``prefix``.

    ``prefix`` is one pathspec or several; several are passed to git as
    separate pathspecs, so the result is their union.

    Two different questions, and the first cut answered the wrong one. "Did a
    not-yet-pulled commit TOUCH this path" is commit-path history; "would
    pulling change any bytes here" is a tree diff. They diverge whenever history
    is reversible: upstream adds `skills/demo/SKILL.md` and removes it in the
    next commit, a clone sits two commits behind, and
    `git log HEAD..origin/main -- skills/` lists both commits while
    `git diff --name-only HEAD..origin/main -- skills/` is EMPTY. Pulling would
    change no skill bytes, yet the probe warned — a false behavioral-staleness
    alarm, which is precisely the alert fatigue this check argues against.
    Reproduced independently by qingyun-wu and john-the-dev on #2573.

    So the TREE DIFF is the gate and history is only the message: if nothing
    under ``prefix`` differs, return nothing; only when it does differ, name the
    commits so the warning is actionable.

    Same last-fetched-ref, no-network contract as `_commits_behind`, and the
    same honest consequence: a stale local ref makes this UNDER-report, never
    cry wolf. Returns `[]` on any failure for the same reason — this is an
    additional signal layered on a probe that must not become the thing that
    breaks the health run.
    """
    paths = [prefix] if isinstance(prefix, str) else list(prefix)
    if not paths:
        return []
    try:
        changed = subprocess.run(
            [git_bin, "-C", str(repo), "diff", "--name-only",
             f"HEAD..origin/{branch}", "--", *paths],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if changed.returncode != 0 or not changed.stdout.strip():
        return []

    try:
        out = subprocess.run(
            [git_bin, "-C", str(repo), "log", "--no-merges", "--format=%s",
             f"HEAD..origin/{branch}", "--", *paths],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


# Source path -> pgrep pattern for each long-running service launched from this
# checkout. A directory entry covers every file the service imports from it.
_SERVICE_SOURCES = (
    ("src/voice-agent.ts", "voice-agent.ts"),
    ("src/web-client.ts", "web-client.ts"),
    ("src/discord-bridge.py", "discord-bridge.py"),
    ("src/telegram-bridge.py", "telegram-bridge.py"),
    ("src/slack-bridge.py", "slack-bridge.py"),
    ("src/core_heartbeat.py", "core_heartbeat.py"),
    ("src/watch-tasks-stream.sh", "watch-tasks-stream.sh"),
    ("src/remote-gateway-bridge.py", "remote-gateway-bridge.py"),
    ("packages/ag2-sparrow/ag2_sparrow/", "remote-gateway-bridge.py"),
    ("skills/phone-conversation/scripts/conversation-server.ts", "conversation-server.ts"),
)


def _running_service_sources() -> "list[str]":
    """Repo-relative paths whose backing service is running FROM THIS CHECKOUT.

    Returns paths, not verdicts: the caller decides what a not-yet-pulled change
    to one of them means. Empty on any probe failure, so a broken `pgrep` cannot
    manufacture a warning.
    """
    live = []
    for path, pattern in _SERVICE_SOURCES:
        try:
            found = subprocess.run(
                ["/usr/bin/pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=5,
            ).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            continue
        # Same checkout-scoping as mark_stale_if_outdated: a sibling clone's
        # process is not evidence about this checkout's source.
        if found and _filter_pids_this_checkout([p for p in found if p]):
            live.append(path)
    return live


def _commits_behind(repo: "Path", branch: str, git_bin: str = "git") -> "int | None":
    """Commits on origin/<branch> that HEAD lacks, or None if unanswerable.

    Never fetches. Without a merge-base the count is not a distance, so returns None.
    """
    try:
        base = subprocess.run(
            [git_bin, "-C", str(repo), "merge-base", "HEAD", f"origin/{branch}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if base.returncode != 0:
        # No shared history: a count here would be a number without a meaning.
        return None
    try:
        out = subprocess.run(
            [git_bin, "-C", str(repo), "rev-list", "--count", f"HEAD..origin/{branch}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None          # no such remote ref (fresh clone, renamed remote)
    raw = out.stdout.strip()
    return int(raw) if raw.isdigit() else None


def check_live_checkout_branch(repo_dir: "Path | None" = None) -> dict:
    """Warn when the live checkout has drifted off its expected branch.

    Bridges and the core boot from this checkout, and Sutando.app's 30-min
    health check auto-restarts bridges onto whatever is checked out HERE.
    Observed 2026-07-29: a Jul-25 session checked out a PR branch on the live
    checkout to author a PR and never switched back — for 4 days every bridge
    auto-restart booted 4-day-stale feature-branch code (75 commits behind
    main), and nothing surfaced it. This probe makes that drift a first-class,
    glanceable signal (structural, not disciplinary — "remember to switch
    back" doesn't survive session death).

    Expected branch defaults to ``main``; nodes intentionally pinned elsewhere
    (e.g. the dual-run pinned hosts) declare it durably in
    ``sutando.config.local.json`` as ``{"core": {"expected_branch": "..."}}``
    — read via the canonical loader so launchd/Sutando.app callers (which
    never inherit an interactive shell's exports) honor the pin across
    restarts. ``SUTANDO_EXPECTED_BRANCH`` remains as a per-invocation env
    override (wins over config; useful for tests/one-offs).
    Read-only; warn (never fail) — an intentional short-lived checkout should
    nag, not page. Degrades to ok when git/branch state can't be read (CI
    tarballs, detached tooling contexts) rather than false-alarming.
    """
    name = "live-checkout-branch"
    repo = Path(repo_dir) if repo_dir is not None else REPO_DIR
    expected = os.environ.get("SUTANDO_EXPECTED_BRANCH")
    if not expected:
        try:
            from sutando_config import load_config  # noqa: PLC0415
            expected = (load_config(repo_root=repo).get("core") or {}).get("expected_branch")
        except Exception:
            expected = None  # config unreadable — fall through to the default
    expected = expected or "main"
    # Resolve the git binary ONCE for both subprocesses in this probe.
    #
    # This used to be `git_bin = "git"` with a comment promising that #2469's
    # resolver "replaces this line when it lands". That was a TODO wearing a
    # design rationale: at the merged tree of both heads, `resolve_git` was
    # imported at module scope and used elsewhere while this probe still shelled
    # the literal — so the cumulative state kept the Xcode-CLT shim modal that
    # #2469 exists to remove. A swap point nobody swaps is not a fix.
    #
    # Imported lazily and defensively so this composes in EITHER merge order:
    # before #2469 lands the module is absent and we behave exactly as today;
    # after it lands both calls go through the resolver with no further edit.
    try:
        from git_binary import resolve_git  # noqa: PLC0415
        git_bin = resolve_git()
    except Exception:
        git_bin = "git"
    if git_bin is None:
        # Resolver says there is no runnable git (CLT absent, only the stub).
        # Degrade like the OSError path below rather than shelling the shim —
        # that modal is the whole point of #2469.
        return {"name": name, "status": "ok",
                "detail": "no runnable git (resolver) — skipping"}
    try:
        # `git_bin` came from the resolver above, so it is never a bare `git`
        # resolving through PATH — which on a Mac without developer tools lands
        # on the Xcode-CLT stub, whose modal install dialog fires BEFORE the
        # return-code check below can degrade. This check is registered
        # unconditionally, so it ran on every health pass.
        out = subprocess.run(
            [git_bin, "-C", str(repo), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"name": name, "status": "ok", "detail": "git not runnable — skipping"}
    if out.returncode != 0:
        return {"name": name, "status": "ok", "detail": "not a git checkout — skipping"}
    branch = out.stdout.strip()
    if not branch:
        # Detached HEAD: can't name a branch; still drift, still worth a nag.
        return {"name": name, "status": "warn",
                "detail": f"live checkout is on a detached HEAD (expected {expected!r}) — "
                          "bridges auto-restart onto whatever is checked out here; "
                          f"switch back (git -C {repo} switch {expected})"}
    if branch != expected:
        return {"name": name, "status": "warn",
                "detail": f"live checkout is on branch {branch!r}, expected {expected!r} — "
                          "bridges/core auto-restart onto this checkout, so a leftover "
                          "PR-branch checkout ships stale/unreviewed code (2026-07-29 "
                          "incident: 4 days on a Jul-25 PR branch). Author PRs in "
                          f"worktrees; switch back (git -C {repo} switch {expected})"}
    # On the expected branch — but that is only half of "is this checkout current".
    # A checkout can be on `main` and still be executing weeks-old code, and the
    # branch-name comparison above returns ok for it. Observed 2026-08-01 on the
    # 24/7 node: on `main`, 0 ahead, **15 commits behind**, and four merged guards
    # were consequently not running — including the MEMORY.md load-limit warning
    # (#2449), so the memory index silently truncated with nothing to report it.
    # A peer node reproduced the same shape at 31 behind. Nothing anywhere
    # surfaced either, because wrong-branch and stale-branch are different
    # failure modes and only the first had a probe.
    behind = _commits_behind(repo, expected, git_bin)
    if behind is not None and behind >= _behind_warn_threshold(repo):
        return {"name": name, "status": "warn",
                "detail": f"live checkout is on {expected!r} but {behind} commits behind "
                          f"origin/{expected} — merged fixes are not running here, and a "
                          "guard that never shipped to this machine reports nothing (so "
                          "silence reads as health). Refresh with "
                          f"`git -C {repo} pull --ff-only` + restart. Count is against the "
                          "last-fetched ref; this probe does not fetch."}
    # Count is the wrong instrument for BEHAVIORAL staleness, and the threshold
    # above is deliberately 10 to avoid alert fatigue — correctly, since `main`
    # moves several times a day. But one commit that rewrites a skill outranks
    # nine that touch docs, and a count cannot tell them apart.
    #
    # `src/` shares the skills blind spot: `*-stale` probes compare a process to
    # the file ON DISK, which agree byte for byte while the checkout is behind.
    #
    # Observed 2026-08-03 on this node: exactly ONE commit behind — far under the
    # threshold, so this probe reported ok — while the live `context-reconstruct`
    # still instructed writing `state/current-track.md`, the shared flat path
    # that delivers one host's anchor onto another host at the same local path
    # (#2567/#2568). The running skill and the merged skill disagreed, and both
    # looked correct from where anyone was standing.
    #
    # This sentence used to say that collision "had destroyed a peer's anchor".
    # Nothing was destroyed — see the UNSAFE_TO_READD comment above, which is the
    # authority: the vault uses per-host branches, so a host only ever merges a
    # peer INTO its own branch. The example still lands, because it never needed
    # the destruction to: the point is that the running skill and the merged one
    # disagreed invisibly, and that is true of any content difference.

    # behind==0 => empty tree diff => both probes below are provably empty, and the
    # census costs ten 5s-timeout pgreps to say so. None is unanswerable, so it probes.
    if behind == 0:
        return {"name": name, "status": "ok",
                "detail": f"live checkout on {expected!r} "
                          f"({_fetch_age_phrase(_last_fetch_age_s(repo, git_bin, expected))})"}
    stale_skills = _behind_commits_changing(repo, expected, "skills/", git_bin)
    # LIVE processes only: `src/` moves several times a day, so the running set
    # is what keeps this from becoming the alert fatigue the threshold prevents.
    live_sources = _running_service_sources()
    stale_services = (_behind_commits_changing(repo, expected, live_sources, git_bin)
                      if live_sources else [])
    if stale_skills or stale_services:
        # No shared history: the tree diff is still valid, but no count and no
        # fast-forward are, so say that rather than render "None commit(s)".
        subset = stale_skills or stale_services
        if behind is None:
            distance = (f"live checkout is on {expected!r} an unknown distance behind "
                        f"origin/{expected} (no common ancestor — shallow clone, so a commit "
                        "count is not available)")
            # "of them" needs a total to refer to.
            share = f"{len(subset)} commit(s) change"
            refresh = (f"`git -C {repo} fetch --unshallow` first; `pull --ff-only` cannot "
                       "apply without a shared history")
        else:
            distance = (f"live checkout is on {expected!r} and only {behind} commit(s) behind "
                        f"origin/{expected} — under the {_behind_warn_threshold(repo)}-commit "
                        "nag threshold")
            # The count above is the TOTAL, so "of them" keeps the subset a subset.
            share = f"{len(subset)} of them change"
            refresh = f"`git -C {repo} pull --ff-only`"
        tail = (f"Measured against the last-fetched ref; this probe does not fetch, "
                f"so it can only under-report.")
        if stale_skills:
            return {"name": name, "status": "warn",
                    "detail": f"{distance} — but {share} `skills/`, "
                              "which the agent re-reads from this checkout on EVERY invocation. "
                              "Those merged skill fixes are not in effect here, and no "
                              "restart-staleness probe can see it: a skill has no running process "
                              f"to compare against. ({'; '.join(stale_skills[:3])}"
                              f"{'; …' if len(stale_skills) > 3 else ''}) Refresh with "
                              f"{refresh}. {tail}"}
        return {"name": name, "status": "warn",
                "detail": f"{distance} — but {share} source backing a service running "
                          "here, so those merged fixes are not executing. No restart-staleness "
                          "probe can see it: they compare a process against the file ON DISK, "
                          "and while the checkout is behind those agree exactly. "
                          f"({'; '.join(stale_services[:3])}"
                          f"{'; …' if len(stale_services) > 3 else ''}) Refresh with "
                          f"{refresh} + restart the affected service. {tail}"}
    return {"name": name, "status": "ok",
            "detail": f"live checkout on {expected!r}"
                      + (f", {behind} commits behind" if behind else "")
                      + f" ({_fetch_age_phrase(_last_fetch_age_s(repo, git_bin, expected))})"}


def check_engine_revision_drift(repo_dir: "Path | None" = None,
                                manifest_path: "Path | None" = None) -> dict:
    """Warn when the checked-out source has moved off the BUILT engine revision.

    `dist/` is gitignored, so source advances while the artifacts stay behind.
    """
    name = "engine-revision-drift"
    repo = Path(repo_dir) if repo_dir is not None else REPO_DIR
    manifest = Path(manifest_path) if manifest_path is not None else repo.parent / "ENGINE_MANIFEST.json"

    if not manifest.is_file():
        # A plain source clone has no bundle manifest — nothing to compare.
        return {"name": name, "status": "ok",
                "detail": "no ENGINE_MANIFEST.json — not a bundled engine, skipping"}
    try:
        built = (json.loads(manifest.read_text()) or {}).get("sha")
    except (OSError, ValueError) as e:
        return {"name": name, "status": "ok",
                "detail": f"unreadable ENGINE_MANIFEST.json ({str(e)[:40]}) — skipping"}
    if not isinstance(built, str) or not built.strip():
        return {"name": name, "status": "ok",
                "detail": "ENGINE_MANIFEST.json has no sha — skipping"}
    built = built.strip()

    # A resolver failure must degrade like resolve_git() -> None, never to bare
    # `git`, which can resolve the Xcode-CLT stub and raise the install dialog.
    try:
        from git_binary import resolve_git  # noqa: PLC0415
        git_bin = resolve_git()
    except Exception:
        git_bin = None
    if git_bin is None:
        return {"name": name, "status": "ok", "detail": "no runnable git — skipping"}

    def _git(*args):
        return subprocess.run([git_bin, "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=10)

    try:
        head = _git("rev-parse", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return {"name": name, "status": "ok", "detail": "git not runnable — skipping"}
    if head.returncode != 0:
        return {"name": name, "status": "ok", "detail": "not a git checkout — skipping"}
    head_sha = head.stdout.strip()
    # Normalise first: an abbreviated sha (or a tag) of the checked-out commit
    # would otherwise compare unequal and print "X != X (0 commits ahead)".
    try:
        resolved = _git("rev-parse", f"{built}^{{commit}}")
        if resolved.returncode == 0 and resolved.stdout.strip():
            built = resolved.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass  # keep the literal; the comparison below is still correct for it
    if head_sha == built:
        return {"name": name, "status": "ok",
                "detail": f"source matches built revision {built[:9]}"}

    # An unanswerable count must not soften the drift, which is already established.
    detail = f"source HEAD {head_sha[:9]} != built revision {built[:9]}"
    try:
        if _git("cat-file", "-e", built).returncode == 0 and \
           _git("merge-base", "--is-ancestor", built, "HEAD").returncode == 0:
            ahead = _git("rev-list", "--count", f"{built}..HEAD").stdout.strip()
            # Zero ahead of an ancestor means it IS this commit under another name.
            if ahead == "0":
                return {"name": name, "status": "ok",
                        "detail": f"source matches built revision {built[:9]}"}
            if ahead:
                detail += f" ({ahead} commits ahead)"
        else:
            detail += " (diverged, or the built commit is outside this shallow clone)"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"name": name, "status": "warn",
            "detail": detail + " — dist/ is gitignored so it did NOT follow the source; "
                               "the Node half still runs the older build. Rebuild and "
                               "reactivate the engine rather than trusting the checkout."}


def check_migrate_reader_contract() -> dict:
    """Verify migration CLASS_RULES are compatible with reader resolution chains (issue #1543).

    Runs tests/migrate-reader-contract.test.py, which asserts that each file
    in sutando-migrate.sh CLASS_RULES lands in a location its reader actually
    checks.  A mismatch causes silent data loss — the reader falls back to a
    default rather than finding the migrated file (see incident #1540).
    """
    name = "migrate-reader-contract"
    test_path = REPO_DIR / "tests" / "migrate-reader-contract.test.py"
    if not test_path.exists():
        return {"name": name, "status": "ok", "detail": "test not found (pre-#1543 install)"}
    try:
        result = subprocess.run(
            [sys.executable, str(test_path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return {"name": name, "status": "ok", "detail": "all CLASS_RULES compatible with reader contracts"}
        first_fail = next(
            (ln.strip() for ln in (result.stdout + result.stderr).splitlines() if "FAIL" in ln or "Error" in ln),
            "contract mismatch — run tests/migrate-reader-contract.test.py for details",
        )
        return {"name": name, "status": "error", "detail": first_fail}
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "warn", "detail": "timed out after 15s"}
    except Exception as e:
        return {"name": name, "status": "error", "detail": str(e)}


def check_tcc_documents_access() -> dict:
    """Detect macOS TCC denial of Documents-folder access (issue #709).

    Relevant when REPO_DIR is inside ~/Documents — the default location for
    git checkouts on macOS. A process that hasn't been granted Documents access
    in System Settings → Privacy & Security → Files and Folders will hit
    PermissionError on every file read/write in the repo, causing tasks to go
    missing and services to crash on startup with no obvious error.

    Probe: attempt to list REPO_DIR and write+unlink a throwaway temp file.
    Safe even when access is denied — the PermissionError is caught and reported
    rather than propagated.
    """
    name = "tcc-documents-access"
    docs_dir = Path.home() / "Documents"
    try:
        in_documents = str(REPO_DIR.resolve()).startswith(str(docs_dir.resolve()))
    except OSError:
        in_documents = True  # can't resolve → assume we're in Documents and probe

    if not in_documents:
        return {"name": name, "status": "ok", "detail": "repo not in ~/Documents — TCC check N/A"}

    probe = REPO_DIR / ".tcc-probe"
    try:
        list(REPO_DIR.iterdir())
        probe.write_text("")
        probe.unlink()
        return {"name": name, "status": "ok", "detail": "Documents folder access granted"}
    except PermissionError:
        try:
            probe.unlink()
        except Exception:
            pass
        return {
            "name": name,
            "status": "fail",
            "detail": (
                "macOS TCC denied Documents folder access — grant in "
                "System Settings → Privacy & Security → Files and Folders "
                "→ Terminal (or your IDE/launchd app)"
            ),
        }
    except OSError:
        return {"name": name, "status": "ok", "detail": "Documents access check inconclusive"}


# ---------------------------------------------------------------------------
# Fix attempts
# ---------------------------------------------------------------------------

# Checks that are named by service but recovered via their launchd job:
# the --fix dispatch matches names starting with "com.sutando." OR names in
# this map (issue #1888 bug 1 — the bare names never matched the prefix
# branch, so --fix silently skipped voice-agent/web-client).
LAUNCHD_BACKED_CHECKS = {
    "voice-agent": "com.sutando.voice-agent",
    "web-client": "com.sutando.web-client",
}


def fix_launchd(label: str) -> str:
    """Try to reload a launchd job.

    A missing plist is NOT an edge case for these two services, and saying
    "no plist found" reads like a launchd failure when it is not one. The
    voice-agent / web-client plists are generated by **Sutando.app's
    installer** (see `_voice_log_path`); this repo ships installers only for
    credential-proxy, cron-runner, health-check and the app itself. On a plain
    checkout `src/startup.sh` launches both services directly instead —
    `run_node_service voice-agent src/voice-agent.ts ... &` — precisely because
    it checks `launchctl print` first and falls through when there is no job.

    So on any host without Sutando.app installed, --fix reaches this function
    for a stale voice-agent, prints one internal-sounding line, and can never
    restart it. Observed on the 24/7 node 2026-08-03: `voice-agent: stale
    (code is 2159 min newer than process)` survived repeated `--fix` runs,
    each reporting `no plist found for com.sutando.voice-agent`.

    Two distinct misses share one message, and only one of them is a bug:
      * label absent from plist_map  -> a real "we don't know this job"
      * label known, plist missing   -> the service is simply not launchd-
        managed here, and the operator needs the command that DOES work
    """
    plist_map = {
        "com.sutando.voice-agent": Path.home() / "Library/LaunchAgents/com.sutando.voice-agent.plist",
        "com.sutando.web-client": Path.home() / "Library/LaunchAgents/com.sutando.web-client.plist",
    }
    plist = plist_map.get(label)
    if not plist:
        return f"no plist found for {label}"
    if not plist.exists():
        # Deliberately names a runnable command. A --fix line that reports a
        # miss without naming the remedy is indistinguishable, to whoever
        # reads the log, from a fix that was attempted and failed.
        return (
            f"{label} is not launchd-managed on this host (no {plist.name} in "
            f"~/Library/LaunchAgents — that plist comes from Sutando.app's installer). "
            f"startup.sh launches this service directly instead, so --fix cannot "
            f"restart it. Remedy: bash src/restart.sh"
        )

    uid = subprocess.run(["/usr/bin/id", "-u"], capture_output=True, text=True).stdout.strip()
    if label == "com.sutando.voice-agent":
        # Amendment T4 (kill-path inventory): NEVER a direct
        # `launchctl kickstart -k` of voice-agent from here. kickstart -k is a
        # kill-and-restart, so the pre-kickstart validation — identity of the
        # running job pid, checked as ONE guarded `voice-lock.py takeover`
        # transaction under the held fcntl guard — must precede it. The
        # guarded wrapper scripts/restart-voice-agent.sh wraps exactly that
        # (validate → TERM → wait → KILL → revalidate → unlink, then
        # kickstart + etime verification). Identity mismatch ⇒
        # takeover-blocked, nothing signaled; no usable interpreter ⇒ the
        # wrapper fails closed (exit 6) before touching the lock or the
        # process. The launchd `bootstrap` fallback below stays available —
        # it loads a job without signaling anything.
        wrapper = REPO_DIR / "scripts" / "restart-voice-agent.sh"
        result = subprocess.run(
            ["/bin/bash", str(wrapper)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return f"restarted {label} (guarded restart wrapper)"
    else:
        # Try kickstart
        result = subprocess.run(
            ["/bin/launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return f"restarted {label}"
    # Try bootstrap
    result = subprocess.run(
        ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return f"bootstrapped {label}"
    return f"failed to restart {label}: {result.stderr.strip()}"


def fix_screen_capture() -> str:
    """Restart the screen-capture server (:7845), guarded like startup.sh.

    Order matters: reap any existing listener first (a dead-perm or wedged
    server holds the port and would block the new bind), then re-verify
    Screen Recording with a real capture — an all-black denial PNG
    compresses to ~43KB at 5K resolution, so <5000 bytes means the
    permission is missing or stale. Starting a server without the perm
    would recreate the stale-:7845 state startup.sh's PERM_OK gate exists
    to prevent: every /capture answered with a black-PNG denial.
    """
    subprocess.run("/usr/sbin/lsof -ti:7845 | xargs kill 2>/dev/null", shell=True, capture_output=True)
    probe = Path("/tmp/sutando-healthfix-permcheck.png")
    subprocess.run(["/usr/sbin/screencapture", "-x", str(probe)], capture_output=True)
    size = probe.stat().st_size if probe.exists() else 0
    probe.unlink(missing_ok=True)
    if size < 5000:
        return ("not restarted — Screen Recording permission missing/stale; grant it in "
                "System Settings → Privacy & Security, fully quit the terminal app, re-run startup.sh")
    log_path = WORKSPACE_DIR / "logs" / "screen-capture.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([sys.executable, str(REPO_DIR / "src" / "screen-capture-server.py")],
                     stdout=open(str(log_path), "a"), stderr=subprocess.STDOUT,
                     start_new_session=True)
    time.sleep(1.5)
    after = check_port(7845, "screen-capture")
    return "restarted on :7845" if after["status"] == "ok" else (
        f"restart attempted but port check says {after['status']} — see {log_path}")


def _checkout_is_canonical(repo_dir) -> tuple:
    """(ok, reason): is the code checkout safe to auto-restart a bridge FROM?"""
    try:
        # Route through git_argv, never a bare "git": a bare-string PATH lookup resolves to
        # the /usr/bin/git SHIM on a macOS host without the Xcode command line tools, which
        branch_proc = subprocess.run(
            git_argv("-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"),
            capture_output=True, text=True, timeout=10,
        )
        dirty_proc = subprocess.run(
            git_argv("-C", str(repo_dir), "status", "--porcelain"),
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001 — any git failure → fail closed
        return (False, f"git state unreadable ({e})")
    # A nonzero git exit means the (possibly empty) stdout can't be trusted — an empty
    # `status --porcelain` from a FAILED call must not read as "clean" and green-light an
    if branch_proc.returncode != 0 or dirty_proc.returncode != 0:
        rc = branch_proc.returncode or dirty_proc.returncode
        return (False, f"git state unreadable (git exit {rc})")
    branch = branch_proc.stdout.strip()
    dirty = dirty_proc.stdout.strip()
    if branch != "main":
        return (False, f"checkout on '{branch or 'detached HEAD'}', not main")
    if dirty:
        return (False, "checkout has uncommitted changes")
    return (True, "clean + on main")


# Printed when an owner alert could not be delivered
ALERT_UNDELIVERED_MARKER = "ALERT-UNDELIVERED"


def _default_local_notifier(msg: str) -> bool:
    """Owner surface that does not depend on any Sutando bridge. True only on a
    zero exit, so a missing osascript reads as undelivered rather than success."""
    safe = str(msg).replace('"', "").replace("\\", "")
    try:
        return subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "Sutando — bridge down"'],
            check=False, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001 — alerting must never break the check
        return False


# Per-bridge EXACT detail: gateway-bridge's other warns (not-serving, duplicate
# pileup) describe a live process, so only this string may respawn one.
GATEWAY_DOWN_DETAIL = (
    "configured but NOT running — ag2.space mobile messages will not be delivered"
)
DOWN_BRIDGE_DETAILS = {
    "telegram-bridge": "configured but not running",
    "discord-bridge": "configured but not running",
    "slack-bridge": "configured but not running",
    "gateway-bridge": GATEWAY_DOWN_DETAIL,
}


def fix_down_bridges(checks: list, *, action=None, sender=None, guard=None,
                     notifier=None) -> list:
    """Restart or alert on configured-but-not-running channel bridges."""
    if action is None:
        action = resolve_down_bridge_action()
    if action == "off":
        return []
    send = sender or _default_slack_sender
    notify = notifier if notifier is not None else _default_local_notifier
    guard = guard or _checkout_is_canonical

    def _alert(msg: str) -> None:
        print(f"  {msg}")
        # The sender reports failure by RETURNING False, not by raising, so a
        # discarded return makes the whole alert path a silent no-op.
        try:
            delivered = bool(send(msg))
        except Exception:  # noqa: BLE001 — alerting must never break the check
            delivered = False
        if not delivered:
            # A down bridge is exactly when a bridge-borne alert cannot arrive, so
            # the fallback must not be another bridge: osascript runs at OS level.
            try:
                delivered = bool(notify(msg))
            except Exception:  # noqa: BLE001 — alerting must never break the check
                delivered = False
            if delivered:
                print("  alert delivered via local notification "
                      "(primary sender reported failure)")
        if not delivered:
            print(f"  {ALERT_UNDELIVERED_MARKER}: owner was NOT alerted — {msg}")

    restarted = []
    for c in checks:
        # The name gate is NOT redundant with the detail match: for an unknown
        # name the lookup is None, and a check with no detail is also None.
        if not (
            c["name"] in DOWN_BRIDGE_DETAILS
            and c["status"] == "warn"
            and c.get("detail") == DOWN_BRIDGE_DETAILS[c["name"]]
        ):
            continue
        name = c["name"]

        # Decide restart vs. alert. "restart" only restarts from a canonical
        # checkout; otherwise it downgrades to an alert.
        if action == "restart":
            ok, why = guard(REPO_DIR)
            if not ok:
                _alert(f"⚠️ health-check: {name} is DOWN — NOT auto-restarted "
                       f"({why}). Start it via startup.sh once the checkout is clean/on main.")
                continue
        else:  # action == "alert"
            _alert(f"⚠️ health-check: {name} is DOWN — alert-only "
                   f"(down_bridge_action=alert). Start it via startup.sh.")
            continue


        # #2905 moved the spawn behind the shared launch policy; this PR's
        # contribution is the decision ABOVE it, so defer rather than keep a copy.
        if _launch_bridge(name):
            restarted.append(name)
            _alert(f"♻️ health-check auto-restarted **{name}** (was down). "
                   f"If this repeats, it's crash-looping — check logs/{name}.log.")
        else:
            _alert(f"⚠️ health-check: {name} is DOWN and could NOT be auto-restarted "
                   f"(no launch plan — see _bridge_launch_plan). Start it via startup.sh.")
    return restarted


def _bridge_launch_plan(name: str) -> "tuple[str, dict] | None":
    """Single owner of bridge-launch policy for every restart path (down and
    stale); side-effect-free so stale callers can confirm a relaunch pre-kill."""
    interp = _bridge_interpreter(name)
    if interp is None:
        # No interpreter can import the bridge's dependency (startup.sh skips too).
        return None
    child_env = os.environ.copy()
    if name == "gateway-bridge":
        # The bridge exits without a token, and startup.sh sources the same
        # channels/ag2space/.env before launching it.
        gw_env = _load_channel_env("ag2space")
        merged = {**os.environ, **gw_env}
        token = merged.get("REMOTE_TASK_TOKEN") or merged.get("AG2_REMOTE_TOKEN")
        if not token:
            return None
        child_env.update(gw_env)
        child_env["REMOTE_TASK_TOKEN"] = token
        # Marks the launch supervised so the bridge stamps launched_via and
        # skips its own bare-launch file log (see remote_gateway_bridge._log).
        child_env["SUTANDO_SUPERVISED"] = "1"
    if name == "slack-bridge":
        # slack-bridge exits without BOTH tokens non-empty; the Keychain vault is
        # a real source (same env -> .env -> vault tiering as the bridge itself).
        slack_env = _load_channel_env("slack")
        merged = {**os.environ, **slack_env}
        if not all(merged.get(v) or token_from_vault(v)
                   for v in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")):
            return None
        # Vault values stay out of the plan — the bridge resolves them itself.
        child_env.update(slack_env)
    return interp, child_env


# Check name → src/<script>.py, for the bridges whose two names differ.
_BRIDGE_SCRIPT = {"gateway-bridge": "remote-gateway-bridge"}


def _launch_bridge(name: str, plan: "tuple[str, dict] | None" = None) -> bool:
    """Spawn a bridge per _bridge_launch_plan; True if spawned."""
    plan = plan or _bridge_launch_plan(name)
    if plan is None:
        return False
    interp, child_env = plan
    log_path = WORKSPACE_DIR / "logs" / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # `with` closes the parent's handle after Popen; the child holds
    # its own dup of the fd, so the log stays writable.
    with open(str(log_path), "a") as log_f:
        subprocess.Popen([interp, str(REPO_DIR / "src" / f"{_BRIDGE_SCRIPT.get(name, name)}.py")],
                         stdout=log_f, stderr=subprocess.STDOUT,
                         env=child_env, start_new_session=True)
    return True


# Interpreter candidates, in the same priority order startup.sh probes. First
# candidate that can import the bridge's required module wins. Keep this list in
# sync with src/startup.sh's discord/slack blocks.
_BRIDGE_INTERP_CANDIDATES = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "python3"]

# Import each bridge needs to boot. telegram-bridge has no special hard import
# probe here (its startup.sh block probes TLS, not a module) — sys.executable is
# a safe default for it, matching the pre-#1898 behavior.
_BRIDGE_REQUIRED_IMPORT = {"discord-bridge": "discord", "slack-bridge": "slack_bolt"}


def _bridge_interpreter(name: str) -> "str | None":
    """Pick an interpreter that can launch `name`, mirroring startup.sh.

    For discord/slack, probe the candidate list for the bridge's required
    import (discord / slack_bolt) — the interpreter that launched health-check
    (sys.executable) often lacks these, so launching with it crash-loops. For
    bridges with no hard import gate (telegram), return sys.executable.
    Returns None when no candidate can import the required module (caller then
    skips the restart, matching startup.sh's labeled-skip behavior).

    The bare `python3` candidate is substituted via _resolved_bare_python3
    before probing — probing executes it, and bare python3 can be the CLT stub.
    """
    required = _BRIDGE_REQUIRED_IMPORT.get(name)
    if required is None:
        return sys.executable
    for cand in _BRIDGE_INTERP_CANDIDATES:
        if cand == "python3":
            resolved = _resolved_bare_python3()
            if resolved is None:
                continue
            cand = resolved
        try:
            if shutil.which(cand) is None and not Path(cand).exists():
                continue
            probe = subprocess.run([cand, "-c", f"import {required}"],
                                   capture_output=True, timeout=10)
            if probe.returncode == 0:
                return cand
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None


def _resolved_bare_python3() -> "str | None":
    """Stand-in for the bare `python3` candidate, mirroring startup.sh's
    resolve_python: never return the Xcode-CLT stub (executing it pops a modal)."""
    override = os.environ.get("SUTANDO_PY", "")
    if override and os.access(override, os.X_OK):
        return override
    bundled = REPO_DIR.parent / "runtime" / "python" / "bin" / "python3"
    if os.access(bundled, os.X_OK):
        return str(bundled)
    path_py = shutil.which("python3")
    if not path_py:
        return None
    if sys.platform != "darwin":
        return path_py
    # Directory compare, not full-path (versioned siblings share the inode).
    if os.path.realpath(os.path.dirname(path_py)) != os.path.realpath("/usr/bin"):
        return path_py
    return path_py if developer_tools_installed() else None


def _load_channel_env(channel: str) -> dict:
    """Parse channels/<channel>/.env into a dict (KEY=VALUE lines).

    Mirrors startup.sh's `set -a; . "$_SL_ENV"; set +a` so the bridge child
    gets SLACK_BOT_TOKEN / SLACK_APP_TOKEN. Returns {} if the file is absent or
    unreadable — the caller treats missing tokens as a reason to skip.
    """
    env: dict = {}
    try:
        env_file = Path(claude_home_path("channels", channel, ".env"))
        if not env_file.exists():
            return env
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            val = val.strip().strip('"').strip("'")
            if key:
                env[key] = val
    except OSError:
        pass
    return env


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def mark_stale_if_outdated(check: dict, src_file: Path, pgrep_pattern: str, threshold_sec: int = 1800,
                          binary_path: Optional[Path] = None, artifact_threshold_sec: int = 120) -> None:
    """Mark `check` as 'stale' in place if a process matching `pgrep_pattern`
    started more than `threshold_sec` before `src_file`'s mtime.

    Extracted so the same logic covers all tsx-managed services
    (voice-agent, web-client, conversation-server) without duplication.
    30 min default threshold tolerates `git checkout` mtime bumps; real
    stale deploys are hours/days old. Silent on any failure — stale
    detection is advisory, not authoritative.

    If `binary_path` is supplied (compiled artifacts like the Swift
    Sutando.app), the function ALSO checks whether the binary itself is
    older than the source. A stale binary means the running process —
    however recently relaunched — is executing old code. When this fires,
    the message tells the user to rebuild, not just restart. That branch
    applies the same `_file_unchanged_since` content cross-check as the
    process-start path, so a mtime bump from `git checkout` on unchanged
    content does not read as "rebuild needed".
    """
    if not src_file.exists():
        return
    # Compiled-artifact check: binary older than source → "rebuild needed",
    # regardless of process start. This catches the case where --fix
    # relaunches a stale binary repeatedly (#528 stopped the leak; this
    # makes the message actionable).
    if binary_path is not None and binary_path.exists():
        try:
            src_mtime = src_file.stat().st_mtime
            bin_mtime = binary_path.stat().st_mtime
            if src_mtime - bin_mtime > threshold_sec:
                # Same git cross-check the other two mtime comparisons carry
                # (PR #253 for the proc_start path below, #255 for the bridges
                # path). `git checkout` bumps mtime on files whose content is
                # byte-identical, so a branch switch alone made this branch
                # report "rebuild needed" for a binary that is in fact built
                # from exactly the source on disk.
                if _binary_is_current(binary_path, src_file):
                    return
                age_min = int((src_mtime - bin_mtime) / 60)
                check["status"] = "stale"
                check["detail"] = f"running, but binary is {age_min} min older than source — rebuild needed"
                return
        except OSError:
            pass
    try:
        pids = subprocess.run(
            ["/usr/bin/pgrep", "-f", pgrep_pattern],
            capture_output=True, text=True, timeout=5
        ).stdout.strip().split("\n")
        pids = [p for p in pids if p]
        if not pids:
            return
        # pgrep -f matches the same service launched from ANY clone on this
        # machine. Comparing our src mtime against a foreign clone's process
        # start produces a perpetual "stale — restart needed" whenever two
        # checkouts coexist (e.g. a staging clone alongside the live one).
        # Only processes that belong to THIS checkout are ours to judge.
        pids = _filter_pids_this_checkout(pids)
        if not pids:
            return
        ps_out = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", ",".join(pids)],
            capture_output=True, text=True, timeout=5
        ).stdout.strip().split("\n")
        from datetime import datetime as _dt
        starts = []
        for line in ps_out:
            line = line.strip()
            if line:
                try:
                    starts.append(_dt.strptime(line, "%a %b %d %H:%M:%S %Y").timestamp())
                except ValueError:
                    pass
        if not starts:
            return
        # Pick the OLDEST start time — the tsx wrapper spawns a child node
        # process; we want the parent's launch time, not the child's.
        proc_start = min(starts)
        # A compiled service executes the ARTIFACT, so src-vs-process cannot see
        # a deploy that refreshes the artifact without touching source.
        if binary_path is not None and binary_path.exists():
            try:
                bin_mtime = binary_path.stat().st_mtime
                # Smaller than the source threshold, which exists to absorb
                # `git checkout` mtime bumps -- those never touch an artifact.
                if bin_mtime - proc_start > artifact_threshold_sec:
                    age_min = int((bin_mtime - proc_start) / 60)
                    check["status"] = "stale"
                    check["detail"] = (
                        f"running, but the artifact it executes was rebuilt "
                        f"{age_min} min after the process started -- restart needed"
                    )
                    return
            except OSError:
                pass
        src_mtime = src_file.stat().st_mtime
        if src_mtime - proc_start > threshold_sec:
            # Before flagging stale, cross-check with git: mtime gets bumped by
            # `git checkout`/`pull`/`rebase` even when the file content is
            # identical, which produced a steady stream of false positives
            # whenever a branch switch left the working tree unchanged on a
            # specific file. Ask git for the last commit time that actually
            # touched this file. If that's older than proc_start AND there
            # are no uncommitted changes to the file, it's a mtime-only
            # bump — the running code is still current.
            if _file_unchanged_since(src_file, proc_start):
                return
            check["status"] = "stale"
            check["detail"] = f"running but code is {int((src_mtime - proc_start) / 60)} min newer than process — restart needed"
    except (subprocess.TimeoutExpired, OSError):
        pass


def _filter_pids_this_checkout(pids: list) -> list:
    """Keep only PIDs whose process belongs to THIS checkout: REPO_DIR appears
    in the argv (path-boundary match, so a sibling clone whose path is a
    prefix/suffix doesn't match), or the process cwd is inside REPO_DIR
    (covers relative-path launches like `npm exec tsx src/voice-agent.ts`).
    Fail-open: a PID whose argv AND cwd both can't be determined is kept, so
    a probe failure can't hide a real stale deploy.

    Scope note (review #1650): fail-open covers only the both-probes-failed
    case. A PID with a readable argv that matches neither repo form, and no
    matching cwd, is DROPPED — i.e. the guarantee is "keep ours + keep
    undeterminable", not "keep everything that isn't provably foreign". Fine
    while services launch with explicit paths or repo-cwd; revisit if a
    launcher ever rewrites argv and cwd both.
    """
    repo_forms = {str(REPO_DIR), str(REPO_DIR.resolve())}  # /tmp vs /private/tmp etc.
    kept = []
    for pid in pids:
        try:
            argv = subprocess.run(
                ["/bin/ps", "-o", "command=", "-p", pid],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            argv = ""
        if any(f"{repo}/" in argv for repo in repo_forms):
            kept.append(pid)
            continue
        try:
            lsof_out = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=5
            ).stdout
            cwd = next((ln[1:] for ln in lsof_out.splitlines() if ln.startswith("n")), "")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            cwd = ""
        if cwd:
            if any(cwd == repo or cwd.startswith(f"{repo}/") for repo in repo_forms):
                kept.append(pid)
        elif not argv:
            kept.append(pid)  # neither probe answered — fail open
    return kept


def sutando_app_newest_source(app_dir: Optional[Path] = None) -> Path:
    """Newest .swift under the app dir — the binary is stale if it predates any of them.

    Globbed, not enumerated: the app is built from several sources now, and a
    hardcoded list silently stops noticing the ones added after it was written.
    """
    d = app_dir if app_dir is not None else REPO_DIR / "src" / "Sutando"
    main = d / "main.swift"
    try:
        srcs = [p for p in d.glob("*.swift") if p.is_file()]
    except OSError:
        return main
    if not srcs:
        return main
    try:
        return max(srcs, key=lambda p: p.stat().st_mtime)
    except OSError:
        return main


def _binary_is_current(binary_path: Path, src_file: Path) -> bool:
    """True if `binary_path` was built from the content now in `src_file`.

    mtime ordering alone is not enough. `git checkout`, `pull`, and `rebase`
    restamp files whose content is byte-identical, so a branch switch can make
    a perfectly current binary look stale. Accept two ways: the binary is at
    least as new as the source, or the source's mtime moved but the content
    cross-check proves the bump was idempotent.

    Fails safe (False) on any stat error, matching `_file_unchanged_since`:
    an unresolvable check must never assert a stale binary is current.
    """
    try:
        bin_mtime = binary_path.stat().st_mtime
        if bin_mtime >= src_file.stat().st_mtime:
            return True
    except OSError:
        return False
    return _file_unchanged_since(src_file, bin_mtime)


def _file_unchanged_since(src_file: Path, proc_start: float) -> bool:
    """Return True if git's last-commit-time for src_file predates proc_start
    AND the file has no uncommitted changes. Used to suppress stale-detection
    false positives from git operations that bump mtime without changing
    content. Silent-failure: returns False on any git error so real stale
    deploys aren't hidden.
    """
    try:
        # git_argv raises GitUnavailable (an OSError) when this host has no
        # runnable git — caught below and treated as "can't tell", exactly like
        # any other git error. Never invoke /usr/bin/git directly: on a Mac
        # without developer tools that is the CLT shim and it raises a modal
        # install dialog, which a health check must never be able to do.
        log = subprocess.run(
            git_argv("log", "-1", "--format=%ct", "HEAD", "--", str(src_file)),
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5
        )
        if log.returncode != 0 or not log.stdout.strip():
            return False
        commit_time = int(log.stdout.strip())
        if commit_time >= proc_start:
            # Real commit landed after proc_start — genuinely stale
            return False
        # No commits since proc_start; check for uncommitted edits
        diff = subprocess.run(
            git_argv("diff", "--quiet", "HEAD", "--", str(src_file)),
            cwd=REPO_DIR, capture_output=True, timeout=5
        )
        return diff.returncode == 0  # 0 = no diff
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False


# Watchers task-bridge starts at voice-agent boot. If any of these is
# missing from the log after the most recent "Sutando — Voice Interface"
# banner, the watcher wasn't registered and the corresponding feature
# (context drop, note view, task results) is silently broken. This
# check was added after a 9-hour incident on 2026-04-09 where the
# note-view watcher was silently absent and nobody noticed until a
# user reported voice hallucinating note titles.
REQUIRED_VOICE_WATCHERS = [
    "Watching for context drops",
    "Watching for note views",
    "Watching for results",
]


def _voice_log_path() -> Path:
    """Resolve where voice-agent's stdout/stderr lands.

    Two paths exist for legitimate reasons:
    - launchd plist (~/Library/LaunchAgents/com.sutando.voice-agent.plist)
      pipes StandardOut/ErrorPath to `~/Library/Application Support/Sutando/
      logs/voice-agent.log`. This is the path **generated by Sutando.app's
      installer** — not a fixed assumption. Hosts that predate Sutando.app
      (or have a hand-written plist) may instead point at
      `<workspace>/logs/voice-agent.log`, in which case the resolver falls
      through to the workspace path below and behavior is unchanged.
    - `src/startup.sh:153` writes to `<workspace>/logs/voice-agent.log`
      when the user starts voice-agent manually (dev mode).

    Prefer the launchd path when it has content. Falls back to the
    workspace path so manually-launched voice-agents still resolve.
    Without this resolver, `voice-watchers` and `voice-transport` would
    permanently warn "voice-agent.log not found" on Sutando.app installs.
    """
    launchd_log = Path.home() / "Library/Application Support/Sutando/logs/voice-agent.log"
    workspace_log = WORKSPACE_DIR / "logs" / "voice-agent.log"
    if launchd_log.exists() and launchd_log.stat().st_size > 0:
        return launchd_log
    return workspace_log


def _voice_dep_detail(voice_check: dict) -> str:
    """Status word plus the dependency's own detail. A bare status reads
    identically at minute one and hour twelve, so it cannot signal urgency."""
    vs = voice_check.get("status")
    if not vs:
        return "voice-agent status unknown"
    dep = (voice_check.get("detail") or "").strip()
    return f"voice-agent {vs}" + (f": {dep}" if dep else "")


def check_voice_watchers(voice_check: dict) -> dict:
    """Verify all 3 task-bridge watchers are registered in the current
    voice-agent process. Parses logs/voice-agent.log for the most recent
    boot banner and confirms each REQUIRED_VOICE_WATCHERS pattern
    appears after it.
    """
    check = {"name": "voice-watchers", "status": "ok", "detail": "all 3 watchers active"}
    # Only run if voice-agent itself is ok; otherwise the check is moot.
    # Distinguish "stale" (process running, old code) from absent.
    vs = voice_check.get("status")
    if vs != "ok":
        check["status"] = "warn"
        check["detail"] = _voice_dep_detail(voice_check)
        return check
    log_file = _voice_log_path()
    if not log_file.exists():
        check["status"] = "warn"
        check["detail"] = "voice-agent.log not found"
        return check
    try:
        lines = log_file.read_text(errors="replace").splitlines()
        # Find the most recent startup banner
        banner_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if "Sutando — Voice Interface" in lines[i]:
                banner_idx = i
                break
        if banner_idx < 0:
            check["status"] = "warn"
            check["detail"] = "no startup banner found in log"
            return check
        tail = lines[banner_idx:]
        # task-bridge logs watchers BEFORE the banner prints — check a
        # bounded window both sides to be safe (20 lines before banner)
        window_start = max(0, banner_idx - 20)
        window = lines[window_start:]
        missing = []
        for pat in REQUIRED_VOICE_WATCHERS:
            if not any(pat in line for line in window):
                missing.append(pat.replace("Watching for ", ""))
        if missing:
            check["status"] = "fail"
            check["detail"] = f"missing watcher(s): {', '.join(missing)} — restart voice-agent"
    except OSError as e:
        check["status"] = "warn"
        check["detail"] = f"log read failed: {e}"
    return check


# Close codes that indicate a healthy voice-agent → Gemini Live transport
# state. Anything else after a startup banner suggests upstream failure
# (quota, auth, network blip, bodhi state-machine wedge).
#   1000 = normal closure
#   4000 = sutando custom goodbye disconnect (bodhi fork commit 44172b8)
VOICE_TRANSPORT_HEALTHY_CLOSE_CODES = {"1000", "4000"}


def _extract_close_code(line: str) -> Optional[str]:
    import re
    m = re.search(r"code=(\d+)", line)
    return m.group(1) if m else None


def _extract_close_reason(line: str) -> Optional[str]:
    import re
    m = re.search(r'reason="([^"]*)"', line)
    return m.group(1) if m else None


def check_voice_transport(voice_check: dict) -> dict:
    """Scan voice-agent.log from the most recent startup banner forward
    for abnormal Gemini transport closes. Flags things like:
        code=1011 "exceeded your current quota"    (the 3.1 tier issue)
        code=1007 "Request contains an invalid argument" (CLOSED→CLOSED)
        code=1006 abnormal / network drop
    Returns ok if the latest transport event since the most recent boot
    is "setup complete", or if an abnormal close was followed by a
    successful "setup complete" (auto-recovery worked).

    Added 2026-04-09 after the Gemini 3.1 dry-run produced a 1011 that
    health-check couldn't see — voice-agent port was up, bodhi was up,
    every existing probe said ok, but the transport was rejected
    server-side. Without this check, that failure mode is only visible
    to whoever manually tails the log.
    """
    check = {"name": "voice-transport", "status": "ok", "detail": "no recent transport errors"}
    vs = voice_check.get("status")
    if vs != "ok":
        check["status"] = "warn"
        check["detail"] = _voice_dep_detail(voice_check)
        return check
    log_file = _voice_log_path()
    if not log_file.exists():
        check["status"] = "warn"
        check["detail"] = "voice-agent.log not found"
        return check
    try:
        lines = log_file.read_text(errors="replace").splitlines()
        banner_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if "Sutando — Voice Interface" in lines[i]:
                banner_idx = i
                break
        if banner_idx < 0:
            check["status"] = "warn"
            check["detail"] = "no startup banner found in log"
            return check
        # Walk from the banner forward. Track the most recent transport
        # event and a few state flags so we can distinguish real failures
        # from expected idle-timeout closes.
        #
        # Expected idle path: Gemini Live fires a `GoAway` (60s warning),
        # then ~60s later closes the transport with code=1011
        # "The service is currently unavailable." Bodhi transitions the
        # session to CLOSED waiting for the next client connect. That's
        # a normal lifecycle event, not a failure — the session
        # reconnects fresh when a client comes back. If we flag every
        # 1011-after-GoAway as a fail, the probe reports a false
        # positive every time voice sits idle for 10+ minutes.
        most_recent_abnormal: Optional[str] = None
        most_recent_abnormal_lineno: int = -1  # relative to banner_idx
        abnormal_recovered = False
        goaway_before_close = False  # GoAway seen since the last setup/close
        for rel_i, line in enumerate(lines[banner_idx:]):
            if "Gemini setup complete" in line or "LLM transport connected and setup complete" in line:
                if most_recent_abnormal is not None:
                    abnormal_recovered = True
                    most_recent_abnormal = None
                    most_recent_abnormal_lineno = -1
                goaway_before_close = False
            elif "GoAway from Gemini" in line:
                goaway_before_close = True
            elif "[VoiceSession] Transport closed" in line:
                m_code = _extract_close_code(line)
                if m_code is None:
                    continue
                if m_code in VOICE_TRANSPORT_HEALTHY_CLOSE_CODES:
                    most_recent_abnormal = None
                    most_recent_abnormal_lineno = -1
                    goaway_before_close = False
                elif goaway_before_close:
                    # Idle timeout path — Google warned, then closed. Not an error.
                    most_recent_abnormal = None
                    most_recent_abnormal_lineno = -1
                    goaway_before_close = False
                else:
                    most_recent_abnormal = line
                    most_recent_abnormal_lineno = rel_i
                    abnormal_recovered = False
        if most_recent_abnormal is not None:
            reason = _extract_close_reason(most_recent_abnormal) or "unknown"
            code = _extract_close_code(most_recent_abnormal) or "?"
            # Count [Health] state=CONNECTING lines after the abnormal close.
            # The health ticker fires every ~30s; >20 consecutive CONNECTING
            # lines = stuck for >10 min and bodhi won't self-recover.
            connecting_after = sum(
                1 for ln in lines[banner_idx + most_recent_abnormal_lineno + 1:]
                if "[Health] state=CONNECTING" in ln
            ) if most_recent_abnormal_lineno >= 0 else 0
            if connecting_after > 20:
                elapsed_min = connecting_after * 30 // 60
                check["status"] = "fail"
                check["detail"] = f"stuck CONNECTING ~{elapsed_min}min after code={code} transport close — needs kickstart"
                check["_stuck_connecting"] = True
            elif code == "1006":
                # code=1006 is an abnormal network close (often a DNS blip). If DNS
                # resolves now the transport will self-recover on next client connect
                # — downgrade to warn so the dashboard isn't stuck on red.
                try:
                    socket.getaddrinfo("generativelanguage.googleapis.com", 443)
                    check["status"] = "warn"
                    check["detail"] = "transient network drop (code=1006, DNS ok now — will recover on next connect)"
                except OSError:
                    check["status"] = "fail"
                    check["detail"] = "network drop code=1006 and DNS still failing"
            else:
                check["status"] = "fail"
                check["detail"] = f"unrecovered transport close: code={code} reason={reason[:80]}"
        elif abnormal_recovered:
            check["detail"] = "transport recovered after earlier error"
    except OSError as e:
        check["status"] = "warn"
        check["detail"] = f"log read failed: {e}"
    return check


# Quota headers land on every proxied upstream response, so a core that is
# working routes several per loop pass. Six hours is far outside any plausible
# gap between passes on a host that is actually wired, and matches the "down"
# tier the comm-sweep freshness probe already uses.
QUOTA_STATE_STALE_SEC = 6 * 60 * 60
# core-status.json is rewritten at the start and end of every loop pass, so a
# half-hour-old one means a pass is either in flight or just finished.
AGENT_ACTIVE_SEC = 30 * 60


# Runtimes whose request path is NOT expected to traverse the Anthropic credential
# proxy. A Codex pass refreshes the very same `core-status.json` this check reads for
# activity, but produces no Anthropic quota headers — so "agent working + stale quota"
# is a perfectly healthy shape there, and warning on it would train operators to ignore
# the check this whole probe exists to make actionable (qingyun, #2445).
NON_PROXY_RUNTIMES = {"codex"}

# The two launch records are stamped by SEPARATE `date +%s` calls in the same
# launcher — `src/agent/codex/cli/start-cli.sh:240` writes core-runtime.json and
# :243 appends session-starts.log — so one launch can legitimately produce
# started_at=N and session_started_at=N+1 if the second rolls between them. A
# strict `<` then reads a CURRENT marker as stale and emits the exact false proxy
# warning this check exists to suppress (qingyun, #2446).
#
# A few seconds of slack cannot mask a real previous-core marker: that marker is
# separated from the next launch by the entire lifetime of the core that wrote it,
# which is minutes at the very least. So the margin is generous on purpose — it
# costs nothing on the true-positive side and closes the whole race, rather than
# assuming the gap is exactly one second.
LAUNCH_RECORD_SKEW_SEC = 5


def _runtime_may_skip_proxy() -> bool:
    """True when this core's runtime is not expected to produce Anthropic quota headers.

    Read from `state/core-runtime.json`. Today the **Codex launcher is its only writer**
    (`src/agent/codex/cli/start-cli.sh` writes it unconditionally once the workspace
    resolves), so its ABSENCE positively excludes Codex rather than merely being
    unknown — which is why absence is treated as proxy-routed instead of silencing the
    check everywhere. An unreadable or malformed file cannot rule Codex out, so it is
    treated as non-proxy and stays silent: a corrupted status file must not manufacture
    a health warning.

    A marker left by a PREVIOUS core is ignored. Today only the Codex launcher writes
    this file and nothing resets it, so after a Codex -> Claude switch a stale
    `{"runtime": "codex"}` sits there describing a core that is no longer running
    (#2406 documents exactly that happening live on 2026-07-30). Trusting it would
    silence this check on a host that IS proxy-routed — the mirror of the false
    positive this function was added to fix. So a marker whose `started_at` predates
    the running core's own start is treated as absent, i.e. proxy-routed.

    When #2406 lands a Claude-side writer, tighten this to positive identification of
    a proxy-routed runtime instead of inferring it from absence.
    """
    path = status_read_path("core-runtime.json", WORKSPACE_DIR)
    try:
        if not path.exists():
            return False          # no Codex launcher ever ran here -> proxy-routed
        marker = json.loads(path.read_text())
    except (OSError, ValueError):
        return True               # cannot rule Codex out -> stay silent
    # Valid JSON is not necessarily an OBJECT. `null`, `[]`, `"codex"` and `3` all
    # decode fine and then raise AttributeError on `.get`, which the handler above
    # does not catch — so a junk state file would crash the entire health run inside
    # the very branch this check hardens (qingyun, #2446). A non-object marker is
    # exactly as uninformative as malformed JSON, so it takes the same silent path.
    if not isinstance(marker, dict):
        return True               # cannot rule Codex out -> stay silent
    runtime = marker.get("runtime")
    # The FIELD has a schema too, not just the container. `{"runtime": []}` and
    # `{"runtime": {}}` reach the membership test and raise TypeError (unhashable);
    # `{"runtime": 3}`, `{"runtime": true}` and a marker with no `runtime` key at all
    # don't crash but fall through to "proxy-routed" and manufacture the very warning
    # this check exists to suppress. A field that isn't a string tells us nothing about
    # the runtime, so it takes the same fail-silent path as malformed JSON
    # (qingyun, #2446 — the same shape one level in from the container guard above).
    if not isinstance(runtime, str):
        return True               # cannot rule Codex out -> stay silent
    if _marker_predates_running_core(marker):
        return False              # belongs to a previous core -> no information
    return runtime in NON_PROXY_RUNTIMES


def _local_host_labels() -> "set[str]":
    r"""Every label a launcher on THIS host could plausibly have persisted.

    The reader and the writers do NOT share a host-label contract, and comparing
    against only one of them discards this host's real launch records:

      reader  `util_paths._host_label()`  -> SUTANDO_HOST_LABEL > scutil
                                             LocalHostName > short hostname
      writers `hostname | sed 's/\..*//'` -> short hostname, always
              (src/agent/codex/cli/start-cli.sh:242,
               src/agent/claude/cli/start-cli.sh:609)

    On macOS those routinely differ — LocalHostName is the stable Bonjour name
    while `hostname` follows DHCP — and with SUTANDO_HOST_LABEL set they differ by
    construction. When they do, EVERY local line is skipped as foreign, the
    boundary becomes None, and a stale `runtime:codex` marker stays trusted
    forever: the stale-quota warning goes permanently silent. That is strictly
    worse than the cross-host false positive the host filter was added to fix
    (qingyun-wu + john-the-dev, #2446, independently reproduced).

    This host is not hypothetical evidence: its own vault carries BOTH
    `host/Chis-MacBook-Pro/…` and `host/Chis-MBP/…` branches, so the short name
    has already drifted here at least once.

    Accepting the union is deliberately the conservative direction. A foreign host
    would have to share one of these exact labels to be mistaken for local, which
    is the pre-existing collision risk; discarding local records, by contrast,
    silences a real alert on every affected host.
    """
    labels = {_host_label()}
    try:
        labels.add(socket.gethostname().split(".")[0])
    except Exception:  # pragma: no cover — gethostname failing is not a reason to go blind
        pass
    return {x for x in labels if x}


def _last_core_launch_at(workspace_dir: Optional[Path] = None) -> "tuple[float, str | None] | None":
    """When the CURRENT core was launched, from `state/session-starts.log`.

    Returns `(timestamp, runtime)`, where `runtime` is the identity the launcher
    recorded for that launch — `"codex"` from the Codex launcher, and None from the
    Claude launcher, which writes no such field
    (`src/agent/claude/cli/start-cli.sh:608`). The identity is returned rather than
    discarded because the skew tolerance below is only safe when the launch record
    and the marker positively agree on the runtime; see
    `_marker_predates_running_core`.

    Both launchers append one line per launch — `src/agent/claude/cli/start-cli.sh:610`
    and `src/agent/codex/cli/start-cli.sh:243` — and both stamp the `host` that launched.

    Only THIS host's records are eligible. `session-starts.log` lives in a workspace that
    is synced across hosts, so the newest line globally is not this host's boundary: a
    later launch on host B would otherwise become host A's boundary and age host A's
    perfectly current marker into a false `warn` — the exact false-positive class this
    check exists to suppress (qingyun-wu + john-the-dev, #2446, independently reproduced:
    local Codex marker at now-60 alone => ok; add a foreign Codex launch at now => warn).

    Legacy policy, stated explicitly: a record whose `host` is absent, non-string, or
    belongs to another host is SKIPPED, never treated as local. Pre-`host` lines cannot
    be attributed, and guessing "probably local" reintroduces the same poisoning from
    older synced logs. Skipping them can leave no boundary at all, which yields None —
    and None already means "no evidence", never "stale", so the failure direction is
    silence rather than a false alarm.

    The heartbeat was the obvious candidate and is WRONG: `core_heartbeat.py` stamps
    `_STARTED_AT` once at module load and both launch paths RETAIN an existing heartbeat
    process, so `.alive.started_at` is the heartbeat process's age, not the session's.
    After a Codex → Claude switch it can be far older than a freshly-written marker,
    which made the staleness comparison silently useless (john-the-dev, #2446).

    Every hop is validated rather than the one that last broke: unreadable file, a line
    that isn't JSON, a decoded value that isn't an object, a missing key, a non-numeric
    value. Anything uninformative yields None, and None means "no evidence", never
    "stale".
    """
    try:
        raw = (status_read_path("session-starts.log", workspace_dir or WORKSPACE_DIR)).read_text()
    except OSError:
        return None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue                      # a torn/partial line is not the boundary
        if not isinstance(entry, dict):
            continue
        entry_host = entry.get("host")
        if not isinstance(entry_host, str) or entry_host not in _local_host_labels():
            continue                      # another host's launch, or unattributable
        try:
            ts = float(entry.get("session_started_at"))
        except (TypeError, ValueError):
            continue                      # keep looking further back
        launch_runtime = entry.get("runtime")
        if not isinstance(launch_runtime, str):
            launch_runtime = None         # absent or wrong type -> no identity claim
        return ts, launch_runtime
    return None


def _marker_predates_running_core(marker: dict) -> bool:
    """True when `core-runtime.json` describes a core older than the one running now.

    Compared against the newest `session-starts.log` entry — a real per-launch boundary
    — not the heartbeat. Both timestamps must be present and comparable; if either is
    missing the marker is taken at face value, because "no evidence of staleness" is not
    evidence of staleness.

    The skew tolerance is IDENTITY-AWARE, and only sound that way. It exists solely
    because the Codex launcher stamps the marker and the session line with two separate
    `date +%s` calls (`src/agent/codex/cli/start-cli.sh:240` and `:242`), so one real
    launch can record `started_at=N` and `session_started_at=N+1`. That case always
    carries `"runtime":"codex"` on BOTH records. Granting the same margin when the
    runtimes do not positively agree would swallow the fast Codex -> Claude switch: a
    stale Codex marker at N against a Claude launch at N+1 is a genuinely different core,
    and Claude is proxy-routed, so silencing it there hides stale quota telemetry
    indefinitely on the one runtime that needs the warning (qingyun-wu + john-the-dev,
    independently reproduced on f887b2a7).

    So the margin applies only on a positive runtime match; otherwise the comparison is
    strict. A Claude launch record carries no `runtime` field at all, which is exactly
    the ambiguity that must NOT earn the tolerance.
    """
    try:
        started = float(marker.get("started_at"))
    except (TypeError, ValueError):
        return False
    launch = _last_core_launch_at()
    if launch is None:
        return False
    launched, launch_runtime = launch
    same_runtime = (
        launch_runtime is not None and launch_runtime == marker.get("runtime")
    )
    margin = LAUNCH_RECORD_SKEW_SEC if same_runtime else 0
    return started < launched - margin


def _local_core_socket(workspace: Optional[Path] = None) -> Optional[str]:
    """This HOST's core tmux socket, or None if this host has no live heartbeat.

    Deliberately NOT `_live_core_socket()`. That resolver globs every synced
    `state/cores/*.alive` and takes the freshest, which the workspace contract
    permits to be ANOTHER MACHINE's — the vault carries one heartbeat per host.
    A reviewer reproduced it with two fresh records: the local one at mtime N-1
    and a peer at N, and the probe targeted `/tmp/peer-core.sock`. That socket
    does not exist locally, so the tri-state correctly degraded to None — which
    silently suppresses the very warning this check exists to raise. Correct
    behaviour, wrong target, false green.

    Host matching uses `_local_host_labels()` rather than one label, because the
    reader and the launchers do not share a host-label contract (see that
    function). Returning None when this host has no fresh heartbeat is right:
    "no local core is running" is not evidence that a core bypasses the proxy.
    """
    if workspace is None:
        workspace = WORKSPACE_DIR
    cores_dir = workspace / "state" / "cores"
    if not cores_dir.is_dir():
        return None
    labels = _local_host_labels()
    now = time.time()
    best_mtime, best_socket = None, None
    for alive_file in cores_dir.glob("*.alive"):
        if alive_file.stem not in labels:
            continue                      # another machine's heartbeat
        try:
            mtime = alive_file.stat().st_mtime
            if not heartbeat_is_fresh(mtime, now):
                continue                  # stale or future-dated — not a live core
            payload = json.loads(alive_file.read_text())
            if not isinstance(payload, dict):
                continue
            sock = payload.get("socket")
        except (OSError, ValueError):
            continue
        if isinstance(sock, str) and sock and (best_mtime is None or mtime > best_mtime):
            best_mtime, best_socket = mtime, sock
    return best_socket


def core_env_has_proxy_url(
    socket_path: Optional[str] = None,
    session: str = "sutando-core",
    tmux_runner=None,
    ps_runner=None,
) -> Optional[bool]:
    """Whether the RUNNING core process carries ANTHROPIC_BASE_URL in its environment.

    ``True``  -> the core is routed through the credential proxy.
    ``False`` -> it demonstrably is NOT (its environment was read, and the var is absent).
    ``None``  -> could not be determined. **Never collapse None into False**: an
                 unreadable environment is not evidence of a bypass, and a warning
                 manufactured from it would be the same defect this check is fixing,
                 pointed the other way.

    Why this exists: ``quota-state.json`` is written by the credential proxy, not by
    the core, so a FRESH file only proves *something* routed through the proxy — not
    that the core did. Measured on 2026-08-02: one throwaway ``claude -p`` run with
    the variable set refreshed the file and flipped this check from a truthful
    ``warn`` (18h stale) to ``ok`` for the whole 6h staleness window, while the
    production core was exactly as unrouted as before. Freshness is a property of the
    artifact; routing is a property of the process.

    The pid comes from tmux rather than ``pgrep``: ``pgrep -f claude`` matches any argv
    containing the string — including the shell running this check — and macOS
    ``pgrep -a`` lists ancestors, not argv.

    But a pane pid is only useful if it is the CORE's pane. The first version of this
    helper targeted the session (``list-panes -t =sutando-core``), which tmux resolves
    to that session's **current window** — and this repo deliberately keeps sibling
    windows (gateway, monitor) in the same session, healing window-scoped precisely so
    they survive (`src/agent/claude/cli/start-cli.sh`). A reviewer built the case and it
    reproduced: with the ``gateway`` window active, the production command returned the
    gateway's pid, so the helper would report on a sibling's environment — false green or
    false warning, independent of the real core. Window NAME is no discriminator either;
    on this host the core's window is auto-named ``2.1.220`` after the claude version.

    So enumerate EVERY pane in the session (``list-panes -s``) and identify the core by
    what it actually is: the process whose argv carries ``--name <session>``, which is
    exactly how `start-cli.sh` launches it. Zero matches or more than one -> ``None``;
    an ambiguous session is not evidence of a bypass.

    Both subprocess calls are injectable so the contract is testable without a live
    core; production passes neither.
    """
    tmux_runner = tmux_runner or (lambda sock, *a: _run_tmux(sock, *a))
    if ps_runner is None:
        def ps_runner(pid):
            return subprocess.run(
                ["ps", "eww", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=15,
            )
    sock = socket_path or _local_core_socket()
    if not sock:
        return None                       # no live LOCAL core -> unknown, not a bypass
    # `-s` = every pane in the SESSION, not just the current window's.
    panes = tmux_runner(sock, "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_pid}")
    if panes is None or getattr(panes, "returncode", 1) != 0:
        return None                       # no such session / tmux unavailable
    pids = [p for p in (panes.stdout or "").split() if p.isdigit()]
    if not pids:
        return None
    # Identify the core by argv, not by position: `--name <session>` is what
    # start-cli.sh passes and no sibling window carries it.
    #
    # TOKEN equality, never substring. `f"--name {session}" in argv` also matches
    # `--name sutando-core-watcher`, so a prefix-named sibling in the same session
    # was accepted as the core (john-the-dev, reproduced on a sole pane: returned
    # True where the contract is None). This is the SAME lookalike class as the
    # `ANTHROPIC_BASE_URL_OLD` control already in the suite — I guarded the env-var
    # axis and then introduced the identical hole on the session-name axis.
    def _names_this_session(argv: str) -> bool:
        toks = argv.split()
        for i, t in enumerate(toks):
            if t == "--name" and i + 1 < len(toks) and toks[i + 1] == session:
                return True
            if t == f"--name={session}":  # the =-joined spelling
                return True
        return False

    matches = []
    for pid in pids:
        try:
            proc = ps_runner(pid)
        except Exception:                 # noqa: BLE001 — a probe failure is "unknown"
            return None
        if proc is None or getattr(proc, "returncode", 1) != 0:
            continue                      # this pane vanished; keep looking
        out = proc.stdout or ""
        if _names_this_session(out):
            matches.append(out)
    # Zero matches: the core is not in this session (or ps could not read any pane).
    # More than one: ambiguous, and an ambiguous session is not evidence of a bypass.
    if len(matches) != 1:
        return None
    tokens = matches[0].split()
    # `ps eww` prints argv alone when the env is unreadable, so "no KEY=VALUE pair"
    # is what keeps an unreadable env reporting None rather than an empty env.
    env_pairs = [t for t in tokens if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
    if not env_pairs:
        return None
    return any(t.startswith("ANTHROPIC_BASE_URL=") for t in env_pairs)


def _agent_activity_age() -> "float | None":
    """Seconds since the agent last recorded loop activity, or None if unknown.

    None is the honest answer for "no evidence either way" and must stay
    distinct from a large number: absent core-status.json means this host does
    not tell us whether the agent is working, and a check that cannot rule out
    the quiet-core explanation must not warn.

    Only the MTIME is read, never the `status` field. A pass that ends by
    writing `{"status": "idle"}` still ran — the write itself is the evidence,
    and reading the value would make a pass count only while mid-flight.
    """
    try:
        p = status_read_path("core-status.json", WORKSPACE_DIR)
        if not p.exists():
            return None
        return time.time() - p.stat().st_mtime
    except OSError:
        return None


def check_quota_telemetry(proxy_status: str, core_env_prober=None) -> dict:
    """Warn when the credential proxy is up but producing no quota state.

    quota-state.json is written by the proxy from the quota headers on
    upstream responses, so it only appears if a core actually ROUTES through
    the proxy. Only the core launcher (`src/agent/claude/cli/start-cli.sh`)
    exports ANTHROPIC_BASE_URL=http://localhost:7846, and only when the proxy
    port already has a listener at launch — so a core started outside the
    launcher, or started before it bound, stays unrouted and every other check
    still reads green.

    The existing credential-proxy check can't catch this: it is a plain
    TCP-listening probe (correctly so — a forwarding proxy has no liveness
    endpoint), so "listening" is all it can ever assert.

    Deliberately narrow to stay quiet in the legitimate cases:
      - proxy not up          -> silent. Not every host routes through it,
                                 and its own check already says so.
      - file present          -> ok, with its age. A bare age threshold is
                                 still refused: a quiet core legitimately
                                 writes nothing for a long time, and firing on
                                 healthy idle hosts is how a check gets muted.

    ...but PRESENCE ALONE IS SATISFIED BY A HISTORICAL WRITE. This check exists
    to catch "the proxy is up and nothing routes through it", and it detects
    only the *never-wired* case. A host that WAS wired, wrote quota-state.json
    once, and then lost the wiring keeps a file on disk forever and reads green
    forever — the exact condition this check was written to make loud, in the
    one shape it cannot see. Observed on this fleet: quota-state.json 323h old,
    credential-proxy `ok`, quota-telemetry `ok`, and the proactive loop's
    per-pass budget gate quoting 13-day-old percentages as current.

    Staleness only becomes evidence when the "quiet core" explanation is ruled
    OUT, so the stale branch is gated on the agent being demonstrably at work:
    core-status.json is rewritten by the agent on every loop pass, and each of
    those writes belongs to a turn that spent tokens. Fresh core-status plus
    stale quota-state means requests are flowing while quota headers are not
    landing — wiring, not quiet. An idle host has a stale core-status too, so
    it stays silent, and a host that never wired at all still takes the
    absent-file branch below.
    """
    # `core_env_prober` is an injectable seam, defaulting to the real probe. It exists
    # because the first version of this branch called `core_env_has_proxy_url()`
    # unconditionally: the existing 39-case suite overrides WORKSPACE_DIR but had no way
    # to override THAT, so its fixtures escaped into the developer's live tmux, observed
    # the real (unrouted) core, and 3 of 39 flipped to `warn`. A required suite whose
    # result depends on ambient host state is worse than the bug it guards -- and CI,
    # having no live core, would have stayed green while developer hosts failed.
    check = {"name": "quota-telemetry", "status": "ok"}
    # "stale" means listening but executing pre-deploy code -- still routing,
    # so gating it out here would mute this check exactly during a redeploy.
    if proxy_status not in ("ok", "stale"):
        check["detail"] = "credential proxy not running — quota telemetry not expected"
        return check
    path = status_read_path("quota-state.json", WORKSPACE_DIR)
    if path.exists():
        try:
            quota_age = time.time() - path.stat().st_mtime
            age_min = int(quota_age / 60)
            check["detail"] = f"quota state present (updated {age_min}m ago)"
            # A FRESH file is not proof the CORE routed — the proxy writes this file
            # for whatever talks to it. Only a demonstrated absence downgrades; None
            # (env unreadable) leaves the fresh reading alone.
            probe = core_env_prober or core_env_has_proxy_url
            if quota_age <= QUOTA_STATE_STALE_SEC and not _runtime_may_skip_proxy() \
                    and probe() is False:
                check["status"] = "warn"
                check["detail"] = (
                    f"quota state is fresh ({age_min}m) but the RUNNING core has no "
                    "ANTHROPIC_BASE_URL in its environment, so the core is not routed "
                    "through the proxy — something else produced that file (a one-off "
                    "`claude -p`, another core, a manual probe). Quota-based budgeting "
                    "is reading numbers this core did not generate. Relaunch the core "
                    "via src/agent/start-cli.sh with the proxy listening on 7846."
                )
                return check
        except OSError:
            # Degrade to the less precise detail rather than raising — and with
            # no age there is nothing to call stale, so never warn from here.
            check["detail"] = "quota state present"
            return check
        agent_age = _agent_activity_age()
        skipped_for_runtime = _runtime_may_skip_proxy()
        if quota_age > QUOTA_STATE_STALE_SEC and agent_age is not None \
                and agent_age < AGENT_ACTIVE_SEC and skipped_for_runtime \
                and _last_core_launch_at() is None:
            # Silenced by a runtime marker we could NOT date. `session-starts.log`
            # only exists on checkouts carrying the launcher write-sites (first
            # landed 17d094f4, 2026-07-13), and a pinned older node has no such
            # file — a live counter-example on this fleet, not a hypothesis. The
            # conservative reading is kept, because refusing to trust the marker
            # would reinstate the false warn on every healthy pre-Jul-13 Codex
            # host, which is the defect this check was opened to remove. But the
            # no-op is stated rather than silent: an unqualified `ok` here would
            # be indistinguishable from a check that actually verified something.
            check["detail"] += (
                " — runtime marker present but UNVERIFIABLE on this checkout "
                "(no state/session-starts.log), so a stale marker cannot be "
                "detected; staleness check inactive here"
            )
        elif quota_age > QUOTA_STATE_STALE_SEC and agent_age is not None \
                and agent_age < AGENT_ACTIVE_SEC and not skipped_for_runtime:
            check["status"] = "warn"
            check["detail"] = (
                f"quota state is {int(quota_age / 3600)}h stale while the agent is "
                f"working ({int(agent_age / 60)}m since the last loop pass) — the proxy "
                "is up but nothing is routing through it any more, so the file on disk "
                "is a leftover from when it was. Quota-based budgeting is quoting stale "
                "numbers as current. Check ANTHROPIC_BASE_URL on the running core: only "
                "the core launcher (src/agent/claude/cli/start-cli.sh) exports it, and "
                "only when the proxy port already has a listener at launch — so a core "
                "started outside the launcher, or started before the proxy bound, stays "
                "unrouted for its whole session."
            )
        return check
    check["status"] = "warn"
    check["detail"] = (
        "credential proxy is up but has never written quota-state.json — nothing "
        "is routing through it (ANTHROPIC_BASE_URL unset on the running core). "
        "Only the core launcher (src/agent/claude/cli/start-cli.sh) exports it, "
        "and only when the proxy port already has a listener at launch — so a "
        "core started outside the launcher, or started before the proxy bound, "
        "stays unrouted for its whole session. Quota-based budgeting is blind "
        "on this host."
    )
    return check


def _fmt_quota_reset(epoch_str: Optional[str]) -> str:
    """Human-readable local time for a unix-epoch reset string; '' if unusable."""
    try:
        return time.strftime("%H:%M %Z", time.localtime(int(epoch_str)))
    except (TypeError, ValueError):
        return ""


def check_core_quota_exhausted(fresh_sec: int = 1800) -> dict:
    """FAIL (loudly, to the remote owner surface) when the core's model quota is
    exhausted — the 'stuck silently' condition.

    Why this exists (owner-reported 2026-08-01): the core was on a model, ran
    OVER QUOTA, and then every task stalled with no report — the agent went dark
    with no signal to the owner. `check_quota_telemetry` above only warns on the
    ABSENCE of quota-state.json; it deliberately never reads the values, so an
    *exhausted* quota reads as "ok, quota state present" and the outage stays
    invisible. This check closes that gap by reading the state and failing on an
    EXPLICIT exhaustion signal (unified status "rejected" or available:false),
    while leaving unknown/missing/corrupt state non-paging (fail-safe).

    Delivery is automatic and core-independent: a `fail` here is picked up by
    `_slack_failures()` → `notify_slack_for_failures()`, the remote owner DM that
    runs from the launchd health-check-fallback and does NOT depend on the (now
    stuck) core agent being able to speak. Dedup is the shared transition-hash
    contract, so the owner is told once per episode, not every tick.

    Staleness guard (fail-safe): quota-state.json is written by the credential
    proxy from upstream rate-limit headers. An *actively* over-quota core keeps
    hitting the API (429s carry the headers), so a genuine exhaustion reads
    FRESH + not-available. A stale not-available reading is ambiguous (an old
    snapshot from a long-quiet host), so we DON'T raise an owner alert on it —
    only on a fresh exhaustion. Absence/staleness of telemetry is already the
    `quota-telemetry` check's job; this one must not false-alarm on old data.
    """
    check = {"name": "core-quota", "status": "ok"}
    path = status_read_path("quota-state.json", WORKSPACE_DIR)
    if not path.exists():
        check["detail"] = "no quota-state.json (absence handled by quota-telemetry)"
        return check
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        check["status"] = "warn"
        check["detail"] = "quota-state.json present but unreadable"
        return check

    # Validate the persisted shape before touching it. quota-state.json is
    # written by a separate process; a corrupt/foreign payload (a JSON list,
    # null, or a non-object `headers`) must yield a bounded warn — NEVER crash
    # the whole health-check run via `.get` on a non-dict, especially since this
    # check runs from the fallback health checker while the core may be down.
    if not isinstance(data, dict):
        check["status"] = "warn"
        check["detail"] = "quota-state.json is not a JSON object"
        return check
    headers = data.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    status = headers.get("anthropic-ratelimit-unified-status", "unknown")

    # Page ONLY on an explicit exhaustion signal. The unified status header is
    # "allowed" or "rejected" (skills/quota-tracker/SKILL.md) and the proxy also
    # writes a top-level `available` bool. Exhaustion = an explicit "rejected"
    # status OR an explicit available:false. Ambiguous state — unknown/missing
    # status, e.g. a fresh partial response carrying available:true with status
    # "unknown" — is NOT exhaustion and must not raise a false owner page.
    exhausted = status == "rejected" or data.get("available") is False
    if not exhausted:
        check["detail"] = f"core quota not exhausted (status={status})"
        # The freshness guard below gates only the exhausted branch, so the
        # REASSURING reading was stated as current at any age. Hedge, don't warn.
        try:
            age_sec = time.time() - path.stat().st_mtime
        except OSError:
            age_sec = None
        if age_sec is None:
            check["detail"] += " — file age unreadable, so its currency is unknown"
        elif age_sec > fresh_sec:
            check["detail"] += (
                f" — but the reading is {int(age_sec / 60)}m old, so it describes "
                "whenever the proxy last saw traffic, not the core running now"
            )
        return check

    # Explicit exhaustion. Only alert if the reading is FRESH — a stale OR
    # age-unknown reading is ambiguous and must not page the owner (fail-safe).
    try:
        age_sec = time.time() - path.stat().st_mtime
    except OSError:
        # An unreadable age is NOT "fresh" — do not page on an age we can't read.
        check["detail"] = (
            "quota reports exhausted but the file age is unreadable — "
            "not alerting (fail-safe)"
        )
        return check
    if age_sec > fresh_sec:
        check["detail"] = (
            f"quota-state reports exhausted (status={status}) but the reading is "
            f"stale ({int(age_sec / 60)}m old) — not alerting on ambiguous old data"
        )
        return check

    reset = _fmt_quota_reset(headers.get("anthropic-ratelimit-unified-5h-reset"))
    reset_note = f" 5h window resets {reset}." if reset else ""
    check["status"] = "fail"
    check["detail"] = (
        f"CORE IS OVER QUOTA (rate-limit status={status}).{reset_note} The core "
        "cannot process tasks until quota resets or you switch models (/model) — "
        "this is the 'stuck silently' condition; tasks will queue undelivered."
    )
    return check


def _scoped_keychain_service(config_dir: Optional[str]) -> Optional[str]:
    """Mirror of credential-proxy.ts `scopedKeychainService`.

    Empty/whitespace -> None, so the caller falls back to the vanilla item —
    the same contract the proxy implements.
    """
    dir_ = (config_dir or "").strip()
    if not dir_:
        return None
    digest = hashlib.sha256(dir_.encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def _keychain_service_exists(service: str) -> bool:
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Distinct from None and from "": the environment could not be READ at all.
# "" means read-but-unset, which is a comparable answer; unreadable is not.
_PROXY_ENV_UNREADABLE = object()


def _proxy_config_dir_from_process(pid_finder=None, ps_runner=None):
    """CLAUDE_CONFIG_DIR of the RUNNING proxy, read from its process environment.

    The launchd plist answers this only for a launchd-managed proxy. One started
    any other way (startup.sh, the desktop supervisor, a hand-run node) inherits
    its env from whatever spawned it, and that env can name a different
    CLAUDE_CONFIG_DIR than the core's just as easily — the plist is one source of
    the mismatch, not the definition of it.

    Returns the value, "" when the proxy has no such variable, or
    _PROXY_ENV_UNREADABLE when the environment cannot be read.
    """
    finder = pid_finder or _proxy_pids
    runner = ps_runner or (lambda pid: subprocess.run(
        ["ps", "eww", "-p", pid], capture_output=True, text=True, timeout=5))
    try:
        pids = finder()
    except Exception:                     # noqa: BLE001 — a probe failure is "unknown"
        return _PROXY_ENV_UNREADABLE
    # Zero: nothing to read. More than one: ambiguous, and an ambiguous proxy is
    # not evidence about which account any single request billed.
    if len(pids) != 1:
        return _PROXY_ENV_UNREADABLE
    try:
        proc = runner(pids[0])
    except Exception:                     # noqa: BLE001
        return _PROXY_ENV_UNREADABLE
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return _PROXY_ENV_UNREADABLE
    out = proc.stdout or ""
    # `ps eww` prints argv ALONE when the env is unreadable, so requiring one
    # KEY=VALUE pair keeps that case UNREADABLE, not "no CLAUDE_CONFIG_DIR".
    if not re.search(r"(?:^| )[A-Za-z_][A-Za-z0-9_]*=", out):
        return _PROXY_ENV_UNREADABLE
    # Capture to the next ` KEY=`, not the next space: the value is a path
    # containing spaces, and a whitespace split hashes the wrong directory.
    m = re.search(
        r"(?:^| )CLAUDE_CONFIG_DIR=(.*?)(?= [A-Za-z_][A-Za-z0-9_]*=|$)", out, re.S)
    return m.group(1) if m else ""


def _proxy_pids() -> "list[str]":
    """PIDs LISTENING on the credential proxy's port (7846).

    `-sTCP:LISTEN` is load-bearing: bare `-ti:7846` also matches every client with
    an open connection to the port, and the routed core is always one of them. That
    returns two pids on a healthy host, which an "exactly one" guard reads as
    ambiguous — disabling the check precisely where it applies.
    """
    out = subprocess.run(
        ["/usr/sbin/lsof", "-ti:7846", "-sTCP:LISTEN"],
        capture_output=True, text=True, timeout=5)
    return [p for p in (out.stdout or "").split() if p.isdigit()]


def _resolved_credential_service(config_dir: Optional[str]) -> Optional[str]:
    """First EXISTING item of [scoped(config_dir), vanilla] — the proxy's order."""
    for service in (_scoped_keychain_service(config_dir), "Claude Code-credentials"):
        if service and _keychain_service_exists(service):
            return service
    return None


def _plist_via_plutil(path: "Path") -> "dict | None":
    """Parse a plist without `plistlib`. None on any failure, which keeps the
    caller's existing warn wherever plutil is absent or unusable."""
    try:
        out = subprocess.run(["/usr/bin/plutil", "-convert", "json", "-o", "-", str(path)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        parsed = json.loads(out.stdout)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def check_quota_account_identity(proxy_status: str, core_env_prober=None) -> dict:
    """Does the proxy resolve THIS core's login, or a different account's?

    `check_quota_telemetry` above answers "is quota-state fresh, and does it
    exist" — both are questions about WHEN, and neither can see the failure
    this check exists for: a proxy that is up, routing, and writing a
    seconds-old file **for someone else's account**.

    Observed 2026-08-03. The owner's login showed 7% of the 7d window used
    while every routed request billed a different account at 88%; the core
    throttled itself to near-idle for an hour against a ceiling that was not
    his. `quota-state.json` was never stale, so no existing branch fired.

    Mechanism: the proxy picks its credential by preferring the keychain item
    scoped to its own CLAUDE_CONFIG_DIR, falling back to vanilla. launchd does
    not inherit the installing shell's environment, so if the plist omits
    CLAUDE_CONFIG_DIR the proxy can only ever resolve the vanilla item — while
    an interactive `/login` in a namespaced core writes the SCOPED one. It does
    not self-heal: the proxy re-reads per request, but re-reads the wrong item
    and refreshes that token back into itself.

    Compares the item the CORE would resolve against the item the PROXY would,
    reading only the plist and keychain ITEM NAMES. No token, and no secret
    material of any kind, is read or logged.
    """
    name = "quota-account-identity"
    # "stale" means listening but executing pre-deploy code -- still routing,
    # so gating it out here would mute this check exactly during a redeploy.
    if proxy_status not in ("ok", "stale"):
        return {"name": name, "status": "ok",
                "detail": "credential proxy not up — nothing to compare"}

    # Whose account only matters if THIS core's requests go through that proxy.
    # A proxy can be up while the running core bypasses it entirely — a Codex or
    # direct runtime, or a supervisor-launched core that never got
    # ANTHROPIC_BASE_URL. Warning there would assert "requests bill that account"
    # and "/login here will not reach the proxy", neither of which is true of a
    # core that does not route. check_quota_telemetry needed the same gate for
    # the same class, so reuse its runtime marker.
    if _runtime_may_skip_proxy():
        return {"name": name, "status": "ok",
                "detail": "core runtime is not proxy-routed — its credential is not this proxy's"}
    # Probe the RUNNING CORE's environment, not this process's.
    #
    # The first version of this gate read `os.environ` on the theory that
    # health-check runs as a child of the core and inherits it. True on the
    # proactive-loop path, and false on the app / fallback-launchd / manual
    # paths, which this file already documents do not carry the core's env
    # (see the launchd notes around line 1417). On those a routed core with a
    # genuine account mismatch would read "comparison inactive" and stay
    # silent — the check disabled exactly where nobody is watching.
    #
    # `core_env_has_proxy_url` is TRI-STATE and its contract is that None must
    # never collapse into False. Applied here:
    #   True  -> the core routes; the comparison below is meaningful.
    #   False -> demonstrably NOT routed; the proxy's account is irrelevant to
    #            it, so every clause of the warning would be false. Silent.
    #   None  -> undeterminable (no tmux, ambiguous session, unreadable env).
    #            Silent, and says so — an unprovable premise licenses nothing.
    # `core_env_prober` is the same injectable seam check_quota_telemetry uses:
    # without it a fixture escapes into the developer's live tmux and reports on
    # the real core, which is how 3 of that suite's 39 cases once flipped.
    probe = core_env_prober or core_env_has_proxy_url
    routed = probe()
    if routed is False:
        return {"name": name, "status": "ok",
                "detail": ("the running core has no ANTHROPIC_BASE_URL — it is not routed "
                           "through this proxy, so whose login the proxy holds is moot")}
    if routed is None:
        return {"name": name, "status": "ok",
                "detail": ("could not read the running core's environment — identity "
                           "comparison inactive here (not evidence either way)")}

    core_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if not core_cfg:
        return {"name": name, "status": "ok",
                "detail": "core has no CLAUDE_CONFIG_DIR — both sides resolve the vanilla item"}

    plist = Path.home() / "Library/LaunchAgents/com.sutando.credential-proxy.plist"
    cfg_source = "plist"
    # Ask the RUNNING listener before the plist: a plist that outlives its job
    # describes a proxy that is not the one holding :7846, in someone else's name.
    from_proc = _proxy_config_dir_from_process()
    if from_proc is not _PROXY_ENV_UNREADABLE:
        return _quota_identity_verdict(name, core_cfg, from_proc, "process",
                                       plist_present=plist.is_file())
    if not plist.is_file():
        return {"name": name, "status": "ok",
                "detail": ("credential proxy is not launchd-managed and its "
                           "environment could not be read — identity comparison "
                           "inactive here (not evidence either way)")}
    # Imported HERE, not at module scope, and this placement is the whole point.
    # `plistlib` pulls in `xml.parsers.expat` -> the `pyexpat` C extension, which
    # is the single most fragile import in the stdlib: it dlopens libexpat, so a
    # Python whose pyexpat was built against a different libexpat than the one it
    # finds at runtime raises ImportError. Measured on a live host 2026-08-03,
    # same file, same commit, two interpreters:
    #
    #   /opt/homebrew/bin/python3 3.14.5 -> ImportError: dlopen pyexpat, symbol
    #                                       _XML_SetAllocTrackerActivationThreshold
    #                                       not found in /usr/lib/libexpat.1.dylib
    #                                       ->  0 of 39 checks ran
    #   /usr/bin/python3          3.9.6  -> 39 of 39 checks ran
    #
    # At module scope that ImportError is unreachable by any handler and kills
    # the process before a single check runs — one optional probe on one platform
    # silently taking down the whole health check, which is the one tool whose
    # job is to notice things being down. Lazy, it costs this probe only. See
    # PR #2582 for the installer-side half (choosing an interpreter that works).
    try:
        import plistlib
    except ImportError as exc:
        # plutil needs no expat, so the comparison stays answerable on exactly
        # the hosts where this import fails.
        rendered = _plist_via_plutil(plist)
        if rendered is None:
            return {"name": name, "status": "warn",
                    "detail": (f"cannot parse the credential-proxy plist — this Python "
                               f"cannot import plistlib ({exc.__class__.__name__}: {exc}) "
                               f"and plutil could not read it either. "
                               f"Every other check is unaffected.")}
    else:
        try:
            rendered = plistlib.loads(plist.read_bytes())
        except (OSError, ValueError) as exc:
            return {"name": name, "status": "warn",
                    "detail": f"cannot read the credential-proxy plist ({exc})"}
    # A plist can PARSE and still be the wrong shape — `EnvironmentVariables`
    # encoded as a string, say. `.get` on that raises AttributeError, which is
    # not caught above and would abort the whole health run, taking every later
    # check with it. Validate both containers as mappings; a parseable file of
    # the wrong shape is a warn, never an exception.
    if not isinstance(rendered, dict):
        return {"name": name, "status": "warn",
                "detail": (f"credential-proxy plist parsed but its root is "
                           f"{type(rendered).__name__}, not a dict — cannot read its environment")}
    env_block = rendered.get("EnvironmentVariables")
    if env_block is None:
        env_block = {}
    if not isinstance(env_block, dict):
        return {"name": name, "status": "warn",
                "detail": (f"credential-proxy plist has EnvironmentVariables as "
                           f"{type(env_block).__name__}, not a dict — cannot read CLAUDE_CONFIG_DIR")}
    proxy_cfg = env_block.get("CLAUDE_CONFIG_DIR")
    if proxy_cfg is not None and not isinstance(proxy_cfg, str):
        return {"name": name, "status": "warn",
                "detail": (f"credential-proxy plist has CLAUDE_CONFIG_DIR as "
                           f"{type(proxy_cfg).__name__}, not a string — cannot resolve its keychain item")}

    return _quota_identity_verdict(name, core_cfg, proxy_cfg, cfg_source,
                                   plist_present=True)


def _quota_identity_verdict(name: str, core_cfg: Optional[str],
                            proxy_cfg: Optional[str], source: str,
                            plist_present: bool = False) -> dict:
    """Compare the two resolved keychain ITEM NAMES and report.

    `source` names where the proxy's CLAUDE_CONFIG_DIR came from ("plist" or
    "process") and selects the remediation — telling someone to edit a plist that
    does not exist on their host is worse than saying nothing.
    """
    core_service = _resolved_credential_service(core_cfg)
    proxy_service = _resolved_credential_service(proxy_cfg)

    if core_service is None or proxy_service is None:
        # No readable credential on one side (locked keychain, fresh host).
        # Say so rather than returning a bare ok that looks like agreement.
        return {"name": name, "status": "ok",
                "detail": "no readable credential on one side — comparison inactive here"}

    if core_service == proxy_service:
        return {"name": name, "status": "ok",
                "detail": (f"proxy and core resolve the same keychain item "
                           f"({core_service}) — name match only; this check "
                           f"does not read tokens")}

    return {
        "name": name,
        "status": "warn",
        "detail": (
            f"the credential proxy resolves a DIFFERENT login than this core's: proxy "
            f"resolves '{proxy_service}', core would resolve '{core_service}'. What "
            f"follows turns on state this check does not read: if the proxy's stored "
            f"token is usable and the request carries an authorization header, the proxy "
            f"substitutes its own — quota numbers then describe that account, requests "
            f"bill it, and a `/login` here will not reach the proxy; if that token is "
            f"unusable the proxy engages pass-through, forwarding whatever credential the "
            f"client supplied and billing that account instead, or answering 401 when the "
            f"client supplied none. Either way "
            + (
                f"the cause is almost always the launchd plist omitting CLAUDE_CONFIG_DIR "
                f"(launchd inherits no shell env): "
                f"proxy plist has {'no' if not proxy_cfg else repr(proxy_cfg)} value. "
                f"Fix: pin CLAUDE_CONFIG_DIR in "
                f"~/Library/LaunchAgents/com.sutando.credential-proxy.plist, then reload it."
                if source == "plist" else
                # Reaching the process path says nothing about who STARTED it,
                # so "not launchd-managed" would assert state never checked.
                f"this reading came from the running process, which inherited "
                f"{'no CLAUDE_CONFIG_DIR' if not proxy_cfg else repr(proxy_cfg)}. "
                + (
                    f"A credential-proxy plist IS installed, so the proxy may be "
                    f"launchd-managed: correct CLAUDE_CONFIG_DIR in "
                    f"~/Library/LaunchAgents/com.sutando.credential-proxy.plist and reload it "
                    f"FIRST — under KeepAlive a bare restart is respawned with the plist's "
                    f"environment and the fix does not stick. "
                    if plist_present else
                    f"No credential-proxy plist is installed, so there is none to correct. "
                )
                + f"Then restart the proxy with CLAUDE_CONFIG_DIR set to this core's "
                f"({core_cfg!r}). Restarting it changes "
                f"which account subsequent requests bill, so confirm that is the intended "
                f"login first."
            )
        ),
    }


def check_bodhi_dist() -> dict:
    """Verify the installed bodhi-realtime-agent dist has the Gemini 3.1
    wire-format fixes applied. Greps the Gemini sendAudio/sendFile bodies
    for the post-fix `audio:`/`video:` keys rather than the deprecated
    `media:` key.

    Added 2026-04-09 after the 1007 "media_chunks is deprecated" regression:
    package-lock.json pointed at the post-fix bodhi commit, but the dist
    on disk was stale (git pull advanced the lockfile without triggering
    npm install). voice-agent booted fine because sendAudio isn't
    exercised until a client connects — so existing probes silently let
    it through. This probe catches that case on every health tick.

    Fix when this check fails: `npm install github:sonichi/bodhi_realtime_agent`
    then `launchctl kickstart -k gui/$(id -u)/com.sutando.voice-agent`.

    Scans whichever artifact the voice-agent ACTUALLY loads, because that
    differs by deployment and the node_modules copy is not always the one
    running:

      - dev checkout  -> node_modules/bodhi-realtime-agent/dist/index.js
      - bundled app   -> dist/voice-agent.js, an esbuild bundle with bodhi
                         inlined and NO node_modules on disk at all

    Checking only the node_modules path made this probe useless in exactly
    the deployment where it matters: a bundled install has an empty
    node_modules, so the check warned "run `npm install`" on every tick
    (noise that reads as benign) while giving ZERO coverage of the running
    bundle. The 1007 regression this was written to catch would have
    shipped undetected. The body scan below needs no change — it matches
    the bundled output as-is.
    """
    check = {"name": "bodhi-dist", "status": "ok", "detail": "Gemini 3.1 wire-format fixes present"}
    # Order matters: node_modules first so a dev checkout reports on the
    # package it actually resolves, bundle second for bundled installs.
    candidates = [
        REPO_DIR / "node_modules" / "bodhi-realtime-agent" / "dist" / "index.js",
        REPO_DIR / "dist" / "voice-agent.js",
    ]
    dist = next((c for c in candidates if c.exists()), None)
    if dist is None:
        check["status"] = "warn"
        check["detail"] = (
            "no bodhi artifact found (checked node_modules and dist/voice-agent.js) — "
            "run `npm install`, or `npm run build` for a bundled install"
        )
        return check
    try:
        text = dist.read_text(errors="replace")
    except OSError as e:
        check["status"] = "warn"
        check["detail"] = f"dist read failed: {e}"
        return check
    check["detail"] = f"Gemini 3.1 wire-format fixes present ({dist.name})"
    # Isolate the Gemini transport's sendAudio body. The OpenAI realtime
    # transport also defines sendAudio but uses `audio: base64Data` as a
    # flat string — a naive grep would false-positive.
    idx = text.find("sendAudio(base64Data) {")
    if idx < 0:
        check["status"] = "warn"
        check["detail"] = f"could not locate sendAudio in {dist.name}"
        return check
    # Find the first two sendAudio definitions; the Gemini one wraps its
    # arg in `this.session.sendRealtimeInput(...)`.
    stale_audio = False
    stale_file = False
    # Scan each sendAudio body for the sendRealtimeInput caller (Gemini).
    for start in _find_all(text, "sendAudio(base64Data) {"):
        body = _extract_body(text, start)
        if "sendRealtimeInput" in body:
            if "media: { data" in body or "media:{data" in body:
                stale_audio = True
            break
    for start in _find_all(text, "sendFile(base64Data, mimeType) {"):
        body = _extract_body(text, start)
        if "sendRealtimeInput" in body:
            if "media: { data" in body or "media:{data" in body:
                stale_file = True
            break
    stale = []
    if stale_audio:
        stale.append("sendAudio")
    if stale_file:
        stale.append("sendFile")
    if stale:
        check["status"] = "fail"
        check["detail"] = (
            f"bodhi dist stale: {'/'.join(stale)} still uses deprecated `media` key — "
            "Gemini 3.1 rejects with 1007. Run `npm install github:sonichi/bodhi_realtime_agent`."
        )
    return check


def _find_all(haystack: str, needle: str):
    """Yield every start index where `needle` occurs in `haystack`."""
    i = 0
    while True:
        i = haystack.find(needle, i)
        if i < 0:
            return
        yield i
        i += len(needle)


def _extract_body(text: str, start: int) -> str:
    """Extract the function body (matched-brace region) starting at the
    first `{` at or after `start`. Returns at most the next 2000 chars.
    """
    brace = text.find("{", start)
    if brace < 0:
        return ""
    depth = 0
    for j in range(brace, min(brace + 2000, len(text))):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace : j + 1]
    return text[brace : brace + 2000]


# ---------------------------------------------------------------------------
# Battery and memory health checks
# -------------------------

def check_battery() -> dict:
    """Check power source and battery level (macOS only). Issue #1486."""
    name = "battery"
    warn_pct = int(os.environ.get("SUTANDO_BATTERY_WARN_PCT", "20"))
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {"name": name, "status": "ok", "detail": "pmset not available (not macOS or VM)"}

    if "AC Power" in output:
        # Plugged in — no concern, but extract percentage if available
        import re
        m = re.search(r'(\d+)%', output)
        pct = int(m.group(1)) if m else None
        detail = f"AC power" + (f", {pct}% charged" if pct is not None else "")
        return {"name": name, "status": "ok", "detail": detail}

    if "Battery Power" in output or "'Battery Power'" in output:
        import re
        m = re.search(r'(\d+)%', output)
        pct = int(m.group(1)) if m else None
        if pct is None:
            return {"name": name, "status": "warn", "detail": "on battery — level unknown"}
        if pct <= warn_pct:
            return {"name": name, "status": "fail", "detail": f"on battery at {pct}% — critically low (threshold {warn_pct}%)"}
        return {"name": name, "status": "warn", "detail": f"on battery at {pct}% — no AC power"}

    return {"name": name, "status": "ok", "detail": "power state unknown"}

def check_memory() -> dict:
    """Warn/fail on real macOS memory pressure, not raw 'unused' pages. Issue #1485.

    The original probe read `top`'s "PhysMem: ... N unused" figure and failed
    when it dipped below a MB threshold. But macOS deliberately keeps unused
    pages low — it spends free RAM on the file cache and compressed memory — so
    "unused" routinely sits near zero on a perfectly healthy machine. That
    produced recurring false FAILs (e.g. "82M free — critically low" while
    `memory_pressure` reported 44% free and swap usage was 0), which in turn
    spawned owner-facing health tasks for a non-issue.

    OOM kills happen under sustained pressure with swap thrashing, so the real
    OOM-proximity signals on macOS are (a) the kernel pressure level —
    `kern.memorystatus_vm_pressure_level`: 1=normal, 2=warning, 4=critical —
    and (b) how much swap is actually in use. Gate warn/fail on those.
    """
    name = "memory"
    swap_warn_mb = int(os.environ.get("SUTANDO_MEMORY_SWAP_WARN_MB", "512"))
    swap_fail_mb = int(os.environ.get("SUTANDO_MEMORY_SWAP_FAIL_MB", "2048"))
    free_fail_pct = int(os.environ.get("SUTANDO_MEMORY_FREE_FAIL_PCT", "15"))
    free_warn_pct = int(os.environ.get("SUTANDO_MEMORY_FREE_WARN_PCT", "25"))
    import re as _re

    try:
        level = int(subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=5).stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        return {"name": name, "status": "ok", "detail": "pressure level unavailable (non-macOS or VM)"}

    # Swap actually in use is the strongest OOM-proximity signal.
    swap_used_mb = 0.0
    try:
        swap_out = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            capture_output=True, text=True, timeout=5).stdout
        sm = _re.search(r'used\s*=\s*([\d.]+)([MG])', swap_out)
        if sm:
            swap_used_mb = float(sm.group(1)) * (1024 if sm.group(2) == "G" else 1)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # System-wide free memory % is the honest OOM-proximity signal — the one
    # this check's own history keeps pointing at. Kernel pressure level is a
    # transient sample that can read 2 ("warning") for a single tick while free
    # memory is abundant, and swap-in-use is sticky (pages swapped out during a
    # *past* event stay counted until touched again). Convicting on those two
    # alone produced recurring false FAILs — e.g. "level 2, swap 5655M" while
    # `memory_pressure` reported 47% free. So free% gets the deciding vote: a
    # transient level-2 or sticky swap only convicts when free memory is
    # actually low. A kernel-declared level-4 (critical) still fails outright.
    # Issue #1485 follow-up.
    free_pct = None
    try:
        mp = subprocess.run(
            ["memory_pressure"], capture_output=True, text=True, timeout=5).stdout
        fm = _re.search(r'free percentage:\s*(\d+)%', mp)
        if fm:
            free_pct = int(fm.group(1))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Kernel-declared critical — trust it regardless of free%.
    if level >= 4:
        return {"name": name, "status": "fail",
                "detail": f"critical memory pressure (level {level}, swap {swap_used_mb:.0f}M in use)"}

    if free_pct is not None:
        # free% available → it is the deciding vote.
        if free_pct < free_fail_pct and (level >= 2 or swap_used_mb >= swap_fail_mb):
            return {"name": name, "status": "fail",
                    "detail": f"critical memory pressure ({free_pct}% free, level {level}, swap {swap_used_mb:.0f}M in use)"}
        if free_pct < free_warn_pct and (level >= 2 or swap_used_mb >= swap_warn_mb):
            return {"name": name, "status": "warn",
                    "detail": f"memory pressure elevated ({free_pct}% free, level {level}, swap {swap_used_mb:.0f}M in use)"}
        if swap_used_mb >= swap_warn_mb:
            return {"name": name, "status": "ok",
                    "detail": f"{free_pct}% free (healthy); swap {swap_used_mb:.0f}M is residue from a past pressure event, not active pressure (level {level})"}
        return {"name": name, "status": "ok",
                "detail": f"pressure normal ({free_pct}% free, level {level}, swap {swap_used_mb:.0f}M)"}

    # free% unavailable (non-macOS, tool missing, or parse failure) → fall back
    # to the level+swap heuristic (prior behavior; never blind the check).
    if level >= 2 and swap_used_mb >= swap_fail_mb:
        return {"name": name, "status": "fail",
                "detail": f"critical memory pressure (level {level}, swap {swap_used_mb:.0f}M in use)"}
    if level >= 2:
        return {"name": name, "status": "warn",
                "detail": f"memory pressure elevated (level {level}, swap {swap_used_mb:.0f}M in use)"}
    if swap_used_mb >= swap_warn_mb:
        return {"name": name, "status": "warn",
                "detail": f"swap {swap_used_mb:.0f}M in use but kernel pressure normal (level {level}) — likely residue from a past pressure event"}
    return {"name": name, "status": "ok", "detail": f"pressure normal (level {level}, swap {swap_used_mb:.0f}M)"}


# Stuck-loop / dead-watcher detection
# ---------------------------------------------------------------------------
# These two checks together catch the failure mode observed 2026-05-06 where
# voice-queued tasks piled up in tasks/ for 5+ minutes with no processing,
# because (a) the watch-tasks fswatch shim wasn't running and (b) the
# core proactive loop's last status update was 5 days old (status="running"
# with a stale ts means a pass crashed mid-execution and the loop never
# re-armed). Each check is a *consequence* signal that fires regardless of
# which underlying mechanism died.

def check_core_proactive_loop(threshold_sec: int = 600) -> dict:
    """Detect a stuck core proactive loop via stale core-status.json.

    The proactive loop writes core-status.json at every state transition
    (see CLAUDE.md "Work Status"). If status reads "running" but the
    timestamp hasn't advanced in `threshold_sec`, a pass crashed mid-
    execution and the loop never re-armed — emitted tasks won't be
    processed. Returns "warn" so emit_task_for_failures surfaces it.

    File missing or malformed → ok (new install, or core has never run).
    Status is anything other than "running" → ok regardless of age.
    """
    name = "core-proactive-loop"
    status_path = status_read_path("core-status.json", WORKSPACE_DIR)
    if not status_path.exists():
        return {"name": name, "status": "ok", "detail": "core-status.json not yet written"}
    try:
        data = json.loads(status_path.read_text())
    except Exception as e:
        # Malformed JSON shouldn't be treated as a stuck loop — could be a
        # half-written file caught between os.write() syscalls. Fall through
        # to ok so the next tick sees a (re-)written status.
        return {"name": name, "status": "ok", "detail": f"core-status.json unreadable: {str(e)[:60]}"}
    state = data.get("status")
    ts = data.get("ts")
    if state != "running":
        return {"name": name, "status": "ok", "detail": f"status={state}"}
    if not isinstance(ts, (int, float)):
        return {"name": name, "status": "ok", "detail": "running, no ts"}
    age = int(time.time() - ts)
    if age > threshold_sec:
        step = data.get("step", "?")
        return {
            "name": name,
            "status": "warn",
            "detail": f"running for {age}s on '{step}' — last heartbeat > {threshold_sec}s ago",
        }
    return {"name": name, "status": "ok", "detail": f"running ({age}s ago)"}


def check_core_supervisor() -> dict:
    """Surface the core-supervisor (Agent Shepherd M1) state for OSS users.

    The monitor (core-input-watch.py) writes state/core-supervisor.json with
    the core's supervised state. The desktop app renders this as an "Action
    needed" banner, but OSS users running bare Sutando have no such UI — so
    surface it here, in the canonical OSS status tool, as the simple in-repo
    consumer of the signal.

    States needing the user (login / an unrecognized prompt) → warn with a
    "needs you" line + the prompt excerpt; degraded states (crashed / hung /
    gateway-down) → warn; healthy (running / idle-ready / blocked-known, the
    last being pre-seeded/auto-answered) → ok. File missing → ok (monitor not
    running, or a pre-supervisor install).
    """
    name = "core-supervisor"
    sig_path = status_read_path("core-supervisor.json", WORKSPACE_DIR)
    if not sig_path.exists():
        return {"name": name, "status": "ok", "detail": "core-supervisor.json not yet written"}
    try:
        data = json.loads(sig_path.read_text())
    except Exception as e:
        return {"name": name, "status": "ok", "detail": f"core-supervisor.json unreadable: {str(e)[:60]}"}
    state = data.get("state", "unknown")
    detail = state
    prompt = data.get("prompt")
    if prompt:
        detail = f"{state} — {str(prompt).splitlines()[0][:60]}"
    needs_user = {"blocked-human", "logged-out"}
    degraded = {"crashed", "hung", "gateway-down"}
    # `gateway-down` means "core up, relay gateway not running". On a host with no
    # gateway configured that is the DESIGNED state — startup.sh never launches the
    # bridge and says so ("deliberately silent when unconfigured"), and the dedicated
    # probe below returns None for exactly this case. Treating it as degraded here
    # made every Sutando-only install carry a permanent warn it could not clear,
    # which is how a status tool teaches its reader to skip the warn line.
    if state == "gateway-down" and not _gateway_configured():
        return {"name": name, "status": "ok",
                "detail": "core up; ag2.space gateway not configured on this host"}
    if state in needs_user:
        return {"name": name, "status": "warn", "detail": f"core needs you: {detail}"}
    if state in degraded:
        return {"name": name, "status": "warn", "detail": f"core degraded: {detail}"}
    return {"name": name, "status": "ok", "detail": detail}


def _gateway_configured() -> bool:
    """Is the ag2.space mobile gateway configured on this host?

    `startup.sh` is deliberately silent when it is not — "a Sutando-only user
    never sees it" — so absence of the bridge is the EXPECTED state, not a fault.
    Shared by the two probes that must agree about that; they did not, which is
    the bug this helper exists to close.
    """
    try:
        if os.environ.get("REMOTE_TASK_TOKEN") or os.environ.get("AG2_REMOTE_TOKEN"):
            return True
        gw_env = claude_home_path("channels", "ag2space", ".env")
        if gw_env.exists():
            return any(
                ln.startswith(("REMOTE_TASK_TOKEN=", "AG2_REMOTE_TOKEN="))
                # errors="replace" is load-bearing, not cosmetic: without it a
                # single non-UTF-8 byte raises, the except below swallows it, and a
                # CONFIGURED gateway reads as unconfigured — which now also silences
                # the gateway-down warn. Fail-open on a decode error is exactly the
                # class this PR closes. (Caught in review by Sutando-Pro.)
                for ln in gw_env.read_text(errors="replace").splitlines()
            )
    except OSError:
        # EXPECTED failures only: the env file is unreadable / the path is bad.
        # Those genuinely mean "cannot confirm a gateway here" -> unconfigured.
        #
        # Deliberately NOT `except Exception`. A resolver contract bug (e.g.
        # claude_home_path raising ValueError) would be swallowed into False,
        # check_core_supervisor would then report a real `gateway-down` as OK,
        # and check_gateway_bridge would vanish -- a CONFIGURED gateway's outage
        # goes silent. That is the same fail-open direction this PR exists to
        # close, one layer up. An unexpected exception propagates instead, which
        # is loud; a programming error in a health probe should not be absorbed
        # by the probe that is supposed to be reporting faults.
        # (Caught in review by john-the-dev.)
        pass
    return False


def _gateway_lock_pids() -> "dict[str, str]":
    """Role -> PID from state/locks/. Instance identity exists only in the lock;
    every instance shares the same argv, so the process table cannot separate them."""
    out: "dict[str, str]" = {}
    try:
        locks = sorted((Path(WORKSPACE_DIR) / "state" / "locks").glob("gateway-bridge*.lock"))
    except Exception:
        return out
    for lk in locks:
        try:
            d = json.loads(lk.read_text())
        except Exception:
            continue
        role, pid = d.get("role"), d.get("pid")
        if role and pid:
            out[str(role)] = str(pid)
    return out


def check_gateway_bridge() -> "dict | None":
    """Health of the ag2.space gateway bridge (remote-gateway-bridge.py) — the
    process that carries MOBILE-app messages from the cloud gateway down to the
    local core (and results back up).

    Returns None when the mobile gateway is NOT configured (no REMOTE_TASK_TOKEN /
    AG2_REMOTE_TOKEN in env or channels/ag2space/.env) — a Sutando-only user
    without the mobile gateway never sees this check. Otherwise: ``warn`` when
    configured-but-not-running (with the delivery impact spelled out) or on a
    duplicate-process pileup, ``ok`` when a single instance is running.

    Added after a 3-day SILENT outage (2026-07-10): the bridge died on Jul 7 and
    nothing reported it, so mobile messages stranded in the cloud invisibly. This
    check makes that state visible on the dashboard.
    """
    configured = _gateway_configured()
    if not configured:
        return None
    try:
        gw = subprocess.run(
            # remote-relay-bridge.py is a shipped compat stub running the same
            # client, so an instance under the old name is a real duplicate.
            ["/usr/bin/pgrep", "-f", r"remote-(gateway|relay)-bridge\.py$"],
            capture_output=True, text=True,
        )
        pids = [p for p in gw.stdout.strip().split("\n") if p] if gw.returncode == 0 else []
    except Exception:
        pids = []
    if not pids:
        return {
            "name": "gateway-bridge",
            "status": "warn",
            "detail": GATEWAY_DOWN_DETAIL,
        }
    claimed = _gateway_lock_pids()
    if claimed:
        # startup.sh gives every instance identical argv, so a supported named
        # secondary is a duplicate only if no role lock claims its PID.
        unclaimed = [p for p in pids if p not in set(claimed.values())]
        if unclaimed:
            held = ", ".join(f"{r}={pid}" for r, pid in sorted(claimed.items()))
            return {
                "name": "gateway-bridge",
                "status": "warn",
                "detail": (
                    f"{len(unclaimed)} gateway process(es) claimed by no instance lock "
                    f"(PIDs: {','.join(unclaimed)}); locks held: {held}"
                ),
            }
    elif len(pids) > 1:
        # No lock data at all (pre-lock build, or locks/ unreadable): the count is
        # all we have, and it over-reports on a multi-instance host.
        return {
            "name": "gateway-bridge",
            "status": "warn",
            "detail": (
                f"multiple processes ({len(pids)} PIDs: {','.join(pids)}) "
                f"— no instance locks found to attribute them to"
            ),
        }
    # A live PROCESS is not a serving CONNECTION. The bridge rewrites
    # state/gateway-status.json on every poll outcome, so consult it before
    # calling this ok — otherwise a bridge stuck in a retry/backoff loop (route
    # gone, endpoint returning non-JSON) reports "running" indefinitely, which
    # is the very silent-outage class this check was added for. Observed
    # 2026-07-28: 5h of connected:false reported as ok/running.
    # Sidecar missing or stale (wedged, or a build too old to emit one) → no
    # opinion, keep the previous process-only verdict.
    verdict = _gateway_serving()
    if verdict is False:
        # WITHOUT the duration this line is byte-identical at minute one and at
        # hour thirteen, so a reader cannot tell a blip from a standing outage.
        age_h = _gateway_last_ok_age_h()
        # A negative age means a clock step or a bad sidecar write, not freshness.
        if age_h is not None and 0 <= age_h * 3600 < GATEWAY_TRANSIENT_OUTAGE_S:
            return {
                "name": "gateway-bridge",
                "status": "ok",
                "detail": f"running; last poll did not succeed, last one that did "
                          f"was {age_h * 3600:.0f}s ago (transient)",
            }
        # Seconds below an hour: `{age_h:.1f}h` renders a 106s age as "0.0h ago",
        # the self-refuting line the transient branch above exists to remove.
        age_s = None if age_h is None or age_h < 0 else age_h * 3600
        since = ("last successful poll UNKNOWN" if age_s is None
                 else f"last successful poll {age_s:.0f}s ago" if age_s < 3600
                 else f"last successful poll {age_s / 3600:.1f}h ago")
        return {
            "name": "gateway-bridge",
            "status": "warn",
            "detail": f"process running but NOT serving — {since}; "
                      "ag2.space mobile messages are not being delivered",
        }
    if verdict is True:
        return {"name": "gateway-bridge", "status": "ok", "detail": "running + connected"}
    return {"name": "gateway-bridge", "status": "ok", "detail": "running"}


GATEWAY_STATUS_MAX_AGE_S = 180.0
# Mirrors POLL_TIMEOUT_GRACE_S in ag2_sparrow/remote_gateway_bridge.py — the same
# env read, so a host that retunes POLL_WAIT moves both together.
GATEWAY_TRANSIENT_OUTAGE_S = 3 * (float(os.environ.get("REMOTE_TASK_POLL_WAIT") or 25) + 10)


def _gateway_last_ok_age_h(path: "Path | None" = None,
                           now: "float | None" = None) -> "float | None":
    """Hours since the sidecar's `last_ok_ts`; None when it is absent or
    unusable, so an unknown start is never reported as a fresh one."""
    import time as _time
    p = path or (status_read_path("gateway-status.json", WORKSPACE_DIR))
    now = _time.time() if now is None else now
    try:
        last = json.loads(Path(p).read_text()).get("last_ok_ts")
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return None
    return max(0.0, (now - float(last)) / 3600.0)


def check_runtime_identity(path: "Path | None" = None,
                           head_sha: "str | None" = None) -> dict:
    """#3279 verification layer 3: the RUNNING gateway process self-reports
    build SHA / entry point / delivery engine into its status sidecar; this
    probe compares that against the checkout so 'the code on disk' and 'the
    code in memory' can no longer silently diverge. Absent sidecar = idle;
    an UNREADABLE or MALFORMED one warns — the probe's own input being
    damaged must never render as a clean pass."""
    name = "runtime-identity"
    p = path or (status_read_path("gateway-status.json", WORKSPACE_DIR))
    p = Path(p)
    if not p.exists():
        return {"name": name, "status": "ok",
                "detail": "no gateway sidecar — nothing to verify (probe idle)"}
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"name": name, "status": "warn",
                "detail": "gateway sidecar exists but is unreadable/unparseable "
                          "— layer-3 verification is blind; inspect the file"}
    if not isinstance(doc, dict):
        return {"name": name, "status": "warn",
                "detail": "gateway sidecar is not a JSON object — malformed"}
    rt = doc.get("runtime")
    if rt is None:
        return {"name": name, "status": "warn",
                "detail": "running bridge predates the runtime self-report — "
                          "restart onto current code to enable layer-3 checks"}
    def _bad(reason):
        return {"name": name, "status": "warn",
                "detail": f"runtime self-report malformed: {reason}"}
    if not isinstance(rt, dict):
        return _bad("runtime block is not an object")
    sha = rt.get("build_sha")
    ep = rt.get("entrypoint")
    if not (isinstance(sha, str) and len(sha) >= 8):
        return _bad("build_sha missing or not a sha string")
    if not (isinstance(ep, str) and ep):
        return _bad("entrypoint missing")
    if not isinstance(rt.get("engine"), str):
        return _bad("engine missing")
    for k in ("core_confirmed", "legacy_sends"):
        v = rt.get(k)
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
            return _bad(f"{k} is not a non-negative int")
    bits = [f"engine={rt.get('engine')}",
            f"core_confirmed={rt.get('core_confirmed')}",
            f"legacy_sends={rt.get('legacy_sends')}"]
    if head_sha is None:
        try:
            head_sha = subprocess.check_output(
                git_argv("-C", str(REPO_DIR), "rev-parse", "HEAD"),
                text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        except Exception:
            head_sha = None
    if not head_sha:
        return {"name": name, "status": "warn",
                "detail": "checkout HEAD unreadable — cannot verify build "
                          "identity; " + " ".join(bits)}
    if sha != head_sha:
        return {"name": name, "status": "warn",
                "detail": f"build drift: process runs {sha[:8]}, checkout "
                          f"HEAD is {head_sha[:8]} — restart to converge; "
                          + " ".join(bits)}
    import hashlib
    for key, rel in (("loader_sha256", ("src", "remote-gateway-bridge.py")),
                     ("module_sha256", ("packages", "ag2-sparrow",
                                        "ag2_sparrow", "remote_gateway_bridge.py"))):
        reported_fp = rt.get(key)
        if not (isinstance(reported_fp, str) and reported_fp):
            continue
        try:
            disk_fp = hashlib.sha256(
                REPO_DIR.joinpath(*rel).read_bytes()).hexdigest()
        except OSError:
            disk_fp = None
        if disk_fp and disk_fp != reported_fp:
            return {"name": name, "status": "warn",
                    "detail": f"{key.split('_')[0]} bytes drift: the running "
                              "process executes different contents than now on "
                              "disk (same-HEAD change?) — restart to converge; "
                              + " ".join(bits)}
    expected = (REPO_DIR / "src" / "remote-gateway-bridge.py").resolve()
    try:
        actual = Path(ep).resolve()
    except (OSError, RuntimeError, ValueError):
        return {"name": name, "status": "warn",
                "detail": f"entrypoint path unresolvable (damaged telemetry): {ep!r}; "
                          + " ".join(bits)}
    if actual != expected:
        return {"name": name, "status": "warn",
                "detail": f"non-canonical entrypoint {ep} (expected {expected}); "
                          + " ".join(bits)}
    return {"name": name, "status": "ok",
            "detail": (f"sha={sha[:8]} entrypoint=canonical " + " ".join(bits))}


def _gateway_serving(path: "Path | None" = None, now: "float | None" = None) -> "bool | None":
    """Whether the gateway bridge's own sidecar says the connection is serving.

    True/False when the sidecar is present and fresh; None (no opinion) when it
    is absent, unreadable, malformed, or older than GATEWAY_STATUS_MAX_AGE_S.
    Mirrors core-input-watch._gateway_status() (#2253)."""
    import time as _time
    p = path or (status_read_path("gateway-status.json", WORKSPACE_DIR))
    now = _time.time() if now is None else now
    try:
        data = json.loads(Path(p).read_text())
        ts = data.get("ts")
        if not isinstance(ts, (int, float)) or (now - ts) > GATEWAY_STATUS_MAX_AGE_S:
            return None
        return bool(data.get("connected"))
    except (OSError, ValueError, AttributeError, TypeError):
        return None


# Free-space thresholds. A full volume is not a slow degradation — it is a hard
# stop: task/result writes fail with ENOSPC, so the bridge silently stops
# delivering and the core looks alive while doing nothing. Observed 2026-07-21,
# when the volume hit 100% and this script still reported "All systems
# operational" because nothing here looked at disk at all.
DISK_WARN_GIB = 10.0
DISK_FAIL_GIB = 2.0


DAILY_LATE_TOLERANCE_MIN = 15
DAILY_MISS_GRACE_MIN = 60


def _interpret_daily_punctuality(jobs: list) -> dict:
    """Score LATENESS, not presence: a file produced daily by another path looks
    identical to one produced by a working schedule."""
    name = "daily-cron-punctuality"
    late, missed, unknown = [], [], []
    for j in jobs:
        due = j["hour"] * 60 + j["minute"]
        if not j["artifacts"]:
            unknown.append(j["name"])
            continue
        # Wrap to the NEAREST occurrence: 23:42 finishing 00:05 is +23 late, not
        # -1417 early. The filename date is logical, often a day off the mtime.
        deltas = sorted(((w - due + 720) % 1440) - 720 for _, w in j["artifacts"])
        median = statistics.median(deltas)
        if median > DAILY_LATE_TOLERANCE_MIN:
            late.append((j["name"], median, len(deltas)))
        if (not j["today_seen"] and j["minutes_since_due"] > DAILY_MISS_GRACE_MIN
                and not j.get("conditional")
                and (j.get("stem_declared") or j["artifacts"])):
            missed.append((j["name"], j["minutes_since_due"]))
    if not late and not missed:
        seen = len(jobs) - len(unknown)
        detail = f"{seen} of {len(jobs)} daily job(s) observable"
        detail += ", all on schedule" if seen else ""
        if unknown:
            detail += (f"; UNCHECKED (no dated artifact, cannot tell whether it ran): "
                       f"{', '.join(sorted(unknown))}")
        if unknown:
            # `ok` would certify jobs nobody measured: on a 1-of-5 host the four
            # UNCHECKED ones miss forever behind green. Coverage gates the verdict.
            scope = "no coverage on this host" if not seen else \
                    f"{len(unknown)} of {len(jobs)} unverifiable — status cannot be ok"
            return {"name": name, "status": "warn", "detail": f"{detail} — {scope}"}
        return {"name": name, "status": "ok", "detail": detail}
    bits = []
    for n, m, c in late:
        bits.append(f"{n}: {c} run(s), median +{m} min late — the schedule is not what "
                    f"produced these; something else is covering for it")
    for n, m in missed:
        bits.append(f"{n}: no output today, {m} min past due")
    if unknown:
        bits.append(f"unverifiable (no dated artifact): {', '.join(sorted(unknown))}")
    return {"name": name, "status": "warn", "detail": "; ".join(bits)}


def _daily_artifact_minutes(results: Path, stem: str, limit: int = 7) -> list:
    """(date, minute-of-day-written) for the newest `<stem>-YYYY-MM-DD.*` files."""
    from datetime import datetime
    out = []
    if not results.is_dir():
        return out
    # rglob, not glob: delivered results are archived into MONTH buckets
    # (results/archive/YYYY-MM/), so the durable copies sit two levels down.
    for f in results.rglob(f"{stem}-20*"):
        m = re.match(rf"{re.escape(stem)}-(\d{{4}}-\d{{2}}-\d{{2}})", f.name)
        if not m:
            continue
        lt = datetime.fromtimestamp(f.stat().st_mtime)
        out.append((m.group(1), lt.hour * 60 + lt.minute))
    out.sort()
    return out[-limit:]


def _daily_completion_minutes(state: Path, job: str, limit: int = 7) -> list:
    """(date, minute-of-day-finished) from `state/<job>-YYYY-MM-DD.sentinel`.

    Jobs that publish nothing dated into results/ still record completion here.
    Prefer the body's ISO stamp; mtime is a fallback because a later touch
    moves it while the recorded finish time cannot drift.
    """
    from datetime import datetime
    out = []
    if not state.is_dir():
        return out
    for f in state.glob(f"{job}-20*.sentinel"):
        m = re.match(rf"{re.escape(job)}-(\d{{4}}-\d{{2}}-\d{{2}})\.sentinel$", f.name)
        if not m:
            continue
        when = None
        try:
            # 3.9's fromisoformat rejects a `Z` suffix; and an aware stamp must be
            # localised or its minute-of-day is UTC while cron times and mtime are local.
            body = f.read_text(errors="ignore").strip()[:32].replace("Z", "+00:00")
            when = datetime.fromisoformat(body)
            if when.tzinfo is not None:
                when = when.astimezone().replace(tzinfo=None)
        except (OSError, ValueError):
            when = None
        if when is None:
            try:
                when = datetime.fromtimestamp(f.stat().st_mtime)
            except OSError:  # pragma: no cover - file vanished between glob and stat
                continue
        out.append((m.group(1), when.hour * 60 + when.minute))
    out.sort()
    return out[-limit:]


def check_daily_cron_punctuality() -> dict:
    """Report a daily job whose output never arrives at its scheduled time."""
    from datetime import datetime, time as dtime
    name = "daily-cron-punctuality"
    ws = WORKSPACE_DIR
    cfg = ws / "hosts" / _host_label() / "crons.json"
    if not cfg.is_file():
        return {"name": name, "status": "ok", "detail": "no per-host crons.json — skipped"}
    try:
        raw = json.loads(cfg.read_text())
    except (OSError, ValueError) as e:
        return {"name": name, "status": "ok", "detail": f"crons.json unreadable ({e}) — skipped"}
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("crons") or raw.get("jobs") or []
    else:
        # `1` is valid JSON: .get() on it aborted the whole always-on health run.
        return {"name": name, "status": "ok",
                "detail": f"crons.json root is {type(raw).__name__}, not a list or "
                          f"object — skipped"}
    now = datetime.now()
    jobs, malformed = [], []
    for e in entries:
        # codex-task entries complete in another runtime that writes no sentinel.
        if not isinstance(e, dict) or e.get("execution") == "codex-task":
            continue
        f = str(e.get("cron") or "").split()
        if len(f) != 5 or not f[0].isdigit() or not f[1].isdigit():
            continue
        if f[2] != "*" or f[3] != "*" or f[4] != "*":
            continue                       # not a plain every-day schedule
        jname = str(e.get("name") or "?")
        try:
            # "61 24 * * *" is all digits and still invalid; dtime() would raise
            # ValueError out of the always-on run_all_checks() path.
            due = datetime.combine(now.date(), dtime(int(f[1]), int(f[0])))
        except ValueError:
            malformed.append(f"{jname} ({' '.join(f[:2])})")
            continue
        # A NAME is not an artifact: "talk-events-nightly" infers stem "nightly"
        # and never sees its real fleet-growth-<date>.mp4 output.
        declared = str(e.get("artifact") or "").strip()
        stem = declared or (jname.split("-")[-1] if "-" in jname else jname)
        launchd = bool(e.get("launchd"))
        # The launchd lane publishes no dated results file; its completion
        # sentinel is the only dated record that it finished.
        arts = (_daily_completion_minutes(ws / "state", jname) if launchd
                else _daily_artifact_minutes(ws / "results", stem))
        jobs.append({
            "name": jname, "hour": int(f[1]), "minute": int(f[0]), "artifacts": arts,
            "today_seen": any(d == now.strftime("%Y-%m-%d") for d, _ in arts),
            "minutes_since_due": max(0, int((now - due).total_seconds() // 60)),
            # `artifact` names a results file, so it cannot vouch for a sentinel:
            # only an observed history makes a missing sentinel today actionable.
            "stem": stem, "stem_declared": bool(declared) and not launchd,
            # Renders only when new input exists, so a quiet day produces nothing
            # and absence is evidence of nothing rather than of a miss.
            "conditional": bool(e.get("conditional")),
        })
    if malformed:
        return {"name": name, "status": "warn",
                "detail": f"unparseable daily cron time(s): {', '.join(malformed)} — "
                          f"fix the crons.json entry; punctuality unchecked for "
                          f"{len(malformed)} job(s)"}
    if not jobs:
        return {"name": name, "status": "ok", "detail": "no plain-daily jobs — skipped"}
    return _interpret_daily_punctuality(jobs)


def check_disk_space() -> dict:
    """Free space on the volume(s) the core actually writes to.

    Checks the workspace and the temp dir, reporting the WORST of the two —
    they are usually the same volume, but a separate /tmp must not hide a full
    workspace (or the reverse). Reports absolute GiB rather than percentage:
    5% of a 460G disk is 23G (fine) while 5% of a 20G disk is 1G (dead), so a
    percentage threshold would mean different things on different hosts.
    """
    name = "disk-space"
    targets = {}
    for label, path in (("workspace", WORKSPACE_DIR), ("tmp", Path(tempfile.gettempdir()))):
        try:
            st = os.statvfs(str(path))
        except OSError as e:
            return {"name": name, "status": "error", "detail": f"cannot stat {label} ({path}): {e}"}
        # Key by device so the same volume isn't reported twice.
        targets[st.f_fsid or label] = (label, path, st.f_bavail * st.f_frsize / (1024 ** 3))

    worst_label, worst_path, worst_free = min(targets.values(), key=lambda t: t[2])
    where = f"{worst_free:.1f} GiB free on {worst_label} ({worst_path})"
    if worst_free < DISK_FAIL_GIB:
        return {"name": name, "status": "fail",
                "detail": f"{where} — writes will fail with ENOSPC; free space now"}
    if worst_free < DISK_WARN_GIB:
        return {"name": name, "status": "warn", "detail": f"{where} — below {DISK_WARN_GIB:.0f} GiB"}
    return {"name": name, "status": "ok", "detail": where}


@functools.lru_cache(maxsize=1)
def _ephemeral_roots() -> tuple:
    """Temp roots whose contents do not survive a sweep or reboot, asked of the
    platform: naming them literally puts a host path in the tree the scan forbids."""
    roots = set()
    for cand in (tempfile.gettempdir(), "/tmp"):
        for p in (cand, os.path.realpath(cand)):
            p = p.rstrip("/")
            if not p:
                continue
            roots.add(p)
            # macOS $TMPDIR is <base>/folders/xx/yyy/T. The shared base counts too, so
            # another session's scratch dir is still recognised as ephemeral.
            head, sep, _ = p.partition("/folders/")
            if sep:
                roots.add(head + "/folders")
    return tuple(sorted(roots))


def _is_ephemeral(target: str) -> bool:
    """True if `target` IS a temp root or lives under one.

    Equality counts: a link pointing AT the root is as ephemeral as one pointing
    inside it, and a trailing-slash prefix test answers False for exactly that case.
    """
    t = os.path.normpath(target).rstrip("/") or "/"
    return any(t == r or t.startswith(r + "/") for r in _ephemeral_roots())


def check_skill_symlinks() -> dict:
    """Detect skills in the OSS repo checkout that are not symlinked into
    the Claude home's skills/ dir. A missing symlink means Claude Code never
    loads the skill — it's silently invisible until manually linked (bug
    d920b18b).

    Scans REPO_DIR/skills/ for directories and checks for a matching entry
    in <claude-home>/skills/. Reports unlinked skills as 'warn'; in --fix
    mode, creates the missing symlinks automatically.

    The destination resolves via claude_home_path() (same as
    _default_memory_dir(), fixed for the identical reason in #1454): a bare
    Path.home()/".claude" ignores the workspace-scoped CLAUDE_CONFIG_DIR, so
    on a migrated install this check scanned a stale ~/.claude/skills/ and
    warned "unlinked" about skills whose symlinks exist — and are loaded —
    under the workspace claude-home.
    """
    name = "skill-symlinks"
    skills_src = REPO_DIR / "skills"
    skills_dst = claude_home_path("skills")

    if not skills_src.exists():
        return {"name": name, "status": "ok", "detail": "skills/ dir not found — skipped"}
    if not skills_dst.exists():
        return {"name": name, "status": "ok", "detail": f"{skills_dst} not found — skipped"}

    # A DANGLING symlink (entry present, target gone) is the case the original
    # condition let through: `exists()` follows the link and is False, but
    # `is_symlink()` is True, so `not exists and not is_symlink` is False and the
    # skill counted as linked. Claude Code cannot load it either way, so it was
    # invisible AND reported healthy. Tracked as #2213.
    unlinked: list[str] = []   # no entry at all -> symlink_to() works
    broken: list[str] = []     # dangling link  -> must be unlinked first
    # A REAL DIRECTORY where a symlink belongs is a fourth state this check did
    # not model, and it fell through both branches below into "healthy":
    # `is_symlink()` is False and `exists()` is True, so neither condition
    # matched and the skill counted as linked.
    #
    # It is not linked. It is a COPY, so `git pull` never reaches it and the
    # running skill diverges from the repo silently and permanently. Both
    # repair paths decline by design: refresh-skill.sh prints "skip <name>
    # (not a symlink -- won't clobber a local/copy install)" and install.sh
    # skips-on-elsewhere, so nothing ever converts it back.
    #
    # Observed on Chis-MacBook-Pro 2026-08-05: `x-twitter` had been a real
    # directory since Jul 17 and was 11 days behind the repo, while this probe
    # reported "all 60 skills linked". The drift was one ruff E401 import split
    # -- harmless that time, which is exactly why it survived unnoticed.
    shadowed: list[str] = []   # real dir where a symlink belongs -> NOT auto-fixed
    misdirected: list[tuple[str, str]] = []  # resolves, but into a temp dir
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir():
            continue
        # Mirror skills/install.sh's filter: only dirs WITH a SKILL.md are
        # slash-invocable skills the installer links. Manifest-loaded and
        # scripts-only skills (gws-gmail-voice, learned-skills, ...) have no
        # SKILL.md, are correctly never symlinked, and must not warn here.
        if not (skill_dir / "SKILL.md").is_file():
            continue
        skill_name = skill_dir.name
        dst = skills_dst / skill_name
        if dst.is_symlink() and not dst.exists():
            broken.append(skill_name)
        elif not dst.exists() and not dst.is_symlink():
            unlinked.append(skill_name)
        elif dst.is_dir() and not dst.is_symlink():
            shadowed.append(skill_name)
        elif (dst.is_symlink() and _is_ephemeral(os.path.realpath(dst))
              and not _is_ephemeral(str(skills_src.resolve()))):
            # The MISMATCH is the defect, not temp-rootedness: a temp-rooted repo
            # is self-consistent, and another DURABLE clone is a supported layout.
            misdirected.append((skill_name, os.path.realpath(dst)))

    # Dangling links whose skill is NOT in this repo are missed by the loop
    # above, which only walks repo skills. They are still dead entries that make
    # a skill silently unloadable, so sweep the destination too. Reported, never
    # auto-removed: the target may belong to another checkout that is simply
    # not mounted right now, and deleting someone else's link is not this
    # check's call.
    orphaned: list[str] = []
    try:
        repo_names = {d.name for d in skills_src.iterdir() if d.is_dir()}
        for entry in sorted(skills_dst.iterdir()):
            if entry.name in repo_names:
                continue
            if entry.is_symlink() and not entry.exists():
                orphaned.append(entry.name)
    except OSError:
        pass

    if not unlinked and not broken and not orphaned and not shadowed and not misdirected:
        return {"name": name, "status": "ok", "detail": f"all {sum(1 for d in skills_src.iterdir() if d.is_dir())} skills linked"}

    parts = []
    if broken:
        parts.append(f"{len(broken)} dangling: {', '.join(broken[:4])}{'...' if len(broken) > 4 else ''}")
    if unlinked:
        parts.append(f"{len(unlinked)} unlinked: {', '.join(unlinked[:4])}{'...' if len(unlinked) > 4 else ''}")
    if orphaned:
        parts.append(f"{len(orphaned)} dangling not in this repo: {', '.join(orphaned[:4])}{'...' if len(orphaned) > 4 else ''}")
    if misdirected:
        parts.append(
            f"{len(misdirected)} loaded from temp (vanishes on sweep): "
            + ", ".join(f"{n} -> {t}" for n, t in misdirected[:2])
            + ("..." if len(misdirected) > 2 else "")
        )
    if shadowed:
        # The remedy must MOVE the real directory aside first. `ln -sfn` alone
        # does NOT repair this state: with the directory still present, macOS
        # `ln` treats the destination as a target DIRECTORY and creates a nested
        # `<dst>/<name>/<name>` symlink, leaving the real dir in place — so the
        # skill stays unlinked while the operator believes it is fixed.
        # Reproduced (john-the-dev, #2660): dest_is_symlink=no, and
        # `readlink <dst>/alpha/alpha` returned the source path.
        #
        # Moving rather than deleting is deliberate and is the whole reason this
        # is not auto-fixed: the directory may carry local edits, and `rm -rf`
        # would destroy them silently.
        #
        # Every complete path is QUOTED. Unquoted, a workspace or checkout path
        # containing a space word-splits before `mv` runs, so the command exits 1
        # with `mv: <tail>/alpha.local-backup is not a directory`, leaves the real
        # directory in place, and creates neither the symlink nor the backup — the
        # operator is told the repair succeeded by a command that did nothing.
        # Reproduced independently by qingyun-wu and bassilkhilo-ag2 (#2660) against
        # `/private/tmp/pr2660 spaced repro .../{src,dst} tree`. The activation test
        # below runs the emitted command under a spaced fixture for this reason.
        #
        # The backup goes OUTSIDE <dst>: the skill loader registers every directory
        # in <dst> as a skill, so a backup left there loads as a phantom duplicate.
        parts.append(
            f"{len(shadowed)} a real dir, not a link (diverges silently; repair with "
            f'`mv "<dst>/<name>" "<dst>/../<name>.skill-backup" && ln -s "<src>/<name>" "<dst>/<name>"` '
            f"— move aside, do NOT `ln -sfn` over it, and keep the backup until you have "
            f"checked it for local edits): "
            f"{', '.join(shadowed[:4])}{'...' if len(shadowed) > 4 else ''}"
        )

    return {
        "name": name,
        "status": "warn",
        "detail": "; ".join(parts),
        "_unlinked": unlinked,
        "_broken": broken,
        "_orphaned": orphaned,
        # Deliberately NOT consumed by fix_skill_symlinks(): a real directory may
        # be an intentional local install someone is editing, and replacing it
        # with a symlink would discard that work silently. Reported only --
        # the same reason refresh-skill.sh refuses to touch it.
        "_shadowed": shadowed,
        "_skills_src": str(skills_src),
        "_skills_dst": str(skills_dst),
    }


def fix_skill_symlinks(check: dict) -> dict:
    """Create missing symlinks for unlinked skills (--fix handler)."""
    unlinked = check.get("_unlinked", [])
    broken = check.get("_broken", [])
    skills_src = Path(check.get("_skills_src", ""))
    skills_dst = Path(check.get("_skills_dst", ""))
    created: list[str] = []
    errors: list[str] = []
    for skill_name in list(broken) + list(unlinked):
        src = skills_src / skill_name
        dst = skills_dst / skill_name
        try:
            # A dangling link occupies the name: symlink_to() would raise
            # FileExistsError, so --fix could not repair the very case it was
            # meant to. Guard on is_symlink() so a real directory is never
            # removed by a health check.
            if dst.is_symlink() and not dst.exists():
                dst.unlink()
            dst.symlink_to(src)
            created.append(skill_name)
        except Exception as e:
            errors.append(f"{skill_name}: {e}")
    result = f"linked {len(created)}"
    if created:
        result += f" ({', '.join(created)})"
    if errors:
        result += f"; errors: {'; '.join(errors)}"
    return {"name": "skill-symlinks", "status": "ok" if not errors else "warn", "detail": result}


def apply_skill_symlink_fixes(checks: list, stream=None) -> None:
    """--fix dispatch for skill-symlinks: warn-level (excluded from the issues
    loop) but auto-fixable, so it is handled by its own pass over checks.

    `stream` is where the repair line goes, default stdout. A caller that emits
    a machine-readable payload on stdout MUST pass `sys.stderr` — this helper
    prints prose, and prose ahead of the payload makes `json.loads(stdout)` fail
    at line 1.

    On a successful repair the check dict is updated IN PLACE, so every
    downstream reader — the JSON payload, the human listing, the summary — sees
    the post-fix state instead of the warning this call just cleared. Reporting
    the pre-fix warning after repairing it hands consumers a payload that
    contradicts the action they asked for. The `_unlinked`/`_broken` keys are
    cleared with it so a second pass cannot re-fix an already-repaired check.
    """
    out = stream if stream is not None else sys.stdout
    for c in checks:
        if c["name"] == "skill-symlinks" and (c.get("_unlinked") or c.get("_broken")):
            result = fix_skill_symlinks(c)
            print(f"  {c['name']}: {result['detail']}", file=out)
            # RE-RUN the check instead of adopting the fixer's own verdict.
            # `fix_skill_symlinks` repairs only `_unlinked`/`_broken`, and its
            # status is computed solely from what it repaired — it is blind to
            # `_orphaned`, which it deliberately does not remove. Copying that
            # status over the check reported `ok` while a dangling link
            # survived: a false clean, in the very payload added to make the
            # post-fix state honest. A repair's self-report is not evidence of
            # the resulting state; only re-measuring is.
            fresh = check_skill_symlinks()
            c.clear()
            c.update(fresh)


def _pending_task_files(tasks_dir: Path, results_dir: Optional[Path] = None) -> list[Path]:
    """Top-level task files that have not produced or archived a result."""
    if results_dir is None:
        results_dir = tasks_dir.parent / "results"
    try:
        archived_names = set()

        def record_archived(path: Path) -> None:
            if not path.is_file():
                return
            archived_names.add(path.name)
            renamed = re.match(r"^(.+)-[0-9]+\.txt$", path.name)
            if renamed:
                archived_names.add(f"{renamed.group(1)}.txt")

        archive_dir = results_dir / "archive"
        for path in archive_dir.glob("*/*.txt"):
            record_archived(path)
        for path in archive_dir.glob("*.txt"):
            record_archived(path)
        # Startup retention uses sibling archive-YYYY-MM-DD directories.
        # task-notifier.sh already treats these as completed deliveries; the
        # health queue and wedge-recovery signal must use the same namespace.
        for retention_dir in results_dir.glob("archive-*"):
            if not retention_dir.is_dir():
                continue
            for path in retention_dir.glob("*.txt"):
                record_archived(path)
        return [
            path for path in tasks_dir.glob("*.txt")
            if path.is_file()
            and not (results_dir / path.name).is_file()
            and path.name not in archived_names
        ]
    except OSError:
        return []


def _bridge_log_belongs_to_process(log_file: Path, process_started_at: "float | None") -> bool:
    """Whether log content can describe the currently running bridge.

    A bridge restarted under tmux may write to its pane while the prior
    startup-managed log remains on disk. Old LoginFailure text must not
    override a live, newly authenticated process.
    """
    if process_started_at is None:
        return True
    try:
        return log_file.stat().st_mtime >= process_started_at - 1
    except OSError:
        return True


def _parse_etime(raw: str) -> "float | None":
    """ps ELAPSED -> seconds. `MM:SS`, `HH:MM:SS`, `D-HH:MM:SS`; None if unparseable."""
    raw = (raw or "").strip()
    days = 0
    if "-" in raw:
        head, _, raw = raw.partition("-")
        if not head.isdigit():
            return None
        days = int(head)
    parts = raw.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    hours, minutes, seconds = ([0] + nums) if len(nums) == 2 else nums
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _worker_holdings(ps_output: "str | None" = None) -> dict:
    """{task filename: worker RUNTIME in seconds, or None if unreadable}.

    A team task stays in tasks/ for the whole run — the worker removes it only
    when it publishes a result — so an in-flight task and an abandoned one are
    byte-identical on disk. Only the worker's argv separates them.

    The runtime comes from the worker PROCESS, not the task file: a task can sit
    queued for hours before a worker claims it, and claiming does not touch the
    file, so file age answers a different question than "has this worker
    outlived its deadline".
    """
    if ps_output is None:
        try:
            ps_output = subprocess.run(["ps", "-Ao", "pid=,etime=,args="], capture_output=True,
                                       text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001 — a probe failure must not fail the check
            return {}
    held: dict = {}
    for line in (ps_output or "").splitlines():
        if "session-worker.py" not in line:
            continue
        fields = line.split()
        # Callers may pass argv-only output; then the leading fields are argv.
        pid = int(fields[0]) if fields and fields[0].isdigit() else None
        proc_age = _parse_etime(fields[1]) if len(fields) > 1 else None
        if pid is None:
            proc_age = _parse_etime(fields[0]) if fields else None
        for tok in fields:
            if tok.endswith(".txt") and "/tasks/" in tok:
                name = Path(tok).name
                held[name] = _provider_runtime(name, proc_age, pid)
    return held


#: The worker stamps this after acquiring its workstream lock; see
#: session-worker.py::_run_mark. Absent + alive = still waiting for the lock.
WORKER_RUN_MARK_DIR = Path("state") / "task-workstream-runs"
WAITING_FOR_LOCK = "waiting"


def _provider_runtime(task_name: str, proc_age: "float | None",
                      pid: "int | None" = None):
    """Seconds the PROVIDER has run, `WAITING_FOR_LOCK`, or None if unreadable.

    Process age is the wrong quantity — same-workstream tasks serialize on a lock
    taken AFTER the process starts — but it still BOUNDS how long an absent mark
    can innocently mean "waiting".
    """
    mark = WORKSPACE_DIR / WORKER_RUN_MARK_DIR / f"{task_name}.started"
    try:
        rec = json.loads(mark.read_text())
        started = float(rec["started"])
        owner = int(rec["pid"])
    except FileNotFoundError:
        rec = None
    except (OSError, ValueError, TypeError, KeyError):
        return None
    else:
        # Only a POSITIVELY matched owner may hand over its clock. A foreign pid
        # is a SIGKILLed worker's corpse; an unknown one cannot rule that out.
        if pid is not None and owner == pid:
            age = time.time() - started
            return age if age >= 0 else 0.0
        rec = None
    if proc_age is None:
        return None
    # No usable mark is innocent only while the process is younger than the
    # deadline; past that, a failed write and a wedged provider look identical.
    return WAITING_FOR_LOCK if proc_age <= _worker_hard_timeout_s() else None


def _tasks_held_by_a_worker(ps_output: "str | None" = None) -> set:
    """Filenames only — the set form, for callers that do not need the runtime."""
    return set(_worker_holdings(ps_output))


def check_task_queue(threshold_count: int = 3, threshold_age_sec: int = 300,
                     stuck_age_sec: int = 900) -> dict:
    """Detect a task-queue pileup, independent of which watcher or loop is dying.
    Two branches: count AND age together, or age alone past stuck_age_sec."""
    name = "task-queue"
    tasks_dir = WORKSPACE_DIR / "tasks"
    if not tasks_dir.exists():
        return {"name": name, "status": "ok", "detail": "tasks/ not yet created"}
    # *.txt at the top level only — archive lives in tasks/archive/<YYYY-MM>/
    # (PR #591) and shouldn't count toward the queue.
    files = _pending_task_files(tasks_dir)
    if not files:
        return {"name": name, "status": "ok", "detail": "queue empty"}
    now = time.time()
    # An in-flight task looks exactly like a stalled one on disk; the worker's
    # argv is the only thing that tells them apart.
    holdings = _worker_holdings()
    inflight = sum(1 for f in files if f.name in holdings)
    held_note = f", {inflight} in flight with a worker" if inflight else ""
    oldest = min(files, key=lambda p: p.stat().st_mtime)
    oldest_age = int(now - oldest.stat().st_mtime)
    # Deadline vs the WORKER's runtime, never the file's age: a task can queue
    # for hours before a worker claims it, and claiming does not touch the file.
    all_held = inflight == len(files)
    held_deadline = int(_worker_hard_timeout_s())
    ages = [holdings.get(f.name) for f in files if f.name in holdings]
    # A holder still waiting for its workstream lock has not started running, so
    # it can never be past its own deadline — that wait is progress, not a stall.
    running = [a for a in ages if isinstance(a, (int, float))]
    unreadable = any(a is None for a in ages)
    worker_age = None if (unreadable or not ages) else (int(max(running)) if running else 0)
    held_is_progress = all_held and worker_age is not None and worker_age <= held_deadline
    wedged_note = (f" — still held by a live worker running {worker_age}s, past its own "
                   f"{held_deadline}s hard deadline, so the WORKER is wedged, not the watcher"
                   if worker_age is not None else
                   " — held by a live worker whose runtime could not be read, so progress "
                   "cannot be established")
    held_verdict = (" — all held by a live worker, not stalled" if held_is_progress
                    else wedged_note if all_held else None)
    if len(files) > threshold_count and oldest_age > threshold_age_sec:
        return {
            "name": name,
            # ok, not warn: every `warn` is alertable (emit_task_for_failures /
            # notify_for_failures), so rewording alone still fires the false alert.
            "status": "ok" if held_is_progress else "warn",
            "detail": (f"{len(files)} tasks queued{held_note}, oldest {oldest_age}s"
                       + (held_verdict or " — watcher or core may be stuck")),
        }
    # ANDing count with age left a single stuck task unreachable, so one owner
    # message could sit indefinitely while this probe printed its age under "ok".
    # A worker configured SHORTER than stuck_age_sec is already overdue here, so
    # the age test alone would let it fall through to "ok" and hide it.
    if oldest_age > stuck_age_sec or (all_held and not held_is_progress):
        return {
            "name": name,
            "status": "ok" if held_is_progress else "warn",
            "detail": (f"{len(files)} task(s) queued{held_note}, oldest {oldest_age}s"
                       + (held_verdict or f" — undrained past {stuck_age_sec}s")),
        }
    return {"name": name, "status": "ok",
            "detail": f"{len(files)} task(s){held_note}, oldest {oldest_age}s"}


def check_orphaned_results(threshold_age_sec: int = 900) -> dict:
    """Detect results that no consumer will ever claim.

    Normal flow: a result is written to `results/task-<id>.txt` while
    `tasks/task-<id>.txt` is still present; the consuming bridge delivers and
    archives both. So a result whose task is already gone from `tasks/` was
    written AFTER that task was archived — and every consumer keys off either a
    tracked task_id or a `task-*` glob it has already retired. Nothing claims
    the file, and the reply is silently never delivered.

    Observed 2026-07-29: a reply sat in `results/` for 2h22m while its task sat
    in `tasks/archive/`, and the conversation read as one-sided to the other
    party because the answer existed on disk but was never sent. Writing a
    result is not answering a task, and until now nothing noticed the
    difference — `check_task_queue` watches the inbound side only, so a queue
    that drains perfectly can still be losing every late reply.

    Scope is deliberately narrow:
      * top-level `task-*.txt` only. `<channel-key>.task-<id>.txt` is the pull
        namespace, claimed by a consumer that did not delegate the work (e.g.
        the phone conversation-server), so its lifetime is not ours to judge.
      * `question-*` / `proactive-*` have their own delivery lifecycles.
      * age-gated, because between our write and the consumer's claim the task
        is legitimately still present for a few seconds.
    """
    name = "orphaned-results"
    results_dir = WORKSPACE_DIR / "results"
    tasks_dir = WORKSPACE_DIR / "tasks"
    if not results_dir.exists():
        return {"name": name, "status": "ok", "detail": "results/ not yet created"}
    now = time.time()
    try:
        entries = list(results_dir.glob("task-*.txt"))
    except OSError as e:  # noqa: BLE001 — a probe failure must not fail the check
        return {"name": name, "status": "warn", "detail": f"could not scan results/: {e}"}
    orphans: list[tuple[str, int]] = []
    unreadable = 0
    for path in entries:
        # Per-file isolation on purpose. One unreadable entry must not decide
        # the answer for the whole directory: with the guard around the loop
        # instead, a single EACCES/EIO aborted the scan and any real orphan
        # sitting beside it went unreported. Note pathlib only swallows a
        # specific errno set (ENOENT/ENOTDIR/EBADF/ELOOP), so `is_file()` does
        # raise for the rest and belongs inside the guard too.
        try:
            if not path.is_file():
                continue
            age = now - path.stat().st_mtime
        except OSError:
            unreadable += 1
            continue
        if age < threshold_age_sec:
            continue
        # Task still present -> the consumer has not reached this pair yet.
        #
        # Must ask "does a task with this id exist", NOT "is there a file with
        # this exact name". `claim_task.py` renames a claimed task to
        # `task-<id>.claimed-core-N.txt`, so a bare-name test reports a LIVE,
        # in-flight task as archived — a valid retrying delivery raising the
        # same signal as a genuinely stranded reply, which is how a detector
        # trains its readers to ignore it. `find_task_file()` is the canonical
        # locator (it is what the bridge archive paths already use).
        try:
            task_path = find_task_file(tasks_dir, path.stem)
        except OSError:
            # The locator itself stats the path, so it raises for the same
            # reasons the age read does; both are partial coverage, not clean.
            unreadable += 1
            continue
        if task_path is not None:
            # A CLAIMED task is owned by a running consumer; its lifetime is
            # not ours to judge, however long it takes.
            if ".claimed-core-" in task_path.name:
                continue
            try:
                task_age = now - task_path.stat().st_mtime
            except OSError:
                # Same treatment as an unreadable result entry: a measurement we
                # could not take is partial coverage, never a silent clean pass.
                unreadable += 1
                continue
            # A task no bridge created is never collected, so an unclaimed
            # pair past the threshold is stranded rather than in flight.
            if task_age < threshold_age_sec:
                continue
        # A declared skip was never going to be delivered. `[deduped:]` stays
        # reported: it PROMISES delivery elsewhere, so stranding it loses a reply.
        try:
            from result_markers import parse_markers  # noqa: PLC0415
            skips = {a.value for a in parse_markers(path.read_text()).actions
                     if a.kind == "skip"}
        except (OSError, ValueError, ImportError):
            skips = set()          # unreadable -> judge it as before, never silently clear
        if skips & {"no-send", "REPLIED"}:
            continue
        orphans.append((path.name, int(age)))
    # Coverage is part of the verdict: say what could not be measured rather
    # than let it round down into a clean result.
    partial = f" ({unreadable} entr{'y' if unreadable == 1 else 'ies'} unreadable)" if unreadable else ""
    if not orphans:
        status = "warn" if unreadable else "ok"
        return {"name": name, "status": status,
                "detail": f"no undeliverable results{partial}"}
    orphans.sort(key=lambda item: -item[1])
    oldest_name, oldest_age = orphans[0]
    return {
        "name": name,
        "status": "warn",
        "detail": (f"{len(orphans)} result(s) with no consumer coming — never delivered; "
                   f"oldest {oldest_name} ({oldest_age // 3600}h{oldest_age % 3600 // 60}m){partial}"),
    }


def check_stranded_destined_proactive() -> dict:
    """A destined proactive file no bridge will claim, named while it can still act.

    The destination grammar (proactive_routing) has a two-tier rule: a known
    `.to-<channel>` tag is claimed only by that bridge; an unrecognized tag is
    claimed by NO bridge — deliberate (a file aimed at a channel this install
    lacks must never fall into another bridge's race), but without a reader
    the strand is silent, and unlike the pre-fix activity strand it cannot
    self-heal: the tag never becomes recognized. Same principle as
    check_proactive_quarantine — preservation without a reader reaches no one
    — in the directory that probe deliberately does not scan.

    Ages past _STRAND_MIN_AGE_S only: a destined file's target bridge may
    legitimately be seconds from its next poll.
    """
    name = "stranded-destined-proactive"
    results = WORKSPACE_DIR / "results"
    if not results.is_dir():
        return {"name": name, "status": "ok", "detail": "no results/ directory"}
    try:
        sys.path.insert(0, str(REPO_DIR / "src"))
        from presenter_mode import presenter_mode_active
        from proactive_routing import PROACTIVE_DESTINATIONS, proactive_destination
    except ImportError as e:
        return {"name": name, "status": "warn",
                "detail": f"could not load proactive_routing: {e}"}
    # Presenter mode intentionally holds ALL proactive deliveries — an aged
    # destined file is the bridge waiting for the talk to end, not a strand.
    if presenter_mode_active(WORKSPACE_DIR):
        return {"name": name, "status": "ok",
                "detail": "presenter mode active — deliveries intentionally held"}
    now = time.time()
    stranded: list[tuple[str, str, int]] = []
    for path in results.glob("proactive-*.txt"):
        dest = proactive_destination(path.name)
        if dest is None:
            continue  # undestined: activity routing owns it, self-heals
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < _STRAND_MIN_AGE_S:
            continue
        kind = ("unrecognized destination" if dest not in PROACTIVE_DESTINATIONS
                else f"destination '{dest}' bridge not claiming")
        stranded.append((path.name, kind, int(age)))
    if not stranded:
        return {"name": name, "status": "ok",
                "detail": "no aged destined proactive files awaiting a claim"}
    # Unrecognized tags outrank age in the headline: that class never
    # self-heals, so it must be the one a human sees first.
    stranded.sort(key=lambda item: (not item[1].startswith("unrecognized"), -item[2]))
    fname, kind, age = stranded[0]
    unrec = sum(1 for item in stranded if item[1].startswith("unrecognized"))
    mix = (f"{unrec} with an unrecognized tag, "
           f"{len(stranded) - unrec} with an unclaimed known destination — "
           if 0 < unrec < len(stranded) else "")
    return {
        "name": name,
        "status": "warn",
        "detail": (f"{len(stranded)} destined proactive file(s) older than "
                   f"{_STRAND_MIN_AGE_S // 60}m that no bridge has claimed — "
                   f"{mix}worst {fname} ({kind}, {age // 3600}h{age % 3600 // 60}m); "
                   f"an unrecognized .to-<tag> never self-heals: fix the tag or "
                   f"start the named bridge"),
    }


_STRAND_MIN_AGE_S = 1800


def check_proactive_quarantine() -> dict:
    """Report proactive bodies that were SAVED from deletion and then forgotten.

    `poll_proactive` used to `unlink()` a DM that Discord rejected, so a message
    the owner never saw was also destroyed — observed live here as
    `413 Payload Too Large (error code: 40005)`. The fix (#2626) moves the body
    to `results/undelivered/` instead of deleting it, which is strictly better
    but left the body with no consumer: a scan of the whole tree at that
    change's head finds the writer and **no reader at all**. The nearest
    candidate, `friction-detector.check_stale_results()`, is a stub that returns
    `[]` and is never called.

    That is the shape this probe exists to close, which makes it the reader —
    so "nobody has been told" stopped being true the moment this shipped, and
    saying it here or in what this probe emits invites the output to be quoted
    back as independent evidence that no reader exists. What stays true is
    narrower: nothing drains or re-drives the directory, so a preserved body
    reaches no one until someone acts. Warning is not delivering.

    Deliberately NOT a failure: quarantine is the correct end state for a body
    Discord will never accept (a 413 never becomes a 200). The action is for a
    human to read it and decide, so `warn` — visible, not alarming.

    Silent before #2626 lands: the directory is created only by that writer, so
    an absent dir reports ok rather than inventing a problem.
    """
    name = "proactive-quarantine"
    quarantine = WORKSPACE_DIR / "results" / "undelivered"
    if not quarantine.is_dir():
        return {"name": name, "status": "ok",
                "detail": "no quarantined proactive bodies (undelivered/ absent)"}
    now = time.time()
    try:
        entries = list(quarantine.iterdir())
    except OSError as e:  # noqa: BLE001 — a probe failure must not fail the check
        return {"name": name, "status": "warn",
                "detail": f"could not scan results/undelivered/: {e}"}
    kept: list[tuple[str, int]] = []
    unreadable = 0
    for path in entries:
        # Per-file isolation, same reason as check_orphaned_results: one
        # unreadable entry must not decide the answer for the directory.
        try:
            if not path.is_file():
                continue
            age = now - path.stat().st_mtime
        except OSError:
            unreadable += 1
            continue
        kept.append((path.name, int(age)))
    partial = (f" ({unreadable} entr{'y' if unreadable == 1 else 'ies'} unreadable)"
               if unreadable else "")
    if not kept:
        status = "warn" if unreadable else "ok"
        return {"name": name, "status": status,
                "detail": f"no quarantined proactive bodies{partial}"}
    kept.sort(key=lambda item: -item[1])
    oldest_name, oldest_age = kept[0]
    return {
        "name": name,
        "status": "warn",
        "detail": (f"{len(kept)} proactive message(s) kept in results/undelivered/ that Discord "
                   f"refused — preserved, but no consumer drains this directory, so they stay "
                   f"until someone acts; oldest {oldest_name} "
                   f"({oldest_age // 3600}h{oldest_age % 3600 // 60}m)"
                   f"{partial}"),
    }


def _ps_snapshot() -> "str | None":
    """One `ps -Ao pid,ppid,args` for callers classifying several pids at once.

    None means UNAVAILABLE, and that includes a `ps` that returned normally with
    a nonzero exit: its empty stdout would otherwise read as a successful scan
    that found nothing, which is the absence callers must not assert.
    """
    try:
        done = subprocess.run(["ps", "-Ao", "pid,ppid,args"],
                              capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001
        return None
    return done.stdout if done.returncode == 0 else None


def _pid_parent(pid: "str | int", ps_output: "str | None" = None) -> "str | None":
    """PPID of `pid` as a string, or None if it cannot be read."""
    if ps_output is None:
        try:
            out = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            return out.split()[0] if out else None
        except Exception:  # noqa: BLE001
            return None
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == str(pid):
            return parts[1]
    return None


def _proc_argv(pid: int) -> str:
    """argv of `pid`, or "" if no such process.

    Deliberately `ps -p <pid>`, NOT `pgrep -f watch-tasks-stream`: pgrep's
    `-f` matches the search string against full argv, and the calling shell's
    own argv contains that string — a transient self-match that returns a PID
    already gone by the next `ps` (the anti-pattern documented in
    /schedule-crons step 5 and /proactive-loop step 9). Inspecting one known
    PID cannot self-match.
    """
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 — a probe failure must not fail the check
        return ""


def _age_hm(age_sec: int) -> str:
    return f"{age_sec // 3600}h{age_sec % 3600 // 60}m"


def check_stale_proactive_backlog(threshold_age_sec: int = 3600,
                                  claim_threshold_age_sec: int = 7200) -> dict:
    """A claim is a body too: `with_suffix(".sending")` REPLACES `.txt`, the one
    shape a `*.txt` glob cannot see — and the startup sweep restores only it."""
    name = "stale-proactive-backlog"
    results_dir = WORKSPACE_DIR / "results"
    if not results_dir.exists():
        return {"name": name, "status": "ok", "detail": "results/ not yet created"}
    now = time.time()
    try:
        # scandir, not glob: glob swallows a directory-level EACCES/EIO and
        # yields nothing, so an unscannable results/ would report a clean one.
        entries = [results_dir / e.name for e in os.scandir(results_dir)
                   if e.name.startswith("proactive-")]
    except OSError as exc:
        return {"name": name, "status": "warn",
                "detail": f"could not scan results/: {exc}"}
    stale, abandoned, unreadable = [], [], 0
    for path in entries:
        # Decide by suffix, and skip any shape neither side of the protocol
        # writes rather than guessing what it meant.
        if path.suffix == ".txt":
            bucket, limit = stale, threshold_age_sec
        elif path.suffix == ".sending":
            bucket, limit = abandoned, claim_threshold_age_sec
        else:
            continue
        # Per-file isolation, as in check_orphaned_results: one unreadable entry
        # must not decide the answer for the directory.
        try:
            if not path.is_file():
                continue
            age = now - path.stat().st_mtime
        except OSError:
            unreadable += 1
            continue
        if age < limit:
            continue
        bucket.append((path.name, int(age)))
    partial = f" ({unreadable} entr{'y' if unreadable == 1 else 'ies'} unreadable)" if unreadable else ""
    if not stale and not abandoned:
        status = "warn" if unreadable else "ok"
        return {"name": name, "status": status,
                "detail": f"no undelivered proactive bodies{partial}"}
    parts = []
    if stale:
        stale.sort(key=lambda item: -item[1])
        oldest_name, oldest_age = stale[0]
        parts.append(f"{len(stale)} proactive body(ies) nobody has delivered; "
                     f"oldest {oldest_name} ({_age_hm(oldest_age)}); "
                     f"deliver or remove them — nothing clears these on its own")
    if abandoned:
        abandoned.sort(key=lambda item: -item[1])
        oldest_name, oldest_age = abandoned[0]
        parts.append(f"{len(abandoned)} claimed body(ies) abandoned mid-send; "
                     f"oldest {oldest_name} ({_age_hm(oldest_age)}); "
                     f"restart a consumer to run its startup .sending sweep")
    return {"name": name, "status": "warn", "detail": "; ".join(parts) + partial}


# The argv must BE the script invocation, not merely mention it. A substring
# test counts the observer: any shell whose command line contains the name —
# a `ps | grep watch-tasks-stream`, or the wrapper running this very check —
# matches, and each one reads as another watcher. Observed 2026-07-21: a loose
# match reported 3 trees where 2 were real, the phantom being the shell that
# ran the query. Same family as the pgrep self-match noted in _proc_argv; the
# fix is to anchor on the whole command rather than search inside it.
_WATCHER_SHELLS = ("sh", "bash", "zsh", "ksh")


def _is_watcher_argv(argv: str) -> bool:
    """True only for `<shell> <path>/watch-tasks-stream.sh` and nothing more."""
    parts = argv.split()
    if len(parts) != 2:
        return False
    exe, script = parts
    return (exe.rsplit("/", 1)[-1] in _WATCHER_SHELLS
            and script.endswith("watch-tasks-stream.sh"))


def _watcher_trees(ps_output: "str | None" = None) -> dict:
    """Map root PID -> set of PIDs for each distinct watcher TREE running.

    Each watcher is several processes (a shell wrapper, the script, a
    subshell), so counting matching lines overcounts. A "root" is a match
    whose parent is not itself a match — one per independent watcher. Callers
    need the whole tree, not just the root, to tell which tree owns the
    sentinel PID (the sentinel records the script's PID, not the wrapper's).

    `ps -Ao` + filtering here rather than `pgrep -f watch-tasks-stream`, for
    the reason in _proc_argv: pgrep would match the caller. Our own argv is
    the health-check invocation, but we drop it explicitly anyway.
    """
    if ps_output is None:
        try:
            ps_output = subprocess.run(["ps", "-Ao", "pid,ppid,args"],
                                       capture_output=True, text=True,
                                       timeout=5).stdout
        except Exception:  # noqa: BLE001
            return {}
    me = str(os.getpid())
    parent = {}
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0] == me:
            continue
        if not _is_watcher_argv(parts[2]):
            continue
        parent[parts[0]] = parts[1]
    trees: dict = {}
    for pid in parent:
        root, seen = pid, set()
        while parent.get(root) in parent and root not in seen:
            seen.add(root)
            root = parent[root]
        trees.setdefault(root, set()).add(pid)
    return trees


def check_task_watcher() -> dict:
    """Direct liveness of the streaming task watcher (src/watch-tasks-stream.sh).

    The two consequence checks above cannot see a dead watcher on their own:
    `check_task_queue` needs BOTH >3 tasks AND >300s age, so a watcher that
    dies against an empty (or small) queue reads green, and a single stranded
    owner DM never trips the count threshold at all; `check_core_proactive_loop`
    reads core-status.json, which the proactive loop writes every pass — it is
    freshest exactly when the loop is alive and the watcher is not. Observed
    2026-07-21: watcher dead, queue empty, health-check reported 0 failures.

    So this is the direct signal to pair with them, read from the PID sentinel
    the watcher maintains itself (written at startup, removed by its cleanup
    trap on clean exit) — present-but-dead therefore means "crashed", absent
    means "not running".

    Gated on `_any_core_alive()`: with no core running, no watcher is expected
    and warning here would latch on permanently for anyone not currently
    running Sutando — a check that is always red carries the same information
    as one that is always green.
    """
    name = "task-watcher"
    pid_file = WORKSPACE_DIR / "state" / "watch-tasks-stream.pid"
    # `_watcher_trees()` returns {} for BOTH a clean empty scan and a failed ps,
    # so take the snapshot here: None is unavailable, "" is genuinely empty.
    ps_out = _ps_snapshot()
    if ps_out is None:
        return {"name": name, "status": "warn",
                "detail": "cannot enumerate processes (ps unavailable) — watcher liveness is "
                          "UNKNOWN, not clear; a dead or duplicate watcher would be invisible"}
    # "Not expected" is a claim about THIS host, so it needs the local heartbeat
    # and must not be made before enumerating: visible watchers outrank it.
    trees = _watcher_trees(ps_out)
    roots = sorted(trees)
    if not roots and _fresh_local_core_record() is None:
        return {"name": name, "status": "ok",
                "detail": "no local core running and no watcher processes — watcher not expected"}
    if not pid_file.exists():
        if roots:
            # Sentinel gone but watchers alive: they are draining tasks/ but
            # nothing supervises them, and each new start adds another (observed
            # 2026-07-21: two trees, both reporting the same TASK_FILE — i.e.
            # duplicate processing, not a stalled queue).

            # A KNOWN parent that is not init: its spawning session still owns it.
            # Unknown parentage cannot support that claim, so it stays an orphan.
            parents = {r: _pid_parent(r, ps_out) for r in roots}
            supervised = [r for r, pp in parents.items() if pp and pp != "1"]
            if len(roots) == 1 and supervised:
                # Its session is still its parent, so it IS supervised and there is
                # no second tree to duplicate work. Killing it is what opens a gap.
                # `--fix` re-stamps the sentinel instead: the pid is a live watcher,
                # so naming it restores Stop-hook cleanup without the restart.
                return {"name": name, "status": "warn",
                        "_sentinel_restamp_pid": roots[0],
                        "detail": f"watcher pid {roots[0]} runs under a live session "
                                  f"(ppid {parents[roots[0]]}) but wrote no PID "
                                  "sentinel, so health-check cannot track it. Do NOT stop it — "
                                  "it IS draining tasks/. Re-stamp the sentinel with --fix, or "
                                  "restart cleanly only when tasks/ is empty."}
            return {"name": name, "status": "warn",
                    "detail": f"{len(roots)} orphaned watcher(s) running with no PID sentinel "
                              f"(pids {', '.join(roots)}) — draining tasks/ unsupervised; "
                              "stop them and restart one cleanly"}
        return {"name": name, "status": "warn",
                "detail": "watcher not running (no PID sentinel) — tasks/ will not be drained; "
                          "restart via Monitor: bash src/watch-tasks-stream.sh"}
    try:
        pid = int(pid_file.read_text().strip())
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": "warn",
                "detail": f"unreadable PID sentinel ({str(e)[:40]}) — restart the watcher"}
    argv = _proc_argv(pid)
    if not argv:
        if roots:
            # The sentinel tracks only the MOST RECENT start, so a dead one does
            # NOT mean nothing drains tasks/ — restarting here makes duplicates.
            return {"name": name, "status": "warn",
                    "detail": f"sentinel pid {pid} is dead but {len(roots)} watcher(s) still run "
                              f"(pids {', '.join(roots)}) — orphaned, tasks/ IS being drained; "
                              "stop them and restart one cleanly"}
        return {"name": name, "status": "warn",
                "detail": f"watcher pid {pid} is dead (crashed — sentinel left behind); restart it"}
    if "watch-tasks-stream" not in argv:
        # PID reuse: the sentinel outlived the watcher and the OS handed the
        # number to something else. `kill -0` alone would call this alive.
        return {"name": name, "status": "warn",
                "detail": f"pid {pid} is not the watcher (PID reuse): {argv[:60]}"}
    extras = sorted(r for r, members in trees.items() if str(pid) not in members)
    if extras:
        return {"name": name, "status": "warn",
                "detail": f"{len(trees)} watcher trees running — {len(extras)} not tracked by the "
                          f"sentinel (root pids {', '.join(extras)}); duplicates process each task "
                          f"more than once. Keep the sentinel's ({pid}), stop the rest"}
    return {"name": name, "status": "ok", "detail": f"streaming watcher alive (pid {pid})"}


#: Track session-worker.py's own SUTANDO_TIER_HARD_TIMEOUT (default 900s,
#: configurable): a constant pages on live work the moment a deploy raises it.
_TASK_CLAIM_HARD_TIMEOUT_DEFAULT_S = 900.0
_TASK_CLAIM_WARN_MULTIPLE = 2
_TASK_CLAIM_DOWN_MULTIPLE = 8


def _worker_hard_timeout_s() -> float:
    """session-worker.py's own per-run deadline (`SUTANDO_TIER_HARD_TIMEOUT`).

    Non-positive or unparseable makes the worker refuse to start
    (session-worker.py:249-252), so the default is the only value a RUNNING
    worker can hold — falling back to it never pages a permitted run.
    """
    raw = os.environ.get("SUTANDO_TIER_HARD_TIMEOUT", "")
    try:
        hard = float(raw)
    except (TypeError, ValueError):
        return _TASK_CLAIM_HARD_TIMEOUT_DEFAULT_S
    return hard if hard > 0 else _TASK_CLAIM_HARD_TIMEOUT_DEFAULT_S


def _task_claim_thresholds() -> tuple[float, float]:
    """(warn, down) seconds from the handler's configured hard timeout."""
    hard = _worker_hard_timeout_s()
    return hard * _TASK_CLAIM_WARN_MULTIPLE, hard * _TASK_CLAIM_DOWN_MULTIPLE


def _pid_alive(pid: int) -> bool:
    """Signal 0 probes existence without delivering anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True           # exists, owned by someone else
    except (OverflowError, ValueError, OSError):
        return False
    return True


def _claim_owner(path: Path):
    """(owner_pid, disposition, task_path) from a claim file; any may be None.

    Format is the shell writer's: pid, watcher id, task path, disposition.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None, None, None
    pid = None
    if lines and lines[0].strip().isdigit():
        pid = int(lines[0].strip())
    task_path = lines[2].strip() if len(lines) > 2 else None
    disposition = lines[3].strip() if len(lines) > 3 else None
    return pid, disposition, task_path


# Archive-then-release is a normal asynchronous transition, so an absent task
# only condemns a claim once it is older than the slowest such handoff.
_TASK_CLAIM_ARCHIVE_GRACE_S = 300


def _claim_observations_path(workspace_dir: Path) -> Optional[Path]:
    """Unsynced first-sighting store, or None. Anchored on the ENGINE checkout, never the
    workspace, whose state/ is refreshed by the same sync that refreshes claim mtime."""
    engine = REPO_DIR.resolve()          # the module already resolves its own repo
    try:
        # rev-parse, not `engine/".git"`: in a WORKTREE .git is a file.
        out = subprocess.run(git_argv("rev-parse", "--git-dir"), cwd=str(engine),
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        git_dir = (engine / out.stdout.strip()).resolve()
    except Exception:
        return None
    if not git_dir.is_dir():
        return None
    # Keyed by workspace identity so two workspaces on one host cannot share
    # (and cross-contaminate) a ledger.
    key = hashlib.sha1(str(Path(workspace_dir).resolve()).encode()).hexdigest()[:12]
    return git_dir / "sutando-claim-observations" / f"{key}.json"


def _claim_ages(entries: list, workspace_dir: Path, now: float) -> tuple:
    """({name: seconds since first local sighting}, trusted). trusted=False means no
    unsynced store exists, so the caller must treat age as unavailable, not as mtime."""
    path = _claim_observations_path(workspace_dir)
    if path is None:
        return {}, False
    lock = path.with_suffix(".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return {}, False
    fd = None
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            seen = json.loads(path.read_text())
            if not isinstance(seen, dict):
                seen = {}
        except FileNotFoundError:
            seen = {}
        except Exception:
            # Corrupt: a reset would reseed every claim from sync-mutable mtime
            # and suppress exactly what this exists to catch.
            return {}, False
        live = {e.name for e in entries}
        seen = {k: v for k, v in seen.items() if k in live and isinstance(v, (int, float))}
        ages = {}
        for e in entries:
            first = seen.get(e.name)
            if first is None:
                try:
                    first = e.stat().st_mtime
                except OSError:
                    continue
                seen[e.name] = first
            ages[e.name] = max(0.0, now - first)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(seen, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return ages, True
    except Exception:
        return {}, False
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except Exception:
                pass


def check_a_fallback_hits(workspace_dir: Optional[Path] = None) -> dict:
    """A dual_read hit means C could not resolve an id the importer should have
    covered — a finding, and the gate that blocks Design A's deletion."""
    from datetime import datetime
    name = "a-fallback-hits"
    results = Path(workspace_dir or WORKSPACE_DIR) / "results"
    hits = []
    migrated = 0
    for root in sorted(results.glob(".outbox*")):
        counter = root / "a-fallback-hits.json"
        if (root / ".items-migrated").is_dir():
            migrated += 1
            if not counter.is_file():
                # Absence of evidence, not evidence of absence: a gate must not
                # pass on an instrument that never ran (dual_read inits count 0).
                hits.append(f"{root.name}: migrated but no counter — dual_read "
                            "never ran here (instrument absent, not a measured zero)")
                continue
        if not counter.is_file():
            continue
        try:
            rec = json.loads(counter.read_text(encoding="utf-8"))
            if not isinstance(rec, dict):
                raise ValueError("counter is not a JSON object")
            count = int(rec.get("count", 0))
        except (OSError, ValueError, TypeError):
            hits.append(f"{root.name}: counter unreadable")
            continue
        if count > 0:
            # fromtimestamp raises on domain garbage (inf/nan/1e30) that
            # isinstance passes; a probe on the gate path must never raise.
            try:
                when_s = datetime.fromtimestamp(
                    rec.get("last_hit_ts")).strftime("%m-%d %H:%M")
            except (TypeError, ValueError, OSError, OverflowError):
                when_s = "?"
            hits.append(f"{root.name}: {count} hit(s), last {rec.get('last_item', '?')} at {when_s}")
    if hits:
        return {"name": name, "status": "warn",
                "detail": "A-fallback (dual_read) HIT — importer coverage gap or "
                          "phantom id; A deletion stays gated until a full release "
                          "records zero new hits: " + "; ".join(hits)}
    if migrated:
        return {"name": name, "status": "ok",
                "detail": f"no fallback hits — {migrated} migrated root(s), "
                          "each with a live counter (measured zero)"}
    return {"name": name, "status": "ok",
            "detail": "no migrated outbox roots — dual-read window not active"}


def check_task_claim_age(workspace_dir: Optional[Path] = None) -> dict:
    """A claim whose task file still exists is queued or running, so no age condemns it;
    one whose task is archived is leaked at any age. Not a --fix: release may drop work."""
    name = "task-claims"
    base = Path(workspace_dir or WORKSPACE_DIR) / "state" / "task-event-handler-claims"
    if not base.is_dir():
        return {"name": name, "status": "ok",
                "detail": "no claims directory — task-event handler never dispatched on this host"}
    now = time.time()
    warn_s, down_s = _task_claim_thresholds()
    bounded, leaked, in_flight, held = [], [], 0, 0
    entries = list(base.glob("task-*.txt"))
    # Ages come from a LOCAL first-sighting; mtime is sync-mutable. An untrusted
    # ledger degrades only the AGE-DEPENDENT decisions, so keep scanning.
    ages, age_trusted = _claim_ages(entries, Path(workspace_dir or WORKSPACE_DIR), now)
    age_unknown = 0
    for entry in entries:
        age = ages.get(entry.name)
        if age is None and age_trusted:
            continue          # raced with a release; the next run sees the truth
        held += 1
        pid, disposition, task_path = _claim_owner(entry)
        # The task file is the progress signal: while it exists the work is queued
        # or running, and 2 workers legitimately hold several claims in a burst.
        task_live = bool(task_path) and Path(task_path).exists()
        # Age-INDEPENDENT and definitive, so it is checked FIRST: nothing can
        # release a claim whose owner is gone, whatever the ledger says.
        if pid is not None and not _pid_alive(pid):
            leaked.append((age, entry.name, "owner process gone"))
        elif not task_live:
            # Archival and claim release are ASYNCHRONOUS; only past that window
            # is an absent task evidence that nothing will release the claim.
            if age is None:
                age_unknown += 1          # grace needs an age we do not have
            elif age >= _TASK_CLAIM_ARCHIVE_GRACE_S:
                leaked.append((age, entry.name, "task already archived"))
            else:
                in_flight += 1
        elif disposition == "must-handle":
            if age is None:
                age_unknown += 1          # the bound is an age comparison
            else:
                bounded.append((age, entry.name))
        else:
            in_flight += 1

    if not held:
        return {"name": name, "status": "ok", "detail": "no held task-handler claims"}

    if leaked:
        # Unknown first — nothing can vouch for it — then genuinely oldest. The
        # negation matters: a plain ascending key names the YOUNGEST leak "oldest".
        leaked.sort(key=lambda r: (r[0] is not None, -(r[0] or 0.0)))
        age, who, why = leaked[0]
        oldest = ("age unknown" if age is None
                  else f"oldest {age:.0f}s" if age < 3600
                  else f"oldest {age / 3600:.1f}h")
        return {"name": name, "status": "down",
                "detail": f"{len(leaked)} of {held} held claim(s) leaked — {why}, "
                          f"{oldest} ({who}); nothing will release "
                          "them, and each publishes a user-visible failure when the "
                          "watcher next exits"}
    if bounded:
        bounded.sort(reverse=True)
        age, who = bounded[0]
        detail = (f"{held} held claim(s); oldest bounded {age / 3600:.1f}h ({who}); "
                  f"handler bound {warn_s / _TASK_CLAIM_WARN_MULTIPLE / 60:.0f}m")
        if age > down_s:
            return {"name": name, "status": "down",
                    "detail": f"{detail} — past its own hard timeout; leaked"}
        if age > warn_s:
            return {"name": name, "status": "warn",
                    "detail": f"{detail} — past {_TASK_CLAIM_WARN_MULTIPLE}x the handler's own bound"}
    if age_unknown:
        return {"name": name, "status": "warn",
                "detail": f"{held} held claim(s); no leak or dead owner found, but "
                          f"claim AGE is unavailable for {age_unknown} of them (no "
                          "trusted observation store), so stale cannot be told from "
                          "fresh for those"}
    # Reaching here with a bounded claim means it is inside its handler bound, so
    # it is running; counting only in_flight renders working claims as "0 running".
    return {"name": name, "status": "ok",
            "detail": f"{held} held claim(s), {in_flight + len(bounded)} still queued "
                      "or running; none leaked and none past its own execution contract"}


def fix_task_watcher_sentinel(check: dict) -> str:
    """Re-stamp the PID sentinel for a supervised watcher that lost it (--fix).

    Only the single-supervised-tree case is repairable: that pid IS a live
    watcher whose session still owns it, so naming it restores Stop-hook
    cleanup and probe tracking. The alternative the warning offers is a
    restart, which strands tasks/ mid-drain.
    """
    pid = str(check.get("_sentinel_restamp_pid") or "")
    if not pid.isdigit():
        return "no re-stampable watcher pid"
    pid_file = WORKSPACE_DIR / "state" / "watch-tasks-stream.pid"
    # Re-measure before writing: the check ran earlier, and this file is what
    # the Stop hook kills.
    if not _is_watcher_argv(_proc_argv(int(pid))):
        return f"pid {pid} is no longer the watcher — not re-stamped"
    try:
        # Separate try: mkdir raises FileExistsError when state/ is a plain
        # file, which is a write failure, not a competing claim.
        pid_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"could not write {pid_file}: {e}"
    try:
        # Exclusive create, not exists()-then-write: only the OS can arbitrate
        # against a watcher claiming the sentinel in the same instant.
        with open(pid_file, "x") as fh:
            fh.write(f"{pid}\n")
    except FileExistsError:
        return "a watcher re-claimed the sentinel — left its file alone"
    except OSError as e:
        return f"could not write {pid_file}: {e}"
    # The probe above was a snapshot taken BEFORE publication; retract our own
    # stamp if it went stale mid-write.
    if not _is_watcher_argv(_proc_argv(int(pid))):
        try:
            # Read-then-unlink, NOT arbitrated the way the write above is:
            # POSIX has no conditional unlink, so a claim landing here is lost.
            if pid_file.read_text().strip() == pid:
                pid_file.unlink()
        except OSError as e:
            # Reporting a withdrawal that did not happen is the same class of
            # lie as the stale stamp; the operator needs the real state.
            return (f"pid {pid} stopped being the watcher mid-write and the "
                    f"stamp could not be withdrawn: {e}")
        return f"pid {pid} stopped being the watcher mid-write — sentinel withdrawn"
    return f"re-stamped the sentinel for live watcher pid {pid}"


def apply_task_watcher_sentinel_fix(checks: list, stream=None) -> None:
    """--fix dispatch for task-watcher: warn-level, so it never reaches the
    issues loop and needs its own pass (same shape as the skill-symlinks one).

    `stream` is where the repair line goes; a caller emitting JSON on stdout
    MUST pass sys.stderr. The check is RE-RUN rather than assumed repaired —
    a fixer's self-report is not evidence of the resulting state.
    """
    out = stream if stream is not None else sys.stdout
    for c in checks:
        if c["name"] == "task-watcher" and c.get("_sentinel_restamp_pid"):
            print(f"  {c['name']}: {fix_task_watcher_sentinel(c)}", file=out)
            fresh = check_task_watcher()
            c.clear()
            c.update(fresh)


def _fresh_local_core_record(
    workspace: "Optional[Path]" = None,
    max_age_s: float = 90.0,
) -> "dict | None":
    """Return this host's fresh core heartbeat, or None.

    `_any_core_alive()` deliberately accepts remote/shared-workspace heartbeats.
    The notifier is a local tmux process, so using that fleet-wide signal here
    could make one host try to inspect or repair another host's session.
    """
    if workspace is None:
        workspace = WORKSPACE_DIR
    alive_file = workspace / "state" / "cores" / f"{_host_label()}.alive"
    try:
        if not heartbeat_is_fresh(alive_file.stat().st_mtime, time.time(), max_age_s):
            return None
        record = json.loads(alive_file.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _run_tmux(socket_path: str, *args: str):
    """Run one bounded tmux probe against a runtime-authored socket."""
    try:
        return subprocess.run(
            [_resolve_tmux_bin(), "-S", socket_path, *args],
            env=_resolve_launch_env(),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 — probe failures become an absent target
        return None


def _codex_runtime_selected() -> bool:
    """Whether config currently selects Codex (fail closed on invalid config)."""
    try:
        return resolve_core_runtime(REPO_DIR) == "codex"
    except Exception:  # noqa: BLE001 — config check reports the underlying error
        return False


def _local_codex_runtime_target(
    heartbeat: "dict | None" = None,
) -> "dict | None":
    """Resolve a locally recorded Codex target without acting on it."""
    if heartbeat is None:
        heartbeat = _fresh_local_core_record()
    if heartbeat is None:
        return None
    socket_path = heartbeat.get("socket")
    if not isinstance(socket_path, str) or not socket_path:
        return None
    try:
        runtime_state = json.loads(
            (WORKSPACE_DIR / "state" / "core-runtime.json").read_text()
        )
    except (OSError, ValueError):
        return None
    if not isinstance(runtime_state, dict) or runtime_state.get("runtime") != "codex":
        return None
    session = runtime_state.get("session")
    if not isinstance(session, str) or not session:
        return None
    return {"socket": socket_path, "session": session}


def _local_codex_core_target(target: "dict | None" = None) -> "dict | None":
    """Resolve and verify the local Codex core's socket and exact session.

    The heartbeat supplies the socket, `core-runtime.json` supplies the session,
    and tmux's own session environment confirms that the live pane is Codex.
    Requiring all three makes the repair path fail closed on stale/config-drift
    state instead of accidentally starting or replacing a different core.
    """
    if target is None:
        target = _local_codex_runtime_target()
    if target is None:
        return None
    socket_path = target["socket"]
    session = target["session"]

    exists = _run_tmux(socket_path, "has-session", "-t", f"={session}")
    if exists is None or exists.returncode != 0:
        return None
    runtime = _run_tmux(
        socket_path,
        "show-environment",
        "-t",
        f"={session}",
        "SUTANDO_CORE_RUNTIME",
    )
    if (
        runtime is None
        or runtime.returncode != 0
        or runtime.stdout.strip() != "SUTANDO_CORE_RUNTIME=codex"
    ):
        return None
    return target


def _expected_codex_notifier_entrypoint() -> Path:
    """Entrypoint for the checkout's notifier topology.

    PR #2280 adds a supervising entrypoint. Accept the direct notifier on
    versions before that change and require the supervisor once it exists, so
    rolling upgrades neither false-alarm nor silently keep obsolete topology.
    """
    supervisor = (
        REPO_DIR / "src" / "agent" / "codex" / "cli" / "task-notifier-supervisor.sh"
    )
    if supervisor.exists():
        return supervisor
    return REPO_DIR / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"


def _command_runs_script(command: str, expected: Path) -> bool:
    """Whether tmux started the expected script as the actual command.

    `pane_start_command` is shell-quoted text. Tokenize it instead of searching
    the raw string: an unrelated wrapper may mention the expected path in an
    argument, suffix, or comment without running it.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    expected_text = str(expected)
    if argv[0] == expected_text:
        return True
    if Path(argv[0]).name != "bash":
        return False
    script_index = 2 if len(argv) > 1 and argv[1] == "--" else 1
    return len(argv) > script_index and argv[script_index] == expected_text


def _probe_codex_task_notifier(target: dict) -> dict:
    """Inspect the exact managed notifier tmux session for one healthy pane."""
    name = "codex-task-notifier"
    socket_path = target["socket"]
    watcher_session = f"{target['session']}-watcher"
    exists = _run_tmux(socket_path, "has-session", "-t", f"={watcher_session}")
    if exists is None or exists.returncode != 0:
        return {
            "name": name,
            "status": "warn",
            "detail": f"managed tmux session {watcher_session!r} is missing",
        }
    panes = _run_tmux(
        socket_path,
        "list-panes",
        "-t",
        f"={watcher_session}",
        "-F",
        "#{pane_dead}\t#{pane_start_command}",
    )
    if panes is None or panes.returncode != 0:
        return {
            "name": name,
            "status": "warn",
            "detail": f"cannot inspect managed tmux session {watcher_session!r}",
        }
    rows = [line.split("\t", 1) for line in panes.stdout.splitlines() if line]
    if len(rows) != 1 or any(len(row) != 2 for row in rows):
        return {
            "name": name,
            "status": "warn",
            "detail": (
                f"managed tmux session {watcher_session!r} has {len(rows)} panes; "
                "expected exactly 1"
            ),
        }
    dead, command = rows[0]
    if dead != "0":
        return {
            "name": name,
            "status": "warn",
            "detail": f"managed tmux session {watcher_session!r} has a dead pane",
        }
    expected = _expected_codex_notifier_entrypoint()
    if not _command_runs_script(command, expected):
        return {
            "name": name,
            "status": "warn",
            "detail": (
                f"managed tmux session {watcher_session!r} runs an unexpected "
                f"command; expected {expected.name}"
            ),
        }
    return {
        "name": name,
        "status": "ok",
        "detail": (
            f"managed notifier healthy in {watcher_session!r} "
            f"({expected.name})"
        ),
    }


def check_codex_task_notifier() -> dict:
    """Detect a missing managed notifier even when a bare watcher looks alive."""
    if not _codex_runtime_selected():
        return {
            "name": "codex-task-notifier",
            "status": "ok",
            "detail": "Codex runtime not selected — notifier not expected",
        }
    heartbeat = _fresh_local_core_record()
    if heartbeat is None:
        return {
            "name": "codex-task-notifier",
            "status": "ok",
            "detail": "no fresh local core heartbeat — notifier not expected",
        }
    recorded_target = _local_codex_runtime_target(heartbeat)
    if recorded_target is None:
        return {
            "name": "codex-task-notifier",
            "status": "warn",
            "detail": "fresh local Codex heartbeat has unusable runtime metadata",
        }
    target = _local_codex_core_target(recorded_target)
    if target is None:
        return {
            "name": "codex-task-notifier",
            "status": "warn",
            "detail": (
                "fresh local Codex runtime recorded, but its exact live tmux "
                "session could not be verified"
            ),
        }
    return _probe_codex_task_notifier(target)


def fix_codex_task_notifier() -> str:
    """Delegate notifier-only recovery to the canonical runtime launcher.

    Calling the launcher without `--restart` preserves the verified live Codex
    core. On current main, the launcher recreates a missing watcher session.
    Other unhealthy existing-session shapes are still detected, but recovery
    depends on the launcher's topology support (for example, #2280's stale
    session replacement); the post-launch probe reports them as not repaired
    rather than claiming success.
    """
    # Re-check config inside the side-effecting function. It may have changed
    # since run_all_checks() took its snapshot; the dispatcher would otherwise
    # interpret that drift as a request to restart Core into another runtime.
    if not _codex_runtime_selected():
        return "not repaired — Codex runtime is not selected"
    target = _local_codex_core_target()
    if target is None:
        return "not repaired — no verified local Codex core"
    current = _probe_codex_task_notifier(target)
    if current["status"] == "ok":
        return "already healthy"
    launcher = REPO_DIR / "src" / "agent" / "start-cli.sh"
    if not launcher.is_file():
        return "not repaired — canonical launcher is missing"
    env = _resolve_launch_env()
    env["SUTANDO_TMUX_SOCKET"] = target["socket"]
    env["SUTANDO_TMUX_SESSION"] = target["session"]
    env["SUTANDO_CORE_RUNTIME"] = "codex"
    try:
        launched = subprocess.run(
            ["/bin/bash", str(launcher)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as error:  # noqa: BLE001
        return f"not repaired — launcher failed ({type(error).__name__})"
    if launched.returncode != 0:
        detail = (launched.stderr or launched.stdout).strip().splitlines()
        suffix = f": {detail[-1][:120]}" if detail else ""
        return f"not repaired — launcher exited {launched.returncode}{suffix}"

    verified = _local_codex_core_target()
    if verified != target:
        return "not repaired — local Codex core changed during repair"
    after = _probe_codex_task_notifier(target)
    if after["status"] != "ok":
        return f"not repaired — {after['detail']}"
    return "repaired managed notifier; live core session preserved"


def check_notes_split_brain() -> "dict | None":
    """Detect notes/ split-brain (#1266): overlapping .md files in both
    <repo>/notes/ and <workspace>/notes/ — fires only when the two paths differ."""
    repo_notes = REPO_DIR / "notes"
    ws_notes = Path(shared_personal_path("notes", WORKSPACE_DIR))
    if repo_notes.resolve() == ws_notes.resolve():
        return None
    if not repo_notes.exists() or not ws_notes.exists():
        return None
    repo_files = {p.name for p in repo_notes.glob("*.md")}
    ws_files = {p.name for p in ws_notes.glob("*.md")}
    overlap = repo_files & ws_files
    if not overlap:
        return None
    examples = ", ".join(sorted(overlap)[:3])
    tail = f" … and {len(overlap) - 3} more" if len(overlap) > 3 else ""
    return {
        "name": "notes-split-brain",
        "status": "warn",
        "detail": (
            f"{len(overlap)} .md file(s) duplicated across <repo>/notes/ and <workspace>/notes/ "
            f"— edits to one side are invisible to the other. "
            f"Run scripts/sutando-migrate.sh to consolidate. "
            f"Overlap: {examples}{tail}"
        ),
    }




def _file_digest(path: Path) -> str:
    """sha256 of a file, or a sentinel that can never equal another file's digest.

    Read errors must NOT make two files compare equal — that would silently turn an
    unreadable pair into "identical" and hide the divergence this probe reports.
    """
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return f"<unreadable:{path}>"


def check_legacy_notes_divergence() -> "dict | None":
    """Detect a canonical-vs-legacy notes/ divergence the #1266 probe cannot see.

    `check_notes_split_brain` compares <repo>/notes against <workspace>/notes.
    A host whose repo has no notes/ dir clears that guard and goes quiet, while the
    pre-v0.8 <legacy workspace>/notes (often a symlink into the memory-sync clone)
    keeps taking writes. Measured on one host:
    91 specs canonical, 105 legacy, 83 shared — bidirectional, and the newest
    canonical spec was 7 days older than the newest legacy one.

    Recursive and extension-agnostic on purpose: the #1266 probe globs top-level
    `*.md`, and this divergence lives in `sutando-wire/episode-specs/*.yaml`,
    so an .md-only top-level scan reports clean twice over.

    Reports counts, and NAMES the legacy-only paths — never a "which side is live"
    verdict. A whole-tree newest-mtime is dominated by whichever file was touched
    last for any reason: on one host it read "canonical newer by 0.0d" while the
    subtree that mattered was 7 days older. And a bare ratio invites dismissal —
    on a second host "2 of 11,442" was first read as nothing to worry about, when
    those 2 were the files a "delete the legacy tree" cleanup would destroy with
    no copy anywhere. Legacy-only, not both directions: canonical is the tree the
    resolver reaches, so a canonical-only file is merely unsynced.
    """
    ws_notes = Path(shared_personal_path("notes", WORKSPACE_DIR))
    legacy_notes = legacy_dotted_workspace() / "notes"
    if not ws_notes.exists() or not legacy_notes.exists():
        return None
    # A read failure must not read as "no divergence": this warning exists to stop a
    # cleanup discarding the only copy, so silence is the one unsafe direction.
    def _unreadable(what: str, exc: OSError) -> "dict":
        return {
            "name": "legacy-notes-divergence",
            "status": "warn",
            "detail": (
                f"could not compare the two notes/ trees — {what}: {exc}. This is NOT "
                f"a clean bill of health: the probe exists to catch legacy-only files "
                f"that a cleanup would destroy, and it could not read them. Resolve the "
                f"access error before deleting or repointing either tree "
                f"(canonical {ws_notes}, legacy {legacy_notes})."
            ),
        }

    try:
        if ws_notes.resolve() == legacy_notes.resolve():
            return None
    except OSError as exc:
        return _unreadable("resolving one of them failed", exc)

    def _rel(root: Path) -> "dict[str, Path] | None":
        """Map relative path -> file. None means the scan FAILED, never "empty"."""
        out = {}
        try:
            for p in root.rglob("*"):
                if p.is_file():
                    out[str(p.relative_to(root))] = p
        except OSError:
            return None
        return out

    a, b = _rel(ws_notes), _rel(legacy_notes)
    for label, scanned in (("the canonical tree", a), ("the legacy tree", b)):
        if scanned is None:
            return _unreadable(f"enumerating {label} failed", OSError("scan incomplete"))
    only_ws, only_legacy = set(a) - set(b), set(b) - set(a)
    # A shared PATH is not a shared FILE. Comparing name sets alone reports healthy
    # when both trees hold the same path with different bytes — measured 57 of 1056.
    differing = sorted(r for r in (set(a) & set(b)) if _file_digest(a[r]) != _file_digest(b[r]))
    if not only_ws and not only_legacy and not differing:
        return None

    ranked = sorted(only_legacy, key=lambda p: (Path(p).name.startswith("."), p))
    named = ", ".join(ranked[:3])
    more = f" … and {len(only_legacy) - 3} more" if len(only_legacy) > 3 else ""
    at_risk = (
        f" LEGACY-ONLY (no copy in the canonical tree): {named}{more}."
        if only_legacy else ""
    )
    # Derived from the computed sets, never asserted: a hardcoded "neither side is a
    # superset" is FALSE when one is, and would tell cleanup the opposite of the truth.
    if only_ws and only_legacy:
        shape = "Neither side is a superset, so pointing a consumer at either one loses files."
    elif only_legacy:
        shape = (f"The legacy tree is a strict superset by name — the canonical one is missing "
                 f"{len(only_legacy)} file(s).")
    elif only_ws:
        shape = (f"The canonical workspace is a strict superset by name — the legacy one is "
                 f"missing {len(only_ws)} file(s).")
    else:
        shape = "Names match on both sides; the divergence is entirely in file CONTENT."
    content = (
        f" {len(differing)} shared path(s) differ in CONTENT, so deleting either tree "
        f"loses bytes even where the names match (e.g. {', '.join(differing[:2])})."
        if differing else ""
    )
    return {
        "name": "legacy-notes-divergence",
        "status": "warn",
        "detail": (
            f"notes/ has diverged from the pre-v0.8 {legacy_notes}: "
            f"{len(only_ws)} file(s) only in the canonical workspace, "
            f"{len(only_legacy)} only in the legacy tree, "
            f"{len(set(a) & set(b))} shared ({len(differing)} of them differing)."
            f"{at_risk}{content} {shape} Decide which tree is authoritative before "
            f"'fixing' any path that reads notes/, and compare the SUBTREE you "
            f"care about — a whole-tree mtime does not say which side is live."
        ),
    }


def _drop_launcher_parents(pids: list) -> list:
    """Collapse a launcher+child pair to the child that is the real process.

    `pgrep -f '<name>\\.py$'` matches the LAUNCHER as well as the bridge,
    because the launcher's own argv ends with the same script path:

        pid 27538  secret-vault.py env TELEGRAM_BOT_TOKEN -- python3 src/telegram-bridge.py
        pid 27541  python3 src/telegram-bridge.py            <- ppid 27538

    Both match, so the duplicate-process check reported "multiple processes
    (2 PIDs)" for a perfectly healthy single bridge — every run, indefinitely.
    The `\\.py$` anchor above was added to stop a different false positive and
    cannot help here: the launcher's command line genuinely ends in the script.

    A standing false warning is worse than no warning: it is the one the
    operator learns to scroll past, and this probe exists to catch a REAL
    duplicate (two pollers racing for the same Telegram `getUpdates` cursor,
    which silently splits inbound messages between them).

    Identify by PID SCOPE, not by pattern: drop any matched pid that is the
    parent of another matched pid. Keeps the leaf — the process actually doing
    the work — and is agnostic to which launcher is in use (vault, `env`, a
    shell wrapper). A pid whose parent is NOT in the set is untouched, so two
    genuinely independent bridges still both survive and still warn.
    """
    if len(pids) < 2:
        return list(pids)
    try:
        out = subprocess.run(
            ["/bin/ps", "-o", "pid=,ppid=", "-p", ",".join(pids)],
            capture_output=True, text=True,
        )
    except Exception:
        return list(pids)
    if out.returncode != 0:
        # Could not resolve parentage — return the input untouched rather than
        # guessing. Over-reporting a duplicate is recoverable; silently
        # dropping a real second poller is not.
        return list(pids)
    parents = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] in pids:
            parents.add(parts[1])
    kept = [p for p in pids if p not in parents]
    return kept or list(pids)


def _should_skip_bridge(channel_name: str, env_path: Path) -> bool:
    """True if SKIP_<CHANNEL>=1 is set in the main .env or as an env var.

    Lets operators silence a bridge on a specific host without removing its
    token from the shared config (issue #1916). The flag is per-host: set it
    in the main .env on the host where the bridge should NOT run. Both the
    health-check (no warn) and --fix (no restart) honor it.
    """
    var = f"SKIP_{channel_name.upper()}"
    if os.environ.get(var) == "1":
        return True
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, val = line.partition("=")
                if sep and key.strip() == var and val.strip().strip('"').strip("'") == "1":
                    return True
        except Exception:
            pass
    return False


def sutando_app_hotkey_detail(workspace_dir) -> str:
    """Detail string for a running sutando-app check.

    Hotkey labels come from <workspace>/state/hotkeys.json, published by the
    app when it registers them (single source of truth since #1920). A running
    process alone doesn't prove hotkeys exist — app lineages without global
    hotkey registration (e.g. the Electron shell) match the pgrep pattern but
    register nothing, and the "(⌃C/⌃V/⌃M)" default asserted here pre-#1920 had
    already drifted from the real state/hotkeys.json values and read as a false
    positive during live debugging (that binding is configurable, not fixed).
    Missing/malformed/empty file → honest "no hotkeys published" rather than a
    guess.
    """
    try:
        entries = json.loads((Path(workspace_dir) / "state" / "hotkeys.json").read_text())
        labels = "/".join(e["label"] for e in entries if e.get("label"))
    except (OSError, ValueError, TypeError, AttributeError):
        labels = ""
    return f"running (hotkeys: {labels})" if labels else "running (no hotkeys published)"


def _outermost_bundle(comm: str) -> Optional[Path]:
    """Map an executable path to its OUTERMOST .app bundle, or None.

    Electron helper processes live at
    …/Sutando.app/Contents/Frameworks/Sutando Helper*.app/Contents/MacOS/…,
    so split on the FIRST `.app/` to resolve them to the top-level bundle
    rather than the nested helper bundle.
    """
    if ".app/" not in comm:
        return None
    return Path(comm.split(".app/", 1)[0] + ".app")


def _is_electron_impostor(comm: str) -> bool:
    """True if `comm` belongs to an Electron bundle squatting the Sutando name.

    The desktop UI also installs as "Sutando.app", and its main binary lives
    at the same …/Contents/MacOS/Sutando suffix the sutando-app pgrep pattern
    matches — so the probe reported "running" while the actual Swift menu-bar
    app (the contextual-chips writer + watcher-auto-restart owner) was dead
    (#2038, 2026-07-09). Electron bundles are distinguishable on disk: they
    ship Contents/Frameworks/Sutando Helper.app; the Swift app has no helper
    frameworks.
    """
    bundle = _outermost_bundle(comm)
    if bundle is None:
        return False  # bare dev binary (src/Sutando/Sutando) — not a bundle
    return (bundle / "Contents" / "Frameworks" / "Sutando Helper.app").exists()


def _ps_comm(pid: str) -> str:
    """Executable path (macOS) / name (linux) for a PID via ps; "" on error."""
    return subprocess.run(
        ["/bin/ps", "-o", "comm=", "-p", pid],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()


def _filter_electron_impostor_pids(pids: list[str]) -> list[str]:
    """Drop PIDs that belong to the Electron desktop app, keep the rest.

    Fail-open per PID: if the ps lookup errors, keep the PID (pre-fix
    behavior) rather than false-alarm "stopped".
    """
    kept = []
    for pid in pids:
        try:
            if _is_electron_impostor(_ps_comm(pid)):
                continue
        except Exception:
            pass
        kept.append(pid)
    return kept


def _resolve_menu_bar_pgrep(pgrep_status: Optional[str], pids: list[str]) -> tuple[Optional[str], list[str]]:
    """Post-process the sutando-app pgrep result: drop Electron impostor
    PIDs, and demote "ok-running" to "ok-stopped" when nothing real remains."""
    if pgrep_status == "ok-running" and pids:
        pids = _filter_electron_impostor_pids(pids)
        if not pids:
            pgrep_status = "ok-stopped"
    return pgrep_status, pids


def bridge_log_content_status(name: str, status: str, tail: list[str],
                              detail: str = "",
                              slack_state: "str | None" = None,
                              log_path: Optional[Path] = None) -> Optional[tuple[str, str]]:
    """Check a bridge's recent log lines for known failure-mode signatures.

    Returns an (status, detail) override, or None if nothing to override.
    discord-bridge: LoginFailure means the token is revoked/invalid. Always
      overrides — there is no point restarting with stale code if the token
      is bad; the token fix is the only path forward.
    slack-bridge: "60s elapsed" hint means Socket Mode connected but events
      weren't routing yet at startup (Slack app Event Subscriptions
      disabled). Only overrides "ok" — stale/dead-inode are higher priority.
      The hint is a one-time startup message that never clears itself in the
      log, so it only counts if no event has actually been processed since
      it last fired (checked via a subsequent "Wrote task-" line) — otherwise
      Event Subscriptions clearly ARE enabled and it's a stale false alarm.
    telegram-bridge: a 409 Conflict is a competing getUpdates poller splitting
      updates. Only counts if no message arrived after the last conflict.
      Overrides the stale-HEARTBEAT warn too, since the heartbeat only advances on
      an accepted poll; `detail` keeps stale-LOG and dead-inode warns intact.
    """
    if name == "discord-bridge":
        if any("LoginFailure" in ln or "Improper token" in ln for ln in tail):
            return "fail", "token invalid (LoginFailure) — regenerate at discord.com/developers/applications"
    elif name == "telegram-bridge" and (
            status == "ok"
            or (status == "warn" and "heartbeat stale" in detail)):
        conflict_idxs = [i for i, ln in enumerate(tail) if "409" in ln and "Conflict" in ln]
        if conflict_idxs:
            # Telegram hands each update to exactly ONE getUpdates caller, so a
            # message received after the last 409 means this host is winning again.
            received_after = any(ln.lstrip().startswith("@") for ln in tail[conflict_idxs[-1] + 1:])
            if not received_after:
                return "warn", ("another getUpdates poller is competing — Telegram splits "
                                "updates between hosts; set SKIP_TELEGRAM=1 on the non-owning "
                                "host. A stale heartbeat here is a CONSEQUENCE of this, not a "
                                "separate fault")
    elif name == "slack-bridge" and status == "ok":
        warn_idxs = [i for i, ln in enumerate(tail) if "60s elapsed with zero events" in ln]
        if warn_idxs:
            events_after = any("Wrote task-" in ln for ln in tail[warn_idxs[-1] + 1:])
            if not events_after:
                # Event Subscriptions alone is the whole fix ONLY when an owner
                # is already enrolled; in TOFU state the code gate also blocks.
                if slack_state is None:
                    try:
                        slack_state = slack_access.access_state(
                            channel_access_path("slack"))
                    except Exception:  # noqa: BLE001 — a probe must not fail the check
                        slack_state = slack_access.UNKNOWN
                if slack_state == slack_access.ENROLLED:
                    return "warn", ("connected but events not arriving — enable Event "
                                    "Subscriptions at api.slack.com/apps")
                if slack_state == slack_access.UNKNOWN:
                    # A resolver we could not run leaves us knowing nothing, so it
                    # keeps the quieter enrolled remedy; a malformed record does not.
                    try:
                        bad_record = channel_access_path("slack")
                    except Exception:  # noqa: BLE001 — a probe must not fail the check
                        bad_record = None
                    if bad_record is not None:
                        return "warn", ("connected but events not arriving, and access.json "
                                        "is unreadable or malformed, so no user can be "
                                        "authorized and TOFU stays closed — Event "
                                        "Subscriptions alone leaves Slack silent: inspect "
                                        f"or repair {shlex.quote(str(bad_record))} "
                                        "(allowFrom must be a list of user-id strings), "
                                        "then enable Event Subscriptions at "
                                        "api.slack.com/apps")
                    return "warn", ("connected but events not arriving — enable Event "
                                    "Subscriptions at api.slack.com/apps")
                # Name the log this check actually read — the workspace is
                # configurable, so a literal path can point at no such file.
                resolved = Path(log_path) if log_path else (
                    WORKSPACE_DIR / "logs" / "slack-bridge.log")
                if slack_state == slack_access.LOCKED:
                    return "warn", ("connected but events not arriving, and access.json "
                                    "allows nobody (empty allowFrom) — Event Subscriptions "
                                    "alone leaves Slack silent because no user may reach "
                                    "it: add an allowed user id to "
                                    f"{shlex.quote(str(channel_access_path('slack')))} too")
                return "warn", ("connected but events not arriving, and no owner is enrolled "
                                "(access.json absent) — BOTH are needed: enable Event "
                                "Subscriptions at api.slack.com/apps, then DM the bot the code "
                                f"from `grep -i \"enrollment code\" {shlex.quote(str(resolved))} "
                                "| tail -1` (regenerated on every bridge start)")
    return None


def _host_runs_comm_sweep(
    workspace_dir: Optional[Path] = None, host_label: Optional[str] = None
) -> bool:
    """True when THIS host has a comm-sweep driver wired, i.e. its own crons.json
    schedules one.

    Comm handling is a SINGLE-OWNER lane: exactly one host in the fleet runs the
    sweep, because a second cron would duplicate sweeps over the owner's real
    comms. So "no stamp here" is only a defect on the host that actually owns it.
    """
    workspace = Path(workspace_dir or WORKSPACE_DIR)
    host = host_label or _host_label()
    try:
        crons = json.loads((workspace / "hosts" / host / "crons.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(crons, list):
        return False
    for entry in crons:
        if not isinstance(entry, dict):
            continue
        if entry.get("prompt_skill") == "comm-sweep":
            return True
        # A prompt-body entry that invokes the skill or its script counts too —
        # matching how the driver is actually scheduled, not one spelling of it.
        if "comm-sweep" in f"{entry.get('name', '')} {entry.get('prompt', '')}":
            return True
    return False


def _host_dynamic_loops(
    workspace_dir: Optional[Path] = None, host_label: Optional[str] = None
) -> list[str]:
    """Names of the `loop: "dynamic"` entries THIS host declares in its crons.json.

    Same per-host, single-owner shape as `_host_runs_comm_sweep`: a dynamic loop
    is launched by `/schedule-crons` on the host whose crons.json declares it, so
    only that host has anything to monitor.
    """
    workspace = Path(workspace_dir or WORKSPACE_DIR)
    host = host_label or _host_label()
    try:
        crons = json.loads((workspace / "hosts" / host / "crons.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(crons, list):
        return []
    names = []
    for entry in crons:
        if not isinstance(entry, dict) or entry.get("loop") != "dynamic":
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _stamped_dynamic_loops(workspace_dir: Optional[Path] = None) -> list[str]:
    """Loop names that have a `dynamic-loop-<name>.alive` sentinel ON DISK.

    The counterpart to `_host_dynamic_loops`, and the reason enumeration is a
    union of the two: a stall whose `crons.json` entry was edited away is still
    a stall. Deriving the watch-list from config alone lets an unrelated config
    edit un-declare a real one into invisibility.

    Globs BOTH locations `status_read_path` reads from, so an un-migrated
    install is enumerated the same way it is read. Never raises: an unreadable
    state dir degrades to "nothing found here", and the declared names still
    produce rows.
    """
    workspace = Path(workspace_dir or WORKSPACE_DIR)
    prefix, suffix = "dynamic-loop-", ".alive"
    names: set[str] = set()
    for base in (workspace / "state", workspace):
        try:
            for path in base.glob(f"{prefix}*{suffix}"):
                stem = path.name[len(prefix):-len(suffix)].strip()
                if stem:
                    names.add(stem)
        except OSError:
            continue
    return sorted(names)


def _positive_seconds(value) -> Optional[float]:
    """A sentinel field is only usable as a duration/timestamp if it is a finite
    positive number. `bool` is an `int` in Python, so exclude it explicitly —
    otherwise a `true` would read as 1 second and manufacture a false alarm.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds <= 0:
        return None
    return seconds


def _as_list(value) -> list:
    """A settings file is hand-editable; anything not a list yields nothing to scan."""
    return value if isinstance(value, list) else []


#: `$(shq "…")` / `$(shq …)` — the installer shell-quotes its paths through a helper
#: before writing them into settings.json. Reading HOOKS as literal source text means
#: the substitution is still unevaluated, so the path is welded into a token like
#: `$(shq` + `<path>)` and never compares equal to anything.
_SHQ_CALL = re.compile(r"\$\(\s*shq\s+(\"[^\"]*\"|'[^']*'|[^)]*)\)")


def _unwrap_installer_command(command: str) -> str:
    """Reduce an installer HOOKS command template to the shape it actually WRITES.

    The template is bash SOURCE, not the stored command. `bash $(shq "$REPO_DIR/x.sh")`
    is written to settings.json as `bash /abs/path/x.sh`, so comparing against the raw
    source can never match — which is exactly how every production hook slipped past
    the positional check and into a permissive fallback.
    """
    # Re-quote rather than inline raw: `shq` IS shell-quoting, and a repo path
    # containing a space would otherwise split across tokens and fail closed on a
    # perfectly healthy host — which is the whole reason the installer uses shq.
    out = _SHQ_CALL.sub(lambda m: shlex.quote(m.group(1).strip("\"'")), command)
    # The array literal is itself quoted in shell, so inner quotes arrive escaped.
    out = out.replace('\\"', '"').replace("\\$", "$")
    # The HOOKS entry is a quoted shell string, so parsing it strips the entry's
    # own closing quote and leaves the backslash that escaped it dangling. That
    # makes shlex raise and drop us into the whitespace fallback, where a token
    # keeps a stray opening quote and no comparison can match — it cost a GENUINE
    # archive hook a false warning. The dangling backslash WAS that closing quote,
    # so restore it rather than deleting it: deleting leaves the quote unbalanced,
    # which is the same failure one step later.
    if out.endswith("\\") and not out.endswith("\\\\"):
        out = out[:-1] + '"'
    return out


def _shell_tokens(command: str) -> list:
    """Shell-split, degrading to whitespace split on unbalanced quoting.

    The fallback keeps surrounding quotes on each token, so tokens are normalized
    either way — otherwise a comparison silently depends on WHICH split path ran.
    """
    try:
        toks = shlex.split(command)
    except ValueError:  # a hand-edited settings file can be unquotable
        toks = command.split()
    out = []
    for tok in toks:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def _same_path(a: str, b_norm: str, b_real: str) -> bool:
    a = os.path.expanduser(a)
    if not os.path.isabs(a):
        # Unresolvable without knowing the hook's cwd, so it never counts as a
        # match: unprovable identity warns, like every other branch here.
        return False
    return os.path.normpath(a) == b_norm or os.path.realpath(a) == b_real


def _hook_command_targets(command: str, expected, owned_cmd: str, marker: str = "") -> bool:
    """Does this command INVOKE `expected` — not merely mention it, not merely contain it?

    Two false-cleans this has to reject, both found in review, both of which let a
    stale or replaced hook keep the probe green:

    1. **Substring containment is not checkout identity.** `/tmp/sutando` is a
       substring of `/tmp/sutando-old/src/check-pending-tasks.sh`, so `str(repo) in
       command` certifies a sibling checkout as this one.
    2. **A path in argument position is not an invocation.** Scanning every absolute
       token accepts `echo <path>`, `printf "%s" <path>`, and
       `bash /tmp/other.sh <path>` — the expected script appears as inert data while
       something else entirely runs.

    So the comparison is positional, against the command the INSTALLER itself writes
    (the third field of its `HOOKS` entry). The installer stays the single source of
    truth for the command shape exactly as it already is for the hook list: whatever
    interpreter it uses, and whichever position it puts its script in, is what a
    registered hook must match. Leading tokens must agree, so `echo` cannot stand in
    for `bash`, and the script position must be the same path.
    """
    owned = _shell_tokens(_unwrap_installer_command(owned_cmd))
    got = _shell_tokens(command)
    if not owned or not got:
        return False

    # PROGRAM CHECK — applies to EVERY owned hook, not just repo-relative ones.
    # This was previously skipped for markers that are not `src/` paths, on the
    # reasoning that a non-repo path has no identity to compare. That conflated
    # path identity with command-shape validation: the archive hook still has an
    # installer-owned shape, and `echo` is not `cp`. With only the substring test,
    # BOTH of these certified as a healthy archive hook:
    #     echo sutando-conversations/
    #     rm -rf sutando-conversations/
    # The second is the one that settles it — a destructive command reported as a
    # working archiver is the opposite of what this probe is for.
    if got[0] != owned[0]:
        return False

    if expected is None:
        # Marker is not repo-relative (the archive hook writes outside the repo),
        # so there is no repo path to compare positionally. Program alone is NOT
        # enough: `cp /tmp/other "$HOME/Desktop/sutando-conversations/x"` and
        # `cp "$TRANSCRIPT_PATH" /tmp/sutando-conversations/y` both pass a program
        # check while archiving the wrong thing, or to the wrong place.
        #
        # An earlier revision of this comment called that an intentional
        # "compatibility boundary", on the reasoning that the installer preserves
        # operator-customized archive hooks. That reasoning was WRONG, and it is
        # worth recording why, because it read as principled: Phase 0 does skip
        # sweeping a custom archiver (install-claude-hooks.sh:170-185), but Phase 1
        # detects presence by EXACT command-string match — `index($cmd)` at :262 —
        # so a custom `cp` never satisfies it and the installer ADDS its own
        # command alongside. The two COEXIST. On any host where the installer has
        # run, its own command is therefore present, and this probe should say so.
        #
        # Compared: the program, the SOURCE argument the installer writes, and the
        # destination prefix up through the marker. Everything after the marker is
        # free — that is where the installer's own $(date …) filename varies, so
        # pinning it would warn on healthy hosts.
        d_idx = next((i for i, tok in enumerate(owned) if marker and marker in tok), None)
        if len(owned) < 2 or d_idx is None:
            return False
        # POSITION and ARITY, not "the prefix appears somewhere". Accepting the
        # prefix in any token certified a three-operand
        #     cp "$TRANSCRIPT_PATH" /tmp/not-the-archive ".../sutando-conversations/x"
        # which cp treats as two SOURCES and a destination — and which fails at
        # runtime unless that last path is a directory, so nothing is archived while
        # the probe reports clean. The installer writes exactly program/source/dest
        # and Phase 1 requires that exact string, so anything of a different arity
        # is not the command it installs.
        if len(got) != len(owned) or got[1] != owned[1]:
            return False
        prefix = owned[d_idx][: owned[d_idx].index(marker) + len(marker)]
        return got[d_idx].startswith(prefix)

    want = os.path.normpath(os.path.expanduser(str(expected)))
    want_real = os.path.realpath(want)
    idx = next((i for i, t in enumerate(owned) if _same_path(t, want, want_real)), None)
    if idx is None:
        # FAIL CLOSED. This branch used to accept the path anywhere in the first two
        # tokens, which read as a safe fallback and was not: the real installer writes
        # `bash $(shq "$REPO_DIR/x.sh")`, so before the unwrap above NO production hook
        # resolved positionally and EVERY one landed here — making `echo <path>` count
        # as registered on the only path that ships. A fallback that the real data
        # always takes is not a fallback, it is the behaviour.
        return False
    if len(got) <= idx:
        return False
    return _same_path(got[idx], want, want_real) and got[:idx] == owned[:idx]


def check_vault_manifest_integrity(
    manifest_path: Optional[Path] = None,
    keychain_probe: Optional[Callable[[str, str], bool]] = None,
    max_keys: int = 200,
    legacy_path: Optional[Path] = None,
) -> dict:
    """Does every name `list_vault_keys()` advertises actually exist in Keychain?

    The vault splits its state in two: names live in the manifest
    (`<workspace>/state/secret-vault/keys.json`), values live in macOS Keychain.
    Nothing keeps the halves in step. A name can outlive its secret — a Keychain
    entry deleted by hand, a machine restored from backup, or a manifest carried
    forward by the legacy-path self-migration in `vault_intercept._read_manifest`.

    The divergence is silent in the direction that matters. CLAUDE.md tells every
    integration to discover keys via `list_vault_keys()` and then fetch with
    `get_vault_key()`. A phantom name passes discovery and raises KeyError on
    fetch, so the documented pattern points callers AT keys that cannot resolve —
    strictly worse than the key simply being absent, because the list says it is
    there. Observed on this host 2026-08-04: 15 advertised, 2 backed, 13 phantom.

    POSITIVE CONTROL, deliberately (`security find-generic-password` exits 44 both
    for "no such key" AND for a wrong `-a <account>`, measured). So a bad account
    name, a locked keychain, or a missing binary would otherwise report EVERY key
    phantom — a maximally alarming, entirely wrong answer. This probe therefore
    refuses to report divergence unless at least one name resolves: zero-of-N is
    treated as "the checker is broken", not "the vault is empty". The cost is
    real (a genuinely 100%-phantom manifest reads inconclusive), and that is the
    correct direction to fail — a false clean here is a nag nobody can act on,
    while a false alarm would send the operator hunting a vault that is fine.

    RESOLUTION MUST MIRROR `_read_manifest()`, canonical-first THEN legacy. The
    first version read only `_manifest_path()` and returned "no vault manifest on
    this host" when the canonical file was absent — while `list_vault_keys()`,
    reading through `_read_manifest()`, still returned the legacy manifest's keys.
    That is a false clean on a pre-migration install, i.e. on exactly the
    population this check exists to diagnose. A probe that does not walk the same
    lookup path as the function it validates is checking a different system.
    """
    name = "vault-manifest"
    try:
        import vault_intercept  # noqa: PLC0415  (optional; absent in trimmed installs)
    except Exception:
        return {"name": name, "status": "ok",
                "detail": "vault_intercept not importable — vault not in use here"}

    if manifest_path is not None:
        candidates = [Path(manifest_path)]
    else:
        # Same order, and the same fallback, as vault_intercept._read_manifest().
        candidates = [Path(vault_intercept._manifest_path())]
        legacy = Path(legacy_path) if legacy_path else Path(vault_intercept._LEGACY_MANIFEST_PATH)
        if legacy != candidates[0]:
            candidates.append(legacy)
    # PARSE per candidate and CONTINUE on failure — existence is not the selector.
    # `_read_manifest()` catches FileNotFoundError AND JSONDecodeError inside its
    # loop, so a malformed canonical file does not stop it reaching the legacy
    # one. Selecting on `.exists()` and then returning on the first decode error
    # diverges exactly there. qingyun-wu's activated control at db708178, with a
    # malformed canonical beside a valid legacy holding REAL + PHANTOM:
    #
    #     list_vault_keys() -> ['PHANTOM', 'REAL']
    #     health -> warn: manifest unreadable — list_vault_keys() would return nothing
    #
    # Both halves wrong: the detail is false, and the names production DOES
    # advertise were never probed — on a pre-migration install, which is the
    # population this check exists for. Same lesson as the resolution-order fix
    # one round earlier: walking a different lookup path than the function under
    # test measures a different system.
    #
    # A non-FileNotFound OSError (permissions, EIO) is NOT folded into the
    # continue: production would let it propagate out of `list_vault_keys()`,
    # which is a louder and different failure than "returns nothing", so it is
    # reported rather than silently skipped past.
    path = None
    manifest = None
    unreadable: "list[str]" = []
    for cand in candidates:
        try:
            manifest = json.loads(cand.read_text())
            path = cand
            break
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            unreadable.append(f"{cand} (JSONDecodeError)")
            continue
        except OSError as e:
            return {"name": name, "status": "warn",
                    "detail": (f"manifest at {cand} could not be read ({type(e).__name__}) — "
                               f"list_vault_keys() raises rather than returning nothing")}
    if path is None:
        if unreadable:
            # Every candidate that existed failed to parse, so `_read_manifest()`
            # falls off its loop and returns {} — discovery is silently empty.
            return {"name": name, "status": "warn",
                    "detail": (f"no readable vault manifest ({', '.join(unreadable)}) — "
                               f"list_vault_keys() would return nothing")}
        return {"name": name, "status": "ok", "detail": "no vault manifest on this host"}
    via_legacy = len(candidates) > 1 and path == candidates[-1]
    if not isinstance(manifest, dict):
        # Valid JSON, wrong shape. This is NOT benign: `_read_manifest()` returns
        # it verbatim and `list_vault_keys()` then calls `.keys()` on it, so the
        # documented discovery call raises AttributeError. Reporting "empty" here
        # would be a clean bill of health for a vault nobody can enumerate.
        return {"name": name, "status": "warn",
                "detail": (f"manifest at {path} is valid JSON but a {type(manifest).__name__}, not an object — "
                           f"list_vault_keys() raises AttributeError on it, so discovery is broken, not empty")}
    names = sorted(manifest)
    if not names:
        return {"name": name, "status": "ok", "detail": "manifest empty — nothing advertised"}

    probe = keychain_probe
    if probe is None:
        if not shutil.which("security"):
            return {"name": name, "status": "ok",
                    "detail": f"{len(names)} key(s) advertised; no `security` binary — cannot verify, not asserting"}

        def probe(account: str, key: str) -> bool:  # noqa: F811
            return subprocess.run(
                ["security", "find-generic-password", "-a", account, "-s", key],
                capture_output=True,
            ).returncode == 0

    account = getattr(vault_intercept, "_ACCOUNT", "sutando")
    checked = names[:max_keys]
    backed = [k for k in checked if probe(account, k)]
    phantom = [k for k in checked if k not in backed]

    if not backed:
        # The control failed: a wrong account / locked keychain looks exactly
        # like a fully-phantom manifest. Say so instead of raising a false alarm.
        return {"name": name, "status": "ok",
                "detail": (f"{len(checked)} advertised key(s), 0 resolved against account "
                           f"'{account}' — treating as an unverifiable keychain, not as divergence")}
    src = " (read via the LEGACY fallback — canonical manifest absent)" if via_legacy else ""
    if not phantom:
        return {"name": name, "status": "ok",
                "detail": f"all {len(backed)} advertised key(s) resolve in Keychain{src}"}

    shown = ", ".join(phantom[:6]) + (f", +{len(phantom) - 6} more" if len(phantom) > 6 else "")
    truncated = f" (checked first {max_keys} of {len(names)})" if len(names) > max_keys else ""
    return {
        "name": name,
        "status": "warn",
        "detail": (f"{len(phantom)}/{len(checked)} advertised key(s) have NO Keychain entry{truncated}{src}, "
                   f"so list_vault_keys() offers them and get_vault_key() raises KeyError: {shown}. "
                   f"Prune {path} or re-store the secrets."),
    }


def check_claude_hook_registration(
    repo_dir: Optional[Path] = None,
) -> dict:
    """Are the Claude Code hooks `install-claude-hooks.sh` owns actually registered?

    Nothing checked this before. On 2026-08-03 this host was found with **zero of
    the four owned hooks** in the installer's own target — including both PreCompact
    entries, so `session-state.md` was never regenerated on compaction and the
    transcript archiver had never run at all. It had been that way for days, silently,
    because no probe looks at hook registration. A peer host showed the same shape.

    The owned list is READ FROM THE INSTALLER's `HOOKS=(...)` array rather than
    duplicated here: a second copy would drift from the script that does the
    installing, and a stale allow-list is how a probe starts lying. Same reason the
    target file is read from its `SETTINGS=` line instead of being assumed.

    Fails toward NOISE, never toward a false clean:
      * installer absent          -> ok, not a sutando checkout (nothing to verify)
      * HOOKS array unparseable   -> WARN. A parse that yields zero hooks would
                                     otherwise report "all registered" over an empty
                                     population, which is the exact shape of a probe
                                     that cannot fail.
      * settings.json absent      -> warn (installer has never run here)
      * settings.json malformed   -> warn, never raise
      * a hook registered but its command points at a DIFFERENT checkout -> warn.
        This host had a SessionEnd entry aimed at a five-day-old `Desktop/sutando`
        copy, so fixes to the live script never executed — present-but-wrong is the
        failure that looks healthiest.
    """
    name = "claude-hooks"
    repo = Path(repo_dir or REPO_DIR)
    installer = repo / "src" / "install-claude-hooks.sh"
    if not installer.is_file():
        return {"name": name, "status": "ok", "detail": "no install-claude-hooks.sh — not a sutando checkout"}
    try:
        src = installer.read_text(errors="ignore")
    except OSError as exc:
        return {"name": name, "status": "warn", "detail": f"cannot read installer ({exc})"}

    m = re.search(r"^HOOKS=\((.*?)^\)", src, re.M | re.S)
    owned = []
    if m:
        for line in m.group(1).split("\n"):
            line = line.strip()
            if not line.startswith('"'):
                continue
            parts = line.strip('"').split("|", 2)
            if len(parts) == 3:
                # third field is the command the installer WRITES — kept so the probe
                # compares against the real command shape rather than guessing one.
                owned.append((parts[0], parts[1], parts[2]))
    if not owned:
        return {"name": name, "status": "warn",
                "detail": "could not parse HOOKS=(...) from install-claude-hooks.sh — "
                          "cannot verify registration (reporting rather than assuming clean)"}

    # Skill-declared hooks are appended to HOOKS at run time, so the static parse
    # above cannot see them. Same discovery the installer uses — a second copy here
    # would drift and quietly stop verifying whatever the installer registered.
    try:
        sys.path.insert(0, str(repo / "src"))
        from skill_hooks import discover as _discover_skill_hooks
        # [:3] — the 4th field is the prior command, which only the installer's
        # migration sweep uses; `owned` also holds 3-tuples parsed from the script.
        owned.extend(r[:3] for r in _discover_skill_hooks(repo))
    except Exception as exc:
        return {"name": name, "status": "warn",
                "detail": f"skill-hook discovery failed ({exc}) — "
                          f"cannot verify skill-declared hooks"}

    sm = re.search(r'^SETTINGS="([^"]+)"', src, re.M)
    settings = Path(sm.group(1).replace("$REPO_DIR", str(repo))) if sm else repo / ".claude" / "settings.json"
    if not settings.is_file():
        return {"name": name, "status": "warn",
                "detail": f"{settings} missing — install-claude-hooks.sh has never run here; "
                          f"{len(owned)} hook(s) unregistered"}
    try:
        conf = json.loads(settings.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"name": name, "status": "warn", "detail": f"{settings.name} unreadable ({exc})"}

    # Parseable is not the same as well-shaped: `[]` is valid JSON, and `.get()` on it
    # raises AttributeError, which would abort every remaining probe in run_all_checks().
    # A malformed schema has to fail toward a warning like every other ambiguous branch.
    if not isinstance(conf, dict):
        return {"name": name, "status": "warn",
                "detail": f"{settings.name} top level is {type(conf).__name__}, not an object — "
                          f"cannot verify {len(owned)} hook(s)"}
    hooks = conf.get("hooks")
    if hooks is None:
        hooks = {}
    elif not isinstance(hooks, dict):
        return {"name": name, "status": "warn",
                "detail": f"{settings.name} \"hooks\" is {type(hooks).__name__}, not an object — "
                          f"cannot verify {len(owned)} hook(s)"}

    missing, foreign = [], []
    for event, marker, owned_cmd in owned:
        # Every container on this path is type-checked, at EVERY level. Validating
        # only the top two left `{"hooks":{"Stop":7}}`, `{"hooks":{"Stop":[{"hooks":7}]}}`
        # and a numeric `command` still raising TypeError straight out of the probe —
        # and since this runs inside run_all_checks(), a raise aborts every later check.
        # A malformed shape yields no commands, so the hook reads as unregistered: the
        # promised warning, not a crash and not a false clean.
        cmds = []
        for g in _as_list(hooks.get(event)):
            if not isinstance(g, dict):
                continue
            for h in _as_list(g.get("hooks")):
                if not isinstance(h, dict):
                    continue
                c = h.get("command")
                if isinstance(c, str):
                    cmds.append(c)
        hit = [c for c in cmds if marker in c]
        if not hit:
            missing.append(f"{event}:{marker}")
        elif not any(
            _hook_command_targets(
                c,
                (repo / marker) if marker.startswith("src/") else None,
                owned_cmd.replace("$REPO_DIR", str(repo)),
                marker,
            )
            for c in hit
        ):
            # Present, but not actually invoking this checkout's script — either aimed
            # at another checkout or carrying the path as an inert argument.
            foreign.append(f"{event}:{marker}")
    if missing or foreign:
        bits = []
        if missing:
            bits.append(f"{len(missing)} NOT registered ({', '.join(missing)})")
        if foreign:
            bits.append(f"{len(foreign)} registered but NOT running the installer's command "
                        f"— a different program, another checkout, or the path is "
                        f"only an argument ({', '.join(foreign)})")
        return {"name": name, "status": "warn",
                "detail": f"{'; '.join(bits)} in {settings} — re-run `bash src/install-claude-hooks.sh`"}
    return {"name": name, "status": "ok", "detail": f"all {len(owned)} owned hooks registered"}


def check_comm_sweep_freshness(
    workspace_dir: Optional[Path] = None, host_label: Optional[str] = None
) -> dict:
    """Comm-handling liveness (P1 of the comm-handling overhaul).

    The comm-sweep driver stamps state/last-comm-sweep.json every run. A stale
    stamp means comm handling has silently STOPPED — the exact failure that let
    the inbox-score loop die 2026-07-21 and owner-comm sweeps lapse for days
    with nobody alerted (comm handling was a *discipline*, not a *mechanism*).
    This probe makes that loud instead of silent: warn past ~2h, down past ~6h.

    LANE-AWARENESS (2026-08-03). The absent-stamp branch used to warn on every
    host, with the detail "driver not wired on this host yet (P1)". That wording
    asserted a per-host adoption gap and was wrong twice over: comm handling is a
    single-owner lane (the driver lives in sonichi/sutando-personal and runs on
    ONE host by design — a second cron would duplicate sweeps over the owner's
    comms), so a non-owning host has nothing to adopt and warns FOREVER. A
    permanent warn is how a health output gets ignored, which would have cost the
    very alarm this probe exists to raise. So absence is now judged against
    whether this host actually schedules the driver.

    Deliberately gated on the ABSENT branch ONLY: once a stamp exists, the age
    thresholds apply unconditionally. Gating those on config too would mean
    deleting the cron entry silently disarms a real stall — failing in the
    dangerous direction.

    Age-checked (unlike quota-telemetry, which is absence-only): comm handling
    is expected to run on a fixed cadence, so a lengthening age IS the signal.
    """
    path = status_read_path("last-comm-sweep.json", workspace_dir or WORKSPACE_DIR)
    name = "comm-sweep"
    if not path.exists():
        if not _host_runs_comm_sweep(workspace_dir, host_label):
            return {"name": name, "status": "ok",
                    "detail": "comm-sweep not scheduled on this host — single-owner lane, "
                              "probe N/A here"}
        return {"name": name, "status": "warn",
                "detail": "comm-sweep is scheduled on this host but has never stamped "
                          "last-comm-sweep.json — driver wired but not producing"}
    try:
        age_h = (time.time() - path.stat().st_mtime) / 3600
    except OSError as exc:
        return {"name": name, "status": "warn",
                "detail": f"last-comm-sweep.json stat failed ({exc})"}
    if age_h > 6:
        return {"name": name, "status": "down",
                "detail": f"last comm sweep {age_h:.1f}h ago (>6h) — comm handling has silently stopped"}
    if age_h > 2:
        return {"name": name, "status": "warn",
                "detail": f"last comm sweep {age_h:.1f}h ago (>2h) — comm handling lagging"}
    return {"name": name, "status": "ok", "detail": f"last comm sweep {age_h:.1f}h ago"}


#: Re-arm cadence assumed when a sentinel carries no usable `next_delay_s`.
#: `/loop`'s dynamic mode is told to lean 1200-1800s for its fallback heartbeat;
#: taking the slow end keeps a malformed stamp from manufacturing an alarm.
_DYNAMIC_LOOP_DEFAULT_DELAY_S = 1800.0


def check_dynamic_loop_freshness(
    workspace_dir: Optional[Path] = None, host_label: Optional[str] = None
) -> list[dict]:
    """Liveness for `loop: "dynamic"` entries — the one scheduled thing nothing else sees.

    A dynamic loop self-paces through ScheduleWakeup, so it is NOT a cron job
    (absent from CronList, so the `session-crons` probe can't count it) and NOT
    an OS process (invisible to pgrep, so no PID sentinel applies). Every other
    liveness probe here keys off one of those two. A dynamic loop that stops
    re-arming therefore pages nobody — which is not hypothetical: the
    inbox-score loop died 2026-07-21 and owner-comm sweeps lapsed for days
    before anyone noticed. `check_comm_sweep_freshness` above makes the
    downstream symptom loud; this closes the same gap one layer up, at the loop.

    The sentinel carries its OWN threshold. `/loop`'s body stamps
    `state/dynamic-loop-<name>.alive` with `{ts, next_delay_s}` on every re-arm,
    so the probe compares against the cadence the loop just chose rather than a
    hardcoded one a self-pacing loop is free to change: warn past
    `next_delay_s + 120`, down past `2*next_delay_s + 300`.

    Age comes from the payload's `ts`, not mtime. `state/cores/*.alive` is
    deliberately vault-EXCLUDED so a synced mtime can never fake liveness;
    `state/dynamic-loop-*.alive` is NOT excluded, so its mtime can be rewritten
    by a sync on an unrelated host. The self-reported `ts` is the honest clock;
    mtime is a fallback for a payload that won't parse, and the detail says so.

    Returns a LIST — one row per loop this host declares OR has a sentinel for,
    and an EMPTY list on a host with neither. That is the lane-awareness lesson
    from `check_comm_sweep_freshness`: a permanent warn on a host with nothing
    to monitor is how a health output gets ignored, which would take this
    probe's real alarms down with it.

    Enumeration is the UNION of the two sources, and that is load-bearing.
    Config gates the ABSENT branch only: `crons.json` can add a loop to watch
    (declared-but-never-stamped ⇒ warn), but it can never remove one, because a
    sentinel on disk is judged on its age no matter what the config says. An
    earlier revision enumerated from `crons.json` alone, so deleting the entry
    during an unrelated edit dropped a genuinely stalled loop out of
    `run_all_checks()` entirely — a probe whose whole purpose is "a stall that
    pages nobody" going silent in exactly that direction.
    """
    checks: list[dict] = []
    loops = sorted(
        set(_host_dynamic_loops(workspace_dir, host_label))
        | set(_stamped_dynamic_loops(workspace_dir))
    )
    for loop in loops:
        name = f"dynamic-loop:{loop}"
        stem = f"dynamic-loop-{loop}.alive"
        path = status_read_path(stem, workspace_dir or WORKSPACE_DIR)
        if not path.exists():
            checks.append({"name": name, "status": "warn",
                           "detail": f"{loop} is declared `loop: \"dynamic\"` in crons.json but has "
                                     f"never stamped {stem} — launched but not re-arming"})
            continue
        try:
            raw = path.read_text()
            mtime = path.stat().st_mtime
        except OSError as exc:
            checks.append({"name": name, "status": "warn",
                           "detail": f"{stem} unreadable ({exc})"})
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        stamped = payload.get("ts") if isinstance(payload, dict) else None
        declared = payload.get("next_delay_s") if isinstance(payload, dict) else None
        ts = _positive_seconds(stamped)
        delay = _positive_seconds(declared)
        caveats = []
        if ts is None:
            ts = mtime
            caveats.append("no usable `ts`, fell back to mtime (sync can rewrite it)")
        if delay is None:
            delay = _DYNAMIC_LOOP_DEFAULT_DELAY_S
            caveats.append(f"no usable `next_delay_s`, assumed {int(delay // 60)}m")
        note = f" [{'; '.join(caveats)}]" if caveats else ""
        age = time.time() - ts
        warn_at = delay + 120
        down_at = 2 * delay + 300
        seen = f"last re-arm {age / 60:.1f}m ago, cadence {delay / 60:.0f}m{note}"
        if age > down_at:
            checks.append({"name": name, "status": "down",
                           "detail": f"{loop}: {seen} — past {down_at / 60:.0f}m; the loop has "
                                     f"stopped re-arming and no cron or process check can see it"})
        elif age > warn_at:
            checks.append({"name": name, "status": "warn",
                           "detail": f"{loop}: {seen} — past its own {warn_at / 60:.0f}m re-arm deadline"})
        else:
            checks.append({"name": name, "status": "ok", "detail": f"{loop}: {seen}"})
    return checks


GLOBAL_PIN_SCOPE = "global"


def _pin_scope_flag(scope: str) -> str:
    """tmux addresses the global environment with -g, never with -t '=global'."""
    return "-g" if scope == GLOBAL_PIN_SCOPE else f"-t '={scope}'"


def _core_argv_pins(socket: str, sessions: list) -> list:
    """[(session, model)] for live cores pinned via argv; (session, None) when no
    argv could be read. argv is immutable, so a tmux clear cannot undo a pin."""
    out = []
    for sess in sessions:
        try:
            pp = subprocess.run(
                # -s spans every window. Without it tmux reports only the ACTIVE
                # window, so a core in window 0 is invisible whenever another is up.
                ["tmux", "-S", socket, "list-panes", "-s", "-t", f"={sess}",
                 "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=10)
            if pp.returncode != 0:
                # A failed read is not an absent pin. Without this the probe
                # reports ok, which is the silent direction.
                out.append((sess, None))
                continue
            pids = [x.strip() for x in (pp.stdout or "").split("\n")
                    if x.strip().isdigit()]
            if not pids:
                # A live session ALWAYS has >=1 pane, so a successful list that
                # yields no pid enumerated nothing — unknown, never "no panes".
                out.append((sess, None))
                continue
            read_failed = False
            for pid in pids:
                ps = subprocess.run(["ps", "-o", "args=", "-p", pid],
                                    capture_output=True, text=True, timeout=10)
                if ps.returncode != 0:
                    # ps failing (permissions, or the pane exited mid-scan) is
                    # indistinguishable from a non-claude pane by argv alone.
                    read_failed = True
                    continue
                argv = (ps.stdout or "").strip()
                if not argv:
                    # rc 0 with EMPTY stdout is not "this pane is not claude" —
                    # nothing was read, so the pane cannot be ruled out.
                    read_failed = True
                    continue
                if "claude" not in argv:
                    continue
                m = re.search(r"--model[= ]+(\S+)", argv)
                if m:
                    out.append((sess, m.group(1)))
            # ANY unread pane leaves the session unverified: a readable unpinned pane says
            # nothing about the one that failed, which may be the pinned core.
            if read_failed:
                out.append((sess, None))
        except (OSError, subprocess.SubprocessError):
            out.append((sess, None))
    return out


def _interpret_core_model_pin(pinned: list, socket: str, running=()) -> dict:
    """Interpret tmux pins AND the live core's argv. A tmux clear cannot change an
    already-running process, so argv must be reported even when tmux is clean."""
    name = "core-model-pin"
    live = [(s, v) for s, v in running if v and v.strip()]
    # An empty-string argv read is as unverified as None; both mean "ps told us
    # nothing", so neither may be reported as a core confirmed unpinned.
    unknown = [s for s, v in running if not (v and v.strip())]
    if not pinned and not live:
        if unknown:
            # WARN, not ok: emit_task_for_failures() gates on status, so an
            # ok-with-caveat reaches the human channel and nothing else.
            return {"name": name, "status": "warn",
                    "detail": (f"could not read argv for a LIVE core session "
                               f"({', '.join(sorted(unknown))}), so it cannot be "
                               f"confirmed unpinned — the tmux env is clear, but a "
                               f"clear cannot move a running core off a pinned model")}
        return {"name": name, "status": "ok",
                "detail": ("no model pin on any session or the global env "
                           "(core uses the default window)")}
    if live:
        where_live = ", ".join(f"{s} argv={v!r}" for s, v in live)
        extra = ""
        if pinned:
            extra = (" The tmux env is ALSO pinned (" +
                     ", ".join(f"{s}={v!r}" for s, v in pinned) + ").")
        elif not pinned:
            extra = (" The tmux env is already clear, so this will NOT show up as an "
                     "env pin — only a restart moves a running core off it.")
        return {"name": name, "status": "warn",
                "detail": (f"a LIVE core is running on a pinned model ({where_live}). "
                           f"argv is immutable.{extra}")}
    where = ", ".join(f"{scope}={val!r}" for scope, val in pinned)
    # Name the scope in the remedy: an operator copies the emitted line, and a
    # `setenv -u` without -t/-g can clear a different scope than the pinned one.
    fixes = "; ".join(
        f"tmux -S {socket} setenv {_pin_scope_flag(scope)} -u SUTANDO_CORE_MODEL"
        for scope, _ in pinned
    )
    return {
        "name": name,
        "status": "warn",
        "detail": (
            f"core model is PINNED ({where}) — nothing in this repo sets it, so this is "
            f"left over or was set by hand. The Claude launcher ignores it and clears both "
            f"tmux scopes on its next start/attach/restart, so a Claude core self-heals; "
            f"the Codex launcher still honors it (docs/codex-core.md). To clear it now "
            f"without waiting for a launch: {fixes}."
        ),
    }


def _tmux_sessions(socket: str) -> list:  # pragma: no cover — thin tmux glue
    res = subprocess.run(
        ["tmux", "-S", socket, "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        # A failed enumeration is not an empty socket: returning [] here would
        # report "no pin" without having inspected a single session.
        raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


TMUX_UNSET_MARKER = "unknown variable"

# tmux reached no server at all. A socket FILE can outlive its server, so this is
# "there is no core to be pinned" — not a query that failed against a live one.
TMUX_NO_SERVER_MARKERS = ("no server running", "error connecting to")


def _tmux_no_server(exc) -> bool:
    parts = [str(exc), getattr(exc, "stderr", "") or "", getattr(exc, "output", "") or ""]
    text = " ".join(str(p) for p in parts).lower()
    return any(m in text for m in TMUX_NO_SERVER_MARKERS)


def _query_pin(socket: str, scope_args: list) -> str:
    """Pinned value for one tmux env scope, or "" when tmux says unset.
    Only tmux's marker means absence; any other nonzero is a FAILED query."""
    res = subprocess.run(
        ["tmux", "-S", socket, "show-environment", *scope_args, "SUTANDO_CORE_MODEL"],
        capture_output=True, text=True, timeout=10,
    )
    out = (res.stdout or "").strip()
    if res.returncode == 0:
        return out.split("=", 1)[1] if out.startswith("SUTANDO_CORE_MODEL=") else ""
    # The marker can arrive on either stream; accept both.
    if TMUX_UNSET_MARKER in (out + " " + (res.stderr or "")):
        return ""
    raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)


def check_core_model_pin() -> dict:
    """Report a core running the wedge-recovery model downgrade. Queries every
    session AND the global env, since tmux's implicit target is not reliable."""
    name = "core-model-pin"
    socket = os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock")
    if not Path(socket).exists():
        return {"name": name, "status": "ok", "detail": "no core tmux socket — skipped"}
    try:
        pinned = []
        # -g first: a global pin needs no session to exist, and a per-session
        # query never reports it, so enumerating sessions alone can miss it.
        val = _query_pin(socket, ["-g"])
        if val:
            pinned.append((GLOBAL_PIN_SCOPE, val))
        for sess in _tmux_sessions(socket):
            val = _query_pin(socket, ["-t", f"={sess}"])
            if val:
                pinned.append((sess, val))
    except (OSError, subprocess.SubprocessError) as e:
        if _tmux_no_server(e):
            return {"name": name, "status": "ok",
                    "detail": f"no tmux server on {socket} — no core to be pinned ({e})"}
        # Not an absent pin: nothing was inspected. emit_task_for_failures() gates
        # on status, so ok here silences the one case that needs a human.
        return {"name": name, "status": "warn",
                "detail": (f"could not query the core tmux env ({e}), so neither the "
                           f"global scope nor any session was inspected — a live core "
                           f"may still be pinned and this probe cannot see it")}
    try:
        sessions = _tmux_sessions(socket)
    except (OSError, subprocess.SubprocessError) as e:
        if _tmux_no_server(e):
            return _interpret_core_model_pin(pinned, socket, ())
        # sessions=[] here would report a clean argv pass having read no core at all.
        return {"name": name, "status": "warn",
                "detail": (f"could not enumerate core tmux sessions ({e}), so no core "
                           f"argv was inspected — the tmux env is clear, but a clear "
                           f"cannot move a running core off a pinned model")}
    return _interpret_core_model_pin(pinned, socket, _core_argv_pins(socket, sessions))


def _process_executes_artifact(artifact: Path, pgrep_pattern: str) -> bool:
    """True when a live process matching `pgrep_pattern` has `artifact` in argv.
    A dev host can hold a build it never executes, so gate on the process."""
    if not artifact.exists():
        return False
    try:
        out = subprocess.run(["/usr/bin/pgrep", "-f", pgrep_pattern],
                             capture_output=True, text=True, timeout=5).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False
    return any(str(artifact) in _proc_argv(int(pid))
               for pid in out.split() if pid.isdigit())


def check_credential_proxy() -> dict:
    """Credential proxy (port 7846). probe=False: a forwarding proxy has no
    liveness endpoint, so an HTTP probe is forwarded and misread as wedged."""
    check = check_port(7846, "credential-proxy", probe=False)
    if check["status"] == "down":
        check["status"] = "warn"
        check["detail"] = "not running (optional)"
    elif check["status"] == "ok":
        artifact = REPO_DIR / "dist" / "credential-proxy.js"
        mark_stale_if_outdated(
            check,
            REPO_DIR / "skills" / "quota-tracker" / "scripts" / "credential-proxy.ts",
            "credential-proxy",
            binary_path=(artifact
                         if _process_executes_artifact(artifact, "credential-proxy")
                         else None),
        )
    return check
MENUBAR_LABEL = "com.sutando.menubar"
MENUBAR_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MENUBAR_LABEL}.plist"


def menubar_app_state(dev_bin, app_bin, plist, is_macos: bool) -> str:
    """installed | expected-missing | not-applicable for the optional menu-bar app.
    The launchd plist is the only durable signal a host ASKED for the app."""
    if dev_bin.exists() or app_bin.exists():
        return "installed"
    if not is_macos:
        return "not-applicable"
    return "expected-missing" if plist.exists() else "not-applicable"


def run_all_checks() -> list[dict]:
    checks = []

    # Core services (required)
    checks.extend(check_voice_stack())

    web_config = resolve_web_client_port()
    if web_config.get("error"):
        web_check = {
            "name": "web-client",
            "status": "down",
            "detail": web_config["error"],
        }
    else:
        web_check = check_port(web_config["port"], "web-client", probe=True)
    if web_check["status"] == "ok":
        mark_stale_if_outdated(web_check, REPO_DIR / "src" / "web-client.ts", "web-client.ts")
    checks.append(web_check)

    # Optional services (downgrade missing to warning, not failure)
    for port, name in [(7843, "agent-api"), (7844, "dashboard"), (7845, "screen-capture")]:
        c = check_port(port, name, probe=True)
        if c["status"] == "down":
            c["status"] = "warn"
            c["detail"] = "not running (optional)"
        # "wedged" is NOT downgraded: listening-but-dead is worse than down —
        # startup.sh's lsof guard sees the port as occupied and won't restart it.
        checks.append(c)

    # Previously unmonitored, so a dead proxy (= broken auth/quota for
    # proxy-routed cores) never surfaced on the dashboard.
    proxy_check = check_credential_proxy()
    checks.append(proxy_check)

    # Quota telemetry — only meaningful when the proxy is actually up.
    checks.append(check_quota_telemetry(proxy_check["status"]))
    # ...and WHOSE account those numbers describe. The check above answers
    # "fresh?"; this one answers "ours?" — a fresh file for a foreign account
    # passes every branch above (observed 2026-08-03).
    checks.append(check_quota_account_identity(proxy_check["status"]))

    # Core over-quota — fail loudly to the remote owner surface so an exhausted
    # model no longer stalls every task silently (owner-reported 2026-08-01).
    checks.append(check_core_quota_exhausted())

    # G1.5: which Node would JS services resolve to (bundled/app-bundle/
    # system), red when none — the silent-dead-services failure class.
    checks.append(check_node_runtime())
    # Comm-handling liveness (P1): loud when the owner-comm sweep goes stale.
    checks.append(check_comm_sweep_freshness())
    checks.extend(check_dynamic_loop_freshness())
    # Vault name/secret divergence: list_vault_keys() advertising keys that
    # get_vault_key() cannot resolve — silent until an integration calls both.
    checks.append(check_vault_manifest_integrity())
    checks.append(check_claude_hook_registration())
    checks.append(check_cron_runner())
    checks.append(check_session_cron_registration())

    # macOS TCC — must come before critical-file checks so if TCC is blocking
    # everything, the operator sees the root cause before the downstream failures.
    checks.append(check_tcc_documents_access())

    # Critical files
    for name, path in [
        ("CLAUDE.md", REPO_DIR / "CLAUDE.md"),
        ("build_log.md", WORKSPACE_DIR / "build_log.md"),
        (".env", _resolve_dotenv()),
    ]:
        checks.append(check_file(path, name))

    # Memory system (check if dir exists — specific files are optional)
    if MEMORY_DIR.exists():
        checks.append(check_directory(MEMORY_DIR, "memory-dir"))
    else:
        checks.append({"name": "memory-dir", "status": "ok", "detail": "not yet created (normal for new installs)"})

    _mem_override = check_memory_dir_override()
    if _mem_override:
        checks.append(_mem_override)

    _mem_siblings = check_memory_dir_siblings()
    if _mem_siblings:
        checks.append(_mem_siblings)

    _root_tidy = check_workspace_root_tidy()
    if _root_tidy:
        checks.append(_root_tidy)

    _wiring = check_workspace_wiring()
    if _wiring:
        checks.append(_wiring)

    _mem_index = check_memory_index_integrity()
    if _mem_index:
        checks.append(_mem_index)

    # Carrier-set enforcement — a stale exclude means the vault is silently not
    # backing up paths the config says it carries (#2565).
    checks.extend(c for c in (check_carrier_set_enforced(),) if c)

    # Notes — canonical home is the resolved workspace post-migration.
    # Pass WORKSPACE_DIR (not REPO_DIR) so the check resolves to
    # <workspace>/notes rather than <repo>/notes — the notes/.gitkeep was
    # removed from the repo in #793's workspace migration. Post-v0.8
    # (#1440) the workspace defaults to <repo>/workspace/.
    checks.append(check_directory(Path(shared_personal_path("notes", WORKSPACE_DIR)), "notes-dir"))

    # Notes split-brain: both <repo>/notes/ and <workspace>/notes/ with overlapping files (#1266)
    _notes_sb = check_notes_split_brain()
    if _notes_sb:
        checks.append(_notes_sb)

    # Sibling of the above, for the pair it cannot reach (see its docstring).
    _legacy_nd = check_legacy_notes_divergence()
    if _legacy_nd:
        checks.append(_legacy_nd)

    # Memory sync
    checks.append(check_memory_sync())

    # Per-host subtree freshness (hosts/<host>/ stopped syncing?)
    checks.append(check_host_subtrees())
    # Per-host channel access.json backup drift (live vs vault-carried copy)
    checks.append(check_per_host_config_backup())
    # Live checkout on its expected branch (PR-branch drift, 2026-07-29 incident)
    checks.append(check_live_checkout_branch())
    checks.append(check_engine_revision_drift())
    onboarding_check = check_onboarding_status()
    if onboarding_check is not None:
        checks.append(onboarding_check)

    # Migration/reader path-contract drift (#1543)
    checks.append(check_migrate_reader_contract())

    # Phone conversation server (optional — only check if Twilio configured and not skipped)
    env_path = _resolve_dotenv()  # pragma: no cover — call-site in untested mega-function
    if env_path.exists():
        env_content = env_path.read_text()
        has_twilio = twilio_configured(env_content)  # pragma: no cover — call-site in untested mega-function
        skip_phone = "SKIP_PHONE=1" in env_content or os.environ.get("SKIP_PHONE") == "1"
        if has_twilio and not skip_phone:
            c = check_port(3100, "conversation-server")
            if c["status"] != "ok":
                c["status"] = "warn"
                c["detail"] = "not running (starts on demand)"
            else:
                mark_stale_if_outdated(
                    c,
                    REPO_DIR / "skills" / "phone-conversation" / "scripts" / "conversation-server.ts",
                    "conversation-server.ts",
                )
            checks.append(c)
            # Tunnel check — depends on TWILIO_WEBHOOK_URL host (Funnel) or ngrok.
            # Skip the whole block when TWILIO_WEBHOOK_URL is unset/empty: with
            # no inbound webhook, no tunnel is required, so flagging ngrok
            # "down — phone calls won't reach server" would be a false alarm
            # (issue #710). The has_twilio gate above only requires
            # TWILIO_ACCOUNT_SID, which the owner may set for outbound-only.
            if c["status"] == "ok":
                webhook_url = ""
                for line in env_content.splitlines():
                    if line.startswith("TWILIO_WEBHOOK_URL="):
                        webhook_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
                if webhook_url:
                    from urllib.parse import urlparse as _urlparse
                    _host = _urlparse(webhook_url).hostname or ""
                    is_funnel = _host.endswith(".ts.net")
                    if is_funnel:
                        # Tailscale Funnel — verify funnel is serving and reachable
                        funnel_c = {"name": "tailscale-funnel", "status": "ok", "detail": f"serving {webhook_url}"}
                        try:
                            import urllib.request
                            req = urllib.request.Request(f"{webhook_url}/health", headers={"User-Agent": "sutando-healthcheck"})
                            with urllib.request.urlopen(req, timeout=5) as resp:
                                if resp.status != 200:
                                    funnel_c["status"] = "down"
                                    funnel_c["detail"] = f"webhook returned {resp.status}"
                        except Exception as e:
                            funnel_c["status"] = "down"
                            funnel_c["detail"] = f"unreachable: {str(e)[:60]}"
                        checks.append(funnel_c)
                    else:
                        ngrok_c = check_port(4040, "ngrok")
                        if ngrok_c["status"] == "ok":
                            ngrok_c["detail"] = "tunnel active (port 4040)"
                        else:
                            # Critical: phone calls fail without ngrok
                            ngrok_c["status"] = "down"
                            ngrok_c["detail"] = "not running — phone calls won't reach server"
                        checks.append(ngrok_c)

    # Messaging bridges (optional — only check if configured and not skipped)
    channels_dir = claude_home_path("channels")
    for name, proc_name in [("telegram-bridge", "telegram-bridge"), ("discord-bridge", "discord-bridge"),
                            ("slack-bridge", "slack-bridge")]:
        channel_name = name.replace("-bridge", "")
        if _should_skip_bridge(channel_name, env_path):
            continue
        env_file = channels_dir / channel_name / ".env"
        access_file = channels_dir / channel_name / "access.json"
        # Check if configured via either .env or access.json
        if not env_file.exists() and not access_file.exists():
            continue
        try:
            # Anchor on the .py suffix so we don't match unrelated processes
            # whose command line happens to contain "discord-bridge" (shell
            # invocations, ps/grep pipelines, etc). Otherwise pgrep -f bare
            # name produces false-positive "multiple processes" warnings
            # that scared us into thinking the bridges were zombied today.
            result = subprocess.run(["/usr/bin/pgrep", "-f", f"{proc_name}\\.py$"], capture_output=True, text=True)
            pids = result.stdout.strip().split("\n") if result.returncode == 0 else []
            pids = [p for p in pids if p]
            # A launcher's argv ends with the same script path, so it matches
            # this pgrep too — see _drop_launcher_parents.
            pids = _drop_launcher_parents(pids)
        except Exception:
            pids = []

        if not pids:
            # This exact detail string is a contract: fix_down_bridges()
            # matches it verbatim to pick restart candidates (and the
            # health-check-fix-down-bridges test locks it). Change both
            # together or --fix goes blind to dead bridges again.
            checks.append({"name": name, "status": "warn", "detail": "configured but not running"})
            continue

        # Check 1: Multiple processes (zombie/duplicate)
        if len(pids) > 1:
            checks.append({"name": name, "status": "warn", "detail": f"multiple processes ({len(pids)} PIDs: {','.join(pids)})"})
            continue

        # Check 2: Log file freshness — prefer logs/ (where startup.sh writes)
        # and fall back to src/ for legacy. The src/ default was silently a
        # no-op since 2026-04 when startup.sh was changed to write logs/<name>.log,
        # so log-stale warnings never fired (caught 2026-05-05 when Mini's
        # logs/discord-bridge.log was 36h stale but health-check stayed "ok").
        import time
        log_file = WORKSPACE_DIR / "logs" / f"{name}.log"
        if not log_file.exists():
            log_file = REPO_DIR / "src" / f"{name}.log"
        detail = "running"
        status = "ok"
        if log_file.exists():
            age_sec = time.time() - log_file.stat().st_mtime
            if age_sec > 300:  # 5 minutes
                status = "warn"
                detail = f"running but log stale ({int(age_sec)}s old)"

        # Check 3: Heartbeat file freshness (overrides log staleness if fresh)
        heartbeat_file = WORKSPACE_DIR / "state" / f"{name}.heartbeat"
        if heartbeat_file.exists():
            hb_age = time.time() - heartbeat_file.stat().st_mtime
            if hb_age <= 120:  # heartbeat is fresh — bridge is alive
                status = "ok"
                detail = "running"
            else:
                status = "warn"
                detail = f"running but heartbeat stale ({int(hb_age)}s old)"

        # Check 4: Stale code — process started before the source file's last
        # modification. This catches the case where a fix is on disk but the
        # running process is from a previous version (e.g., PR #203 silently
        # not in effect because nobody restarted the bridge after merge).
        proc_start = None
        try:
            src_file = REPO_DIR / "src" / f"{name}.py"
            if src_file.exists() and pids:
                src_mtime = src_file.stat().st_mtime
                # Use ps to get process start time as Unix epoch
                ps_out = subprocess.run(
                    ["/bin/ps", "-o", "lstart=", "-p", pids[0]],
                    capture_output=True, text=True, timeout=5
                ).stdout.strip()
                if ps_out:
                    from datetime import datetime as _dt
                    proc_start = _dt.strptime(ps_out, "%a %b %d %H:%M:%S %Y").timestamp()
                    # Threshold tuned to avoid false positives from `git checkout`
                    # which bumps the mtime of every file that differs between
                    # branches, even when content is identical. Real stale deploys
                    # (the original target of #228) are usually hours/days old,
                    # so 30 min comfortably catches them while tolerating routine
                    # branch switching.
                    if src_mtime - proc_start > 1800:  # source >30 min newer
                        # Cross-check with git before flagging — #253 added this
                        # for voice-agent + web-client via mark_stale_if_outdated,
                        # this path does the same check inline to reach bridges.
                        if not _file_unchanged_since(src_file, proc_start):
                            status = "stale"
                            detail = f"running but code is {int((src_mtime - proc_start) / 60)} min newer than process — restart needed"
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass

        # Check 5: Dead-log-inode detection — last so heartbeat doesn't override.
        # Bridge process FDs point to a path that's been renamed/deleted (the
        # 2026-05-05 case where discord-bridge stdout was going to
        # /discord-bridge.log.bak after the file was unlinked, so logging
        # silently went to /dev/null while bridge appeared healthy). Heartbeats
        # don't catch this because they're written to a separate file via
        # state/<name>.heartbeat.
        try:
            lsof_out = subprocess.run(
                ["/usr/sbin/lsof", "-p", pids[0]], capture_output=True, text=True, timeout=5
            ).stdout
            for line in lsof_out.splitlines():
                parts = line.split()
                # Columns: COMMAND PID USER FD TYPE DEVICE SIZE NODE NAME
                # FD column is index 3 — "1w" or "2w" carry log writes
                if len(parts) < 9 or parts[3] not in ("1w", "2w"):
                    continue
                # NAME starts at col 8 — join remaining tokens to handle paths
                # with spaces (per MacBook's PR #596 review nit).
                log_path = " ".join(parts[8:])
                if log_path.endswith(".log") or log_path.endswith(".log.bak"):
                    if not Path(log_path).exists():
                        status = "warn"
                        detail = (
                            f"running but log inode dead ({log_path} unlinked) — "
                            f"restart with: launchctl kickstart -k gui/$(id -u)/com.sutando.{name} "
                            "(or nohup+disown on Mini)"
                        )
                        break
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        # Check 6: Log-content health for known failure modes.
        # discord-bridge: LoginFailure means the token is revoked/invalid.
        #   Always overrides — there is no point restarting with stale code
        #   if the token is bad; the token fix is the only path forward.
        # slack-bridge: "60s elapsed" hint means Socket Mode connected but
        #   events aren't routing (Slack app Event Subscriptions disabled).
        #   Only overrides "ok" — stale/dead-inode are higher priority.
        # telegram-bridge: a 409 Conflict is a second poller taking a share of
        #   the updates. Overrides "ok" and the stale-HEARTBEAT warn only; the
        #   stale-log and dead-inode warns must survive, so detail is passed.
        if (log_file.exists() and name in ("discord-bridge", "slack-bridge", "telegram-bridge")
                and _bridge_log_belongs_to_process(log_file, proc_start)):
            try:
                tail = log_file.read_text(errors="replace").splitlines()[-60:]
                override = bridge_log_content_status(name, status, tail, detail,
                                                     log_path=log_file)
                if override is not None:
                    status, detail = override
            except OSError:
                pass

        checks.append({"name": name, "status": status, "detail": detail})

    # ag2.space gateway bridge (mobile path); check_gateway_bridge() returns
    # None when the gateway isn't configured, so filter it out. (The function's
    # branches are unit-tested in tests/health-check-gateway-bridge.test.py; this
    # call site is exercised by the running health check, not that unit test.)
    checks += [c for c in (check_gateway_bridge(),) if c is not None]  # pragma: no cover

    # (External plugin probes moved out with their plugins in #1427 round ④ —
    # a plugin manifest declares its own health_probe; the host checks host
    # services only.)

    # Sutando menu bar app — check either dev-built binary or installed .app.
    # On the distributed .app path the dev binary doesn't ship; we still want
    # the menu bar check to run so dashboard reports accurate status.
    dev_bin = REPO_DIR / "src" / "Sutando" / "Sutando"
    app_bin = Path("/Applications/Sutando.app/Contents/MacOS/Sutando")
    _menubar = menubar_app_state(
        dev_bin, app_bin, MENUBAR_PLIST, sys.platform == "darwin")
    if _menubar == "installed":
        # Distinguish pgrep failures (exit code != 0 and != 1) from a real
        # no-match (exit code 1). Pre-fix the bare try/except swallowed
        # subprocess errors AND empty results into a single "no pids" path,
        # which surfaced as a false "not running" warn when pgrep itself
        # hiccupped (CPU contention, fd exhaustion, etc.). Chi hit this
        # 2026-05-18 — app was alive (PID 34586 since May 17) but a
        # health-check tick reported "not running."
        pgrep_status = None  # "ok-running" | "ok-stopped" | "error"
        pgrep_err = ""
        pids: list[str] = []
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-f", "(Sutando|MacOS)/Sutando"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                pids = [p for p in result.stdout.strip().split("\n") if p]
                pgrep_status = "ok-running"
            elif result.returncode == 1:
                # pgrep convention: 1 = no match
                pgrep_status = "ok-stopped"
            else:
                pgrep_status = "error"
                pgrep_err = (result.stderr or f"pgrep exit={result.returncode}").strip()[:120]
        except Exception as e:
            pgrep_status = "error"
            pgrep_err = f"{type(e).__name__}: {e}"[:120]

        # Disqualify Electron impostors (see _resolve_menu_bar_pgrep).
        pgrep_status, pids = _resolve_menu_bar_pgrep(pgrep_status, pids)

        if pgrep_status == "ok-running" and pids:
            # pragma: no cover — reachable only when pgrep finds the macOS
            # menu-bar app (never on ubuntu CI); detail derivation is covered
            # at helper level in health-check-sutando-app-hotkeys.test.py.
            check = {"name": "sutando-app", "status": "ok",  # pragma: no cover
                     "detail": sutando_app_hotkey_detail(WORKSPACE_DIR)}
            # Staleness check is meaningful only in the dev workflow — the
            # .app binary and bundled main.swift share a build mtime, so a
            # comparison there is always equal. Skip when dev_bin missing.
            if dev_bin.exists():
                mark_stale_if_outdated(
                    check,
                    sutando_app_newest_source(),
                    "(Sutando|MacOS)/Sutando",
                    binary_path=dev_bin,
                )
            checks.append(check)
        elif pgrep_status == "ok-stopped":
            checks.append({"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"})
        else:
            # pgrep itself errored — don't false-alarm "not running" when we
            # actually couldn't determine state. Surface as a transient warn
            # with the cause so it's debuggable, not a routine "app is down."
            checks.append({"name": "sutando-app", "status": "warn", "detail": f"detection failed (pgrep: {pgrep_err or 'unknown error'}) — actual app state unknown"})
    elif _menubar == "expected-missing":
        # Only a host that ASKED for the app can be missing it; a headless
        # install has no plist and gets no row, like check_gateway_bridge().
        checks.append({"name": "sutando-app", "status": "warn", "detail": (
            f"launchd job {MENUBAR_LABEL} is installed but no binary exists at "
            f"{dev_bin} or {app_bin} — app state UNKNOWN, not ok")})

    # Battery and memory health checks

    # Stuck-loop / queue-pileup detection — consequence-level signals that
    # fire whether the watcher died, the proactive loop crashed mid-pass, or
    # both. Independent of which mechanism died.
    loop_stale_sec = int(os.environ.get("SUTANDO_HEALTH_LOOP_STALE_SEC", "600"))
    queue_age_sec = int(os.environ.get("SUTANDO_HEALTH_QUEUE_AGE_SEC", "300"))
    queue_count = int(os.environ.get("SUTANDO_HEALTH_QUEUE_COUNT", "3"))
    checks.append(check_battery())
    checks.append(check_memory())
    checks.append(check_core_proactive_loop(threshold_sec=loop_stale_sec))
    checks.append(check_core_supervisor())
    checks.append(check_task_queue(threshold_count=queue_count, threshold_age_sec=queue_age_sec))
    checks.append(check_orphaned_results())
    checks.append(check_proactive_quarantine())
    checks.append(check_stranded_destined_proactive())
    checks.append(check_stale_proactive_backlog())
    checks.append(check_task_watcher())
    checks.append(check_task_claim_age())
    checks.append(check_a_fallback_hits())
    checks.append(check_codex_task_notifier())
    checks.append(check_skill_symlinks())
    checks.append(check_core_model_pin())
    checks.append(check_daily_cron_punctuality())
    checks.append(check_runtime_identity())
    checks.append(check_disk_space())

    return checks


def _any_core_alive(workspace: Optional[Path] = None, max_age_s: float = 90.0) -> bool:
    """Return True if any sutando-core on any host has a live heartbeat.

    Each running core writes `<workspace>/state/cores/<hostname>.alive` every
    30 seconds (src/core_heartbeat.py). A file younger than `max_age_s` (3
    missed beats at 30s each) means the core is alive. When it is, the
    proactive loop already handles health inline — no need to queue a task.

    `workspace` defaults to `WORKSPACE_DIR` at call time (not at import time)
    so tests can patch the module-level name and have the change take effect.
    """
    if workspace is None:
        workspace = WORKSPACE_DIR
    cores_dir = workspace / "state" / "cores"
    if not cores_dir.is_dir():
        return False
    now = time.time()
    for alive_file in cores_dir.glob("*.alive"):
        try:
            if heartbeat_is_fresh(alive_file.stat().st_mtime, now, max_age_s):
                return True
        except OSError:
            pass
    return False


def _alerts_suppressed(check: dict) -> bool:
    """True when a check must NOT wake anyone, whatever its status says.

    `main()` computes a local `issues` list, and it is tempting to treat that as
    "what alerts". It is not. `--emit-task`, `--notify-on-fail` and
    `--notify-slack` each consume the FULL `checks` list through their own
    filters, and all three count a plain `warn` as a failure. A carve-out tested
    only against `issues` therefore still fires a task and a macOS notification
    on the first transition (qingyun-wu, #2570 — verified on the exact head with
    a one-check witness: notification written, 1 Slack message, 1 task file).

    So suppression has to be an explicit property of the CHECK, honored at every
    surface that can wake someone, rather than a status the reader hopes is
    benign. A probe sets `"alerting": False` when its result is genuinely
    informational — visible on the dashboard, never a page.

    Deliberately narrow: only an explicit `False` suppresses. A missing key
    alerts, so no existing check changes behavior by omission, and a typo cannot
    silence a real failure.
    """
    return check.get("alerting") is False


def emit_task_for_failures(checks: list[dict], state_file: Optional[Path] = None, tasks_dir: Optional[Path] = None) -> None:
    """Emit a task file describing health-check failures so the proactive
    loop's CLI session sees them via the watcher and can decide what to do
    (restart, DM owner, ignore as transient).

    Bridges the detection-vs-action gap: dashboard + morning-briefing already
    surface failures to the user, but no path drove the AGENT to act on them.
    Now health-check at any cron tick can produce a task file → watcher fires
    → CLI processes as owner-tier task → LLM judgment at the act step.

    Dedup via failure-SET hash, alerting only on a TRANSITION — the set
    changing from what was last alerted (one service recovers, another
    fails, or a wholly new failure appears). A persistent, unchanged
    failure set does NOT re-fire on a timer; per-hash timestamps used to
    expire after a 1h cooldown and then re-alert the identical set forever,
    which is exactly the "spams me hourly about the same known issue" bug
    (owner complaint 2026-07-01) — a set that never resolves (e.g. an
    intentionally-unconfigured optional feature) alerted once per hour,
    indefinitely. Fixed by tracking only the MOST RECENTLY alerted hash
    (`_LAST_HASH_KEY`) and suppressing whenever the current hash matches it,
    regardless of elapsed time.

    `state_file` and `tasks_dir` default to the workspace paths used in
    production. Tests inject temp paths.
    """
    # `warn` is the status used for "service is up but has a real issue"
    # (e.g., the dead-log-inode case from PR #596 — bridge running but
    # logging silently to a deleted file). Including warn means the
    # watchdog catches the bug class that motivated this PR. Excluding
    # would have missed Mini's discord-bridge issue this morning. Per
    # her PR review note 2026-05-05.
    failures = [c for c in checks
                if c["status"] in ("down", "missing", "not_loaded", "fail", "stale", "warn")
                and not _alerts_suppressed(c)]
    if not failures:
        return

    if state_file is None or tasks_dir is None:
        if state_file is None:
            state_file = WORKSPACE_DIR / "state" / "health-last-alerted.json"
        if tasks_dir is None:
            # Durable-lane address comes from the endpoint resolver; this is
            # the crash-path writer, so descriptor failure falls back locally.
            try:
                from agent_endpoint import load_descriptor, resolve
                tasks_dir = Path(resolve("self", "durable", load_descriptor()).address)
            except Exception as e:
                print(f"health-check: endpoint resolve failed ({e}); "
                      f"falling back to workspace tasks dir", file=sys.stderr)
                tasks_dir = WORKSPACE_DIR / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Hash the full failure set (sorted) — Mini's #2 review note: hash MUST
    # cover the active set, not member-by-member, else suppressing legit
    # re-alerts when the set is identical to last alert.
    set_key = "|".join(sorted(c["name"] for c in failures))
    hash_key = hashlib.sha256(set_key.encode()).hexdigest()[:16]
    now_ms = int(time.time() * 1000)

    # Read prior alert state.
    history: dict = {}
    try:
        if state_file.exists():
            history = json.loads(state_file.read_text())
    except Exception:
        history = {}

    if history.get(_LAST_HASH_KEY) == hash_key:
        # Unchanged failure set since the last alert — no re-fire, no matter
        # how much time has passed. Only a transition re-alerts.
        return

    # task-last shape via the protocol's write side: the multi-line bullet
    # body cannot shadow source/access_tier/priority.
    ts_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    bullet_str = "\n".join(f"- {c['name']}: {c['status']} ({c['detail']})" for c in failures)
    from local_task_protocol import write_task_file
    task_path = write_task_file(
        tasks_dir, f"task-health-{now_ms}",
        [("id", f"task-health-{now_ms}"),
         ("timestamp", ts_iso),
         ("source", "health-check"),
         ("interaction_type", "system_event"),
         ("user_id", "health-check"),
         ("access_tier", "owner"),
         ("priority", "low")],
        "Health check found issues. Decide whether to restart, DM owner, "
        "or treat as transient:\n" + bullet_str)

    # Update history. Prune timestamp entries older than 24h to bound file
    # size — `_LAST_HASH_KEY` is a hash string, not a timestamp, so it's
    # excluded from the age comparison and re-added after pruning.
    history[hash_key] = now_ms
    history[_LAST_HASH_KEY] = hash_key
    cutoff = now_ms - (24 * 3600 * 1000)
    history = {k: v for k, v in history.items() if k == _LAST_HASH_KEY or v >= cutoff}
    try:
        state_file.write_text(json.dumps(history))
    except Exception:
        pass


def notify_for_failures(
    checks: list[dict],
    state_file: Optional[Path] = None,
    notify_cmd: Optional[list[str]] = None,
) -> None:
    """Surface health-check failures via macOS notification.

    Companion to `emit_task_for_failures` — same dedup contract: alert only
    on a TRANSITION of the failure-set hash (not a timed re-fire of an
    unchanged set — see `_LAST_HASH_KEY`), separate state file. Two surfaces
    are needed for robustness: emit-task only delivers if the agent is alive
    to read tasks/, osascript runs at OS level and surfaces even when every
    Sutando service is dead. The launchd-supervised fallback health-check
    (com.sutando.health-check-fallback) relies on this property — it's the
    alert path that survives "all of Sutando is down."

    `notify_cmd` is the executable + args used to fire the notification;
    defaults to `osascript` driving `display notification`. Tests inject a
    fake to avoid spamming the developer's own notification center.
    """
    failures = [c for c in checks
                if c["status"] in ("down", "missing", "not_loaded", "fail", "stale", "warn")
                and not _alerts_suppressed(c)]
    if not failures:
        return

    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "health-last-notified.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    set_key = "|".join(sorted(c["name"] for c in failures))
    hash_key = hashlib.sha256(set_key.encode()).hexdigest()[:16]
    now_ms = int(time.time() * 1000)

    history: dict = {}
    try:
        if state_file.exists():
            history = json.loads(state_file.read_text())
    except Exception:
        history = {}

    if history.get(_LAST_HASH_KEY) == hash_key:
        # Unchanged failure set since the last alert — no re-fire.
        return

    # Build a short notification body — macOS truncates aggressively. Lead
    # with count, then top failure names. Full detail is in emit-task.
    names = [c["name"] for c in failures[:3]]
    extra = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
    body = f"{len(failures)} health check failure(s): {', '.join(names)}{extra}"

    # AppleScript single-quote escaping: drop double-quotes and backslashes
    # so the shell command literal can't be broken by check details.
    safe = body.replace('"', '').replace('\\', '')
    cmd = notify_cmd or [
        "osascript", "-e",
        f'display notification "{safe}" with title "Sutando — health check"',
    ]
    try:
        subprocess.run(cmd, check=False, timeout=10)
    except Exception:
        # Notification failure is non-fatal — we still want emit-task to fire.
        pass

    history[hash_key] = now_ms
    history[_LAST_HASH_KEY] = hash_key
    cutoff = now_ms - (24 * 3600 * 1000)
    history = {k: v for k, v in history.items() if k == _LAST_HASH_KEY or v >= cutoff}
    try:
        state_file.write_text(json.dumps(history))
    except Exception:
        pass


def _slack_failures(checks: list[dict]) -> list[dict]:
    """Failures worth a remote owner DM.

    Same hard-failure statuses as notify_for_failures, but drops benign
    on-demand `warn`s (e.g. a plugin server / conversation-server "not running
    (on-demand)") — those are the steady state for per-session processes and
    would spam the owner's DM every cooldown window. The signals that matter
    for a remote watchdog (stuck core-proactive-loop, task-queue pileup, a
    bridge that's actually down) all survive this filter.
    """
    out = []
    for c in checks:
        st = c["status"]
        if _alerts_suppressed(c):
            continue
        if st in ("down", "missing", "not_loaded", "fail", "stale"):
            out.append(c)
        elif st == "warn" and "on-demand" not in (c.get("detail") or "") \
                and not _alerts_suppressed(c):
            out.append(c)
    return out


def _slack_token_from_env_file() -> str:
    """Read SLACK_BOT_TOKEN from disk. The launchd-supervised fallback runs
    with a minimal environment (no sourced .env), so $SLACK_BOT_TOKEN is
    usually absent there — but the token persists on disk. Reading the file
    directly keeps the watchdog self-sufficient without putting the secret in
    the world-readable LaunchAgents plist. Returns "" if absent/unreadable.

    Order matters: the slack bridge's canonical token location is
    $CLAUDE_CONFIG_DIR/channels/slack/.env (startup.sh sources exactly that file before
    launching the bridge — see src/startup.sh). The original implementation
    only checked $REPO/.env, where the token does NOT live on a standard
    install — so the watchdog DM silently no-op'd (creds=None) and the owner
    got no alert at all. Check the channel .env first, then fall back to
    $REPO/.env for hosts that keep it there instead.
    """
    candidates = [
        claude_home_path("channels", "slack", ".env"),
        REPO_DIR / ".env",
    ]
    for env_path in candidates:
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("SLACK_BOT_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:
            continue
    return ""


def _slack_owner_creds() -> "tuple[str, str] | None":
    """Return (bot_token, owner_user_id) for a direct Slack DM, or None.

    Token from $SLACK_BOT_TOKEN (same one the slack bridge uses), falling back
    to the on-disk .env files (channel .env first, then $REPO/.env) for the
    minimal-env launchd path; owner from
    $CLAUDE_CONFIG_DIR/channels/slack/access.json (`tofuOwner`, else first `allowFrom`).
    Both must be present — otherwise there's no one to DM.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip() or _slack_token_from_env_file()
    if not token:
        return None
    access = claude_home_path("channels", "slack", "access.json")
    try:
        data = json.loads(access.read_text())
    except Exception:
        return None
    owner = data.get("tofuOwner")
    if not owner:
        allow = data.get("allowFrom") or []
        owner = allow[0] if allow else None
    if not owner:
        return None
    return token, owner


def _slack_api(token: str, method: str, payload: dict) -> dict:
    """Minimal Slack Web API POST via urllib (no slack_bolt dependency, so
    this works in the launchd-supervised fallback even if the bridge venv
    isn't on the path). Returns the parsed JSON response."""
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _default_slack_sender(text: str) -> bool:
    """Open a DM to the owner and post `text`. Returns True on success."""
    creds = _slack_owner_creds()
    if not creds:
        return False
    token, owner = creds
    try:
        opened = _slack_api(token, "conversations.open", {"users": owner})
        if not opened.get("ok"):
            return False
        channel = opened["channel"]["id"]
        posted = _slack_api(token, "chat.postMessage", {"channel": channel, "text": text})
        return bool(posted.get("ok"))
    except Exception:
        return False


def _env_file_dict(path: Path) -> dict:
    """Parse a KEY=VALUE .env file into a dict (launchd-minimal-env safe).
    Returns {} if unreadable. Strips surrounding quotes; ignores comments."""
    out: dict = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _gateway_owner_room(source: str = "ag2space") -> "str | None":
    """The room to post owner alerts to — an EXPLICITLY configured, owner-only
    room (REMOTE_ALERT_ROOM in channels/<source>/.env). None if unset.

    We deliberately do NOT infer the room from state/last-owner-activity.json:
    that records wherever the owner *last spoke*, which may be a SHARED room, and
    a health alert can carry host/config/outage details that must never leak into
    a team room (qingyun #2487 P1-privacy). Requiring an explicit config entry
    makes the target owner-controlled and owner-only by construction; unset means
    no gateway post at all (the Slack surface stays as the backup)."""
    return (_env_file_dict(claude_home_path("channels", source, ".env")).get("REMOTE_ALERT_ROOM") or "").strip() or None


def _gateway_creds(source: str = "ag2space") -> "tuple[str, str] | None":
    """(url, token) for the gateway op:message endpoint, or None. Supports the
    one-token onboarding form (REMOTE_TASK_TOKEN='https://gw|secret') and the
    legacy AG2_REMOTE_URL / AG2_REMOTE_TOKEN aliases honored by startup.sh."""
    env = _env_file_dict(claude_home_path("channels", source, ".env"))

    def get(key: str) -> str:
        return os.environ.get(key) or env.get(key) or ""

    url = (get("REMOTE_TASK_URL") or get("AG2_REMOTE_URL")).strip().rstrip("/")
    token = (get("REMOTE_TASK_TOKEN") or get("AG2_REMOTE_TOKEN")).strip()
    if "|" in token:
        _u, token = token.split("|", 1)
        url = url or _u.strip().rstrip("/")
    return (url, token) if url and token else None


def _default_gateway_sender(text: str) -> bool:
    """Post `text` to the owner's ag2.space room via the gateway op:message —
    the same transport notify.py uses (POST {url}/v1/room, op:message), so it
    reaches the owner even when the core is wedged. Returns True on a 2xx."""
    room = _gateway_owner_room()
    creds = _gateway_creds()
    if not room or not creds:
        return False
    url, token = creds
    try:
        req = urllib.request.Request(
            f"{url}/v1/room",
            data=json.dumps({"op": "message", "room_id": room, "body": text}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "sutando-health-check/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return 200 <= code < 300
    except Exception:
        return False


def notify_slack_for_failures(
    checks: list[dict],
    state_file: Optional[Path] = None,
    sender=None,
) -> None:
    """DM the owner on Slack when health checks fail — a remote-visible
    surface that does NOT depend on the core agent being alive.

    This is the watchdog the owner asked for: when the core session wedges
    (e.g. loops on the 1M-context usage-credit API error), `core-heartbeat`
    keeps beating from its own background process, so `_any_core_alive()`
    stays True and `emit_task_for_failures` stays silent — but the
    `core-proactive-loop` check flips to `warn` and this DMs Slack anyway.
    Deliberately NOT gated on core liveness, for exactly that reason.

    Same dedup contract as notify_for_failures — alert only on a TRANSITION
    of the failure-set hash, no timed re-fire of an unchanged set (see
    `_LAST_HASH_KEY`) — but a separate state file so the Slack and macOS
    surfaces never suppress each other. The dedup hash is recorded only on a
    SUCCESSFUL send, so a transient Slack/API outage doesn't silence the
    alert. `sender` is injected by tests to avoid real API calls.
    """
    failures = _slack_failures(checks)
    if not failures:
        return

    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "health-last-slacked.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    set_key = "|".join(sorted(c["name"] for c in failures))
    hash_key = hashlib.sha256(set_key.encode()).hexdigest()[:16]
    now_ms = int(time.time() * 1000)

    history: dict = {}
    try:
        if state_file.exists():
            history = json.loads(state_file.read_text())
    except Exception:
        history = {}

    if history.get(_LAST_HASH_KEY) == hash_key:
        # Unchanged failure set since the last successful send — no re-fire.
        return

    lines = [f"• {c['name']}: {c['status']} ({c['detail']})" for c in failures[:5]]
    extra = f"\n…(+{len(failures) - 5} more)" if len(failures) > 5 else ""
    text = (
        f":rotating_light: *Sutando health check* — {len(failures)} issue(s):\n"
        + "\n".join(lines)
        + extra
    )

    send = sender or _default_slack_sender
    if not send(text):
        # Send failed — don't record dedup, so the next tick retries.
        return

    history[hash_key] = now_ms
    history[_LAST_HASH_KEY] = hash_key
    cutoff = now_ms - (24 * 3600 * 1000)
    history = {k: v for k, v in history.items() if k == _LAST_HASH_KEY or v >= cutoff}
    try:
        state_file.write_text(json.dumps(history))
    except Exception:
        pass


def notify_gateway_for_failures(
    checks: list[dict],
    state_file: Optional[Path] = None,
    sender=None,
) -> None:
    """DM the owner on ag2.space (the gateway) when health checks fail — the
    remote-visible surface the owner actually watches, and one that does NOT
    depend on the core agent being alive (posts straight to the gateway
    op:message endpoint from the launchd fallback).

    Owner-requested 2026-08-01: the over-quota alert must land where the owner
    is (ag2.space), not only Slack. Same transition-hash dedup contract as
    notify_slack_for_failures, but a SEPARATE state file so the gateway and
    Slack surfaces never suppress each other, and dedup is recorded only on a
    SUCCESSFUL send so a transient gateway blip doesn't silence the alert.
    `sender` is injected by tests to avoid real network calls.
    """
    failures = _slack_failures(checks)
    if not failures:
        return

    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "health-last-gateway.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    set_key = "|".join(sorted(c["name"] for c in failures))
    hash_key = hashlib.sha256(set_key.encode()).hexdigest()[:16]
    now_ms = int(time.time() * 1000)

    history: dict = {}
    try:
        if state_file.exists():
            history = json.loads(state_file.read_text())
    except Exception:
        history = {}
    if not isinstance(history, dict):
        history = {}

    if history.get(_LAST_HASH_KEY) == hash_key:
        return

    lines = [f"• {c['name']}: {c['status']} ({c['detail']})" for c in failures[:5]]
    extra = f"\n…(+{len(failures) - 5} more)" if len(failures) > 5 else ""
    text = "⚠️ Sutando health check — " + f"{len(failures)} issue(s):\n" + "\n".join(lines) + extra

    send = sender or _default_gateway_sender
    if not send(text):
        # Send failed — don't record dedup, so the next tick retries.
        return

    history[hash_key] = now_ms
    history[_LAST_HASH_KEY] = hash_key
    cutoff = now_ms - (24 * 3600 * 1000)
    history = {k: v for k, v in history.items() if k == _LAST_HASH_KEY or v >= cutoff}
    try:
        state_file.write_text(json.dumps(history))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core wedge auto-recovery (--recover-core)
# ---------------------------------------------------------------------------
#
# The 2026-06-02 outage: the core session crossed into 1M extended context,
# hit the interactive `/usage-credits` gate — which CANNOT be pre-authorized
# for an unattended agent (it's a per-session, account-side toggle, no
# settings key / env var / CLI flag exists) — and then looped on the API
# error. core_heartbeat.py runs as its own process, so the core still "looked
# alive" while no task ever drained. --notify-slack makes that VISIBLE; this
# makes it SELF-HEALING.
#
# Recovery action is the one mechanism we already own and trust:
# `src/agent/start-cli.sh --restart`. The dispatcher restarts whichever core
# runtime is configured. A restarted session starts fresh under the
# standard context boundary; because the /usage-credits enable persists
# ACCOUNT-WIDE once a human sets it (and on Max/Team plans 1M is included with
# no gate at all), the restarted core keeps 1M and re-clears the gate by
# itself. Queued task files survive a restart (the bridge is file-based), so
# no work is lost. 1M therefore stays the DEFAULT — we never disable it.
#
# Heavily guarded, because auto-restarting a 24/7 agent is consequential:
#   - Fires only on a CONFIRMED, SUSTAINED wedge: core process alive AND the
#     oldest queued task older than RECOVER_WEDGE_SEC AND the core didn't just
#     boot — observed on two passes ≥ RECOVER_CONFIRM_SEC apart. Never a blip.
#   - Identity + progress gating (so a legitimately long-running single task is
#     not killed mid-work): the SAME oldest task must persist across the window
#     (a draining queue surfaces a different oldest each pass → resets) AND
#     core-status.json must not advance (a core making progress isn't wedged).
#     Residual: a long single task that NEVER updates its status is still
#     indistinguishable from a wedge by external signals — bounded by the
#     cooldown + give-up cap, and such tasks should heartbeat core-status.json.
#   - RECOVER_COOLDOWN_SEC between restarts; an exclusive flock on the state
#     file serializes the decision so a manual + launchd run can't double-fire.
#   - Hard cap of RECOVER_MAX_PER_HOUR; past that it DMs "giving up" and stops,
#     so a pathological wedge can't become a restart loop.
#   - NO model change on restart: an inherited pin is indistinguishable from a
#     deliberate choice. The cap above is the bound on a re-wedging core.
#   - DMs the owner before each restart and records whether the DM succeeded
#     (last_restart_dm_sent) + logs failures, so a restart is never invisible
#     even if Slack is down — recovery still proceeds (recovery > notification).
#
# All side-effecting collaborators are injectable so the escalation / cooldown
# / give-up logic is unit-tested without real restarts or Slack calls. Only
# wired into the launchd fallback job (its own process, outside the core), and
# start-cli.sh has its own from-inside-core guard — two independent guarantees
# the recovery never runs from within the session it would kill.

RECOVER_WEDGE_SEC = int(os.environ.get("SUTANDO_RECOVER_WEDGE_SEC", "600"))        # task stuck this long = wedged
RECOVER_CONFIRM_SEC = int(os.environ.get("SUTANDO_RECOVER_CONFIRM_SEC", "120"))    # wedge must persist across passes
RECOVER_COOLDOWN_SEC = int(os.environ.get("SUTANDO_RECOVER_COOLDOWN_SEC", "1800")) # min gap between restarts
RECOVER_MAX_PER_HOUR = int(os.environ.get("SUTANDO_RECOVER_MAX_PER_HOUR", "3"))


def _oldest_pending_task(now: float, tasks_dir: Optional[Path] = None) -> "tuple[str, int] | None":
    """(identity, age_seconds) of the oldest top-level tasks/*.txt, or None if
    the queue is empty. Mirrors check_task_queue's globbing (top-level only;
    archive/ excluded). This is the precise wedge signal for recovery: a healthy
    core drains a task in seconds-to-minutes, so a task sitting for
    RECOVER_WEDGE_SEC while the core process is alive means the core is stuck —
    regardless of what core-status.json last said (check_core_proactive_loop
    misses a wedge that happens while status reads 'idle', because it only flags
    'running').

    The identity is `"<name>|<int(mtime)>"`. Recovery requires the SAME identity
    to persist across the confirm window before restarting (PR #1428 review,
    blocker 3): if the oldest task changes (a task drained → a different oldest)
    or its mtime moves (the file was rewritten/reprocessed), the queue is
    draining, not wedged, and the observation resets — so a busy-but-healthy
    backlog never triggers a restart."""
    if tasks_dir is None:
        tasks_dir = WORKSPACE_DIR / "tasks"
    files = _pending_task_files(tasks_dir)
    if not files:
        return None
    try:
        oldest = min(files, key=lambda p: p.stat().st_mtime)
        mtime = oldest.stat().st_mtime
    except OSError:
        return None
    return (f"{oldest.name}|{int(mtime)}", int(now - mtime))


def _core_status_ts(workspace: Optional[Path] = None) -> "float | None":
    """Current core-status.json `ts`, or None if unavailable. Used as a
    progress signal: a core actively working (even a long single task that
    periodically updates status per CLAUDE.md) advances this; a core looping on
    the usage-credit API error cannot complete a turn to update it. If it
    advances across the confirm window, recovery treats the core as making
    progress and resets — so legitimately long work isn't restarted out from
    under itself (PR #1428 review, blocker 3)."""
    if workspace is None:
        workspace = WORKSPACE_DIR
    try:
        data = json.loads(status_read_path("core-status.json", workspace).read_text())
        ts = data.get("ts")
        return ts if isinstance(ts, (int, float)) else None
    except Exception:
        return None


def _core_started_within(seconds: float, workspace: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """True if the freshest LIVE core heartbeat reports started_at within the
    last `seconds`. Guards against restarting a core that only just booted and
    hasn't had time to drain the queue yet (its tasks look 'old' but it's
    catching up, not wedged)."""
    if workspace is None:
        workspace = WORKSPACE_DIR
    if now is None:
        now = time.time()
    cores_dir = workspace / "state" / "cores"
    if not cores_dir.is_dir():
        return False
    youngest_start = None
    for alive_file in cores_dir.glob("*.alive"):
        try:
            if not heartbeat_is_fresh(alive_file.stat().st_mtime, now):
                continue  # stale or future-dated — not a live core
            data = json.loads(alive_file.read_text())
        except (OSError, ValueError):
            continue
        started = data.get("started_at")
        if isinstance(started, (int, float)):
            if youngest_start is None or started > youngest_start:
                youngest_start = started
    if youngest_start is None:
        return False
    return (now - youngest_start) < seconds


def _resolve_launch_env() -> dict:
    """Environment for out-of-process core restarts (start-cli.sh --restart).

    launchd's minimal PATH (``/usr/bin:/bin:/usr/sbin:/sbin``) cannot find the
    tools start-cli.sh needs — homebrew ``tmux``, ``claude`` in ``~/.local/bin``,
    or the Sutando.app-bundled ``node`` runtime — so the restart exits rc=127
    (``node unavailable`` / ``exec: claude: not found``) and silently falls
    through to the legacy fallback. Prepend all of them.

    Extends the existing tmux-only PATH fix to node + claude. Incident
    2026-07-10: the watchdog restart path was broken under launchd for ~70 min
    (queue backlog) because every canonical restart hit rc=127. Same PATH-
    narrowing class as _resolve_tmux_bin (2026-06-09), applied here.
    """
    env = dict(os.environ)
    extra = [
        "/opt/homebrew/bin",                        # homebrew (Apple Silicon) — tmux
        "/usr/local/bin",                           # homebrew (Intel) / misc
        str(Path.home() / ".local" / "bin"),        # `claude` install location
    ]
    # Sutando.app-bundled node runtime: sibling of the repo inside the app bundle
    # (Contents/Resources/{repo,runtime}). Absent in a plain dev checkout → skipped.
    bundled_bin = REPO_DIR.parent / "runtime" / "bin"
    if bundled_bin.is_dir():  # pragma: no cover — only present inside the app bundle
        extra.append(str(bundled_bin))
    env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "/usr/bin:/bin")
    return env


def _default_core_restart() -> bool:
    """Restart the core out-of-process, setting no model: recovery must not change
    which model the core runs on. True if the restart command exited 0."""
    script = REPO_DIR / "src" / "agent" / "start-cli.sh"
    if not script.exists():
        return False
    env = _resolve_launch_env()  # pragma: no cover — real-subprocess restart path (integration, not unit)
    try:
        proc = subprocess.run(
            ["/bin/bash", str(script), "--restart"],
            env=env, capture_output=True, text=True, timeout=120,
        )
        return proc.returncode == 0
    except Exception:
        return False


def recover_core_if_wedged(
    state_file: Optional[Path] = None,
    now: Optional[float] = None,
    alive_fn=None,
    oldest_task_fn=None,
    status_ts_fn=None,
    just_booted_fn=None,
    restart_fn=None,
    sender=None,
) -> "dict | None":
    """Auto-restart the core when it is alive-but-wedged. Returns a dict
    describing the action taken (for tests / observability), or None when no
    action was warranted. See the module comment above for the guard rationale.
    All side-effecting collaborators are injectable for tests.

    The whole load→decide→restart→save sequence runs under an exclusive,
    non-blocking flock on `<state_file>.lock` (PR #1428 review, suggestion):
    a manual `--recover-core` from the CLI and the launchd job firing in the
    same window must not both clear the cooldown and issue duplicate restarts.
    A second concurrent invocation returns {"action": "locked"} and no-ops.
    """
    if now is None:
        now = time.time()
    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "core-recovery.json"
    alive_fn = alive_fn or _any_core_alive
    oldest_task_fn = oldest_task_fn or (lambda: _oldest_pending_task(now))
    status_ts_fn = status_ts_fn or _core_status_ts
    just_booted_fn = just_booted_fn or (lambda: _core_started_within(RECOVER_WEDGE_SEC, now=now))
    restart_fn = restart_fn or _default_core_restart
    send = sender or _default_slack_sender

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Serialize the critical section (suggestion: no concurrent double-restart).
    lock_path = state_file.with_name(state_file.name + ".lock")
    lock_fh = None
    if fcntl is not None:
        try:
            lock_fh = open(lock_path, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another recovery invocation holds the lock — skip this pass.
            if lock_fh is not None:
                lock_fh.close()
            return {"action": "locked"}

    try:
        try:
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}

        def _save():
            try:
                state_file.write_text(json.dumps(state))
            except Exception:
                pass

        def _reset_observation():
            state["wedge_first_seen"] = 0
            state["wedge_task"] = None
            state["wedge_status_ts"] = None

        oldest = oldest_task_fn()                    # (identity, age) | None
        cur_key = oldest[0] if oldest else None
        oldest_age = oldest[1] if oldest else None
        status_ts = status_ts_fn()
        wedged = (
            alive_fn()
            and oldest is not None
            and oldest_age > RECOVER_WEDGE_SEC
            and not just_booted_fn()
        )

        if not wedged:
            # Healthy / no queued work / core down / just booted. Clear any
            # in-progress observation so a future wedge starts fresh.
            # last_restart / history are preserved (cooldown + give-up survive).
            if state.get("wedge_first_seen") or state.get("wedge_task") is not None:
                _reset_observation()
                _save()
            return None

        # Identity + progress gating (blocker 3): age alone can't tell a wedge
        # from a legitimately long single task. Reset the confirmation window if
        # EITHER the oldest task changed (queue draining → a different oldest, or
        # the file was rewritten → new mtime) OR the core advanced core-status.json
        # (it's making progress, not looping). Only a SAME-task, NO-progress
        # streak across the window is treated as a real wedge.
        prev_key = state.get("wedge_task")
        prev_status_ts = state.get("wedge_status_ts")
        first_seen = state.get("wedge_first_seen") or 0
        progressed = (
            isinstance(prev_status_ts, (int, float))
            and isinstance(status_ts, (int, float))
            and status_ts > prev_status_ts
        )
        if (not first_seen) or prev_key != cur_key or progressed:
            state["wedge_first_seen"] = now
            state["wedge_task"] = cur_key
            state["wedge_status_ts"] = status_ts
            _save()
            return {"action": "observed", "oldest_age": oldest_age, "task": cur_key}

        if now - first_seen < RECOVER_CONFIRM_SEC:
            return {"action": "confirming", "oldest_age": oldest_age, "for": int(now - first_seen)}

        # Cooldown between restarts.
        last_restart = state.get("last_restart") or 0
        if last_restart and now - last_restart < RECOVER_COOLDOWN_SEC:
            return {"action": "cooldown", "oldest_age": oldest_age, "since_restart": int(now - last_restart)}

        # Give-up cap: prune restart history to the trailing hour.
        history = [t for t in (state.get("restart_history") or []) if isinstance(t, (int, float)) and now - t < 3600]
        if len(history) >= RECOVER_MAX_PER_HOUR:
            # DM once per give-up episode. Record gave_up_at only on a SUCCESSFUL
            # send so a Slack outage doesn't silence the give-up alert for an hour.
            if not state.get("gave_up_at") or now - state["gave_up_at"] > 3600:
                if send(
                    ":octagonal_sign: *Sutando core auto-recovery gave up* — restarted "
                    f"{len(history)}× in the last hour and the core is still wedged "
                    f"(oldest task stuck {oldest_age // 60} min). Needs manual attention: "
                    "check the CLI / `/usage-credits`."
                ):
                    state["gave_up_at"] = now
                    _save()
                else:
                    print("[recover-core] WARNING: give-up DM to owner failed", flush=True)
            return {"action": "gave_up", "restarts_last_hour": len(history)}

        # No model escalation: RECOVER_MAX_PER_HOUR plus the give-up DM above
        # are the bound on a re-wedging core.
        # DM the owner BEFORE restarting. Capture the result (blocker 2): if the
        # DM fails we still restart (recovery > notification — don't leave the
        # core wedged because Slack is down), but we record dm_sent=False and log
        # to stderr/launchd so the restart is never invisible.
        dm_ok = send(
            f":hourglass: *Sutando core wedged* — oldest task stuck {oldest_age // 60} min "
            f"while the core process is alive (likely the 1M usage-credit gate or a "
            f"stalled turn). Auto-restarting on its configured model. Queued tasks "
            f"are preserved."
        )
        if not dm_ok:
            print("[recover-core] WARNING: wedge-restart DM failed; restarting anyway", flush=True)

        if not restart_fn():
            # Restart launch failed — don't burn a cooldown/history slot, and
            # keep the observation so we stay confirmed and retry next pass.
            return {"action": "restart_failed", "dm_sent": dm_ok}

        history.append(now)
        state["restart_history"] = history
        state["last_restart"] = now
        state.pop("last_restart_mode", None)   # retired with the model escalation
        state["last_restart_dm_sent"] = dm_ok
        _reset_observation()  # re-observe after the restart settles
        state.pop("gave_up_at", None)
        _save()
        return {
            "action": "restarted", "oldest_age": oldest_age,
            "restarts_last_hour": len(history), "dm_sent": dm_ok,
        }
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fh.close()


# Cron-layer death in a LIVE core (the third recovery gap)
# ---------------------------------------------------------------------------
# recover_core_if_wedged handles a core that is alive-but-STUCK; the dead-core
# relaunch (PR #2160) handles a core that EXITED. Neither catches the failure
# mode observed 2026-07-17: the core session alive and responsive, but its
# IN-SESSION cron layer dead — session crons are registered per-session via
# CronCreate and auto-expire after 7 days, so a long-lived core (23 days that
# morning) silently outlives its own crons. Scheduled work (the morning report,
# the briefing, the */5 main loop) stops firing while every liveness probe
# reads healthy. Owner: "Core was not dead. Cron was dead."
#
# Heartbeat source: `<workspace>/state/core-status.json` `ts`. The canonical
# main-loop cron (/proactive-loop) stamps it at every pass start AND end
# (CLAUDE.md "Work Status" / proactive-loop step 0), so a live cron layer
# advances `ts` at least once per main-loop period — no new writer needed. A
# frozen `ts` while the core heartbeat (`state/cores/<host>.alive` mtime) stays
# fresh means the scheduler died inside the session. Other activity (task
# processing) also stamps `ts`; that can only DELAY detection, never
# false-fire it.
#
# CRON_STALE_SEC = 1800: 3 × the 10-minute /schedule-crons step-4 fallback
# cadence — the LARGEST canonical main-loop period. Tolerates one long pass
# plus one missed tick before declaring the layer dead; a */5 config simply
# detects a little later than it strictly could.
#
# Recovery is a NUDGE, not a restart: type `/schedule-crons` into the live
# core's tmux pane — the same keystroke channel Sutando.app's checkWatcher
# uses (`watcher` keystroke) when the task watcher dies — so the session
# re-arms its own crons and keeps its context. Bounded by the SAME
# confirm/cooldown/give-up discipline as the wedge path so it can't
# nudge-storm a pane.

CRON_STALE_SEC = int(os.environ.get("SUTANDO_CRON_STALE_SEC", "1800"))


CORE_HEARTBEAT_MAX_AGE_S = 90.0
# Callers snapshot `now` BEFORE they stat, so an atomic heartbeat rewrite in
# between yields a slightly newer mtime; cross-host sync adds small clock skew.
CORE_HEARTBEAT_FUTURE_TOLERANCE_S = 5.0


def heartbeat_is_fresh(mtime: float, now: float,
                       max_age_s: float = CORE_HEARTBEAT_MAX_AGE_S) -> bool:
    """A `state/cores/*.alive` heartbeat is fresh only inside
    [-CORE_HEARTBEAT_FUTURE_TOLERANCE_S, max_age_s).

    Bounded from BELOW as well: a far-future file has a negative age, and every
    one-sided `age >= max` test accepts that as fresh — so a clock step or a bad
    write reads as a live core forever. The bound is a tolerance, not zero: a
    strictly non-negative test discards a live heartbeat that was atomically
    rewritten between a caller's `now` snapshot and its stat (sub-ms in practice).
    """
    age = now - mtime
    return -CORE_HEARTBEAT_FUTURE_TOLERANCE_S <= age < max_age_s


def _live_core_socket(workspace: Optional[Path] = None) -> str:
    """Tmux socket of the freshest LIVE core heartbeat — the runtime-authored
    `socket` field of `state/cores/<host>.alive` (the same source
    `sutando-config.sh runtime` trusts, correct for custom sockets and immune
    to a foreign caller's ambient env). Falls back to the default OSS socket
    when no fresh heartbeat records one."""
    if workspace is None:
        workspace = WORKSPACE_DIR
    default = os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock")
    cores_dir = workspace / "state" / "cores"
    if not cores_dir.is_dir():
        return default
    now = time.time()
    best_mtime = None
    best_socket = None
    for alive_file in cores_dir.glob("*.alive"):
        try:
            mtime = alive_file.stat().st_mtime
            if not heartbeat_is_fresh(mtime, now):
                continue  # stale or future-dated — not a live core
            payload = json.loads(alive_file.read_text())
            # A heartbeat that decodes to a NON-OBJECT (`null`, `[]`, `"x"`, `3`)
            # raises AttributeError on `.get`, which this handler does not catch —
            # so one junk file takes down the caller. That caller is
            # `_rearm_core_crons()`, a RECOVERY path, so it fails exactly when
            # something is already wrong. The writer is atomic (tmp + replace in
            # core_heartbeat.py), but this globs `*.alive` for EVERY host, so the
            # file may come from another machine running different code.
            if not isinstance(payload, dict):
                continue
            sock = payload.get("socket")
        except (OSError, ValueError):
            continue
        if isinstance(sock, str) and sock and (best_mtime is None or mtime > best_mtime):
            best_mtime = mtime
            best_socket = sock
    return best_socket or default


def _resolve_tmux_bin(candidates: "tuple[str, ...]" = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux")) -> str:
    """Absolute tmux path when a known install location exists, else a bare
    PATH lookup (run with _resolve_launch_env's healed PATH). Same
    PATH-narrowing class as _resolve_launch_env: under launchd's minimal PATH,
    homebrew tmux doesn't resolve. Candidates injectable for tests."""
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return "tmux"


def _default_cron_nudge(
    tmux_bin: Optional[str] = None,
    sock: Optional[str] = None,
    session: Optional[str] = None,
) -> bool:
    """Re-arm the live core's in-session crons by typing `/schedule-crons`
    into its tmux pane — the same keystroke channel Sutando.app's checkWatcher
    uses for a dead task watcher (main.swift tmuxSendKeys). Returns True only
    when the session exists and send-keys succeeded. tmux_bin/sock/session are
    injectable so tests can drive the real subprocess path against a fake
    tmux binary."""
    if sock is None:
        sock = _live_core_socket()
    if session is None:
        session = os.environ.get("SUTANDO_TMUX_SESSION", "sutando-core")
    if tmux_bin is None:
        tmux_bin = _resolve_tmux_bin()
    env = _resolve_launch_env()
    try:
        has = subprocess.run(
            [tmux_bin, "-S", sock, "has-session", "-t", session],
            env=env, capture_output=True, timeout=15,
        )
        if has.returncode != 0:
            return False
        send = subprocess.run(
            [tmux_bin, "-S", sock, "send-keys", "-t", session, "/schedule-crons", "Enter"],
            env=env, capture_output=True, timeout=15,
        )
        return send.returncode == 0
    except Exception:
        return False


def recover_cron_if_dead(
    state_file: Optional[Path] = None,
    now: Optional[float] = None,
    alive_fn=None,
    status_ts_fn=None,
    just_booted_fn=None,
    nudge_fn=None,
    sender=None,
) -> "dict | None":
    """Nudge the core to re-arm its in-session crons when the core is ALIVE
    but its cron layer is dead (main-loop heartbeat frozen — see the module
    comment above). Returns an action dict for tests/observability, or None
    when no action was warranted.

    Deliberately NOT a restart: the session is fine — killing it would lose
    its context for a scheduler that one `/schedule-crons` keystroke re-arms.
    A truly dead core (no fresh heartbeat) is out of scope here — that's the
    dead-core relaunch branch (PR #2160). Same guard shape as the wedge path:
    flock-serialized, confirmed across two passes ≥ RECOVER_CONFIRM_SEC apart,
    RECOVER_COOLDOWN_SEC between nudges, give-up DM past RECOVER_MAX_PER_HOUR.
    All side-effecting collaborators are injectable for tests."""
    if now is None:
        now = time.time()
    if state_file is None:
        state_file = WORKSPACE_DIR / "state" / "cron-recovery.json"
    alive_fn = alive_fn or _any_core_alive
    status_ts_fn = status_ts_fn or _core_status_ts
    # Boot grace = CRON_STALE_SEC (not RECOVER_WEDGE_SEC): a freshly restarted
    # core inherits the previous session's stale core-status.json and needs a
    # full main-loop period (plus slack) to stamp its first pass.
    just_booted_fn = just_booted_fn or (lambda: _core_started_within(CRON_STALE_SEC, now=now))
    nudge_fn = nudge_fn or _default_cron_nudge
    send = sender or _default_slack_sender

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    lock_path = state_file.with_name(state_file.name + ".lock")
    lock_fh = None
    if fcntl is not None:
        try:
            lock_fh = open(lock_path, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if lock_fh is not None:
                lock_fh.close()
            return {"action": "locked"}

    try:
        try:
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}

        def _save():
            try:
                state_file.write_text(json.dumps(state))
            except Exception:
                pass

        def _reset_observation():
            state["cron_first_seen"] = 0
            state["cron_status_ts"] = None

        status_ts = status_ts_fn()
        stale_for = int(now - status_ts) if isinstance(status_ts, (int, float)) else None
        cron_dead = (
            alive_fn()                       # core alive — a DEAD core is #2160's branch, not ours
            and stale_for is not None        # no stamp ever written → new install, not a death
            and stale_for > CRON_STALE_SEC
            and not just_booted_fn()
        )

        if not cron_dead:
            # Healthy stamp / core down / just booted / new install. Clear any
            # in-progress observation; last_nudge / history survive (cooldown
            # + give-up persist across episodes within the hour).
            if state.get("cron_first_seen") or state.get("cron_status_ts") is not None:
                _reset_observation()
                _save()
            return None

        # Confirm across passes. If the stamp ADVANCED since first seen, the
        # cron layer (or something) is stamping again — reset, no nudge.
        prev_ts = state.get("cron_status_ts")
        first_seen = state.get("cron_first_seen") or 0
        progressed = (
            isinstance(prev_ts, (int, float))
            and isinstance(status_ts, (int, float))
            and status_ts > prev_ts
        )
        if (not first_seen) or progressed:
            state["cron_first_seen"] = now
            state["cron_status_ts"] = status_ts
            _save()
            return {"action": "observed", "stale_for": stale_for}

        if now - first_seen < RECOVER_CONFIRM_SEC:
            return {"action": "confirming", "stale_for": stale_for, "for": int(now - first_seen)}

        last_nudge = state.get("last_nudge") or 0
        if last_nudge and now - last_nudge < RECOVER_COOLDOWN_SEC:
            return {"action": "cooldown", "stale_for": stale_for, "since_nudge": int(now - last_nudge)}

        history = [t for t in (state.get("nudge_history") or []) if isinstance(t, (int, float)) and now - t < 3600]
        if len(history) >= RECOVER_MAX_PER_HOUR:
            if not state.get("gave_up_at") or now - state["gave_up_at"] > 3600:
                if send(
                    ":octagonal_sign: *Sutando cron-layer recovery gave up* — nudged the core "
                    f"{len(history)}× in the last hour and scheduled passes still aren't stamping "
                    f"core-status.json (stale {stale_for // 60} min). The core itself is alive; "
                    "run `/schedule-crons` in its pane manually."
                ):
                    state["gave_up_at"] = now
                    _save()
                else:
                    print("[recover-cron] WARNING: give-up DM to owner failed", flush=True)
            return {"action": "gave_up", "nudges_last_hour": len(history)}

        # DM the owner BEFORE nudging — wording is explicit that the CRON
        # LAYER died, not the core (the core is alive and keeps its session).
        dm_ok = send(
            ":alarm_clock: *Sutando cron layer died in the live core* — the core heartbeat is "
            f"fresh but no scheduled pass has stamped core-status.json for {stale_for // 60} min "
            "(session crons expire after ~7 days; a long-lived core outlives them). Nudging the "
            "core to re-arm via `/schedule-crons` — the core itself is fine, no restart."
        )
        if not dm_ok:
            print("[recover-cron] WARNING: cron-nudge DM failed; nudging anyway", flush=True)

        if not nudge_fn():
            # Nudge failed to land — don't burn a cooldown/history slot, keep
            # the observation so we stay confirmed and retry next pass.
            return {"action": "nudge_failed", "dm_sent": dm_ok}

        history.append(now)
        state["nudge_history"] = history
        state["last_nudge"] = now
        state["last_nudge_dm_sent"] = dm_ok
        _reset_observation()  # re-observe after the nudge settles
        state.pop("gave_up_at", None)
        _save()
        return {
            "action": "nudged", "stale_for": stale_for,
            "nudges_last_hour": len(history), "dm_sent": dm_ok,
        }
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fh.close()
def community_support_line() -> str:
    """The issue-time pointer to the official Discord (real humans +
    community-run agents). Pure so it's unit-testable without invoking the
    full health-check main() path (owner request 2026-07-17)."""
    return "  Stuck? Community support (real humans + community agents): https://discord.gg/uZHWXXmrCS"


#: Statuses that are NOT problems. Everything else is, by the same rule the
#: issue list uses: `issues = [c for c in checks if is_issue(c)]`.
#: `stale` is an issue but gets its own glyph because it names a specific remedy.
_BENIGN_STATUSES = ("ok", "warn")


def status_icon(status: str) -> str:
    """Glyph for a probe status — unrecognized reads as a PROBLEM, not a shrug.

    The human-readable listing used to enumerate `down`/`missing`/`not_loaded` as
    severe and fall back to `~` for anything else. That put **`fail`** — the most
    severe status any probe emits, and the one nine probes use — on the least
    alarming glyph, sharing it with "status I don't recognize". `error` (5 probes)
    and `wedged` landed there too.

    It is a real miss, not a cosmetic one: a peer host filtered health-check output
    with `grep -E "⚠|✗"` and the single `fail` line was the one the filter hid, so a
    run with a genuine failure read as three routine warnings.

    `--quiet` never had this bug — it renders every non-stale issue as `✗`. The two
    output modes disagreed about the same status. This makes them agree by deriving
    both from one predicate, and inverts the default so a status added later shows
    up as a problem until someone deliberately classifies it as benign.
    """
    if status == "ok":
        return "✓"
    if status == "warn":
        return "⚠"
    if status == "stale":
        return "♻"
    return "✗"


def is_issue(check) -> bool:
    """The single definition of "not healthy". `summary_line` and the exit-code
    list must use this one, or a run exits non-zero while printing an all-clear."""
    return check.get("status") not in _BENIGN_STATUSES


def summary_line(checks) -> str:
    """The no-failures summary. Warnings are deliberately NOT issues — they must
    not fail the exit code or wake the launchd notifier, and that is unchanged.
    But the unqualified "All systems operational." printed directly under a
    `⚠ core degraded` row contradicts the screen, and anything summarising this
    tool by its last line (a human skimming, a grepped status check) then reports
    healthy while a warning stands. Name them instead.

    Pure and importable on purpose, so a regression test exercises THIS code
    rather than a copy of it.
    """
    fails = [c for c in checks if is_issue(c)]
    warns = [c for c in checks if c.get("status") == "warn"]
    if fails:
        line = (f"{len(fails)} ISSUE(S): "
                + ", ".join(c["name"] for c in fails))
        if warns:
            line += (f" — plus {len(warns)} warning(s): "
                     + ", ".join(c["name"] for c in warns))
        return line
    if not warns:
        return "All systems operational."
    return (f"No failures — {len(warns)} warning(s): "
            + ", ".join(c["name"] for c in warns))
def main():
    as_json = "--json" in sys.argv
    do_fix = "--fix" in sys.argv
    do_emit = "--emit-task" in sys.argv
    do_notify = "--notify-on-fail" in sys.argv
    do_notify_slack = "--notify-slack" in sys.argv
    do_notify_gateway = "--notify-gateway" in sys.argv
    do_recover = "--recover-core" in sys.argv
    quiet = "--quiet" in sys.argv or "-q" in sys.argv

    checks = run_all_checks()
    issues = [c for c in checks if is_issue(c)]
    codex_notifier = (
        next(
            (
                c
                for c in checks
                if c["name"] == "codex-task-notifier" and c["status"] == "warn"
            ),
            None,
        )
        if do_fix
        else None
    )

    # skill-symlinks is warn-level, so it is never in `issues`. Its fix pass has
    # to sit ABOVE both gates that follow, because each one independently made
    # the repair unreachable:
    #   * `if quiet: ... elif codex_notifier is None: sys.exit(0)` returns before
    #     any fix runs, and
    #   * the fix block itself lived inside `else:` of `if not issues:`.
    # Net effect on a host whose ONLY problem was broken symlinks — the exact
    # case this fixer exists for — `--fix` printed nothing and repaired nothing;
    # it worked only when some UNRELATED check happened to be failing too.
    # Prints only when it actually repairs something, so a healthy run is silent.
    # Under --json the repair line goes to STDERR: stdout carries the payload,
    # and prose ahead of it makes json.loads() fail at line 1 (caught in review
    # of #2663 — the first version of this hoist printed to stdout regardless).
    if do_fix:
        apply_skill_symlink_fixes(checks, stream=sys.stderr if as_json else sys.stdout)
        apply_task_watcher_sentinel_fix(checks, stream=sys.stderr if as_json else sys.stdout)

    # Optional: macOS notification surface for the launchd-supervised path
    # (com.sutando.health-check-fallback). Notifies on the INITIAL check set
    # — the launchd fallback wants the user-visible alert immediately, even
    # if --fix would resolve some issues. Independent dedup state from
    # emit-task — the two surfaces are deliberately decoupled so neither
    # can suppress the other.
    if do_notify:
        notify_for_failures(checks)

    # Optional: remote Slack DM surface. Unlike --notify-on-fail (local
    # macOS notification) and --emit-task (needs a live core to read the
    # task), this reaches the owner off-machine and fires even when the core
    # session is wedged but its heartbeat process still ticks. Intended for
    # the launchd-supervised fallback invocation so outages self-report.
    if do_notify_slack:
        notify_slack_for_failures(checks)

    # Optional: remote ag2.space (gateway) DM surface — the channel the owner
    # actually watches. Same core-independent guarantee as --notify-slack, via
    # the gateway op:message endpoint. Intended for the launchd fallback so an
    # over-quota / wedged core self-reports where the owner will see it.
    if do_notify_gateway:
        notify_gateway_for_failures(checks)

    # Optional: auto-recover a wedged core (alive-but-stuck) by restarting it.
    # Independent of the checks list — keys off the queue-drain + heartbeat
    # signals directly (see recover_core_if_wedged). Intended for the
    # launchd-supervised fallback so the core self-heals from the 1M-gate wedge
    # without waiting for a human. Heavily guarded (confirm window, cooldown,
    # give-up cap); a no-op when the core is healthy.
    if do_recover:
        wedge_action = recover_core_if_wedged()
        # Cron-layer check rides the same flag: core ALIVE but its in-session
        # cron layer dead → nudge `/schedule-crons` into the pane (see
        # recover_cron_if_dead). Skipped when the wedge path just RESTARTED
        # the core — a restart re-arms crons via the startup path anyway, and
        # keystrokes into a relaunching pane are noise.
        if not (wedge_action and wedge_action.get("action") == "restarted"):
            recover_cron_if_dead()

    # Emit-task: when NOT running --fix, the initial check IS the residual,
    # so emit here BEFORE the early-exit paths (--json return, --quiet
    # sys.exit). Per Mini's PR #640 v2-regression catch: my prior change
    # moved emit-task to end-of-main, which the launchd fallback's
    # `--quiet --emit-task --notify-on-fail` invocation bypassed via the
    # quiet-path sys.exit(1). Splitting the emit logic by --fix state
    # restores coverage for the no-fix path.
    #
    # Skip when a live core is present (issue #635 dedup-runners): the
    # proactive loop already handles health inline — writing a task file
    # here creates a duplicate that re-queues the same check. The task-file
    # path is only useful when the core is dead (queues for next restart).
    if do_emit and not do_fix and not _any_core_alive():
        emit_task_for_failures(checks)

    if as_json:
        # Underscore keys are the fix pass's internal channel, and whether one
        # is present depends on the flags — so they must not reach consumers.
        payload = [{k: v for k, v in c.items() if not k.startswith("_")} for c in checks]
        print(json.dumps({"checks": payload, "issues": len(issues), "total": len(checks)}, indent=2))
        return

    # --quiet: print only issues (or nothing if clean). Exit code reflects state.
    # Useful for cron callers and automation that only cares about problems.
    if quiet:
        if issues:
            for c in issues:
                icon = status_icon(c["status"])
                print(f"{icon} {c['name']}: {c['status']} ({c['detail']})")
            if do_fix:
                # Fall through to existing fix path below
                pass
            else:
                sys.exit(1)
        elif codex_notifier is None:
            sys.exit(0)

    # Human-readable
    if not quiet:
        print("Sutando Health Check")
        print("=" * 40)

        for c in checks:
            icon = status_icon(c["status"])
            print(f"  {icon} {c['name']:30s} {c['status']:12s} {c['detail']}")

        print()
    if not issues:
        if not quiet:
            print(summary_line(checks))
    else:
        # One summary for both outcomes: a separate issue-count header here is how
        # the exit code and the printed summary drifted apart in the first place.
        print(summary_line(checks))
        for c in issues:
            print(f"  - {c['name']}: {c['status']} ({c['detail']})")
        print(community_support_line())  # pragma: no cover — main() summary glue; the line's content is unit-tested via community_support_line()

        if do_fix:
            print()
            print("Attempting fixes...")
            for c in issues:
                if c["name"].startswith("com.sutando."):
                    result = fix_launchd(c["name"])
                    print(f"  {c['name']}: {result}")
                elif c["name"] in LAUNCHD_BACKED_CHECKS:  # pragma: no cover — dispatch in untested main()
                    # Named by service, recovered via launchd (issue #1888
                    # bug 1: the com.sutando.* branch above never matches the
                    # bare names, so --fix silently skipped the two most
                    # user-visible services). fix_launchd() kickstarts (or
                    # bootstraps) the job, which also replaces a wedged
                    # launchd-owned listener. A rogue non-launchd port-holder
                    # (issue #1888 bug 2, double-management) is out of scope
                    # here — the result string will say the restart failed.
                    result = fix_launchd(LAUNCHD_BACKED_CHECKS[c["name"]])  # pragma: no cover
                    print(f"  {c['name']}: {result}")  # pragma: no cover
                elif c["name"] in DOWN_BRIDGE_DETAILS:  # pragma: no cover - --fix restart path spawns real subprocesses; not unit-tested
                    # LoginFailure means the token is bad — restarting won't help
                    # and would create a duplicate alongside the launchd-managed one.
                    if "LoginFailure" in c.get("detail", "") or "token invalid" in c.get("detail", ""):
                        print(f"  {c['name']}: token invalid — regenerate at discord.com/developers/applications (no restart)")
                    else:
                        # Plan BEFORE any kill: killing a working stale bridge
                        # with no viable relaunch turns a warning into an outage.
                        plan = _bridge_launch_plan(c["name"])
                        if plan is None:
                            print(f"  {c['name']}: no capable interpreter/env — restart skipped (see startup.sh launch requirements)")
                            continue
                        # If stale (process older than source code), kill old PID first
                        # so the new process doesn't conflict with a still-running zombie.
                        if c["status"] == "stale":
                            try:
                                # Anchor to `\.py$` to match the detect path at
                                # line ~277. Without this, a bare `pgrep -f
                                # discord-bridge` also catches grep pipelines
                                # and shell invocations whose command line
                                # contains the bridge name, and we'd kill them
                                # instead of (or in addition to) the real
                                # bridge process. PR #243 fixed the detect
                                # side; this keeps the kill side consistent.
                                old_pids = subprocess.run(
                                    ["/usr/bin/pgrep", "-f", f"{c['name']}\\.py$"], capture_output=True, text=True
                                ).stdout.strip().split("\n")
                                for pid in old_pids:
                                    if pid:
                                        subprocess.run(["/bin/kill", pid], check=False)
                                import time as _t; _t.sleep(1)
                            except Exception:
                                pass
                        # Shared launch policy — a bare sys.executable spawn
                        # crash-loops on missing imports.
                        _launch_bridge(c["name"], plan)
                        print(f"  {c['name']}: {'restarted (stale code)' if c['status'] == 'stale' else 'restarted'}")
                elif c["name"] == "sutando-app":
                    # Two distinct failure modes:
                    #   1. status="warn" + detail="not running …" → binary may
                    #      already be fresh; just needs to be launched. Safe
                    #      to auto-fix via `open` (singleton enforcement is
                    #      not at risk because no PID exists yet).
                    #   2. status="stale" → main.swift is newer than the
                    #      running binary's process start time. Real fix
                    #      needs pkill + swiftc rebuild + open; an earlier
                    #      auto-fix path leaked duplicate instances (macOS
                    #      doesn't enforce singleton on this bundle —
                    #      observed 3 concurrent on 2026-04-19), so we
                    #      defer that path to a manual rebuild + relaunch.
                    binary = REPO_DIR / "src" / "Sutando" / "Sutando"
                    source = sutando_app_newest_source()
                    if (
                        c.get("status") == "warn"
                        and "not running" in (c.get("detail") or "")
                        and binary.exists()
                        and source.exists()
                        and _binary_is_current(binary, source)
                    ):
                        try:
                            subprocess.run(["/usr/bin/open", str(binary)],
                                           check=True, timeout=5)
                            print(f"  {c['name']}: launched (binary fresh, no rebuild needed)")
                        except Exception as e:
                            print(f"  {c['name']}: launch failed ({type(e).__name__}: {e}) — try `open {binary}` manually")
                    else:
                        print(f"  {c['name']}: not auto-fixed — needs manual rebuild + relaunch (see memory feedback_sutando_app_launch_method.md)")
                elif c["name"] == "ngrok":
                    # Read ngrok domain from .env if set, otherwise use default
                    env_path = _resolve_dotenv()  # pragma: no cover
                    domain_arg = []
                    if env_path.exists():
                        for line in env_path.read_text().splitlines():
                            if line.startswith("NGROK_DOMAIN="):
                                domain = line.split("=", 1)[1].strip().strip('"').strip("'")
                                if domain:
                                    domain_arg = [f"--domain={domain}"]
                                break
                    subprocess.Popen(["ngrok", "http", "3100"] + domain_arg,
                                     stdout=open("/tmp/ngrok.log", "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: restarted")
                elif c["name"] == "tailscale-funnel":
                    # Re-enable Tailscale Funnel for port 3100
                    ts_bin = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
                    subprocess.run([ts_bin, "funnel", "--bg", "3100"],
                                   capture_output=True, timeout=10)
                    print(f"  {c['name']}: restarted")
                elif c["name"] == "voice-transport" and c.get("_stuck_connecting"):
                    result = fix_launchd("com.sutando.voice-agent")
                    print(f"  voice-agent (stuck CONNECTING): {result}")
                elif c["name"] == "conversation-server":
                    # If stale, kill old PIDs first so the new process doesn't
                    # bind-fail or end up alongside a still-running zombie.
                    if c["status"] == "stale":
                        try:
                            old_pids = subprocess.run(
                                ["/usr/bin/pgrep", "-f", "conversation-server.ts"],
                                capture_output=True, text=True
                            ).stdout.strip().split("\n")
                            for pid in old_pids:
                                if pid:
                                    subprocess.run(["/bin/kill", pid], check=False)
                            import time as _t; _t.sleep(1)
                        except Exception:
                            pass
                    subprocess.Popen(["npx", "tsx", "skills/phone-conversation/scripts/conversation-server.ts"],
                                     cwd=str(REPO_DIR),
                                     stdout=open("/tmp/conversation-server.log", "a"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
                    print(f"  {c['name']}: {'restarted (stale code)' if c['status'] == 'stale' else 'restarted'}")

    # Screen-capture (:7845) is optional, so a down server is downgraded to
    # warn and never enters `issues` — the fix loop above can't reach it. An
    # owner running --fix still wants it back when the Screen Recording
    # permission is in place, so dispatch off `checks` here. Runs even when
    # `issues` is empty, hence outside the if/else above.
    if do_fix:
        sc = next((c for c in checks if c["name"] == "screen-capture" and c["status"] == "warn"
                   and "not running" in (c.get("detail") or "")), None)
        if sc:
            print(f"  screen-capture: {fix_screen_capture()}")

    # The managed Codex notifier is warn-only, like the generic task watcher:
    # a missing bridge does not mean Core itself is down. It is still safe to
    # repair under --fix because fix_codex_task_notifier() re-verifies the live
    # local Codex session and delegates topology to the canonical launcher.
    if codex_notifier:
        print(f"  codex-task-notifier: {fix_codex_task_notifier()}")

    # Channel bridges have the same optional-component shape: "configured but
    # not running" is warn-only, so the fix loop above can't reach a dead
    # bridge (see fix_down_bridges for the incident that motivated this).
    if do_fix:
        for name in fix_down_bridges(checks):
            # "attempted", not "restarted": bridges boot slowly, so an in-run
            # liveness recheck is unreliable — the next health run's verdict
            # is the source of truth.
            print(f"  {name}: restart attempted (was not running)")

    # Emit task on the RESIDUAL failure set when --fix ran (per PR #640 v2
    # review). The no-fix path emits earlier, before --quiet / --json early
    # exits (per #640 v2-regression: launchd's `--quiet --emit-task` was
    # bypassing the end-of-main emit via sys.exit(1)).
    if do_emit and do_fix and issues and not _any_core_alive():
        # Brief delay so restarts have a chance to register before re-check.
        # 2s matches the fix-loop's per-service `time.sleep(1)` budget.
        import time as _t; _t.sleep(2)
        residual_checks = run_all_checks()
        emit_task_for_failures(residual_checks)

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
