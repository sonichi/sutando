#!/usr/bin/env python3
"""Shared helpers for the engine-conflict-resolve skill scripts.

Mirrors the contracts of the desktop app's engine/update-engine-git.sh:
same mkdir lock (ENGINE_UPDATE_LOCK.d + info pid=/ts= file), same
:(exclude)workspace pathspec, same /* + !/workspace/ sparse guard.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

# The scratch merge commit identity is fixed so proposals are deterministic
# regardless of the host's git config.
SUTANDO_IDENT = {
    "GIT_AUTHOR_NAME": "Sutando",
    "GIT_AUTHOR_EMAIL": "noreply@local",
    "GIT_COMMITTER_NAME": "Sutando",
    "GIT_COMMITTER_EMAIL": "noreply@local",
}

WS_EXCLUDE = ":(exclude)workspace"
META_NAME = "sutando-engine-conflict.json"
LOCK_NAME = "ENGINE_UPDATE_LOCK.d"
PROPOSAL_NAME = "ENGINE_CONFLICT_PROPOSAL.json"

EXIT_NO_GIT = 6

# Declared in this skill's manifest.json config block (skills/MANIFEST.md
# convention) — read precedence: CLI --git > env > manifest default.
GIT_CONFIG_KEY = "ENGINE_CONFLICT_GIT"

_GIT: Optional[str] = None
_EXEC_PATH: Optional[str] = None  # "" = derivation attempted and failed
_CLI_GIT: Optional[str] = None
_ENGINE_HINT: Optional[Path] = None


def set_git(path: Optional[str]) -> None:
    """CLI --git override; the top tier of the declared config precedence."""
    global _CLI_GIT
    if path:
        _CLI_GIT = path


def set_engine_hint(engine) -> None:
    """The engine checkout location; lets resolution derive the bundle's git."""
    global _ENGINE_HINT
    if engine:
        _ENGINE_HINT = Path(engine)


def engine_hint_from_scratch(scratch: Path) -> None:
    """A linked worktree's .git is a plain 'gitdir: <engine>/.git/worktrees/<id>'
    pointer file — readable without git, so the bundle tier works for propose too."""
    try:
        text = (Path(scratch) / ".git").read_text().strip()
    except OSError:
        return
    if text.startswith("gitdir:") and "/.git/worktrees/" in text:
        set_engine_hint(text[len("gitdir:"):].strip().split("/.git/worktrees/")[0])


def skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def manifest_config(key: str) -> str:
    try:
        data = json.loads((skill_dir() / "manifest.json").read_text())
        val = (data.get("config") or {}).get(key)
        return val if isinstance(val, str) else ""
    except (OSError, ValueError):
        return ""


def _runnable(path: str) -> bool:
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def _repo_src() -> Optional[Path]:
    for p in Path(__file__).resolve().parents:
        if (p / "src" / "git_binary.py").is_file():
            return p / "src"
    return None


def _finalize_git(cand: str) -> str:
    """Cache the binary AND its exec-path: git's merge machinery re-execs
    itself, and under an empty PATH only GIT_EXEC_PATH lets that resolve."""
    global _GIT, _EXEC_PATH
    _GIT = cand
    try:
        proc = subprocess.run([cand, "--exec-path"], capture_output=True, text=True, timeout=15)
        _EXEC_PATH = proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        _EXEC_PATH = ""
    return _GIT


def resolve_git() -> str:
    """Trusted git, never a traceback and never the bare CLT stub:
    --git > $ENGINE_CONFLICT_GIT > manifest config > <engine-parent>/bin/git
    (the desktop bundle, #305) > src/git_binary.py contract."""
    if _GIT:
        return _GIT
    for tier, cand in (("--git", _CLI_GIT),
                       ("$" + GIT_CONFIG_KEY, os.environ.get(GIT_CONFIG_KEY)),
                       ("manifest config " + GIT_CONFIG_KEY, manifest_config(GIT_CONFIG_KEY))):
        if cand:
            if not _runnable(cand):
                emit({"status": "no-git", "reason": "no-git",
                      "error": f"configured git ({tier}) is not runnable: {cand}"},
                     exit_code=EXIT_NO_GIT)
            return _finalize_git(cand)
    if _ENGINE_HINT is not None:
        bundled = Path(_ENGINE_HINT).resolve().parent / "bin" / "git"
        if _runnable(str(bundled)):
            return _finalize_git(str(bundled))
    src = _repo_src()
    if src is not None:
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        try:
            import git_binary
            cand = git_binary.resolve_git()
        except Exception:
            cand = None
        if cand:
            return _finalize_git(cand)
    emit({"status": "no-git", "reason": "no-git",
          "error": "no usable git — pass --git, set ENGINE_CONFLICT_GIT (declared in "
                   "skills/engine-conflict-resolve/manifest.json), or run against a desktop "
                   "bundle whose engine dir ships bin/git"},
         exit_code=EXIT_NO_GIT)


def run_git(repo, *args: str, check: bool = True, ident: bool = False) -> "subprocess.CompletedProcess[str]":
    git = resolve_git()
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if _EXEC_PATH:
        env["GIT_EXEC_PATH"] = _EXEC_PATH
    if ident:
        env.update(SUTANDO_IDENT)
    try:
        proc = subprocess.run(
            [git, "-C", str(repo)] + list(args),
            capture_output=True, text=True, env=env,
        )
    except OSError as e:
        detail = "" if _EXEC_PATH else " (GIT_EXEC_PATH could not be derived via --exec-path)"
        emit({"status": "no-git", "reason": "no-git",
              "error": f"git executable failed to launch ({e}){detail} — "
                       "set ENGINE_CONFLICT_GIT or pass --git"},
             exit_code=EXIT_NO_GIT)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc


