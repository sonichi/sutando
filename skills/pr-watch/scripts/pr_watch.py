#!/usr/bin/env python3
"""pr-watch: event-driven GitHub PR watching through `gh webhook forward`.

Subcommands:
  add --repo O/R [--author LOGIN ...] [--me LOGIN ...] [--reviewed]
                                                         watch a repo (merges into an existing entry)
  remove --repo O/R                                      stop watching a repo
  list                                                   print the watch config
  status                                                 runner, forwarders and recorded hook ids
  install-mod --mods-dir DIR                             write the Claude Code mod into DIR/pr-watch
  cleanup [--force]                                      delete the hook ids this skill recorded
  config get [KEY] | config set KEY VALUE                read or set a setting (the config.json layer)
  track --pr O/R#N [--author A] [--state S] [--head SHA8] [--verdict V] [--event TEXT] [--link URL]
                                                         record a PR's outcome; publish the tracker page
  run                                                    the forwarder supervisor the mod spawns

`run` prints `PR_WATCH_PROMPT_TEMPLATE <json>` on start and on SIGHUP, one `PR_EVENT ...` line per
relevant event, and `PR_WATCH_NOTE ...` lines for problems the session should see. State lives in
<workspace>/state/pr-watch/, never in the mod folder. Settings: manifest.json `config`, read as
CLI > env > manifest > config.json. Standard library only, plus `gh` with cli/gh-webhook.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import html
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
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

SKILL_DIR = Path(__file__).resolve().parents[1]  # lint-workspace-resolution: allow-repo-root
REPO_ROOT = SKILL_DIR.parents[1]
MOD_SRC = SKILL_DIR / "mod"
MOD_NAME = "pr-watch"


def _is_state_copy(here: Path) -> bool:
    return here.name == MOD_NAME and here.parent.name == "state"


# install-mod copies the manifest beside the runner copy, which runs without a checkout around it.
MANIFEST = (Path(__file__).resolve().parent / "manifest.json" if _is_state_copy(Path(__file__).resolve().parent)
            else SKILL_DIR / "manifest.json")
SETTINGS = ("PR_WATCH_PROMPT", "PR_WATCH_TRACKER_ROOM", "PR_WATCH_TRACKER_TITLE", "PR_WATCH_COMMONS_SCRIPT")
TEMPLATE_MARK = "PR_WATCH_PROMPT_TEMPLATE "
NOTE_MARK = "PR_WATCH_NOTE "
COMMONS_TIMEOUT_S = 60
TRACK_STATES = ("open", "merged", "closed")
TRACK_VERDICTS = ("approved", "changes_requested", "commented", "none")
PAGE_KIND_RE = re.compile(r"^html-[A-Za-z0-9_.-]+$")

EVENTS = "pull_request,pull_request_review"
PR_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review", "closed")
REVIEWED_ACTIONS = ("synchronize", "closed", "reopened", "ready_for_review")
REVIEWED_TTL_S = 600
REVIEWED_TIMEOUT_S = 10
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

TRACK_HINT = (
    " Then record the outcome: the pr-watch skill's `pr_watch.py track --pr <repo>#<n>` with --state, "
    "--head, --verdict and --event."
)


# ---- state paths ---------------------------------------------------------------------------

def resolve_state_dir(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path(__file__).resolve().parent
    # The copy install-mod places in the state dir runs without a checkout around it.
    if _is_state_copy(here):
        return here
    sys.path.insert(0, str(REPO_ROOT / "src"))
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
        self.tracker = root / "tracker.json"
        self.backoff = root / "backoff.json"

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
    return cfg


# ---- settings --------------------------------------------------------------------------------

def manifest_config() -> dict:
    try:
        cfg = json.loads(MANIFEST.read_text()).get("config") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def setting_source(name: str, cli: Optional[str] = None, st: Optional[State] = None) -> Tuple[str, str]:
    """(value, layer) for a setting, most specific first: CLI > env > manifest > config.json.

    An empty value at any layer counts as unset, so the manifest's empty default falls through."""
    if name not in SETTINGS:
        raise SystemExit(f"pr-watch: unknown setting {name!r}; one of {', '.join(SETTINGS)}")
    if cli:
        return cli, "cli"
    if os.environ.get(name):
        return os.environ[name], "env"
    declared = manifest_config().get(name)
    if isinstance(declared, str) and declared:
        return declared, "manifest"
    if st is not None:
        stored = (read_json(st.config, {}).get("settings") or {}).get(name)
        if isinstance(stored, str) and stored:
            return stored, "config.json"
    return "", "unset"


