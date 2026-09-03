#!/usr/bin/env python3
"""Routing policy seam for the pool: WHO takes a task, separated from HOW it
is assigned (the lead's atomic rename, reclaim and trace stay in pool_lead).

There is no golden routing answer, so the choice is a policy: a named
built-in, an ordered rules file, or owner-supplied code — all behind one
`pick(task, workers, affinity) -> worker | None` call. A policy that raises
or names a worker that is not live is overridden by the default and traced.

Every seat in the pool is a member the policy may pick; the seat that owns
the loop, memory and owner relationship is the HOME member. A single-seat
install is the same policy evaluated over a membership of one (`solo_pick`),
not a second code path — the counting convention is decided elsewhere.

Config: `<state>/pool/routing.json` (or $SUTANDO_POOL_ROUTING). Absent or
malformed means `affinity-first`, the lead's historical behaviour.
Stdlib only; everything injected so tests compose fake pools.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_POLICY = "affinity-first"
HOME_ID = "home"

_HDR_RE = re.compile(
    r"^(?P<key>channel_id|source|access_tier|priority|user_id|room_name"
    r"|target_worker|fan_out):\s*(?P<val>.+?)\s*$", re.M)


@dataclass
class TaskMeta:
    name: str
    channel: "str | None" = None
    lane: str = "owner"
    source: "str | None" = None
    access_tier: str = "owner"
    priority: str = "normal"
    sender: "str | None" = None
    room_name: "str | None" = None
    target: "str | None" = None
    fan_out: bool = False


@dataclass
class MemberView:
    id: str
    load: int = 0
    claiming: bool = True
    runtime: str = "claude"
    is_home: bool = False


@dataclass
class Decision:
    worker: "str | None"
    policy: str
    rule: "int | None" = None
    fallback: bool = False
    reason: "str | None" = None


@dataclass
class RoutingConfig:
    policy: str = DEFAULT_POLICY
    rules: list = field(default_factory=list)
    allow_delegation: bool = False
    source: "str | None" = None
    roots: list = field(default_factory=list)


def read_task_meta(path: Path, lane: str = "owner") -> TaskMeta:
    """Headers end at the `task:` delimiter — a body line can never forge
    routing (same containment rule the lead applies to addressing)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return TaskMeta(name=path.name, lane=lane)
    m = re.search(r"^task:", text, re.M)
    head = text[:m.start()] if m else text
    f = {mm.group("key"): mm.group("val") for mm in _HDR_RE.finditer(head)}
    return TaskMeta(
        name=path.name, channel=f.get("channel_id"), lane=lane,
        source=f.get("source"), access_tier=f.get("access_tier", "owner"),
        priority=f.get("priority", "normal"), sender=f.get("user_id"),
        room_name=f.get("room_name"), target=f.get("target_worker"),
        fan_out=str(f.get("fan_out", "")).lower() in ("1", "true", "yes"))


# ── config ────────────────────────────────────────────────────────────────
def config_path(state_dir: Path) -> Path:
    env = os.environ.get("SUTANDO_POOL_ROUTING")
    return Path(env) if env else Path(state_dir) / "pool" / "routing.json"


def _custom_roots(state_dir: Path, cfg: Path) -> list:
    """Where owner code may live: the repo, the workspace that holds the state
    dir, and the config's own directory. A state file naming /tmp is refused."""
    repo = Path(__file__).resolve().parents[2]
    out = []
    for r in (repo, Path(state_dir).resolve().parent, cfg.resolve().parent):
        if r not in out:
            out.append(r)
    return out


def load_config(state_dir: Path) -> RoutingConfig:
    p = config_path(state_dir)
    roots = _custom_roots(state_dir, p)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return RoutingConfig(roots=roots)
    if not isinstance(data, dict):
        return RoutingConfig(roots=roots)
    rules = data.get("rules")
    return RoutingConfig(
        policy=str(data.get("policy") or DEFAULT_POLICY),
        rules=rules if isinstance(rules, list) else [],
        allow_delegation=bool(data.get("allow_delegation", False)),
        source=str(p), roots=roots)


# ── built-in policies ─────────────────────────────────────────────────────
def _live(workers: list) -> list:
    return [w for w in workers if w.claiming]


def _least_loaded(task, workers, affinity, state):
    live = _live(workers) or list(workers)
    if not live:
        return None
    last = state.setdefault("last_pick", {})
    pick = min(live, key=lambda w: (w.load, last.get(w.id, 0.0), w.id))
    last[pick.id] = state.get("tick", 0) + 1
    state["tick"] = last[pick.id]
    return pick.id


def _round_robin(task, workers, affinity, state):
    live = sorted(_live(workers) or list(workers), key=lambda w: w.id)
    if not live:
        return None
    i = state.get("rr", 0) % len(live)
    state["rr"] = i + 1
    return live[i].id


def _sticky_sender(task, workers, affinity, state):
    """Same sender keeps its worker while that worker is live; a new sender
    lands least-loaded. Stickiness is in-memory — a lead restart resets it."""
    ids = {w.id for w in _live(workers)}
    table = state.setdefault("sticky", {})
    if task.sender and table.get(task.sender) in ids:
        return table[task.sender]
    pick = _least_loaded(task, workers, affinity, state)
    if task.sender and pick:
        table[task.sender] = pick
    return pick


