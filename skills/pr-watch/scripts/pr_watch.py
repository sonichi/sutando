#!/usr/bin/env python3
"""pr-watch: event-driven GitHub PR watching through `gh webhook forward`.

Subcommands:
  add --repo O/R [--author LOGIN ...] [--me LOGIN ...]   watch a repo (merges into an existing entry)
  remove --repo O/R                                      stop watching a repo
  list                                                   print the watch config
  status                                                 runner, forwarders and recorded hook ids
  install-mod --mods-dir DIR                             write the Claude Code mod into DIR/pr-watch
  cleanup [--force]                                      delete the hook ids this skill recorded
  run                                                    the forwarder supervisor the mod spawns

`run` prints one `PR_EVENT ...` line per relevant event on stdout and `PR_WATCH_NOTE ...` lines for
problems the session should see. State lives in <workspace>/state/pr-watch/, never in the mod folder.
Standard library only, plus the `gh` CLI with the cli/gh-webhook extension.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SKILL_DIR = Path(__file__).resolve().parents[1]  # lint-workspace-resolution: allow-repo-root
MOD_SRC = SKILL_DIR / "mod"
MOD_NAME = "pr-watch"

EVENTS = "pull_request,pull_request_review"
PR_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review", "closed")
FORWARDER_HOOK_NAME = "cli"
FORWARDER_URL_MARK = "webhook-forwarder.github.com"
HOOK_EXISTS = "Hook already exists"
RECORD_WINDOW_S = 30
RESTART_DELAY_S = 15
CONFLICT_DELAY_S = 300
LOCK_WAIT_S = 20
MAX_TAIL = 16 * 1024 * 1024
# Generated per install, or by Claude Code when it loads the mod.
SKIP_IN_MOD = ("node_modules", "tsconfig.json", ".claude-plugin/types", "hooks/paths.ts")

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")

DEFAULT_PROMPT = (
    "[pr-watch] {event}\n"
    "Standing ask from the owner: keep watching these pull requests. If the head moved, re-review "
    "at the new head. If a review was requested from the owner, review it. Note merges and closes. "
    "Tell the owner only when something needs them; otherwise finish quietly."
)


# ---- state paths ---------------------------------------------------------------------------

def resolve_state_dir(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path(__file__).resolve().parent
    # The copy install-mod places in the state dir runs without a checkout around it.
    if here.name == MOD_NAME and here.parent.name == "state":
        return here
    sys.path.insert(0, str(SKILL_DIR.parents[1] / "src"))
    from workspace_default import resolve_workspace
    ws = resolve_workspace()
    return Path(ws) / "state" / MOD_NAME


class State:
    def __init__(self, root: Path):
        self.root = root
        self.config = root / "config.json"
        self.hooks = root / "hooks.json"
        self.forwarders = root / "forwarders.json"
        self.runner_pid = root / "run.pid"
        self.lock = root / "run.lock"
        self.logs = root / "logs"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(exist_ok=True)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise SystemExit(f"pr-watch: cannot read {path}: {exc}")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


# ---- config ----------------------------------------------------------------------------------

def load_config(st: State) -> dict:
    cfg = read_json(st.config, {})
    if not isinstance(cfg, dict):
        raise SystemExit(f"pr-watch: {st.config} is not a JSON object")
    cfg.setdefault("watches", [])
    cfg.setdefault("prompt", DEFAULT_PROMPT)
    return cfg


def _logins(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for v in values:
        if v != "*" and not LOGIN_RE.match(v):
            raise SystemExit(f"pr-watch: not a GitHub login: {v!r}")
        if v.lower() not in [o.lower() for o in out]:
            out.append(v)
    return out


def config_add(cfg: dict, repo: str, authors: List[str], me: List[str]) -> dict:
    if not REPO_RE.match(repo):
        raise SystemExit(f"pr-watch: --repo must be OWNER/REPO, got {repo!r}")
    if not authors and not me:
        raise SystemExit("pr-watch: give at least one --author or --me")
    for w in cfg["watches"]:
        if w["repo"].lower() == repo.lower():
            w["authors"] = _logins(list(w.get("authors", [])) + authors)
            w["me"] = _logins(list(w.get("me", [])) + me)
            return w
    w = {"repo": repo, "authors": _logins(authors), "me": _logins(me)}
    cfg["watches"].append(w)
    return w


def config_remove(cfg: dict, repo: str) -> bool:
    before = len(cfg["watches"])
    cfg["watches"] = [w for w in cfg["watches"] if w["repo"].lower() != repo.lower()]
    return len(cfg["watches"]) != before


# ---- event filter ----------------------------------------------------------------------------

def _has(logins: Iterable[str], login: str) -> bool:
    low = login.lower()
    return any(x == "*" or x.lower() == low for x in logins)


def _safe(value) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-\[\]/#]", "?", str(value))[:80]


def classify(payload: dict, watches: List[dict]) -> Optional[str]:
    """One `PR_EVENT ...` line for a payload a watch asks for, else None."""
    repo = (payload.get("repository") or {}).get("full_name") or ""
    watch = next((w for w in watches if w["repo"].lower() == repo.lower()), None)
    pr = payload.get("pull_request")
    if watch is None or not isinstance(pr, dict):
        return None
    action = payload.get("action") or ""
    number = pr.get("number") or payload.get("number")
    author = (pr.get("user") or {}).get("login") or ""
    head = ((pr.get("head") or {}).get("sha") or "")[:8]
    where = f"{_safe(repo)}#{_safe(number)}"
    review = payload.get("review")
    if isinstance(review, dict):
        if action != "submitted" or not _has(watch.get("authors", []), author):
            return None
        by = (review.get("user") or {}).get("login") or ""
        state = (review.get("state") or "").lower()
        commit = (review.get("commit_id") or "")[:8]
        return (f"PR_EVENT {where} review_submitted by={_safe(by)} state={_safe(state)} "
                f"author={_safe(author)} head={_safe(commit or head)}")
    if action == "review_requested":
        reviewer = (payload.get("requested_reviewer") or {}).get("login") or ""
        if not reviewer or not _has([m for m in watch.get("me", []) if m != "*"], reviewer):
            return None
        return (f"PR_EVENT {where} review_requested reviewer={_safe(reviewer)} "
                f"author={_safe(author)} head={_safe(head)}")
    if action in PR_ACTIONS and _has(watch.get("authors", []), author):
        merged = "true" if pr.get("merged") else "false"
        return (f"PR_EVENT {where} {action} author={_safe(author)} head={_safe(head)} "
                f"merged={merged}")
    return None


def split_json_stream(buffer: str) -> Tuple[List[dict], str]:
    """Complete JSON objects at the front of `buffer`, and the unparsed tail."""
    dec = json.JSONDecoder()
    out: List[dict] = []
    i, n = 0, len(buffer)
    while True:
        while i < n and buffer[i] != "{":
            i += 1
        if i >= n:
            return out, ""
        try:
            obj, end = dec.raw_decode(buffer, i)
        except ValueError:
            # Unparsable with a later top-level object behind it: malformed, skip it.
            # Otherwise it was cut mid-write and waits for the rest, up to MAX_TAIL.
            nxt = buffer.find("\n{", i + 1)
            if nxt != -1:
                i = nxt + 1
                continue
            tail = buffer[i:]
            return out, tail if len(tail) <= MAX_TAIL else ""
        if isinstance(obj, dict):
            out.append(obj)
        i = end


# ---- GitHub hooks ----------------------------------------------------------------------------

def gh(args: List[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True, check=check)


def forwarder_hook_ids(repo: str) -> Optional[List[int]]:
    """Ids of the repo's gh-webhook forwarder hooks, or None when they cannot be listed."""
    jq = (f'.[] | select(.name == "{FORWARDER_HOOK_NAME}" and '
          f'((.config.url // "") | contains("{FORWARDER_URL_MARK}"))) | .id')
    r = gh(["api", f"repos/{repo}/hooks", "--paginate", "--jq", jq])
    if r.returncode != 0:
        return None
    try:
        return sorted(int(x) for x in r.stdout.split())
    except ValueError:
        return None


