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
  python3 src/health-check.py --recover-core   # auto-restart the core when alive-but-wedged (guarded)

Checks:
  - macOS TCC Documents-folder access (when repo is under ~/Documents)
  - Voice agent (port 9900), web client, agent API, dashboard
  - Critical files (CLAUDE.md, build_log.md, ACTIVITY.md)
  - Memory system (MEMORY.md index, key memory files)
  - Notes directory
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import fcntl  # POSIX file locking for the recovery critical section
except ImportError:  # non-POSIX (e.g. Windows) — the lock degrades to a no-op
    fcntl = None

REPO_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from util_paths import claude_home_path, shared_personal_path  # noqa: E402
from workspace_default import resolve_workspace, status_read_path  # noqa: E402
from sutando_config import resolve_core_runtime  # noqa: E402

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
    slug = str(repo).replace("/", "-")
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


def _resolve_dotenv() -> Path:
    """Resolve the `.env` path via the canonical resolver.

    The 2-tier fallback (repo root -> workspace, #1871) lives in
    `sutando_config.py` — the canonical resolver — so this consumer never
    inlines the path. (The #1973 Sutando.app bundle tier is deferred pending the
    app-bundle install-location decision — see sutando_config.resolve_dotenv.)
    """
    from sutando_config import resolve_dotenv  # noqa: PLC0415
    return resolve_dotenv(REPO_DIR, WORKSPACE_DIR)


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


def check_file(path: Path, name: str) -> dict:
    """Check if a file exists and is non-empty."""
    if not path.exists():
        return {"name": name, "status": "missing", "detail": str(path)}
    size = path.stat().st_size
    if size == 0:
        return {"name": name, "status": "empty", "detail": str(path)}
    return {"name": name, "status": "ok", "detail": f"{size} bytes"}


def check_directory(path: Path, name: str) -> dict:
    """Check if a directory exists and has files."""
    if not path.exists():
        return {"name": name, "status": "missing", "detail": str(path)}
    count = len(list(path.glob("*.md")))
    return {"name": name, "status": "ok", "detail": f"{count} .md files"}


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


def check_memory_index_integrity() -> "dict | None":
    """Catch memories that exist on disk but will never load into a session.

    A memory only loads if it is (a) present in the LIVE memory dir and (b)
    referenced in that dir's MEMORY.md index. Two silent-loss modes have bitten
    us (recurring field report 64340119): a memory file written to the live dir
    but never added to MEMORY.md, and a hard-won capability memory stranded in a
    ``*-BACKUP`` tree (created by scripts/sutando-migrate.sh) that never made it
    into the live index — so the rule it carried was written yet never recalled.

    Warn (never fail) listing the orphaned/stranded files so the divergence is
    visible instead of silently dropping the memory. Returns None on a clean
    index or when the memory dir does not exist yet.
    """
    if not MEMORY_DIR.exists():
        return None
    index = MEMORY_DIR / "MEMORY.md"
    index_text = index.read_text(errors="ignore") if index.exists() else ""

    # (a) live memory files not referenced anywhere in MEMORY.md → won't load.
    unindexed = [
        p.name for p in sorted(MEMORY_DIR.glob("*.md"))
        if p.name != "MEMORY.md"
        and p.name not in index_text and p.name[:-3] not in index_text
    ]

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

    if not unindexed and not stranded:
        return {"name": "memory-index", "status": "ok",
                "detail": "all memory files present in the MEMORY.md index"}
    parts = []
    if unindexed:
        parts.append(
            f"{len(unindexed)} memory file(s) not in MEMORY.md (won't load): "
            + ", ".join(unindexed[:6]) + ("…" if len(unindexed) > 6 else "")
        )
    if stranded:
        parts.append(
            f"{len(stranded)} memory file(s) stranded in a *-BACKUP tree, absent from the live dir: "
            + ", ".join(sorted(set(stranded))[:6]) + ("…" if len(set(stranded)) > 6 else "")
        )
    return {"name": "memory-index", "status": "warn", "detail": "; ".join(parts)}


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
    # Canonical config first (vault.remote_url), then the deprecated .env alias.
    repo_url = vault.get("remote_url") or ""
    if not repo_url:
        env_path = _resolve_dotenv()
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("SUTANDO_MEMORY_REPO="):
                    repo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
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
                return {"name": name, "status": "warn", "detail": f"last sync {age_h:.0f}h ago (stale)"}
            return {"name": name, "status": "ok", "detail": f"last sync {age_h:.1f}h ago"}
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
            return {"name": name, "status": "warn", "detail": f"last sync {age_h:.0f}h ago (stale)"}
        return {"name": name, "status": "ok", "detail": f"last sync {age_h:.1f}h ago"}
    return {"name": name, "status": "ok", "detail": "initialized, never fetched"}


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
        todo = sorted(k for k, v in rows.items() if isinstance(v, dict) and v.get("state") == "todo")
        age_s = max(0, int(time.time()) - int(data.get("updated_at", 0) or 0))
    except (ValueError, OSError, TypeError):
        return {"name": name, "status": "warn", "detail": "onboarding-status.json unreadable"}
    if todo:
        return {
            "name": name,
            "status": "warn",
            "detail": f"user-facing setup incomplete: {', '.join(todo)} (as of {age_s}s ago)",
        }
    return {"name": name, "status": "ok", "detail": f"all checklist rows satisfied ({age_s}s ago)"}