def _home_first(task, workers, affinity, state):
    """Explicit address wins; everything else goes to the home seat, which
    claims and processes like any member. No home seat → least-loaded."""
    ids = {w.id: w for w in workers}
    if task.target and task.target in ids and ids[task.target].claiming:
        return task.target
    core = next((w for w in workers if w.is_home), None)
    if core is not None and core.claiming:
        return core.id
    return _least_loaded(task, workers, affinity, state)


BUILTINS = {
    "least-loaded": _least_loaded,
    "round-robin": _round_robin,
    "sticky-sender": _sticky_sender,
    "home-first": _home_first,
    "core-first": _home_first,  # pre-ruling spelling, kept for one release
}


def _load_custom(spec: str, roots=()):
    """`custom:<path.py>:<attr>` — owner-supplied code behind the same
    signature. Import failure is a config error: caller falls back."""
    _, path, attr = spec.split(":", 2)
    resolved = Path(path).resolve()
    if roots and not any(resolved == r or r in resolved.parents for r in roots):
        raise PermissionError(f"{spec}: custom policy must live under the repo or the workspace")
    mod_spec = importlib.util.spec_from_file_location("sutando_routing_custom", str(resolved))
    if mod_spec is None or mod_spec.loader is None:
        raise ImportError(spec)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    fn = getattr(mod, attr)
    if not callable(fn):
        raise TypeError(f"{spec} is not callable")
    return fn


class Router:
    """Resolves the configured policy once; `pick` never raises — a broken
    policy degrades to `default_fn` (the lead's historical choice)."""

    def __init__(self, config: RoutingConfig, default_fn):
        self.config = config
        self.default_fn = default_fn
        self.state: dict = {}
        self._cache: dict = {}
        self.error: "str | None" = None

    def _policy_fn(self, name: str):
        if name == DEFAULT_POLICY:
            return self.default_fn
        if name in self._cache:
            return self._cache[name]
        if name in BUILTINS:
            fn = BUILTINS[name]
        elif name.startswith("custom:"):
            fn = _load_custom(name, self.config.roots)
        else:
            raise KeyError(name)
        self._cache[name] = fn
        return fn

    @staticmethod
    def _matches(rule: dict, task: TaskMeta, workers: list) -> bool:
        match = rule.get("match") or {}
        if not isinstance(match, dict):
            return False
        for k, v in match.items():
            if k == "runtime":
                if not any(w.runtime == v for w in workers):
                    return False
                continue
            actual = getattr(task, k, None)
            want = v if isinstance(v, list) else [v]
            if actual not in want:
                return False
        return True

    def _run(self, name: str, task, workers, affinity) -> "str | None":
        got = self._policy_fn(name)(task, workers, affinity, self.state)
        return str(got) if got is not None else None

    def pick(self, task: TaskMeta, workers: list, affinity: dict) -> Decision:
        # A member that is not claiming is not a valid answer: assigning to it
        # parks the task until the stuck-reclaim path repools it.
        live_ids = {w.id for w in workers if w.claiming}
        try:
            for i, rule in enumerate(self.config.rules):
                if not isinstance(rule, dict) or not self._matches(rule, task, workers):
                    continue
                cand = list(workers)
                if rule.get("to"):
                    to = rule["to"] if isinstance(rule["to"], list) else [rule["to"]]
                    cand = [w for w in cand if w.id in to]
                if rule.get("only"):
                    cand = [w for w in cand if w.id in rule["only"]]
                if rule.get("exclude"):
                    cand = [w for w in cand if w.id not in rule["exclude"]]
                name = str(rule.get("policy") or "least-loaded")
                if not cand:
                    return Decision(None, name, i, reason="rule narrowed to no live worker")
                got = self._run(name, task, cand, affinity)
                if got is not None and got not in {w.id for w in cand if w.claiming}:
                    raise ValueError(f"{name} returned {got!r}, not a claiming candidate")
                return Decision(got, name, i)
            name = self.config.policy
            got = self._run(name, task, workers, affinity)
            if got is not None and got not in live_ids:
                raise ValueError(f"{name} returned {got!r}, not live")
            return Decision(got, name)
        except Exception as exc:  # any policy fault → default, traced
            self.error = f"{type(exc).__name__}: {exc}"
            got = self.default_fn(task, workers, affinity, self.state)
            return Decision(got, DEFAULT_POLICY, fallback=True, reason=self.error)


def solo_pick(router: Router, task: TaskMeta, self_id: str,
              is_home: bool = True) -> bool:
    """Single-member evaluation: True when the configured policy lets
    `self_id` take `task`. A lone seat is a membership of one."""
    me = MemberView(id=self_id, is_home=is_home)
    return router.pick(task, [me], {}).worker == self_id


def build_router(state_dir: Path, default_fn=None) -> Router:
    fallback = default_fn or _least_loaded
    return Router(load_config(state_dir), fallback)