def delete_hook(repo: str, hook_id: int) -> bool:
    r = gh(["api", "-X", "DELETE", f"repos/{repo}/hooks/{hook_id}"])
    return r.returncode == 0 or "HTTP 404" in (r.stderr or "")


_hooks_lock = threading.Lock()


def record_hooks(st: State, repo: str, ids: Iterable[int]) -> None:
    with _hooks_lock:
        data = read_json(st.hooks, {})
        have = set(data.get(repo, []))
        data[repo] = sorted(have | set(ids))
        write_json(st.hooks, data)


def drop_recorded(st: State, repo: Optional[str] = None) -> Dict[str, List[int]]:
    """Delete exactly the hook ids this skill recorded; keep the ones a delete failed on."""
    with _hooks_lock:
        data = read_json(st.hooks, {})
        left: Dict[str, List[int]] = {}
        for r, ids in data.items():
            if repo is not None and r != repo:
                left[r] = ids
                continue
            kept = [i for i in ids if not delete_hook(r, i)]
            if kept:
                left[r] = kept
        write_json(st.hooks, left)
        return left


# ---- processes -------------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pid_command(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip()


def is_forwarder(pid: int) -> bool:
    cmd = pid_command(pid)
    return "webhook" in cmd and "forward" in cmd


def runner_pid(st: State) -> Optional[int]:
    try:
        pid = int(st.runner_pid.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) and "pr_watch.py" in pid_command(pid) else None