def setting(name: str, cli: Optional[str] = None, st: Optional[State] = None) -> str:
    return setting_source(name, cli, st)[0]


def prompt_template(st: State) -> str:
    """The event prompt the mod fills `{event}` into; `{track}` is the tracker hint, when a room is set."""
    t = setting("PR_WATCH_PROMPT", st=st)
    return t.replace("{track}", TRACK_HINT if setting("PR_WATCH_TRACKER_ROOM", st=st) else "")


def commons_script(st: State, cli: Optional[str] = None) -> Path:
    p = Path(setting("PR_WATCH_COMMONS_SCRIPT", cli, st)).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


def cmd_config(st: State, action: str, key: Optional[str], value: Optional[str]) -> int:
    if action == "get":
        if key:
            print(setting(key, st=st))
            return 0
        print(json.dumps({k: dict(zip(("value", "from"), setting_source(k, st=st))) for k in SETTINGS}, indent=2))
        return 0
    if not key or value is None:
        raise SystemExit("pr-watch: config set KEY VALUE")
    setting_source(key)
    st.ensure()
    cfg = load_config(st)
    settings = cfg.setdefault("settings", {})
    if value:
        settings[key] = value
    else:
        settings.pop(key, None)
    write_json(st.config, cfg)
    notify_runner(st)
    now, layer = setting_source(key, st=st)
    if layer in ("env", "manifest") and now != value:
        print(f"pr-watch: {key} saved, but the {layer} value still wins over config.json")
    else:
        print(f"{key} = {json.dumps(value)}")
    return 0