def check_host_subtrees() -> dict:
    """Surface per-host subtrees (hosts/<host>/) that have stopped syncing.

    Under the hosts/<hostname>/ convention each host writes only its own
    subtree, so the newest file mtime in a subtree is that host's last sync. A
    subtree not updated in SUTANDO_STALE_HOST_DAYS days means that host went
    quiet (crashed, decommissioned, or sync broke) — surface it rather than
    letting it silently rot (a gap in both the old machine-<host>/ model and the
    new one until now). Read-only.
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
    stale, fresh = [], 0
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
        age_d = (now - newest) / 86400
        if age_d > stale_days:
            stale.append(f"{d.name} ({age_d:.0f}d)")
        else:
            fresh += 1
    if stale:
        return {"name": name, "status": "warn",
                "detail": f"{len(stale)} host subtree(s) stale (>{stale_days:.0f}d): "
                          f"{', '.join(stale)} — host stopped syncing?"}
    return {"name": name, "status": "ok", "detail": f"{fresh} host subtree(s), all synced <{stale_days:.0f}d"}


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
    """Try to reload a launchd job."""
    plist_map = {
        "com.sutando.voice-agent": Path.home() / "Library/LaunchAgents/com.sutando.voice-agent.plist",
        "com.sutando.web-client": Path.home() / "Library/LaunchAgents/com.sutando.web-client.plist",
    }
    plist = plist_map.get(label)
    if not plist or not plist.exists():
        return f"no plist found for {label}"

    uid = subprocess.run(["/usr/bin/id", "-u"], capture_output=True, text=True).stdout.strip()
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


def fix_down_bridges(checks: list) -> list:
    """Restart configured-but-not-running channel bridges.

    A dead bridge reports status "warn" (optional channels don't page), which
    keeps it out of `issues` — so main()'s fix loop never reaches it, and
    owner DMs silently queue channel-side until someone notices (2026-07-02:
    discord-bridge died at boot with nothing logged; --fix left it down and 8
    DMs sat undelivered). The exact-detail match excludes every other bridge
    warn (multiple PIDs, token invalid, stale log), each of which needs
    different handling than a plain start.

    Returns the list of bridge names restarted.

    Launch parity with startup.sh (per PR #1898 review): a naive
    `sys.executable src/<bridge>.py` skips the bootstrapping startup.sh does and
    crash-loops for two bridges:
      - discord-bridge needs an interpreter that can `import discord`;
        sys.executable (whatever launched health-check) frequently can't.
      - slack-bridge needs SLACK_BOT_TOKEN/SLACK_APP_TOKEN, which startup.sh
        sources from channels/slack/.env before launch — without them the
        bridge exits immediately.
    So mirror startup.sh: probe the same interpreter candidates for the
    bridge's import, and inject the slack channel .env into the child's env.
    Fail-safe: if no capable interpreter is found (or the required env is
    missing), skip that bridge rather than spawn a guaranteed crash-loop.
    """
    restarted = []
    for c in checks:
        if (
            c["name"] in ("telegram-bridge", "discord-bridge", "slack-bridge")
            and c["status"] == "warn"
            and c.get("detail") == "configured but not running"
        ):
            name = c["name"]
            interp = _bridge_interpreter(name)
            if interp is None:
                # No interpreter that can import the bridge's dependency —
                # spawning would just crash-loop (startup.sh skips it too).
                continue
            child_env = os.environ.copy()
            if name == "slack-bridge":
                # startup.sh sources channels/slack/.env so SLACK_BOT_TOKEN /
                # SLACK_APP_TOKEN reach the child. Mirror that here; skip the
                # restart if the env file / tokens are missing (fail-safe).
                slack_env = _load_channel_env("slack")
                if "SLACK_BOT_TOKEN" not in {**os.environ, **slack_env}:
                    continue
                child_env.update(slack_env)
            log_path = WORKSPACE_DIR / "logs" / f"{name}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # `with` closes the parent's handle after Popen; the child holds
            # its own dup of the fd, so the log stays writable.
            with open(str(log_path), "a") as log_f:
                subprocess.Popen([interp, str(REPO_DIR / "src" / f"{name}.py")],
                                 stdout=log_f, stderr=subprocess.STDOUT,
                                 env=child_env, start_new_session=True)
            restarted.append(name)
    return restarted


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
    """
    required = _BRIDGE_REQUIRED_IMPORT.get(name)
    if required is None:
        return sys.executable
    for cand in _BRIDGE_INTERP_CANDIDATES:
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

def mark_stale_if_outdated(check: dict, src_file: Path, pgrep_pattern: str, threshold_sec: int = 1800, binary_path: Optional[Path] = None) -> None:
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
    the message tells the user to rebuild, not just restart.
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


def _file_unchanged_since(src_file: Path, proc_start: float) -> bool:
    """Return True if git's last-commit-time for src_file predates proc_start
    AND the file has no uncommitted changes. Used to suppress stale-detection
    false positives from git operations that bump mtime without changing
    content. Silent-failure: returns False on any git error so real stale
    deploys aren't hidden.
    """
    try:
        log = subprocess.run(
            ["/usr/bin/git", "log", "-1", "--format=%ct", "HEAD", "--", str(src_file)],
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
            ["/usr/bin/git", "diff", "--quiet", "HEAD", "--", str(src_file)],
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
        check["detail"] = f"voice-agent {vs}" if vs else "voice-agent status unknown"
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
        check["detail"] = f"voice-agent {vs}" if vs else "voice-agent status unknown"
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


def check_quota_telemetry(proxy_status: str) -> dict:
    """Warn when the credential proxy is up but producing no quota state.

    quota-state.json is written by the proxy from the quota headers on
    upstream responses, so it only appears if a core actually ROUTES through
    the proxy. `src/startup.sh` is the only thing that exports
    ANTHROPIC_BASE_URL=http://localhost:7846 — and a core launched by the
    desktop supervisor never runs startup.sh. Result on such a host: the
    proxy is healthy and listening, every check is green, and quota
    telemetry is silently absent forever. The proactive loop's per-pass
    budget check reads "unknown" on every pass and nobody is told why.

    The existing credential-proxy check can't catch this: it is a plain
    TCP-listening probe (correctly so — a forwarding proxy has no liveness
    endpoint), so "listening" is all it can ever assert.

    Deliberately narrow to stay quiet in the legitimate cases:
      - proxy not up          -> silent. Not every host routes through it,
                                 and its own check already says so.
      - file present          -> ok, with its age. NOT stale-checked: a quiet
                                 core legitimately writes nothing for a long
                                 time, so an age threshold would fire on
                                 healthy idle hosts. Absence is the signal
                                 that actually distinguishes broken wiring.
    """
    check = {"name": "quota-telemetry", "status": "ok"}
    if proxy_status != "ok":
        check["detail"] = "credential proxy not running — quota telemetry not expected"
        return check
    path = status_read_path("quota-state.json", WORKSPACE_DIR)
    if path.exists():
        try:
            age_min = int((time.time() - path.stat().st_mtime) / 60)
            check["detail"] = f"quota state present (updated {age_min}m ago)"
        except OSError:
            check["detail"] = "quota state present"
        return check
    check["status"] = "warn"
    check["detail"] = (
        "credential proxy is up but has never written quota-state.json — "
        "nothing is routing through it (ANTHROPIC_BASE_URL unset; set by "
        "src/startup.sh, which a supervisor-launched core never runs). "
        "Quota-based budgeting is blind on this host."
    )
    return check


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
    if state in needs_user:
        return {"name": name, "status": "warn", "detail": f"core needs you: {detail}"}
    if state in degraded:
        return {"name": name, "status": "warn", "detail": f"core degraded: {detail}"}
    return {"name": name, "status": "ok", "detail": detail}


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
    try:
        gw_env = claude_home_path("channels", "ag2space", ".env")
        configured = bool(os.environ.get("REMOTE_TASK_TOKEN") or os.environ.get("AG2_REMOTE_TOKEN"))
        if not configured and gw_env.exists():
            configured = any(
                ln.startswith(("REMOTE_TASK_TOKEN=", "AG2_REMOTE_TOKEN="))
                for ln in gw_env.read_text(errors="replace").splitlines()
            )
    except OSError:
        configured = False
    if not configured:
        return None
    try:
        gw = subprocess.run(
            ["/usr/bin/pgrep", "-f", r"remote-gateway-bridge\.py$"],
            capture_output=True, text=True,
        )
        pids = [p for p in gw.stdout.strip().split("\n") if p] if gw.returncode == 0 else []
    except Exception:
        pids = []
    if not pids:
        return {
            "name": "gateway-bridge",
            "status": "warn",
            "detail": "configured but NOT running — ag2.space mobile messages will not be delivered",
        }
    if len(pids) > 1:
        return {
            "name": "gateway-bridge",
            "status": "warn",
            "detail": f"multiple processes ({len(pids)} PIDs: {','.join(pids)})",
        }
    return {"name": "gateway-bridge", "status": "ok", "detail": "running"}


def check_skill_symlinks() -> dict:
    """Detect skills in the OSS repo checkout that are not symlinked into
    ~/.claude/skills/. A missing symlink means Claude Code never loads the
    skill — it's silently invisible until manually linked (bug d920b18b).

    Scans REPO_DIR/skills/ for directories and checks for a matching entry
    in ~/.claude/skills/. Reports unlinked skills as 'warn'; in --fix mode,
    creates the missing symlinks automatically.
    """
    name = "skill-symlinks"
    skills_src = REPO_DIR / "skills"
    skills_dst = Path.home() / ".claude" / "skills"

    if not skills_src.exists():
        return {"name": name, "status": "ok", "detail": "skills/ dir not found — skipped"}
    if not skills_dst.exists():
        return {"name": name, "status": "ok", "detail": "~/.claude/skills/ not found — skipped"}

    # A DANGLING symlink (entry present, target gone) is the case the original
    # condition let through: `exists()` follows the link and is False, but
    # `is_symlink()` is True, so `not exists and not is_symlink` is False and the
    # skill counted as linked. Claude Code cannot load it either way, so it was
    # invisible AND reported healthy. Tracked as #2213.
    unlinked: list[str] = []   # no entry at all -> symlink_to() works
    broken: list[str] = []     # dangling link  -> must be unlinked first
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        dst = skills_dst / skill_name
        if dst.is_symlink() and not dst.exists():
            broken.append(skill_name)
        elif not dst.exists() and not dst.is_symlink():
            unlinked.append(skill_name)

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

    if not unlinked and not broken and not orphaned:
        return {"name": name, "status": "ok", "detail": f"all {sum(1 for d in skills_src.iterdir() if d.is_dir())} skills linked"}

    parts = []
    if broken:
        parts.append(f"{len(broken)} dangling: {', '.join(broken[:4])}{'...' if len(broken) > 4 else ''}")
    if unlinked:
        parts.append(f"{len(unlinked)} unlinked: {', '.join(unlinked[:4])}{'...' if len(unlinked) > 4 else ''}")
    if orphaned:
        parts.append(f"{len(orphaned)} dangling not in this repo: {', '.join(orphaned[:4])}{'...' if len(orphaned) > 4 else ''}")

    return {
        "name": name,
        "status": "warn",
        "detail": "; ".join(parts),
        "_unlinked": unlinked,
        "_broken": broken,
        "_orphaned": orphaned,
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


def apply_skill_symlink_fixes(checks: list) -> None:
    """--fix dispatch for skill-symlinks: warn-level (excluded from the issues
    loop) but auto-fixable, so it is handled by its own pass over checks."""
    for c in checks:
        if c["name"] == "skill-symlinks" and (c.get("_unlinked") or c.get("_broken")):
            result = fix_skill_symlinks(c)
            print(f"  {c['name']}: {result['detail']}")


def check_task_queue(threshold_count: int = 3, threshold_age_sec: int = 300) -> dict:
    """Detect a task-queue pileup — tasks/ directory growing without
    being drained. Independent of which watcher / loop is dying: the queue
    backs up either way. Fires when BOTH count and age cross thresholds so
    a transient spike of fresh tasks (normal during heavy use) doesn't
    alert.
    """
    name = "task-queue"
    tasks_dir = WORKSPACE_DIR / "tasks"
    if not tasks_dir.exists():
        return {"name": name, "status": "ok", "detail": "tasks/ not yet created"}
    # *.txt at the top level only — archive lives in tasks/archive/<YYYY-MM>/
    # (PR #591) and shouldn't count toward the queue.
    files = [p for p in tasks_dir.glob("*.txt") if p.is_file()]
    if not files:
        return {"name": name, "status": "ok", "detail": "queue empty"}
    now = time.time()
    oldest = min(files, key=lambda p: p.stat().st_mtime)
    oldest_age = int(now - oldest.stat().st_mtime)
    if len(files) > threshold_count and oldest_age > threshold_age_sec:
        return {
            "name": name,
            "status": "warn",
            "detail": f"{len(files)} tasks queued, oldest {oldest_age}s — watcher or core may be stuck",
        }
    return {"name": name, "status": "ok", "detail": f"{len(files)} task(s), oldest {oldest_age}s"}


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
    if not _any_core_alive():
        return {"name": name, "status": "ok", "detail": "no core running — watcher not expected"}
    trees = _watcher_trees()
    roots = sorted(trees)
    if not pid_file.exists():
        if roots:
            # Sentinel gone but watchers alive: they are draining tasks/ but
            # nothing supervises them, and each new start adds another (observed
            # 2026-07-21: two trees, both reporting the same TASK_FILE — i.e.
            # duplicate processing, not a stalled queue).
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
            # The sentinel tracks only the MOST RECENT start (the script writes
            # $$ at startup), so a dead sentinel does NOT mean nothing is
            # draining tasks/ — an older watcher can still be running. Saying
            # "not running" here would be false, and restarting on that basis is
            # what produces the duplicates in the first place.
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


def bridge_log_content_status(name: str, status: str, tail: list[str]) -> Optional[tuple[str, str]]:
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
    """
    if name == "discord-bridge":
        if any("LoginFailure" in ln or "Improper token" in ln for ln in tail):
            return "fail", "token invalid (LoginFailure) — regenerate at discord.com/developers/applications"
    elif name == "slack-bridge" and status == "ok":
        warn_idxs = [i for i, ln in enumerate(tail) if "60s elapsed with zero events" in ln]
        if warn_idxs:
            events_after = any("Wrote task-" in ln for ln in tail[warn_idxs[-1] + 1:])
            if not events_after:
                return "warn", "connected but events not arriving — enable Event Subscriptions at api.slack.com/apps"
    return None


def run_all_checks() -> list[dict]:
    checks = []

    # Core services (required)
    voice_check = check_port(9900, "voice-agent", probe=True)
    if voice_check["status"] == "ok":
        mark_stale_if_outdated(voice_check, REPO_DIR / "src" / "voice-agent.ts", "voice-agent.ts")
    checks.append(voice_check)
    checks.append(check_voice_watchers(voice_check))
    checks.append(check_voice_transport(voice_check))
    checks.append(check_bodhi_dist())

    web_check = check_port(8080, "web-client", probe=True)
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

    # Credential proxy (port 7846) — the OAuth-injection + quota-header path
    # (skills/quota-tracker/scripts/credential-proxy.ts). It was previously
    # unmonitored, so a dead proxy (= broken auth/quota for proxy-routed cores)
    # never surfaced on the dashboard. Plain TCP-listening check (probe=False):
    # it's a forwarding proxy with no liveness endpoint, so an HTTP probe would
    # be forwarded upstream and misread as "wedged". Optional (not every node
    # routes through it) → down is a warning, not a failure.
    proxy_check = check_port(7846, "credential-proxy", probe=False)
    if proxy_check["status"] == "down":
        proxy_check["status"] = "warn"
        proxy_check["detail"] = "not running (optional)"
    checks.append(proxy_check)

    # Quota telemetry — only meaningful when the proxy is actually up.
    checks.append(check_quota_telemetry(proxy_check["status"]))

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

    _mem_index = check_memory_index_integrity()
    if _mem_index:
        checks.append(_mem_index)

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

    # Memory sync
    checks.append(check_memory_sync())

    # Per-host subtree freshness (hosts/<host>/ stopped syncing?)
    checks.append(check_host_subtrees())
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
        if log_file.exists() and name in ("discord-bridge", "slack-bridge"):
            try:
                tail = log_file.read_text(errors="replace").splitlines()[-60:]
                override = bridge_log_content_status(name, status, tail)
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
    if dev_bin.exists() or app_bin.exists():
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
                    REPO_DIR / "src" / "Sutando" / "main.swift",
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
    checks.append(check_task_watcher())
    checks.append(check_skill_symlinks())

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
            if now - alive_file.stat().st_mtime < max_age_s:
                return True
        except OSError:
            pass
    return False


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
    failures = [c for c in checks if c["status"] in ("down", "missing", "not_loaded", "fail", "stale", "warn")]
    if not failures:
        return

    if state_file is None or tasks_dir is None:
        if state_file is None:
            state_file = WORKSPACE_DIR / "state" / "health-last-alerted.json"
        if tasks_dir is None:
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

    # Build task content. task: is placed LAST (after trusted metadata fields)
    # so that the multi-line bullet body cannot shadow source/access_tier/priority
    # even in the theoretical case where check detail strings ever carry
    # external data. Consistent with the bridge field-order convention.
    ts_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    bullet_str = "\n".join(f"- {c['name']}: {c['status']} ({c['detail']})" for c in failures)
    body = (
        f"id: task-health-{now_ms}\n"
        f"timestamp: {ts_iso}\n"
        f"source: health-check\n"
        f"interaction_type: system_event\n"
        f"user_id: health-check\n"
        f"access_tier: owner\n"
        f"priority: low\n"
        f"task: Health check found issues. Decide whether to restart, DM owner, or treat as transient:\n"
        f"{bullet_str}\n"
    )
    task_path = tasks_dir / f"task-health-{now_ms}.txt"
    task_path.write_text(body)

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
    failures = [c for c in checks if c["status"] in ("down", "missing", "not_loaded", "fail", "stale", "warn")]
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
        if st in ("down", "missing", "not_loaded", "fail", "stale"):
            out.append(c)
        elif st == "warn" and "on-demand" not in (c.get("detail") or ""):
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
#   - Graceful degradation: the FIRST restart of an episode keeps 1M; if the
#     wedge recurs (the 1M restart didn't hold), the next restart pins
#     SUTANDO_CORE_MODEL=opus (standard 200K) so the agent keeps WORKING.
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
    try:
        files = [p for p in tasks_dir.glob("*.txt") if p.is_file()]
    except OSError:
        return None
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
            if now - alive_file.stat().st_mtime >= 90.0:
                continue  # stale heartbeat — not a live core
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


def _default_core_restart(standard_context: bool) -> bool:
    """Run the selected core CLI dispatcher with --restart out-of-process. When
    standard_context is True and Claude is selected, pin
    SUTANDO_CORE_MODEL=opus so the restarted core uses the standard 200K window.
    Codex restarts without a provider-specific model override. Returns True if
    the restart command exited 0."""
    script = REPO_DIR / "src" / "agent" / "start-cli.sh"
    if not script.exists():
        return False
    env = _resolve_launch_env()  # pragma: no cover — real-subprocess restart path (integration, not unit)
    if standard_context and resolve_core_runtime(REPO_DIR) == "claude":
        env["SUTANDO_CORE_MODEL"] = "opus"
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

        # Escalation: the FIRST restart in the trailing hour keeps 1M; if we're
        # wedged again after a prior restart, that restart didn't hold — degrade
        # to standard 200K context so the agent keeps working instead of re-wedging.
        standard_context = len(history) >= 1
        mode = "standard" if standard_context else "1m"
        ctx_note = (
            "in standard 200K context (the 1M restart didn't hold)" if standard_context
            else "keeping 1M context"
        )
        # DM the owner BEFORE restarting. Capture the result (blocker 2): if the
        # DM fails we still restart (recovery > notification — don't leave the
        # core wedged because Slack is down), but we record dm_sent=False and log
        # to stderr/launchd so the restart is never invisible.
        dm_ok = send(
            f":hourglass: *Sutando core wedged* — oldest task stuck {oldest_age // 60} min "
            f"while the core process is alive (likely the 1M usage-credit gate or a "
            f"stalled turn). Auto-restarting {ctx_note}. Queued tasks are preserved."
        )
        if not dm_ok:
            print(f"[recover-core] WARNING: wedge-restart DM failed; restarting anyway (mode={mode})", flush=True)

        if not restart_fn(standard_context):
            # Restart launch failed — don't burn a cooldown/history slot, and
            # keep the observation so we stay confirmed and retry next pass.
            return {"action": "restart_failed", "mode": mode, "dm_sent": dm_ok}

        history.append(now)
        state["restart_history"] = history
        state["last_restart"] = now
        state["last_restart_mode"] = mode
        state["last_restart_dm_sent"] = dm_ok
        _reset_observation()  # re-observe after the restart settles
        state.pop("gave_up_at", None)
        _save()
        return {
            "action": "restarted", "mode": mode, "oldest_age": oldest_age,
            "restarts_last_hour": len(history), "dm_sent": dm_ok,
        }
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fh.close()


def main():
    as_json = "--json" in sys.argv
    do_fix = "--fix" in sys.argv
    do_emit = "--emit-task" in sys.argv
    do_notify = "--notify-on-fail" in sys.argv
    do_notify_slack = "--notify-slack" in sys.argv
    do_recover = "--recover-core" in sys.argv
    quiet = "--quiet" in sys.argv or "-q" in sys.argv

    checks = run_all_checks()
    issues = [c for c in checks if c["status"] not in ("ok", "warn")]

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

    # Optional: auto-recover a wedged core (alive-but-stuck) by restarting it.
    # Independent of the checks list — keys off the queue-drain + heartbeat
    # signals directly (see recover_core_if_wedged). Intended for the
    # launchd-supervised fallback so the core self-heals from the 1M-gate wedge
    # without waiting for a human. Heavily guarded (confirm window, cooldown,
    # give-up cap); a no-op when the core is healthy.
    if do_recover:
        recover_core_if_wedged()

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
        print(json.dumps({"checks": checks, "issues": len(issues), "total": len(checks)}, indent=2))
        return

    # --quiet: print only issues (or nothing if clean). Exit code reflects state.
    # Useful for cron callers and automation that only cares about problems.
    if quiet:
        if issues:
            for c in issues:
                icon = "♻" if c["status"] == "stale" else "✗"
                print(f"{icon} {c['name']}: {c['status']} ({c['detail']})")
            if do_fix:
                # Fall through to existing fix path below
                pass
            else:
                sys.exit(1)
        else:
            sys.exit(0)

    # Human-readable
    if not quiet:
        print("Sutando Health Check")
        print("=" * 40)

        for c in checks:
            icon = "✓" if c["status"] == "ok" else "⚠" if c["status"] == "warn" else "✗" if c["status"] in ("down", "missing", "not_loaded") else "♻" if c["status"] == "stale" else "~"
            print(f"  {icon} {c['name']:30s} {c['status']:12s} {c['detail']}")

        print()
    if not issues:
        if not quiet:
            print("All systems operational.")
    else:
        print(f"{len(issues)} issue(s) found:")
        for c in issues:
            print(f"  - {c['name']}: {c['status']} ({c['detail']})")

        if do_fix:
            print()
            print("Attempting fixes...")
            # skill-symlinks is "warn" (excluded from issues) but auto-fixable —
            # handle it separately from the issues loop.
            apply_skill_symlink_fixes(checks)
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
                elif c["name"] in ("telegram-bridge", "discord-bridge", "slack-bridge"):  # pragma: no cover - --fix restart path spawns real subprocesses; not unit-tested
                    # LoginFailure means the token is bad — restarting won't help
                    # and would create a duplicate alongside the launchd-managed one.
                    if "LoginFailure" in c.get("detail", "") or "token invalid" in c.get("detail", ""):
                        print(f"  {c['name']}: token invalid — regenerate at discord.com/developers/applications (no restart)")
                    else:
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
                        # Use sys.executable to avoid launchd's minimal PATH
                        # resolving `python3` to /usr/bin/python3 (3.9), which
                        # doesn't have the homebrew site-packages (discord,
                        # dotenv, etc.) — restart would crash on import.
                        # Log path uses logs/ (post-PR #251 refactor).
                        subprocess.Popen([sys.executable, str(REPO_DIR / "src" / f"{c['name']}.py")],
                                         stdout=open(str(WORKSPACE_DIR / "logs" / f"{c['name']}.log"), "a"),
                                         stderr=subprocess.STDOUT, start_new_session=True)
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
                    source = REPO_DIR / "src" / "Sutando" / "main.swift"
                    if (
                        c.get("status") == "warn"
                        and "not running" in (c.get("detail") or "")
                        and binary.exists()
                        and source.exists()
                        and binary.stat().st_mtime >= source.stat().st_mtime
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