def kill_stale_forwarders(st: State) -> None:
    # A runner killed outright leaves its gh children re-parented and still forwarding.
    for repo, pid in read_json(st.forwarders, {}).items():
        if isinstance(pid, int) and pid_alive(pid) and is_forwarder(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    write_json(st.forwarders, {})


def gh_webhook_installed() -> bool:
    if shutil.which("gh") is None:
        return False
    r = gh(["extension", "list"])
    return r.returncode == 0 and "gh-webhook" in r.stdout


# ---- runner ----------------------------------------------------------------------------------

class Runner:
    def __init__(self, st: State):
        self.st = st
        self.stop = threading.Event()
        self.out_lock = threading.Lock()
        self.threads: Dict[str, Tuple[threading.Thread, threading.Event]] = {}
        self.procs: Dict[str, subprocess.Popen] = {}
        self.procs_lock = threading.Lock()

    def emit(self, line: str) -> None:
        with self.out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def note(self, text: str) -> None:
        self.emit("PR_WATCH_NOTE " + text.replace("\n", " "))

    def _save_forwarders(self) -> None:
        with self.procs_lock:
            write_json(self.st.forwarders, {r: p.pid for r, p in self.procs.items() if p.poll() is None})

    def watches(self) -> List[dict]:
        return load_config(self.st)["watches"]

    def sync(self) -> None:
        wanted = {w["repo"] for w in self.watches()}
        for repo in list(self.threads):
            if repo not in wanted:
                self.threads[repo][1].set()
                with self.procs_lock:
                    p = self.procs.get(repo)
                if p and p.poll() is None:
                    p.terminate()
                del self.threads[repo]
        for repo in sorted(wanted - set(self.threads)):
            ev = threading.Event()
            t = threading.Thread(target=self.loop, args=(repo, ev), daemon=True)
            self.threads[repo] = (t, ev)
            t.start()

    def loop(self, repo: str, gone: threading.Event) -> None:
        while not self.stop.is_set() and not gone.is_set():
            delay = self.forward_once(repo)
            if self.stop.wait(delay) or gone.is_set():
                break
        drop_recorded(self.st, repo)

    def forward_once(self, repo: str) -> int:
        drop_recorded(self.st, repo)
        before = forwarder_hook_ids(repo)
        if before is None:
            self.note(f"{repo}: cannot list hooks (need admin on the repo); retrying")
            return CONFLICT_DELAY_S
        if before:
            self.note(f"{repo}: another webhook forwarder already holds this repo "
                      f"(hook {before[0]}); GitHub allows one, retrying later")
            return CONFLICT_DELAY_S
        err_path = self.st.logs / (repo.replace("/", "_") + ".err")
        proc = subprocess.Popen(
            ["gh", "webhook", "forward", f"--repo={repo}", f"--events={EVENTS}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        with self.procs_lock:
            self.procs[repo] = proc
        self._save_forwarders()
        conflict = threading.Event()
        threading.Thread(target=self._stderr, args=(proc, err_path, conflict), daemon=True).start()
        threading.Thread(target=self._record, args=(repo, before, proc), daemon=True).start()
        buf = ""
        assert proc.stdout is not None
        for chunk in iter(proc.stdout.readline, ""):
            objs, buf = split_json_stream(buf + chunk)
            for payload in objs:
                line = classify(payload, self.watches())
                if line:
                    self.emit(line)
        proc.wait()
        with self.procs_lock:
            self.procs.pop(repo, None)
        self._save_forwarders()
        drop_recorded(self.st, repo)
        if conflict.is_set():
            self.note(f"{repo}: GitHub refused a second forwarder ({HOOK_EXISTS}); retrying later")
            return CONFLICT_DELAY_S
        return RESTART_DELAY_S

    def _stderr(self, proc: subprocess.Popen, path: Path, conflict: threading.Event) -> None:
        assert proc.stderr is not None
        with open(path, "a") as fh:
            for line in proc.stderr:
                fh.write(line)
                fh.flush()
                if HOOK_EXISTS in line:
                    conflict.set()

    def _record(self, repo: str, before: List[int], proc: subprocess.Popen) -> None:
        deadline = time.time() + RECORD_WINDOW_S
        while time.time() < deadline and proc.poll() is None and not self.stop.is_set():
            time.sleep(2)
            now = forwarder_hook_ids(repo)
            new = [i for i in (now or []) if i not in before]
            if new:
                record_hooks(self.st, repo, new)
                return

    def shutdown(self) -> None:
        self.stop.set()
        with self.procs_lock:
            procs = list(self.procs.values())
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        drop_recorded(self.st)
        write_json(self.st.forwarders, {})


def cmd_run(st: State) -> int:
    st.ensure()
    if not gh_webhook_installed():
        print("PR_WATCH_NOTE gh or its cli/gh-webhook extension is missing; ask the owner before "
              "running `gh extension install cli/gh-webhook`", flush=True)
        return 3
    lock_fh = open(st.lock, "w")
    deadline = time.time() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                print("PR_WATCH_NOTE another pr-watch runner is active; this one exits", flush=True)
                return 4
            time.sleep(1)
    st.runner_pid.write_text(f"{os.getpid()}\n")
    kill_stale_forwarders(st)
    drop_recorded(st)
    runner = Runner(st)
    signal.signal(signal.SIGTERM, lambda *_: runner.stop.set())
    signal.signal(signal.SIGINT, lambda *_: runner.stop.set())
    reload = threading.Event()
    signal.signal(signal.SIGHUP, lambda *_: reload.set())
    try:
        runner.sync()
        while not runner.stop.wait(1):
            if reload.is_set():
                reload.clear()
                runner.sync()
    finally:
        runner.shutdown()
        try:
            st.runner_pid.unlink()
        except OSError:
            pass
    return 0


# ---- CLI -------------------------------------------------------------------------------------

def notify_runner(st: State) -> None:
    pid = runner_pid(st)
    if pid:
        os.kill(pid, signal.SIGHUP)


def cmd_status(st: State) -> int:
    pid = runner_pid(st)
    print(f"runner: {'pid ' + str(pid) if pid else 'not running'}")
    fwd = read_json(st.forwarders, {})
    for w in load_config(st)["watches"]:
        p = fwd.get(w["repo"])
        alive = isinstance(p, int) and pid_alive(p) and is_forwarder(p)
        print(f"forwarder {w['repo']}: {'pid ' + str(p) if alive else 'not running'}")
    hooks = read_json(st.hooks, {})
    if not hooks:
        print("recorded hook ids: none")
    for repo, ids in sorted(hooks.items()):
        print(f"recorded hook ids {repo}: {' '.join(str(i) for i in ids)}")
    return 0


def cmd_install_mod(st: State, mods_dir: str) -> int:
    st.ensure()
    dest = Path(mods_dir).expanduser().resolve() / MOD_NAME
    if st.root == dest or dest in st.root.parents:
        raise SystemExit("pr-watch: the state dir must live outside the mod folder")
    for src in MOD_SRC.rglob("*"):
        rel = src.relative_to(MOD_SRC)
        prefixes = {"/".join(rel.parts[:k]) for k in range(1, len(rel.parts) + 1)}
        if not src.is_file() or prefixes & set(SKIP_IN_MOD):
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
    runner = st.root / "pr_watch.py"
    shutil.copyfile(Path(__file__).resolve(), runner)
    (dest / "hooks").mkdir(parents=True, exist_ok=True)
    (dest / "hooks" / "paths.ts").write_text(
        "// Written by pr_watch.py install-mod; the state dir lives outside this folder.\n"
        f"export const STATE_DIR = {json.dumps(str(st.root))}\n"
        f"export const RUNNER = {json.dumps(str(runner))}\n")
    print(f"mod written to {dest}")
    print(f"runner copied to {runner}")
    return 0


def cmd_cleanup(st: State, force: bool) -> int:
    if runner_pid(st) and not force:
        print("pr-watch: a runner is active and owns these hooks; stop it or pass --force", file=sys.stderr)
        return 1
    left = drop_recorded(st)
    if left:
        print("could not delete: " + json.dumps(left))
        return 1
    print("recorded hook ids deleted")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pr_watch.py", description=__doc__.split("\n")[0])
    ap.add_argument("--state-dir", help="override <workspace>/state/pr-watch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--repo", required=True)
    a.add_argument("--author", action="append", default=[])
    a.add_argument("--me", action="append", default=[])
    r = sub.add_parser("remove")
    r.add_argument("--repo", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    i = sub.add_parser("install-mod")
    i.add_argument("--mods-dir", required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--force", action="store_true")
    sub.add_parser("run")
    args = ap.parse_args(argv)
    st = State(resolve_state_dir(args.state_dir))

    if args.cmd == "add":
        st.ensure()
        cfg = load_config(st)
        w = config_add(cfg, args.repo, args.author, args.me)
        write_json(st.config, cfg)
        notify_runner(st)
        print(json.dumps(w))
        return 0
    if args.cmd == "remove":
        cfg = load_config(st)
        if not config_remove(cfg, args.repo):
            print(f"pr-watch: {args.repo} is not watched", file=sys.stderr)
            return 1
        write_json(st.config, cfg)
        notify_runner(st)
        print(f"removed {args.repo}")
        return 0
    if args.cmd == "list":
        print(json.dumps(load_config(st)["watches"], indent=2))
        return 0
    if args.cmd == "status":
        return cmd_status(st)
    if args.cmd == "install-mod":
        return cmd_install_mod(st, args.mods_dir)
    if args.cmd == "cleanup":
        return cmd_cleanup(st, args.force)
    return cmd_run(st)


if __name__ == "__main__":
    sys.exit(main())