def _logins(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for v in values:
        if v != "*" and not LOGIN_RE.match(v):
            raise SystemExit(f"pr-watch: not a GitHub login: {v!r}")
        if v.lower() not in [o.lower() for o in out]:
            out.append(v)
    return out


def config_add(cfg: dict, repo: str, authors: List[str], me: List[str], reviewed: bool = False) -> dict:
    if not REPO_RE.match(repo):
        raise SystemExit(f"pr-watch: --repo must be OWNER/REPO, got {repo!r}")
    if not authors and not me and not reviewed:
        raise SystemExit("pr-watch: give at least one --author or --me")
    w = next((x for x in cfg["watches"] if x["repo"].lower() == repo.lower()), None)
    merged = {"repo": w["repo"] if w else repo,
              "authors": _logins(list((w or {}).get("authors", [])) + authors),
              "me": _logins(list((w or {}).get("me", [])) + me)}
    if reviewed or (w or {}).get("reviewed"):
        if not _mine(merged["me"]):
            raise SystemExit("pr-watch: --reviewed needs a --me login (not *)")
        merged["reviewed"] = True
    if w is None:
        cfg["watches"].append(merged)
    else:
        w.clear()
        w.update(merged)
    return merged


def config_remove(cfg: dict, repo: str) -> bool:
    before = len(cfg["watches"])
    cfg["watches"] = [w for w in cfg["watches"] if w["repo"].lower() != repo.lower()]
    return len(cfg["watches"]) != before


# ---- event filter ----------------------------------------------------------------------------

def _has(logins: Iterable[str], login: str) -> bool:
    low = login.lower()
    return any(x == "*" or x.lower() == low for x in logins)


def _mine(logins: Iterable[str]) -> List[str]:
    return [m for m in logins if m != "*"]


def _safe(value) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-\[\]/#]", "?", str(value))[:80]


# (repo, PR number, the watch's `me` logins) -> whether one of them reviewed it; None when unknown.
ReviewedFn = Callable[[str, int, List[str]], Optional[bool]]


def classify(payload: dict, watches: List[dict], reviewed: Optional[ReviewedFn] = None) -> Optional[str]:
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
    me = _mine(watch.get("me", []))
    review = payload.get("review")
    if isinstance(review, dict):
        by = (review.get("user") or {}).get("login") or ""
        if action == "dismissed":
            if not by or not _has(me, by):
                return None
            return (f"PR_EVENT {where} review_dismissed reviewer={_safe(by)} "
                    f"author={_safe(author)} head={_safe(head)}")
        # An owner's own review echoing back: observe() still marks the PR reviewed.
        if action != "submitted" or not _has(watch.get("authors", []), author) or _has(me, by):
            return None
        state = (review.get("state") or "").lower()
        commit = (review.get("commit_id") or "")[:8]
        return (f"PR_EVENT {where} review_submitted by={_safe(by)} state={_safe(state)} "
                f"author={_safe(author)} head={_safe(commit or head)}")
    if action == "review_requested":
        reviewer = (payload.get("requested_reviewer") or {}).get("login") or ""
        if not reviewer or not _has(me, reviewer):
            return None
        return (f"PR_EVENT {where} review_requested reviewer={_safe(reviewer)} "
                f"author={_safe(author)} head={_safe(head)}")
    if action not in PR_ACTIONS:
        return None
    base = (f"PR_EVENT {where} {action} author={_safe(author)} head={_safe(head)} "
            f"merged={'true' if pr.get('merged') else 'false'}")
    # The author rule wins without a lookup, so a PR matching both yields one line.
    if _has(watch.get("authors", []), author):
        return base + " reason=author"
    if not watch.get("reviewed") or action not in REVIEWED_ACTIONS or not me:
        return None
    try:
        seen = reviewed(repo, int(number), me) if reviewed else None
    except (TypeError, ValueError):
        seen = None
    if seen is False:
        return None
    return base + " reason=reviewed" + ("" if seen else " reviewed=unknown")


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

def gh(args: List[str], check: bool = False, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + args, capture_output=True, text=True, check=check, timeout=timeout)


class ReviewedLookup:
    """Whether a `me` login reviewed a PR: one reviews API call per (repo, PR), cached.

    A yes is kept for the runner's lifetime; a no is re-asked after `ttl` seconds."""

    def __init__(self, ttl: float = REVIEWED_TTL_S, timeout: float = REVIEWED_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl, self.timeout, self.clock = ttl, timeout, clock
        self.cache: Dict[Tuple[str, int, FrozenSet[str]], Tuple[bool, float]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _key(repo: str, number: int, me: Iterable[str]) -> Tuple[str, int, FrozenSet[str]]:
        return repo.lower(), int(number), frozenset(m.lower() for m in me)

    def __call__(self, repo: str, number: int, me: List[str]) -> Optional[bool]:
        key = self._key(repo, number, me)
        now = self.clock()
        with self.lock:
            hit = self.cache.get(key)
        if hit and (hit[0] or now - hit[1] < self.ttl):
            return hit[0]
        found = self.fetch(repo, number, me)
        if found is not None:
            with self.lock:
                self.cache[key] = (found, now)
        return found

    def fetch(self, repo: str, number: int, me: List[str]) -> Optional[bool]:
        try:
            r = gh(["api", f"repos/{repo}/pulls/{number}/reviews", "--paginate",
                    "--jq", ".[].user.login"], timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return any(_has(me, login) for login in r.stdout.split())

    def observe(self, payload: dict, watches: List[dict]) -> None:
        """A review submitted by a `me` login marks its PR reviewed without asking GitHub."""
        review = payload.get("review")
        pr = payload.get("pull_request")
        repo = (payload.get("repository") or {}).get("full_name") or ""
        if payload.get("action") != "submitted" or not isinstance(review, dict) or not isinstance(pr, dict):
            return
        by = (review.get("user") or {}).get("login") or ""
        for w in watches:
            me = _mine(w.get("me", []))
            if w["repo"].lower() == repo.lower() and me and _has(me, by):
                try:
                    key = self._key(repo, pr.get("number"), me)
                except (TypeError, ValueError):
                    return
                with self.lock:
                    self.cache[key] = (True, self.clock())


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
        self.reviewed = ReviewedLookup()
        self.wake_cond = threading.Condition()
        self.wake_gen = 0
        self.backoff_lock = threading.Lock()

    def emit(self, line: str) -> None:
        with self.out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def note(self, text: str) -> None:
        self.emit(NOTE_MARK + text.replace("\n", " "))

    def emit_template(self) -> None:
        self.emit(TEMPLATE_MARK + json.dumps(prompt_template(self.st)))

    def _save_forwarders(self) -> None:
        with self.procs_lock:
            write_json(self.st.forwarders, {r: p.pid for r, p in self.procs.items() if p.poll() is None})

    def watches(self) -> List[dict]:
        return load_config(self.st)["watches"]

    def wake(self) -> None:
        """Cut every backed-off loop's wait short, so a reload retries at once."""
        with self.wake_cond:
            self.wake_gen += 1
            self.wake_cond.notify_all()

    def pause(self, delay: float, gone: threading.Event, seen: int) -> bool:
        """Wait `delay`, or until stopped, the repo is dropped or a wake after `seen`; True to end the loop."""
        with self.wake_cond:
            self.wake_cond.wait_for(
                lambda: self.stop.is_set() or gone.is_set() or self.wake_gen != seen, timeout=delay)
        return self.stop.is_set() or gone.is_set()

    def set_backoff(self, repo: str, reason: Optional[str], delay: float = 0) -> None:
        with self.backoff_lock:
            data = read_json(self.st.backoff, {})
            if reason is None:
                data.pop(repo, None)
            else:
                data[repo] = {"reason": reason, "retry_at": time.time() + delay}
            write_json(self.st.backoff, data)

    def back_off(self, repo: str, reason: str, note: str) -> int:
        self.note(note)
        self.set_backoff(repo, reason, CONFLICT_DELAY_S)
        return CONFLICT_DELAY_S

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
            seen = self.wake_gen
            delay = self.forward_once(repo)
            if self.pause(delay, gone, seen):
                break
        self.set_backoff(repo, None)
        drop_recorded(self.st, repo)

    def forward_once(self, repo: str) -> int:
        drop_recorded(self.st, repo)
        before = forwarder_hook_ids(repo)
        if before is None:
            return self.back_off(repo, "cannot list hooks (need admin on the repo)",
                                 f"{repo}: cannot list hooks (need admin on the repo); retrying")
        if before:
            return self.back_off(repo, f"foreign forwarder hook {before[0]}",
                                 f"{repo}: another webhook forwarder already holds this repo "
                                 f"(hook {before[0]}); GitHub allows one, retrying later")
        self.set_backoff(repo, None)
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
                watches = self.watches()
                self.reviewed.observe(payload, watches)
                line = classify(payload, watches, self.reviewed)
                if line:
                    self.emit(line)
        proc.wait()
        with self.procs_lock:
            self.procs.pop(repo, None)
        self._save_forwarders()
        drop_recorded(self.st, repo)
        if conflict.is_set():
            return self.back_off(repo, f"GitHub refused a second forwarder ({HOOK_EXISTS})",
                                 f"{repo}: GitHub refused a second forwarder ({HOOK_EXISTS}); retrying later")
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
        self.wake()
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
        write_json(self.st.backoff, {})


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
        runner.emit_template()
        runner.sync()
        while not runner.stop.wait(1):
            if reload.is_set():
                reload.clear()
                runner.emit_template()
                runner.sync()
                runner.wake()
    finally:
        runner.shutdown()
        try:
            st.runner_pid.unlink()
        except OSError:
            pass
    return 0


# ---- tracker ---------------------------------------------------------------------------------

PR_REF_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")
COLUMNS = ("PR", "Author", "State", "Head reviewed", "Verdict", "Last event", "Updated")

TRACKER_CSS = """
:root { --bg: #ffffff; --fg: #1f2328; --muted: #59636e; --line: #d1d9e0; --head: #f6f8fa;
  --link: #0969da; --open: #1a7f37; --merged: #8250df; --closed: #cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #3d444d; --head: #151b23;
    --link: #4493f8; --open: #3fb950; --merged: #ab7df8; --closed: #f85149; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
.note { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
.wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; min-width: 640px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: var(--head); font-weight: 600; white-space: nowrap; }
a { color: var(--link); text-decoration: none; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
.s-open { color: var(--open); } .s-merged { color: var(--merged); } .s-closed { color: var(--closed); }
.empty { color: var(--muted); }
"""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url, label: str) -> str:
    """An anchor for an http(s) url; anything else is the escaped label alone."""
    try:
        ok = isinstance(url, str) and urlparse(url).scheme in ("http", "https") and bool(urlparse(url).netloc)
    except ValueError:
        ok = False
    return f'<a href="{_esc(url)}" rel="noopener noreferrer">{_esc(label)}</a>' if ok else _esc(label)


def tracker_order(rows: Iterable[dict]) -> List[dict]:
    """Open PRs first, then most recently updated first."""
    by_time = sorted(rows, key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return sorted(by_time, key=lambda r: r.get("state") != "open")


def render_tracker(rows: Iterable[dict], title: str, now: str) -> str:
    """A self-contained HTML page of the tracked PRs; every value is escaped."""
    body = []
    for r in tracker_order(rows):
        label = f"{r.get('repo', '')}#{r.get('number', '')}"
        state = str(r.get("state") or "")
        cls = f' class="s-{state}"' if state in TRACK_STATES else ""
        cells = [_link(r.get("link"), label), _esc(r.get("author")), f"<span{cls}>{_esc(state)}</span>",
                 f"<code>{_esc(r.get('head'))}</code>", _esc(r.get("verdict")), _esc(r.get("event")),
                 _esc(r.get("updated_at"))]
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    if not body:
        body.append(f'<tr><td class="empty" colspan="{len(COLUMNS)}">No pull requests tracked yet.</td></tr>')
    head = "".join(f"<th>{_esc(c)}</th>" for c in COLUMNS)
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<meta name=\"color-scheme\" content=\"light dark\">\n"
            f"<title>{_esc(title)}</title>\n<style>{TRACKER_CSS}</style>\n</head>\n<body>\n"
            f"<h1>{_esc(title)}</h1>\n"
            f"<p class=\"note\">Generated by pr-watch; edits here are overwritten. Updated {_esc(now)}.</p>\n"
            f"<div class=\"wrap\"><table>\n<thead><tr>{head}</tr></thead>\n<tbody>\n"
            + "\n".join(body) + "\n</tbody>\n</table></div>\n</body>\n</html>\n")


class PublishError(Exception):
    pass


def _verb(args: List[str]) -> str:
    return next((a for a in args if a in ("pages", "page-add", "read", "replace")), "room_commons")


def run_commons(script: Path, args: List[str]) -> str:
    try:
        r = subprocess.run([sys.executable, str(script)] + args, capture_output=True, text=True,
                           timeout=COMMONS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise PublishError(f"{_verb(args)} timed out after {COMMONS_TIMEOUT_S}s")
    except OSError as exc:
        raise PublishError(f"{_verb(args)}: {exc}")
    if r.returncode != 0:
        why = (r.stderr or r.stdout or "").strip().splitlines()
        raise PublishError(f"{_verb(args)} exited {r.returncode}: {why[-1] if why else 'no output'}")
    return r.stdout


def _commons_json(script: Path, args: List[str]):
    out = run_commons(script, args)
    try:
        return json.loads(out)
    except ValueError:
        raise PublishError(f"{_verb(args)} printed no JSON")


def publish_tracker(st: State, data: dict, room: str, title: str, script: Path) -> str:
    """Write the whole tracker page into the room, creating it when it is missing."""
    page = render_tracker(data.get("rows", {}).values(), title, _now())
    kind = data.get("page_kind") if data.get("page_room") == room else None
    if kind:
        listed = _commons_json(script, ["--json", "pages", room])
        if not isinstance(listed, list):
            raise PublishError("pages printed no list")
        if kind not in {p.get("kind") for p in listed if isinstance(p, dict)}:
            kind = None
    if not kind:
        made = _commons_json(script, ["--json", "page-add", "--kind", "html", room, title])
        kind = made.get("kind") if isinstance(made, dict) else None
        if not isinstance(kind, str) or not PAGE_KIND_RE.match(kind):
            raise PublishError(f"page-add returned no html page kind: {str(made)[:80]}")
        data["page_kind"], data["page_room"] = kind, room
        write_json(st.tracker, data)
    got = _commons_json(script, ["--json", "--kind", kind, "read", room])
    current = got.get("text") if isinstance(got, dict) else None
    if not isinstance(current, str):
        raise PublishError("read returned no page text")
    if current != page:
        run_commons(script, ["--kind", kind, "replace", "--", room, current, page])
    return kind


def cmd_track(st: State, args: argparse.Namespace) -> int:
    m = PR_REF_RE.match(args.pr or "")
    if not m:
        raise SystemExit(f"pr-watch: --pr must be OWNER/REPO#N, got {args.pr!r}")
    repo, number = m.group(1), int(m.group(2))
    if args.author and not LOGIN_RE.match(args.author):
        raise SystemExit(f"pr-watch: not a GitHub login: {args.author!r}")
    st.ensure()
    data = read_json(st.tracker, {})
    if not isinstance(data, dict):
        raise SystemExit(f"pr-watch: {st.tracker} is not a JSON object")
    rows = data.setdefault("rows", {})
    key = f"{repo}#{number}"
    row = rows.get(key) or {"repo": repo, "number": number, "link": f"https://github.com/{repo}/pull/{number}"}
    for field in ("author", "state", "head", "verdict", "event", "link"):
        value = getattr(args, field)
        if value is not None:
            row[field] = value
    row["updated_at"] = _now()
    rows[key] = row
    write_json(st.tracker, data)
    room = setting("PR_WATCH_TRACKER_ROOM", args.room, st)
    if not room:
        print(f"recorded {key}; no tracker room set (PR_WATCH_TRACKER_ROOM), so nothing was published")
        return 0
    script = commons_script(st)
    if not script.is_file():
        print(f"{NOTE_MARK}tracker publish skipped: no room-commons script at {script} (PR_WATCH_COMMONS_SCRIPT)")
        return 0
    title = setting("PR_WATCH_TRACKER_TITLE", args.title, st) or "PR watch"
    try:
        kind = publish_tracker(st, data, room, title, script)
    except PublishError as exc:
        print(f"{NOTE_MARK}tracker publish failed: {str(exc)[:200]}; recorded {key}, the next track republishes")
        return 0
    print(f"recorded {key}; tracker published to {room} --kind {kind} ({len(rows)} PRs)")
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
    backoff = read_json(st.backoff, {}) if pid else {}
    for w in load_config(st)["watches"]:
        p = fwd.get(w["repo"])
        alive = isinstance(p, int) and pid_alive(p) and is_forwarder(p)
        b = backoff.get(w["repo"])
        if alive:
            why = f"pid {p}"
        elif isinstance(b, dict) and isinstance(b.get("retry_at"), (int, float)):
            at = time.strftime("%H:%M", time.localtime(b["retry_at"]))
            why = f"backing off: {b.get('reason')}, retry at {at} (or on the next add, remove or config set)"
        else:
            why = "not running"
        print(f"forwarder {w['repo']}: {why}")
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
    shutil.copyfile(MANIFEST, st.root / "manifest.json")
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
    a.add_argument("--reviewed", action="store_true",
                   help="also forward PRs any --me login has reviewed, whoever wrote them")
    r = sub.add_parser("remove")
    r.add_argument("--repo", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    i = sub.add_parser("install-mod")
    i.add_argument("--mods-dir", required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--force", action="store_true")
    cf = sub.add_parser("config")
    cf.add_argument("action", choices=("get", "set"))
    cf.add_argument("key", nargs="?", choices=SETTINGS)
    cf.add_argument("value", nargs="?")
    t = sub.add_parser("track")
    t.add_argument("--pr", required=True, help="OWNER/REPO#N")
    t.add_argument("--author")
    t.add_argument("--state", choices=TRACK_STATES)
    t.add_argument("--head", help="the short sha that was reviewed")
    t.add_argument("--verdict", choices=TRACK_VERDICTS)
    t.add_argument("--event", help="what happened, in a few words")
    t.add_argument("--link")
    t.add_argument("--room", help="override PR_WATCH_TRACKER_ROOM")
    t.add_argument("--title", help="override PR_WATCH_TRACKER_TITLE")
    sub.add_parser("run")
    args = ap.parse_args(argv)
    st = State(resolve_state_dir(args.state_dir))

    if args.cmd == "add":
        st.ensure()
        cfg = load_config(st)
        w = config_add(cfg, args.repo, args.author, args.me, args.reviewed)
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
    if args.cmd == "config":
        return cmd_config(st, args.action, args.key, args.value)
    if args.cmd == "track":
        return cmd_track(st, args)
    return cmd_run(st)


if __name__ == "__main__":
    sys.exit(main())