class GitError(RuntimeError):
    pass


def git_out(repo, *args: str) -> str:
    return run_git(repo, *args).stdout.strip()


def emit(payload: dict, exit_code: int = 0) -> "None":
    print(json.dumps(payload, indent=2))
    sys.exit(exit_code)


def die(error: str, reason: str = "error", exit_code: int = 1, **extra) -> "None":
    payload = {"status": "error", "reason": reason, "error": error}
    payload.update(extra)
    emit(payload, exit_code)


def load_pending(path: Path) -> dict:
    if not path.is_file():
        die(f"pending file not found: {path}", reason="pending-missing")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        die(f"pending file unreadable: {e}", reason="pending-invalid")
    for key in ("new_sha", "old_sha", "snapshot_branch"):
        if not isinstance(data.get(key), str) or not data[key]:
            die(f"pending file missing required field '{key}': {path}", reason="pending-invalid")
    if not isinstance(data.get("conflicting_files"), list):
        data["conflicting_files"] = []
    return data


def is_git_checkout(repo: Path) -> bool:
    return run_git(repo, "rev-parse", "--git-dir", check=False).returncode == 0


def tree_dirty(repo: Path) -> str:
    """Non-empty string = dirty. Same pathspec as the updater's tree_status."""
    return run_git(repo, "status", "--porcelain", "--", ".", WS_EXCLUDE).stdout.strip()


def default_scratch(engine: Path, new_sha: str) -> Path:
    digest = hashlib.sha256(f"{engine.resolve()}\n{new_sha}".encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "sutando-engine-conflict" / digest


def git_path(repo: Path, name: str) -> Path:
    out = git_out(repo, "rev-parse", "--git-path", name)
    p = Path(out)
    return p if p.is_absolute() else (Path(repo) / p).resolve()


def read_meta(scratch: Path) -> Optional[dict]:
    p = git_path(scratch, META_NAME)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def write_meta(scratch: Path, meta: dict) -> None:
    p = git_path(scratch, META_NAME)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, p)


def proposal_path(pending: Path) -> Path:
    """The confirmed-proposal record lives beside the pending file (state dir)."""
    return Path(pending).resolve().parent / PROPOSAL_NAME


def write_proposal(pending: Path, record: dict) -> None:
    p = proposal_path(pending)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, p)


def read_proposal(pending: Path) -> Optional[dict]:
    p = proposal_path(pending)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def unmerged_paths(repo: Path) -> List[str]:
    out = run_git(repo, "ls-files", "-u").stdout
    seen: List[str] = []
    for line in out.splitlines():
        path = line.split("\t", 1)[1] if "\t" in line else ""
        if path and path not in seen:
            seen.append(path)
    return seen


def merge_in_progress(repo: Path) -> bool:
    return run_git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0


def commit_exists(repo: Path, sha: str) -> bool:
    return run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def ensure_workspace_guard(repo: Path) -> None:
    """Same defense the updater re-asserts: sparse-checkout excludes /workspace/."""
    run_git(repo, "config", "core.sparseCheckout", "true")
    run_git(repo, "config", "core.sparseCheckoutCone", "false")
    sparse = git_path(repo, "info/sparse-checkout")
    sparse.parent.mkdir(parents=True, exist_ok=True)
    if not sparse.is_file() or "!/workspace/" not in sparse.read_text().splitlines():
        sparse.write_text("/*\n!/workspace/\n")


def log(msg: str) -> None:
    print(f"engine-conflict-resolve: {msg}", file=sys.stderr)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_lock(state_dir: Path, busy_exit: int = 3) -> Path:
    """Take the updater's own mutex. Busy → exit(busy_exit); dead holder → reclaim."""
    lock_dir = state_dir / LOCK_NAME
    tries = 0
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            pass
        opid: Optional[int] = None
        try:
            for line in (lock_dir / "info").read_text().splitlines():
                if line.startswith("pid="):
                    opid = int(line[4:].strip())
                    break
        except (OSError, ValueError):
            opid = None
        if opid is None:
            # Holder may be between mkdir and info-write: only reclaim an old dir.
            try:
                age = time.time() - lock_dir.stat().st_mtime
            except OSError:
                age = 0.0
            if age < 10:
                die(f"another engine mutator is starting (lock {lock_dir}) — not touching the checkout",
                    reason="lock-busy", exit_code=busy_exit)
        elif _pid_alive(opid):
            die(f"another engine mutator is running (pid {opid}, lock {lock_dir}) — not touching the checkout",
                reason="lock-busy", exit_code=busy_exit)
        log(f"reclaiming stale lock (pid {opid if opid is not None else 'unknown'} not running)")
        _remove_lock(lock_dir)
        tries += 1
        if tries >= 3:
            die(f"could not acquire {lock_dir} after reclaim attempts", reason="lock-error")
    (lock_dir / "info").write_text(
        "pid=%d\nts=%s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    )
    return lock_dir


def _remove_lock(lock_dir: Path) -> None:
    """Idempotent like the updater's rm -rf: a concurrent reclaim is not an error."""
    try:
        for child in lock_dir.iterdir():
            try:
                child.unlink()
            except FileNotFoundError:
                pass
        lock_dir.rmdir()
    except FileNotFoundError:
        pass
    except OSError as e:
        die(f"could not remove lock {lock_dir}: {e}", reason="lock-error")


def release_lock(lock_dir: Path) -> None:
    if lock_dir.is_dir():
        _remove_lock(lock_dir)
