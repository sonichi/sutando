"""The managed launcher of the tmux-hosted session: `run`.

The session is one immutable generation {server pid+start, session id, session created, pane id,
pane pid, start command, nonce, title token} recorded at creation; names alone are never trusted.
Every start classifies the recorded generation: adopt when the pane's claude verifies by pid and
start and the plugin witness carries the configured session id; create only over a free root with
no verified orphan, a clear pane-key guard and a dead or absent recorded generation; unknown is the
live waiter (the 90 s deadline and the card id persist in the supervisor record across restarts);
only foreign terminal contention and a root whose layout it may never serve are the logical 75.
The stop path is `/exit` through the in-command fence, a bounded grace, `kill-session` through the
same fence, then the server's own exit. Stdlib.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ... import connect_common as cm, task_io, tmux_pane_keys, tmux_probe
from ... import claude_local_accepter as accepter, claude_local_card as card, claude_local_records as records
from . import detector, hold, inbox, trust
from .shape import (AUTOARM_VAR, HITL_ID_PREFIX, LAUNCHER_LOCK, LOCAL, MIN_TMUX, SESSION_NAME, SOCKET_NAME,
                    SOCKET_PATH_MAX, SUPERVISOR_LOCK)

EXIT_RETRY = 74            # retryable: the manager simply starts the launcher again
TICK_S = 2.0
PROBE_TIMEOUT_S = 10
UNKNOWN_WAIT_S = 90.0      # three throttles
CLAUDE_GRACE_S = 30.0
SERVER_EXIT_S = 20.0
CONSUMER_WAIT_S = 30.0
INNER_STOP_S = 120.0
TITLE_CHANGED = "title changed"
FIELD_SEP = "|:ag2:|"
START_COMMAND_RE = re.compile(r'^"?ag2-sparrow-claude-local hold --generation=[0-9a-f]{32} --"?$')
TMUX_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
SESSION_ENV_KEYS = ("CLAUDE_CONFIG_DIR", "HMAC_KEY_FILE", "HMAC_STRICT")


def socket_path(config_dir) -> Path:
    return Path(config_dir) / SOCKET_NAME


def socket_problem(config_dir) -> str:
    path = str(socket_path(config_dir))
    if len(path.encode("utf-8")) > SOCKET_PATH_MAX:
        return "the tmux socket path %s is %d bytes, over the %d the platform allows; use a shorter config dir" % (
            path, len(path.encode("utf-8")), SOCKET_PATH_MAX)
    return ""


def tmux_version(tmux="tmux", run=subprocess.run) -> "tuple[int, int] | None":
    try:
        r = run([tmux, "-u", "-V"], capture_output=True, text=True, timeout=cm.RUN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    m = TMUX_VERSION_RE.search((r.stdout or "") + (r.stderr or "")) if r.returncode == 0 else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def tmux_problem(tmux="tmux", run=subprocess.run) -> str:
    found = tmux_version(tmux, run)
    if found is None:
        return "tmux -V did not answer; install tmux %d.%d or newer" % MIN_TMUX
    if found < MIN_TMUX:
        return "tmux %d.%d is older than the %d.%d this adapter needs (new-session -e)" % (found + MIN_TMUX)
    return ""


class Tmux:
    """The adapter's own server on its own socket; every probe bounded, every outcome explicit."""

    def __init__(self, sock, tmux=None, run=subprocess.run):
        self.sock, self.tmux, self._run = str(sock), tmux or shutil.which("tmux") or "tmux", run

    def run(self, *args, timeout=PROBE_TIMEOUT_S, env=None):
        try:
            return self._run([self.tmux, "-u", "-S", self.sock, *args], capture_output=True, text=True, timeout=timeout,
                             env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None

    def has_session(self, name=SESSION_NAME):
        return tmux_probe.has_session(self.sock, name, timeout=PROBE_TIMEOUT_S, tmux=self.tmux)

    def pane_fields(self, pane_id) -> "dict | None":
        # A printable separator no locale escapes; the title, the one free-text field, comes last.
        r = self.run("display-message", "-p", "-t", pane_id, FIELD_SEP.join(
            ("#{pane_id}", "#{pane_pid}", "#{session_id}", "#{session_created}", "#{pid}", "#{pane_start_command}",
             "#{pane_title}")))
        if r is None or r.returncode != 0:
            return None
        parts = (r.stdout or "").rstrip("\n").split(FIELD_SEP, 6)
        if len(parts) != 7:
            return None
        try:
            return {"pane_id": parts[0], "pane_pid": int(parts[1]), "session_id": parts[2],
                    "session_created": int(parts[3]), "server_pid": int(parts[4]), "start_command": parts[5],
                    "title": parts[6]}
        except ValueError:
            return None

    def capture(self, pane_id) -> "str | None":
        r = self.run("capture-pane", "-p", "-t", pane_id, "-S", "-50")
        return r.stdout if r is not None and r.returncode == 0 else None


def server_env(cfg: dict, environ=None) -> dict:
    """What the clean server starts with under env -i: PATH (the console script's dir first), HOME,
    TERM, USER and the allowlist; never the host's SUTANDO_* or any credential."""
    base = os.environ if environ is None else environ
    env = {k: base[k] for k in LOCAL.env_keep if base.get(k)}
    script = cm.console_script(LOCAL)
    env["PATH"] = ":".join([p for p in ([os.path.dirname(script)] if script else []) + (env.get("PATH") or "/usr/bin:/bin").split(":") if p])
    env.setdefault("TERM", "xterm-256color")
    # The server renders the pane in its own locale: without UTF-8, Claude's UI draws as '_'.
    env["LC_CTYPE"] = "en_US.UTF-8"
    return env


def session_env(cfg: dict, config_dir, root) -> dict:
    """The session environment set at creation with -e: what hold and the Mod read."""
    env = {hold.ENV_ROOT: str(root), hold.ENV_CONFIG_DIR: str(config_dir), hold.ENV_SESSION_ID: cfg["SESSION_ID"],
           hold.ENV_HITL_STORE: cfg.get("HITL_STORE", ""), AUTOARM_VAR: "1"}
    for key, var in (("CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR"), ("HMAC_KEY_FILE", "AG2_HMAC_KEY_FILE"),
                     ("HMAC_STRICT", "AG2_HMAC_STRICT")):
        if cfg.get(key):
            env[var] = cfg[key]
    return env


def create_session(tm: Tmux, cfg: dict, config_dir, root, nonce: str, environ=None, run=subprocess.run) -> "tuple[dict | None, str]":
    """new-session under env -i, -f /dev/null, the session environment in one step, the exact pane
    command; then update-environment cleared and the start command read back against the fixed
    alphabet. (generation, '') or (None, why)."""
    command = hold.pane_command(LOCAL.command, nonce)
    argv = ["env", "-i", *["%s=%s" % kv for kv in server_env(cfg, environ).items()], tm.tmux, "-u", "-S", tm.sock,
            "-f", "/dev/null", "new-session", "-d", "-s", SESSION_NAME, "-x", "200", "-y", "50", "-c", str(root)]
    for k, v in session_env(cfg, config_dir, root).items():
        argv += ["-e", "%s=%s" % (k, v)]
    argv += ["--", command]
    try:
        r = run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "tmux new-session did not run (%s)" % e
    if r.returncode != 0:
        return None, "tmux new-session failed: %s" % cm._why(r)
    tm.run("set-option", "-g", "update-environment", "")
    fields = tm.pane_fields("=%s:0.0" % SESSION_NAME)
    if fields is None:
        return None, "the new session's pane could not be read back"
    if not START_COMMAND_RE.match(fields["start_command"]) or nonce not in fields["start_command"]:
        tm.run("kill-session", "-t", "=%s" % SESSION_NAME)
        return None, "the pane's start command is not the fixed command (%r); session refused" % fields["start_command"]
    start = task_io.start_time(fields["server_pid"])
    if not start:
        return None, "the server's start time is unreadable"
    return {"server_pid": fields["server_pid"], "server_start": start, "session_id": fields["session_id"],
            "session_created": fields["session_created"], "pane_id": fields["pane_id"], "pane_pid": fields["pane_pid"],
            "start_command": fields["start_command"], "nonce": nonce}, ""


def await_consumer(tm: Tmux, root, pane_id, wait_s=CONSUMER_WAIT_S, sleep=time.sleep) -> "tuple[dict | None, str | None]":
    """The holder record's live consumer and the pane title hold set, within the bound."""
    deadline = time.monotonic() + wait_s
    while True:
        consumer = task_io.holder_consumer(root)
        if consumer and consumer.get("live") is True:
            fields = tm.pane_fields(pane_id)
            title = fields["title"] if fields else ""
            if hold.TITLE_RE.match(title):
                return {"pid": consumer["pid"], "start": consumer["start"]}, title
        if time.monotonic() >= deadline:
            return None, None
        sleep(0.2)


def tuple_matches(fields: dict, gen: dict) -> bool:
    return all(fields.get(k) == gen.get(k) for k in ("pane_id", "pane_pid", "session_id", "session_created",
                                                       "server_pid", "start_command"))


def classify(tm: Tmux, rec: dict, cfg: dict, root, created=None) -> "tuple[str, str]":
    """adopt | create | unknown | foreign, with the check that decided it. `created` is the nonce of
    the generation this very launcher made: its witness is waived until the Mod writes one."""
    gen, present = rec.get("generation"), tm.has_session()
    occupancy = task_io.occupancy(root)
    if occupancy == "held":
        holder = task_io.lock_holder(root)
        if holder.get("adapter") != LOCAL.name or holder.get("install_id") != task_io.install_id():
            return "foreign", "%s is held by %s (install %s)" % (root, holder.get("adapter") or "an unreadable record",
                                                                  holder.get("install_id") or "?")
    if present is None:
        return "unknown", "tmux did not answer on %s" % tm.sock
    if gen is None:
        if present:
            return "unknown", "a session answers %s that this launcher did not create and record" % tm.sock
        return ("create", "") if occupancy == "free" else ("unknown", "root %s without a recorded generation" % occupancy)
    if not present:
        if occupancy == "free":
            return "create", "the recorded generation is gone"
        return "unknown", "the recorded session is gone but the root reads %s" % occupancy
    fields = tm.pane_fields(gen["pane_id"])
    if fields is None or not tuple_matches(fields, gen):
        return "unknown", "the session answers but the recorded tuple does not match it"
    if task_io.verify_lineage(gen["server_pid"], gen["server_start"]) is not True:
        return "unknown", "the recorded server does not verify by pid and start"
    consumer = task_io.holder_consumer(root)
    if not consumer or consumer.get("live") is not True or (rec.get("consumer") and
                                                           {"pid": consumer["pid"], "start": consumer["start"]} != rec["consumer"]):
        return "unknown", "the pane's claude does not verify by pid and start"
    if rec.get("title_token") and fields["title"] != rec["title_token"]:
        return "unknown", "%s: the pane title %r is not the recorded token; fenced actions are withheld" % (
            TITLE_CHANGED, fields["title"])
    try:
        plugin = records.load_plugin(root)
    except records.RecordError as e:
        return "unknown", "the plugin record is unreadable (%s)" % e
    # The Mod's witness counts only for this generation's consumer; an older one reads absent.
    witness = plugin.get("session_id") if plugin.get("generation") == rec.get("consumer") else None
    if witness != cfg["SESSION_ID"] and not (witness is None and created and gen.get("nonce") == created):
        return "unknown", "plugin witness: session id %s, configured %s" % (witness or "absent", cfg["SESSION_ID"])
    return "adopt", ""


class Launcher:
    def __init__(self, cfg: dict, config_dir, *, log=None, run=subprocess.run, sleep=time.sleep, clock=time.time,
                 environ=None, tmux=None):
        self.cfg, self.config_dir, self.root = cfg, Path(config_dir), cm.work_root(cfg)
        self.log = log or (lambda line: print("claude-local: %s" % line, file=sys.stderr, flush=True))
        self.run, self.sleep, self.clock, self.environ = run, sleep, clock, environ
        self.tm = Tmux(socket_path(config_dir), tmux=tmux, run=run)
        self.state = cm.stop_state()
        self.observation: dict = {}
        self.created: "str | None" = None
        self.adopted = False
        self.refused = ""
        self.store_root = Path(cfg["HITL_STORE"]) if cfg.get("HITL_STORE") else None
        self.supervisor_lock = self.config_dir / SUPERVISOR_LOCK
        self.opaque = card.opaque_install(task_io.install_id(), self.root)

    # ── the record, always under the supervisor lock ─────────────────────────

    def _with_record(self, mutate):
        with records.flocked(self.supervisor_lock):
            return self._mutate_held(mutate)

    def _mutate_held(self, mutate):
        """Only with the supervisor lock already held (a second flock in this process would block)."""
        rec = records.load_supervisor(self.root)
        out = mutate(rec)
        if out is not None:
            records.save_supervisor(self.root, out)
        return out if out is not None else rec

    def record(self) -> dict:
        return self._with_record(lambda rec: None)

    @staticmethod
    def attach_command() -> str:
        """What the card shows: the adapter's own command, never a host path."""
        return "%s attach" % LOCAL.command

    # ── create / adopt ───────────────────────────────────────────────────────

    def create(self, rec: dict) -> "tuple[bool, str]":
        fence = tmux_pane_keys.fence_status(self.tm.sock)
        if fence != "clear":
            return False, "the pane-key guard on %s is %s; creation is fenced until it is recovered" % (self.tm.sock, fence)
        consumer = task_io.holder_consumer(self.root)
        if consumer and consumer.get("live") is not False:
            return False, "a recorded claude consumer still verifies or is unobserved; no second session"
        gen = rec.get("generation")
        if gen and task_io.verify_lineage(gen["server_pid"], gen["server_start"]) is not False:
            return False, "the recorded server is not proven gone"
        problem = inbox.prepare_root(self.root)     # the format marker first: the record lives in state/
        if problem:
            self.refused = "" if inbox.retryable(problem) else problem
            return False, problem
        agent_id = self.cfg.get("AGENT_ID") or None
        problem = records.agent_problem(self.root, agent_id or "")
        if problem:
            self.refused = problem
            return False, problem
        problem = trust.record(self.cfg.get("CLAUDE_CONFIG_DIR", ""), os.path.join(self.root, "tasks"),
                               home=(os.environ if self.environ is None else self.environ).get("HOME", ""))
        if problem:
            return False, problem
        rec = self.retire_card()
        nonce = os.urandom(16).hex()
        generation, problem = create_session(self.tm, self.cfg, self.config_dir, self.root, nonce, self.environ, self.run)
        if generation is None:
            return False, problem
        self.created, self.adopted = nonce, False
        self._with_record(lambda r: dict(r, generation=generation, consumer=None, title_token=None, title_changed=None,
                                         incident=None, agent_id=r.get("agent_id") or agent_id,
                                         stall=None, deadline=None, effect=None, install_id=self.opaque,
                                         launcher=task_io.process_identity()))
        consumer, title = await_consumer(self.tm, self.root, generation["pane_id"], sleep=self.sleep)
        if consumer is None:
            return False, "no consumer attached within %d s of the session's start" % CONSUMER_WAIT_S
        self._with_record(lambda r: dict(r, consumer=consumer, title_token=title))
        self.log("created session %s (pane %s, claude pid %d)" % (generation["session_id"], generation["pane_id"], consumer["pid"]))
        return True, ""

    # ── the detector and the card ────────────────────────────────────────────

    def observe(self, rec: dict) -> dict:
        gen = rec.get("generation")
        text = self.tm.capture(gen["pane_id"]) if gen else None
        if text is None:
            return rec
        waiting = trust.dialog(text)
        if waiting != self.observation.get("dialog", None):
            self.log("the session waits at Claude Code's %s dialog; answer it in the pane (%s)" % (waiting, self.attach_command())
                     if waiting else "the session is past its startup dialog")
        try:
            plugin = records.load_plugin(self.root)
        except records.RecordError:
            plugin = records.empty_plugin()     # a torn record still lets the alarm through
        plugin = dict(plugin, stuck=inbox.unresolved_stuck(self.root, plugin.get("stuck")),
                      progress_unwritable=records.read_progress_alarm(self.config_dir))
        self.observation = detector.derive(self.observation, text, plugin, self.clock())
        stall = self.observation["stall"]
        if (rec.get("stall") or None) != stall:
            if rec.get("stall"):
                self.log("progress: the stall %s" % ("cleared" if stall is None else "changed"))
            return self.clear_card(stall, plugin.get("session_id"))
        return rec

    def raise_card(self, rec: dict, kind: str, failed_check: str) -> dict:
        """One card per incident; the id lives in the supervisor record so a restart resumes it."""
        if rec.get("hitl") or self.store_root is None:
            return rec
        stall = rec.get("stall") or {"reason": failed_check, "since": self.clock(), "frame_hash": "", "task": None,
                                     "checks": failed_check, "lines": []}
        incident = card.new_id(records.stall_token(stall) or failed_check)[len(HITL_ID_PREFIX):]
        store, mgr = card.manager(self.store_root)
        gen = rec.get("generation")
        tail = (self.tm.capture(gen["pane_id"]) or "").splitlines() if gen else []
        req = card.build(kind, incident=incident, install_id=self.opaque, room="", stall=stall,
                         tail=tail or stall.get("lines") or [], attach_cmd=self.attach_command(), failed_check=failed_check)
        created = mgr.create(req)
        self.log("card %s raised: %s (%s)" % (created.id, kind, failed_check))
        return self._with_record(lambda r: dict(r, stall=stall, incident=incident,
                                                hitl={"id": created.id, "store_root": str(self.store_root)}))

    def clear_card(self, stall, observed=None) -> dict:
        """The new stall evidence, under the accepter's lock order: a still-pending card is cancelled
        and unpointed; one already accepted keeps its recorded phase (progress after it loses)."""
        def mutate(r):
            r = dict(r, stall=stall, observed_session_id=observed)
            pointer = r.get("hitl")
            if pointer and self.store_root is not None:
                req = store.load(pointer["id"])
                if req is None or req.status == "pending":
                    if req is not None:
                        mgr.cancel(req.id)
                    r.update(hitl=None, incident=None)
            return r
        if self.store_root is None:
            return self._with_record(lambda r: dict(r, stall=stall, observed_session_id=observed))
        store, mgr = card.manager(self.store_root)
        with accepter.locks(self.root, self.supervisor_lock, store):
            return self._mutate_held(mutate)

    def retire_card(self) -> dict:
        """Before a new generation: the old one's card never acts on it. A pending card is cancelled;
        an accepted stop records that the session was already gone; a rearm's successor is cancelled."""
        pointer = self.record().get("hitl")
        if not pointer or self.store_root is None:
            return self.record()
        store, mgr = card.manager(self.store_root)
        with accepter.locks(self.root, self.supervisor_lock, store):
            req = store.load(pointer["id"])
            sub = card.local(req) if req is not None else {}
            if req is not None and req.status == "pending":
                mgr.cancel(req.id)
            elif req is not None and req.status == "in_progress" and sub.get("effect_phase") == card.PHASE_STOPPING \
                    and not sub.get("effect_evidence"):
                sub["effect_evidence"] = {"phase": card.PHASE_STOPPING, "hitl_id": req.id, "at": self.clock(),
                                          "outcome": "the session was gone before the stop"}
                req.subject = dict(req.subject, **{card.SUBJECT: sub})
                store.save(req, store.projection(req.id))
                mgr.resolve(req.id)
            elif req is not None and sub.get("effect_phase") == card.PHASE_REARMING and sub.get("saga_key"):
                try:
                    mgr.cancel(card.reconcile_rearm(store, mgr, req, now=self.clock()))
                except ValueError as e:
                    self.log("retiring the old card: %s" % e)
            return self._mutate_held(lambda r: dict(r, hitl=None, incident=None, stall=None, deadline=None))

    # ── effects: what an accepted action does, executed here only ────────────

    def apply_effects(self, rec: dict) -> dict:
        """Execute an accepted action once: the recorded phase is read (and a commit-unknown record
        made durable) under the three locks; the stop runs outside them; its evidence is recorded
        under them again, so a restart never stops twice."""
        pointer = rec.get("hitl")
        if not pointer or self.store_root is None:
            return rec
        store, mgr = card.manager(self.store_root)
        with accepter.locks(self.root, self.supervisor_lock, store):
            req = store.load(pointer["id"])
            if req is None:
                return rec
            if req.status == "in_progress":
                accepter.reconcile_unconfirmed(store, mgr, req)
            sub = card.local(req)
            phase = sub.get("effect_phase")
            if phase == card.PHASE_REARMING and sub.get("saga_key"):
                try:
                    sid = card.reconcile_rearm(store, mgr, req, now=self.clock())
                except ValueError as e:
                    self.log("keep waiting refused: %s" % e)      # fail closed: nothing created, pointer kept
                    return rec
                self.log("keep waiting: successor %s" % sid)
                return self._mutate_held(lambda r: dict(r, hitl={"id": sid, "store_root": str(self.store_root)},
                                                        deadline=self.clock() + UNKNOWN_WAIT_S))
            if req.status != "in_progress" or phase != card.PHASE_STOPPING or sub.get("effect_evidence"):
                return rec
        outcome = self.stop_session(rec, "the owner confirmed the stop")
        evidence = {"phase": card.PHASE_STOPPING, "hitl_id": req.id, "outcome": outcome, "at": self.clock()}
        with accepter.locks(self.root, self.supervisor_lock, store):
            req = store.load(pointer["id"])
            sub = card.local(req)
            sub["effect_evidence"] = evidence
            req.subject = dict(req.subject, **{card.SUBJECT: sub})
            store.save(req, store.projection(req.id))
            mgr.resolve(req.id)
            stopped = outcome == "stopped"
            return self._mutate_held(lambda r: dict(r, effect=evidence, hitl=None, incident=None, stall=None,
                                                    deadline=None, generation=None if stopped else r.get("generation")))

    # ── the stop path ────────────────────────────────────────────────────────

    def fenced(self, gen: dict, title: str, action: str) -> int:
        pred = tmux_pane_keys.predicate(gen["nonce"], gen["pane_pid"], gen["session_id"], gen["session_created"],
                                        title_token=title or None)
        argv = tmux_pane_keys.fence_argv(self.tm.sock, gen["pane_id"], pred, action, tmux=self.tm.tmux)
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=tmux_pane_keys.TIMEOUT_S + 5)
        except (OSError, subprocess.SubprocessError):
            return tmux_pane_keys.UNCERTAIN
        return r.returncode

    def stop_session(self, rec: dict, why: str) -> str:
        """/exit through the fence, CLAUDE_GRACE_S for the consumer to be gone, kill-session through
        the fence, the server's own exit; every refusal is one line and the session stays."""
        gen, title = rec.get("generation"), rec.get("title_token") or ""
        if not gen:
            return "nothing to stop"
        started = self.clock()
        self.log("stopping (%s): /exit to pane %s" % (why, gen["pane_id"]))
        rc = self.fenced(gen, title, "send-keys -t %s /exit Enter" % tmux_pane_keys.tmux_quote(gen["pane_id"]))
        if rc != 0:
            return self.unsent(gen, title, rc, "/exit") or self.stopped(started)
        deadline = time.monotonic() + CLAUDE_GRACE_S
        consumer = task_io.holder_consumer(self.root)
        while consumer and consumer.get("live") is not False and time.monotonic() < deadline:
            self.sleep(0.5)
            consumer = task_io.holder_consumer(self.root)
        rc = self.fenced(gen, title, "kill-session -t %s" % tmux_pane_keys.tmux_quote(gen["session_id"]))
        if rc != 0:
            return self.unsent(gen, title, rc, "kill-session") or self.stopped(started)
        deadline = time.monotonic() + SERVER_EXIT_S
        while self.tm.has_session() is True and time.monotonic() < deadline:
            self.sleep(0.5)
        if self.tm.has_session() is True:
            return "kill-session sent but the session still answers after %d s" % SERVER_EXIT_S
        return self.stopped(started)

    def stopped(self, started) -> str:
        self.log("stopped in %.1f s" % (self.clock() - started))
        return "stopped"

    def unsent(self, gen: dict, title: str, rc: int, what: str) -> str:
        """A fenced action that did not report sent: '' when the session is already gone (a /exit
        that worked takes the server with it), else the one-line outcome for its exit code."""
        if rc == tmux_pane_keys.UNCERTAIN:
            self._with_record(lambda r: dict(r, fence_uncertain=True))
        if self.tm.has_session() is False:
            return ""
        if rc == tmux_pane_keys.WITHHELD:
            fields = self.tm.pane_fields(gen["pane_id"])
            if title and fields and tuple_matches(fields, gen) and fields["title"] != title:
                self._with_record(lambda r: dict(r, title_changed={"recorded": title, "seen": fields["title"],
                                                                   "at": self.clock()}))
                return ("%s: the pane title changed from the recorded token (pid, session and nonce still "
                        "match): the pane's claude retitled it; nothing sent" % TITLE_CHANGED)
            return "cancelled: the tuple changed before %s; nothing sent" % what
        if rc == tmux_pane_keys.UNCERTAIN:
            return "uncertain: the %s send is unconfirmed; creation is fenced until the guard is recovered" % what
        if rc == tmux_pane_keys.BUSY:
            return "busy: another pane-key sender holds the guard; %s not sent, retry later" % what
        return "error: the pane-key send of %s exited %d; nothing confirmed sent" % (what, rc)

    # ── the loop ─────────────────────────────────────────────────────────────

    def tick(self, rec: dict) -> "tuple[int | None, dict]":
        verdict, detail = classify(self.tm, rec, self.cfg, self.root, created=self.created)
        if verdict == "foreign":
            self.log("refusing to start: %s" % detail)
            return cm.EXIT_HELD, rec
        if verdict == "create":
            ok, problem = self.create(rec)
            if not ok:
                self.log("cannot create a session: %s" % problem)
                if self.refused:
                    # A layout refusal holds until the owner fixes the root: terminal, so the manager stops.
                    self.log("stopping: the work root needs attention, then run %s install again" % LOCAL.command)
                    return cm.EXIT_HELD, rec
                return EXIT_RETRY, rec
            rec = self._with_record(lambda r: dict(r, deadline=None))
            return None, rec
        if verdict == "unknown":
            self.adopted = False
            now = self.clock()
            if rec.get("deadline") is None:
                rec = self._with_record(lambda r: dict(r, deadline=now + UNKNOWN_WAIT_S))
                self.log("unknown: %s; waiting %d s" % (detail, UNKNOWN_WAIT_S))
            elif now >= rec["deadline"] and not rec.get("hitl"):
                rec = self._with_record(lambda r: dict(r, stall={"reason": "unknown: " + detail, "since": r["deadline"],
                                                                 "frame_hash": "", "task": None, "checks": detail, "lines": []}))
                rec = self.raise_card(rec, card.KIND_UNKNOWN, detail)
            return None, self.apply_effects(rec)
        if not self.adopted:
            self.adopted = True
            rec = self._with_record(lambda r: dict(r, launcher=task_io.process_identity(), deadline=None))
            self.log("supervising session %s (pane %s)" % (rec["generation"]["session_id"], rec["generation"]["pane_id"]))
        rec = self.observe(rec)
        if rec.get("stall") and not rec.get("hitl"):
            rec = self.raise_card(rec, card.KIND_STALL, rec["stall"]["checks"])
        return None, self.apply_effects(rec)

    def serve(self) -> int:
        problem = socket_problem(self.config_dir) or tmux_problem(self.tm.tmux, self.run)
        if problem:
            self.log("refusing to start: %s" % problem)
            return cm.EXIT_UNUSABLE
        before = cm.stop_handlers(self.state)    # over the inherited dispositions, before any lock
        try:
            with records.flocked(self.config_dir / LAUNCHER_LOCK, blocking=False):
                return self._serve()
        except BlockingIOError:
            self.log("another launcher of this install is alive; retrying later")
            return EXIT_RETRY
        finally:
            cm.restore_handlers(self.state, before)

    def _serve(self) -> int:
        if self.store_root is not None:
            try:
                card.register_install(self.store_root, self.opaque, self.root, self.supervisor_lock)
            except (OSError, records.RecordError) as e:
                self.log("cannot register with the bridge's card store (%s); retrying later" % e)
                return EXIT_RETRY
        rec = self.apply_effects(self.record())
        while True:
            if self.state["sig"] is not None:
                outcome = self.stop_session(rec, "the manager asked for a stop")
                self.log("stop: %s" % outcome)
                return 0 if outcome in ("stopped", "nothing to stop") else EXIT_RETRY
            rc, rec = self.tick(rec)
            if rc is not None:
                return rc
            self.sleep(TICK_S)
            rec = self.record()


def launchd_exit(rc: int) -> int:
    """The entry shim's table: logical 75 is terminal (exit 0, launchd stops respawning); every other
    return, and any signal death, is non-zero so launchd respawns after the throttle."""
    if rc == cm.EXIT_HELD:
        return 0
    if rc <= 0:
        return 1
    return rc


def run_launchd(child_argv: list, popen=subprocess.Popen) -> int:
    """`run --launchd`: the launcher as a child, the stop signals forwarded, its outcome mapped."""
    child = popen(child_argv)
    forwarded = []

    def forward(signum, frame):
        if child.poll() is None:
            child.send_signal(signum)
            forwarded.append(signum)
    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        rc = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return launchd_exit(rc)
