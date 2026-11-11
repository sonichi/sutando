#!/usr/bin/env python3
"""End-to-end check of agent-native worker provisioning against one server.

    python3 provision_e2e.py --base-url https://<gateway> --worker <worker> [--room ROOM]
                             [--single-claim] [--no-tmux] [--workspace WS] ...

Acts as the core's own agent on the server at --base-url (the core must already be connected
there) and walks the whole flow for one live pool worker that has no agent of its own yet:
`agent.create`, the claim redeem with its refusals, the worker answering a mention, `agent.retire`,
and a scan of what this install writes (the workspace, the session transcripts, the core's and the
worker's tmux scrollback, the shell history) for the secrets the run handled, the core's own
credential included (expected only in its own configured env file). It prints one table.

What a full run does on the server: it creates two agents (the worker's, and a second whose claim
is left to expire) and retires both. Each `agent.create` and `agent.retire` needs the owner's
approval, and the run waits for the second claim's five-minute lifetime. A core may create five
agents an hour. `--single-claim` creates one agent and skips the expired-claim and different-caller
checks. On a server that cannot ask the owner yet, the create is refused and the run stops there.

No token and no claim secret is ever printed: they are compared in this process, and every line
of output passes through a redaction of the values the run holds.

Exit: 0 every check passed; 1 a check failed; 3 none failed but some were skipped or incomplete.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import connections
import pool_provision as pp
import pool_roster as pr
from workspace_default import resolve_workspace

PASS, FAIL, SKIP, INCOMPLETE = "PASS", "FAIL", "SKIP", "INCOMPLETE"
POLL_S = pp.POLL_S
EXPIRY_MARGIN_S = 5
MIN_NEEDLE = 16
# The owner's own project trees: large, and nothing in this flow writes there.
UNSCANNED = ("projects",)
SHELL_HISTORIES = (".zsh_history", ".bash_history")
CHUNK = 1 << 20
find, approval_state = pp.find, pp.approval_state

CREATE, PROVENANCE, NAMING = "create is gated on approval", "agent exists with the expected provenance", \
    "agent id is <owner>-<core>-<name>"
WRONG_SECRET, NO_CREDENTIAL = "redeem with a wrong secret fails", "redeem without a credential fails"
REDEEM, NO_STORE = "redeem succeeds once", "redeem answer is marked no-store"
SECRET_GONE, WHOAMI = "claim secret is deleted after the redeem", "stored token is the new agent's (whoami)"
SECOND, OTHER_CALLER, EXPIRED = "second redeem fails", "redeem by a different caller fails", "expired claim fails"
MENTION = "worker answers a mention as its own agent"
RETIRED, LOCAL_GONE = "retired agent's old token is rejected", "keychain item and connection row are gone"
CLEANUP = "second agent retired (cleanup)"
CONTROL, LEAKS = "leak scan finds a planted secret", "no token or claim secret in logs, tasks, results, transcripts, tmux"
CORE_CREDENTIAL = "core credential"
# The status and `details.reason` the server refuses each bad redeem with.
REFUSALS = {WRONG_SECRET: (403, "wrong_secret"), NO_CREDENTIAL: (401, "invalid_bearer"),
            SECOND: (409, "claim_redeemed"), OTHER_CALLER: (404, "claim_not_found"),
            EXPIRED: (410, "claim_expired")}
CHECKS = (CREATE, PROVENANCE, NAMING, WRONG_SECRET, NO_CREDENTIAL, REDEEM, NO_STORE, SECRET_GONE, WHOAMI, SECOND,
          OTHER_CALLER, EXPIRED, MENTION, RETIRED, LOCAL_GONE, CLEANUP, CONTROL, LEAKS)


class Halt(Exception):
    """The run cannot go on; the checks it did not reach are reported as skipped."""


class Report:
    """One row per check, in a fixed order. A check nobody set is a SKIP, never a pass."""

    def __init__(self, known: list):
        self.known, self.rows, self.notes = known, {}, []

    def set(self, name: str, status: str, detail: str = "") -> None:
        self.rows[name] = (status, pp.scrub(detail, self.known))

    def status(self, name: str) -> str:
        return self.rows.get(name, (SKIP, ""))[0]

    def note(self, text: str) -> None:
        self.notes.append(pp.scrub(text, self.known))

    def render(self) -> str:
        width = max(len(name) for name in CHECKS)
        lines = [f"{'check'.ljust(width)}  result      detail", f"{'-' * width}  ----------  ------"]
        for name in CHECKS:
            status, detail = self.rows.get(name, (SKIP, "not reached"))
            lines.append(f"{name.ljust(width)}  {status.ljust(10)}  {detail}")
        tally = {s: sum(1 for n in CHECKS if self.status(n) == s) for s in (PASS, FAIL, SKIP, INCOMPLETE)}
        lines.append("")
        lines.append(", ".join(f"{count} {status}" for status, count in tally.items()))
        return "\n".join(lines + self.notes)

    def exit_code(self) -> int:
        statuses = [self.status(name) for name in CHECKS]
        return 1 if FAIL in statuses else (3 if SKIP in statuses or INCOMPLETE in statuses else 0)


def expiry_of(value: object) -> Optional[float]:
    """Epoch seconds of an `expires_at` (a number, or ISO 8601); None when it cannot be read."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        when = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)).timestamp()


# ── the leak scan ────────────────────────────────────────────────────────────

def count_in_stream(chunks: Iterable[bytes], needles: Mapping[str, bytes]) -> dict:
    """Occurrences of each needle in a byte stream, found across chunk boundaries."""
    counts = {label: 0 for label in needles}
    tails = {label: b"" for label in needles}
    for chunk in chunks:
        for label, needle in needles.items():
            buf = tails[label] + chunk
            counts[label] += buf.count(needle)
            tails[label] = buf[-(len(needle) - 1):]
    return counts


def _chunks(path: Path) -> Iterable[bytes]:
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                return
            yield chunk


def _files(root: Path, links: list, unreadable: list) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for folder, folders, names in os.walk(root, followlinks=False,
                                          onerror=lambda e: unreadable.append(str(e.filename or root))):
        for name in folders + names:
            path = Path(folder) / name
            if path.is_symlink():
                links.append(str(path))
            elif name in names and path.is_file():
                yield path


def scan(needles: Mapping[str, str], roots: Iterable[Path], texts: Mapping[str, bytes]) -> dict:
    """Search every regular file at or under `roots`, once each, and every named text for the needles,
    in this process. Links are not followed, and are listed; a directory that cannot be listed is
    unreadable, like a file that cannot be read. Returns counts and places only:
    {"hits": [(label, where, count)], "files": n, "unreadable": [where], "links": [where]}."""
    wanted = {label: value.encode("utf-8") for label, value in needles.items() if value}
    if not wanted or any(len(v) < MIN_NEEDLE for v in wanted.values()):
        raise ValueError(f"every needle must be at least {MIN_NEEDLE} bytes: a shorter one matches by chance")
    out = {"hits": [], "files": 0, "unreadable": [], "links": []}
    seen = set()

    def record(where: str, counts: Mapping[str, int]) -> None:
        out["hits"] += [(label, where, n) for label, n in counts.items() if n]

    for root in roots:
        for path in _files(root, out["links"], out["unreadable"]):
            try:
                stat = path.stat()
                if (stat.st_dev, stat.st_ino) in seen:
                    continue
                seen.add((stat.st_dev, stat.st_ino))
                record(str(path), count_in_stream(_chunks(path), wanted))
                out["files"] += 1
            except OSError:
                out["unreadable"].append(str(path))
    for where, text in texts.items():
        record(where, count_in_stream([text], wanted))
    return out


def tmux_scrollback(socket: str, session: str, run=subprocess.run) -> bytes:
    """Every pane's whole history in one tmux session, wrapped lines joined; OSError when it has none."""
    import tmux_probe
    tmux = tmux_probe.tmux_binary()
    panes = run([tmux, "-S", socket, "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}"],
                capture_output=True, timeout=10)
    if panes.returncode != 0 or not panes.stdout.split():
        raise OSError(f"no tmux session {session}")
    out = b""
    for pane in panes.stdout.split():
        got = run([tmux, "-S", socket, "capture-pane", "-p", "-J", "-S", "-", "-E", "-", "-t", pane.decode()],
                  capture_output=True, timeout=30)
        if got.returncode != 0:
            raise OSError(f"tmux could not capture a pane of {session}")
        out += got.stdout + b"\n"
    return out


def scan_roots(workspace: Path, extra: Iterable[Path] = ()) -> Tuple[list, list]:
    """(what must exist, what is scanned). Must exist: the logs, results, tasks and transcripts a run
    always writes, and `extra`. Scanned: those wherever they really are, everything else in the
    workspace but the owner's own project trees and links out of it, the config home's transcripts
    when they live elsewhere, and the shell's history files."""
    from util_paths import claude_home_path
    transcripts = workspace / ".claude-sutando" / "projects"
    required = [workspace / "logs", workspace / "results", workspace / "tasks", transcripts, *extra]
    scanned = sorted(p for p in workspace.iterdir() if p.name not in UNSCANNED and not p.is_symlink()) \
        if workspace.is_dir() else []
    scanned += [r.resolve() for r in required]
    home = claude_home_path("projects")
    if home.resolve() != transcripts.resolve():
        scanned.append(home)
    return required, scanned + [Path.home() / name for name in SHELL_HISTORIES]


def leak_check(report: Report, needles: Mapping[str, str], workspace: Path, *, extra_roots, tmux_targets, tmux,
               kept: Optional[Mapping[str, Tuple[str, set]]] = None) -> None:
    """The two scan rows, from one pass: it looks for the run's secrets and for a control value planted
    in the workspace's own logs and in a text. A pass that misses the control proves nothing. `kept`
    maps a credential's label to (its value, the files that hold it by design): a hit in one of those
    files is expected, anywhere else a leak."""
    wanted = {label: value for label, value in needles.items() if value}
    required, scanned = scan_roots(workspace, extra_roots)
    missing = [str(r) for r in required if not r.is_dir()]
    texts, unscanned = {}, []
    kept = dict(kept or {})
    for label, (value, _) in kept.items():
        if wanted and len(value.encode("utf-8")) >= MIN_NEEDLE:
            wanted[label] = value
        elif wanted:
            unscanned.append(f"{label} is too short to search for")
    for name, socket, session in tmux_targets:
        try:
            texts[f"tmux:{name}"] = tmux(socket, session)
        except (OSError, subprocess.SubprocessError) as e:
            unscanned.append(f"tmux:{name} ({e})")
    canary = secrets.token_hex(32)
    with tempfile.TemporaryDirectory() as tmp:
        logs = workspace / "logs"
        planted = (logs if logs.is_dir() else Path(tmp)) / f".provision-e2e-control-{canary[:12]}.log"
        try:
            planted.write_text(f"token={canary}\n", encoding="utf-8")
            real = planted.resolve()
            result = scan({**wanted, CONTROL: canary}, [r for r in scanned + [Path(tmp)] if r.exists()],
                          {**texts, CONTROL: canary.encode()})
        except OSError as e:
            result, real = {"hits": [], "files": 0, "unreadable": [f"the control file ({type(e).__name__})"]}, planted
        finally:
            try:
                planted.unlink()
            except OSError:
                pass
    control = sorted((where if where == CONTROL else str(Path(where).resolve()), n)
                     for label, where, n in result["hits"] if label == CONTROL)
    if control != sorted([(CONTROL, 1), (str(real), 1)]):
        report.set(CONTROL, FAIL, f"the scan did not find a value planted in {planted.parent} and in a text")
        report.set(LEAKS, INCOMPLETE, "not run: the scan cannot be trusted")
        return
    report.set(CONTROL, PASS, f"found once in {planted.parent} and once in a planted text")
    if not wanted:
        report.set(LEAKS, SKIP, "the run produced no secret to look for")
        return
    hits = [(label, where, n) for label, where, n in result["hits"]
            if label != CONTROL and not (label in kept and str(Path(where).resolve()) in kept[label][1])]
    gaps = [f"missing {m}" for m in missing] + unscanned + [f"unreadable {u}" for u in result["unreadable"]]
    summary = f"{result['files'] - 1} files, {len(texts)} tmux sessions"
    if result["links"]:
        summary += f" ({len(result['links'])} links not followed)"
    if hits:
        places = "; ".join(f"{label} x{n} in {where}" for label, where, n in hits)
        report.set(LEAKS, FAIL, f"{len(hits)} places hold a secret: {places}")
    elif gaps:
        report.set(LEAKS, INCOMPLETE, f"0 hits in {summary}, but not everything was scanned: {'; '.join(gaps)}")
    else:
        report.set(LEAKS, PASS, f"0 hits for {len(wanted)} secrets in {summary}")


def core_label(row: Mapping[str, object]) -> str:
    """The label the server reads from a core's id and puts in its workers' ids: `<label>` of
    `@<owner>-<label>.agent`; `sutando` for the owner's default agent and the legacy form."""
    owner = str(row.get("owner_mxid") or "").lstrip("@").partition(":")[0]
    local = str(row.get("agent_mxid") or "").lstrip("@").partition(":")[0]
    if owner and local.startswith(owner + "-") and local.endswith(".agent"):
        return local[len(owner) + 1:-len(".agent")]
    return "sutando"


def credential_files(row: Mapping[str, object]) -> set:
    """The env files a core lane's credential is kept in by design: its channel directory's, the
    launch-key file and the launcher-named device file. Empty when they cannot be resolved, so that
    a hit anywhere then counts as a leak."""
    try:
        from channel_credentials import CHANNEL
        from channel_env_resolve import candidates, device_env, resolve_launch_key_env
        from util_paths import claude_home_path
        home, instance = claude_home_path("channels"), str(row.get("instance") or "")
        source = connections.lane_channel_dir(instance) if instance else CHANNEL
        files = candidates(home / source) + [device_env(source), device_env(CHANNEL),
                                             resolve_launch_key_env(home, CHANNEL)]
        return {str(Path(f).resolve()) for f in files if f}
    except Exception:  # noqa: BLE001 - an unresolvable layout exempts nothing
        return set()


# ── the flow ─────────────────────────────────────────────────────────────────

def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


class Flow:
    """One run: the checks in order, each recording its own row."""

    def __init__(self, args, report: Report, known: list, needles: dict, *, request, vault, preview, sleep, clock,
                 say=_say):
        self.args, self.report, self.known, self.needles = args, report, known, needles
        self.kept: dict = {}
        self.request, self.vault, self.preview, self.sleep, self.clock = request, vault, preview, sleep, clock
        self.mine = pp._vault(vault)
        self.say = lambda text: say(pp.scrub(text, known))
        self.ws = Path(args.workspace)
        self.wid, self.created, self.redeem_headers, self.redeemed_at = "", [], {}, 0.0

    def hold(self, label: str, value: Optional[str]) -> str:
        """Remember a secret: redacted from every line printed, and looked for by the scan."""
        if value and value not in self.needles.values():
            self.known.append(value)
            self.needles[label] = value
        return value or ""

    def redeem_once(self, bearer: str, claim_id: str, secret: str) -> Tuple[int, dict]:
        url = pp.origin_of(self.gateway) + pp.REDEEM_PATH.format(claim_id=claim_id)
        status, _, body = self.request("POST", url, bearer=bearer, payload={"secret": secret})
        if status == 200:
            self.hold(f"token from extra redeem {len(self.needles)}", str(body.get("bearer") or ""))
        return status, body

    def refusal(self, name: str, status: int, body: Mapping[str, object],
                caller: Optional[Tuple[str, str]] = None) -> None:
        """Record a check that a redeem made as `caller` (bearer, agent; None for no credential) was
        refused for the reason under test. A token is a failure. Only the status and reason the server
        gives for that rule count, and only when the caller's own credential is accepted just after:
        any other refusal says nothing about the rule."""
        said = f"http {status} {pp.error_code(body)}".rstrip()
        if status == 200:
            self.report.set(name, FAIL, "the server delivered a token")
        elif (status, pp.error_code(body)) != REFUSALS[name]:
            want = "http %d %s" % REFUSALS[name]
            self.report.set(name, INCOMPLETE, f"not this refusal: the server answered {said}, not {want}")
        elif caller and not self.accepted(*caller):
            self.report.set(name, INCOMPLETE, f"refused ({said}), but the caller's own credential was not accepted "
                                              "just after, so this says nothing about the claim")
        else:
            self.report.set(name, PASS, f"refused ({said})")

    def accepted(self, bearer: str, agent: str) -> bool:
        status, _, body = self.request("GET", self.gateway.rstrip("/") + "/v1/whoami", bearer=bearer)
        return status == 200 and body.get("agent_mxid") == agent

    def create(self, name: str, key_sha256: str) -> Tuple[Callable[[], dict], dict, str]:
        """Ask for an agent: (the same call, to make again; its first answer; how that stands)."""
        operation_id = f"provision-e2e-{uuid.uuid4().hex}"
        arguments = pp.create_arguments(name, key_sha256, role=self.args.role or "")

        def call() -> dict:
            return self.actions.execute("agent.create", arguments, operation_id=operation_id)
        first = call()
        return call, first, approval_state(first)

    def approved(self, action: str, call: Callable[[], dict], first: dict) -> Tuple[str, dict]:
        timeout = self.args.approval_timeout
        return pp.settle(call, first=first, timeout=timeout, sleep=self.sleep, clock=self.clock, poll=POLL_S,
                         waiting=lambda: self.say(f"waiting up to {int(timeout)}s for the owner to approve {action}"))

    def run(self) -> None:
        args, report = self.args, self.report
        roster = pr.load_roster(self.ws) or {}
        wid = pr.resolve_label(roster, args.worker)
        row = (roster.get("workers") or {}).get(wid)
        if wid == pr.CORE or not isinstance(row, dict) or row.get("state") == "retired":
            raise Halt(f"no live pool worker {args.worker!r}")
        self.core = pp.core_lane(self.ws, lane=args.lane, origin=args.base_url)
        if pp.origin_of(str(self.core["gateway_url"])) != pp.origin_of(args.base_url):
            raise Halt("the named lane's gateway is not at --base-url; no credential was sent")
        self.gateway, token = connections.connection_credential(self.core, vault=self.vault)
        self.core_bearer = pp.bearer_of(token)
        self.known.append(self.core_bearer)
        self.kept[CORE_CREDENTIAL] = (self.core_bearer, credential_files(self.core))
        self.actions = pp.Actions(self.gateway, self.core_bearer, self.request, self.known)
        begun = pp.begin(wid, role=str(row.get("label") or ""), workspace=self.ws, vault=self.vault,
                         lane=str(self.core["instance"]))
        self.wid = wid
        secret_a = self.hold("claim secret", pp.held(self.mine, pp.claim_key(wid)))
        name = args.name or begun["name"]
        call_a, first_a, state_a = self.create(name, begun["claim_key_sha256"])
        if not self.gate(first_a, state_a):
            return
        second = None
        if not args.single_claim:
            secret_b = self.hold("second claim secret", secrets.token_hex(32))
            call_b, first_b, _ = self.create(f"{name[:30].rstrip('-')}-x", pp.claim_hash(secret_b))
            second = (secret_b, call_b, first_b)
        state_a, env_a = self.approved("agent.create", call_a, first_a)
        agent_a, claim_a = str(find(env_a, "agent_id") or ""), str(find(env_a, "claim_id") or "")
        if state_a != "done" or not agent_a or not claim_a:
            report.set(PROVENANCE, FAIL, f"agent.create did not complete ({state_a}); no agent to check")
            if second:
                report.note("a second agent.create was requested and may still be approved: deny its card")
            return
        self.created.append(agent_a)
        self.provenance(agent_a, env_a, name)
        core = (self.core_bearer, str(self.core["agent_mxid"]))
        self.refusal(WRONG_SECRET, *self.redeem_once(self.core_bearer, claim_a, secrets.token_hex(32)), caller=core)
        self.refusal(NO_CREDENTIAL, *self.redeem_once("", claim_a, secret_a))
        worker_bearer = self.redeem(wid, claim_a, agent_a)
        if not worker_bearer:
            for name in (WRONG_SECRET, NO_CREDENTIAL):
                if report.status(name) == PASS:
                    report.set(name, INCOMPLETE, f"{report.rows[name][1]}, but the claim was never redeemed, so it "
                                                 "was not shown to be valid")
            return
        ask_again_at = self.second_redeem(claim_a, secret_a, worker_bearer)
        claim_b = self.second_claim(second, worker_bearer, agent_a)
        self.mention(agent_a)
        if claim_b is not None:
            wait = max(0.0, claim_b[2] + EXPIRY_MARGIN_S - self.clock())
            self.say(f"waiting {int(wait)}s for the second claim to expire")
            self.sleep(wait)
            self.refusal(EXPIRED, *self.redeem_once(self.core_bearer, claim_b[0], claim_b[1]), caller=core)
        if ask_again_at:
            self.sleep(max(0.0, ask_again_at - self.clock()))
            self.refusal(SECOND, *self.redeem_once(self.core_bearer, claim_a, secret_a), caller=core)
        self.retire(wid, agent_a, worker_bearer)
        for other in [a for a in self.created if a != agent_a]:
            state = self.retire_remote(other)
            report.set(CLEANUP, PASS if state == "done" else FAIL, f"agent.retire {state}")

    def gate(self, first: dict, state: str) -> bool:
        if state == "held":
            self.report.set(CREATE, PASS, "held for the owner's approval")
        elif state == "fail_closed":
            self.report.set(CREATE, PASS, "fails closed with approval_required: this server cannot ask the owner "
                                          "yet, so no later check could run")
        elif state == "done" and str(find(first, "source") or "").lower() in ("delegation", "grant"):
            self.report.set(CREATE, PASS, "ran at once under a delegation grant")
        elif state == "done":
            self.report.set(CREATE, FAIL, "the agent was created with neither an approval nor a grant")
        else:
            self.report.set(CREATE, FAIL, f"agent.create was {state} ({pp.error_code(first) or 'no code'})")
        return state in ("held", "done")

    def provenance(self, agent_id: str, env: Mapping[str, object], name: str) -> None:
        status, _, body = self.request("GET", self.gateway.rstrip("/") + "/v1/agents", bearer=self.core_bearer)
        listed = body.get("agents") if status == 200 and isinstance(body.get("agents"), list) else []
        entry = next((a for a in listed if isinstance(a, dict) and a.get("id") == agent_id), None)
        wrong, unknown = [], []
        if entry is None:
            wrong.append(f"{agent_id} is not in the owner's agent listing (http {status})")
        for key, want, whose in (("owner", self.core["owner_mxid"], "the core's owner"),
                                 ("created_by", self.core["agent_mxid"], "the core")):
            got = find(env, key)
            if got is None:
                unknown.append(f"the Action's answer names no {key}")
            elif got != want:
                wrong.append(f"{key} is not {whose}")
        source = str(find(env, "source") or "").lower()
        if not source:
            unknown.append("the Action's answer names no authority source")
        elif source not in ("approval", "delegation", "grant"):
            wrong.append(f"authority source is {source}")
        self.report.set(PROVENANCE, FAIL if wrong else (INCOMPLETE if unknown else PASS),
                        "; ".join(wrong + unknown) or f"owner and created_by match; authority is {source}")
        owner = str(self.core["owner_mxid"]).lstrip("@").partition(":")[0]
        server = connections.server_of_mxid(str(self.core["agent_mxid"]))
        stem = f"@{owner}-{core_label(self.core)}-{name}"
        named = agent_id.endswith(f".agent:{server}") and agent_id[:-len(f".agent:{server}")] in \
            [stem] + [f"{stem}-{n}" for n in range(2, 10)]
        self.report.set(NAMING, PASS if named else FAIL,
                        agent_id if named else f"got {agent_id}, expected {stem}.agent:{server}")

    def redeem(self, wid: str, claim_id: str, agent_id: str) -> str:
        """The real redeem, through `pool_provision.finish`; the worker's bearer, '' when it failed."""
        def recording(method, url, **kw):
            status, headers, body = self.request(method, url, **kw)
            if status == 200:
                self.redeem_headers = dict(headers)
            return status, headers, body
        try:
            out = pp.finish(wid, claim_id, agent_id, workspace=self.ws, vault=self.vault, lane=str(self.core["instance"]),
                            request=recording, preview=self.preview, sleep=self.sleep)
        except pp.ProvisionError as e:
            self.report.set(REDEEM, FAIL, str(e))
            return ""
        self.redeemed_at = self.clock()
        bearer = self.hold("worker token", pp.bearer_of(pp.held(self.mine, str(out["row"]["token_ref"])) or ""))
        self.report.set(REDEEM, PASS, f"saved as {out['row']['instance']}")
        marked = "no-store" in str(self.redeem_headers.get("cache-control") or "").lower()
        self.report.set(NO_STORE, PASS if marked else FAIL, "" if marked else "Cache-Control lacks no-store")
        left = pp.held(self.mine, pp.claim_key(wid))
        self.report.set(SECRET_GONE, FAIL if left else PASS, "still in the vault" if left else "")
        status, _, body = self.request("GET", self.gateway.rstrip("/") + "/v1/whoami", bearer=bearer)
        mine = status == 200 and body.get("agent_mxid") == agent_id == out["agent_id"]
        self.report.set(WHOAMI, PASS if mine else FAIL, agent_id if mine else f"whoami answered http {status}")
        if not self.args.no_start:
            launch = connections.start_added(out["row"], repo=self.args.repo or _REPO,
                                             logs_dir=self.args.logs_dir or self.ws / "logs", workspace=self.ws)
            if launch is not None and not launch.get("ok"):
                self.report.note(f"the worker's lane did not start: {launch.get('reason')}")
        return bearer

    def second_redeem(self, claim_id: str, secret: str, worker_bearer: str) -> float:
        """Record the second redeem; the time to ask once more when the server re-delivered instead."""
        status, body = self.redeem_once(self.core_bearer, claim_id, secret)
        if status != 200:
            self.refusal(SECOND, status, body, caller=(self.core_bearer, str(self.core["agent_mxid"])))
            return 0.0
        if body.get("bearer") != worker_bearer:
            self.report.set(SECOND, FAIL, "a second, different token was minted")
            return 0.0
        self.report.set(SECOND, INCOMPLETE, "re-delivered the same token; not asked again after the window")
        return self.redeemed_at + self.args.recollect_window

    def second_claim(self, second, worker_bearer: str, worker_agent: str) -> Optional[Tuple[str, str, float]]:
        """Settle the second create and try its claim as the new worker agent: (claim, secret, expiry)."""
        if second is None:
            for name in (OTHER_CALLER, EXPIRED, CLEANUP):
                self.report.set(name, SKIP, "needs the second claim (run without --single-claim)")
            return None
        secret_b, call_b, first_b = second
        state, env = self.approved("agent.create", call_b, first_b)
        agent_b, claim_b = str(find(env, "agent_id") or ""), str(find(env, "claim_id") or "")
        if state != "done" or not agent_b or not claim_b:
            for name in (OTHER_CALLER, EXPIRED, CLEANUP):
                self.report.set(name, INCOMPLETE, f"the second agent.create did not complete ({state})")
            return None
        self.created.append(agent_b)
        expires = expiry_of(find(env, "expires_at")) or self.clock() + self.args.claim_ttl
        if self.clock() >= expires:
            self.report.set(OTHER_CALLER, INCOMPLETE, "the second claim expired before it could be tried")
        else:
            self.refusal(OTHER_CALLER, *self.redeem_once(worker_bearer, claim_b, secret_b),
                         caller=(worker_bearer, worker_agent))
        return claim_b, secret_b, expires

    def mention(self, agent_id: str) -> None:
        room = self.args.room
        if not room:
            self.report.set(MENTION, SKIP, "no --room given")
            return

        def replies() -> set:
            messages = find(self.actions.read("room.context.read", {"limit": 50}, room), "messages")
            return {str(m.get("event_id")) for m in (messages if isinstance(messages, list) else [])
                    if isinstance(m, dict) and m.get("sender") == agent_id}
        timeout = self.args.mention_timeout
        self.say(f"invite {agent_id} to {room} now: waiting up to {int(timeout)}s for it to be a member")
        deadline = self.clock() + timeout
        while True:
            members = self.actions.read("room.members.list", {}, room)
            if members.get("is_error") or agent_id in json.dumps(members):
                break
            if self.clock() >= deadline:
                self.report.set(MENTION, FAIL, f"{agent_id} was not a member of the room within {int(timeout)}s")
                return
            self.sleep(POLL_S)
        before = replies()
        sent = self.actions.execute("room.message.send", {
            "body": f"{agent_id} provisioning check: please reply here with one word.", "mentions": [agent_id]},
            room_id=room)
        if sent.get("is_error"):
            self.report.set(MENTION, FAIL, f"the mention was not sent ({pp.error_code(sent) or 'no code'})")
            return
        self.say(f"waiting up to {int(timeout)}s for {agent_id} to answer the mention")
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            self.sleep(POLL_S)
            if replies() - before:
                self.report.set(MENTION, PASS, f"{agent_id} replied in the room")
                return
        self.report.set(MENTION, FAIL, f"no reply from {agent_id} within {int(timeout)}s")

    def retire_remote(self, agent_id: str) -> str:
        operation_id = f"provision-e2e-{uuid.uuid4().hex}"

        def call() -> dict:
            return self.actions.execute("agent.retire", {"agent_id": agent_id}, operation_id=operation_id)
        state, env = self.approved("agent.retire", call, call())
        if state == "done" and find(env, "bearer_revoked") is not True:
            state = "done without bearer_revoked"
        if state == "done":
            self.created.remove(agent_id)
        return state

    def retire(self, wid: str, agent_id: str, worker_bearer: str) -> None:
        state = self.retire_remote(agent_id)
        if state != "done":
            self.report.set(RETIRED, FAIL, f"agent.retire did not complete ({state})")
            return
        status, _, _ = self.request("GET", self.gateway.rstrip("/") + "/v1/whoami", bearer=worker_bearer)
        self.report.set(RETIRED, PASS if status in (401, 403) else FAIL, f"whoami answered http {status}")
        try:
            out = pp.retire(wid, workspace=self.ws, vault=self.vault, preview=self.preview)
        except (pp.ProvisionError, connections.ConnectionsError) as e:
            self.report.set(LOCAL_GONE, FAIL, str(e))
            return
        gone = out["status"] == "retired" and connections.worker_identity(wid, self.ws) is None
        self.report.set(LOCAL_GONE, PASS if gone else FAIL, out["status"])


def run(args, *, request=None, vault=None, preview=None, sleep=time.sleep, clock=time.time,
        tmux=tmux_scrollback, tmux_targets=(), out=print, say=_say) -> int:
    """Run every check and print the table; the scan runs last, whatever happened before it."""
    known, needles = [], {}
    report = Report(known)
    flow = Flow(args, report, known, needles, request=request or pp.http_json, vault=vault, preview=preview,
                sleep=sleep, clock=clock, say=say)
    try:
        flow.run()
    except (Halt, pp.ProvisionError, connections.ConnectionsError, pr.AmbiguousWorkerName) as e:
        report.note(f"stopped: {e}")
    except Exception as e:  # noqa: BLE001 - whatever broke, the scan below still runs and nothing is echoed
        report.note(f"stopped: unexpected {type(e).__name__}")
    finally:
        try:
            if flow.wid:
                pp.abort(flow.wid, vault=vault)
        except pp.ProvisionError as e:
            report.note(f"cleanup: {e}")
    for agent in flow.created:
        report.note(f"left on the server: {agent} (retire it with agent.retire, or delete it as the owner)")
    leak_check(report, needles, Path(args.workspace), extra_roots=[Path(p) for p in args.scan_root],
               tmux_targets=[] if args.no_tmux else list(tmux_targets), tmux=tmux, kept=flow.kept)
    if args.no_tmux and report.status(LEAKS) == PASS:
        report.set(LEAKS, INCOMPLETE, report.rows[LEAKS][1] + "; tmux scrollback not scanned (--no-tmux)")
    out(report.render())
    return report.exit_code()


def default_tmux_targets(workspace: Path, worker: str, core: str = "") -> list:
    """[(name, socket, session)] for the core's session and the worker's; `core` is `SOCKET=SESSION`."""
    import core_heartbeat
    import pool_sessions
    import spawn_worker
    socket, _, session = core.partition("=")
    targets = [("core", socket or spawn_worker.default_socket(), session or core_heartbeat.core_session())]
    wid = pr.resolve_label(pr.load_roster(workspace) or {}, worker)
    for row in pool_sessions.sessions(workspace):
        if row["worker_id"] == wid:
            targets.append(("worker", row["tmux_socket"] or spawn_worker.default_socket(), row["session_name"]))
    return targets


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="provision_e2e", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True, help="the gateway to run against; the core must be connected to it")
    p.add_argument("--worker", required=True, help="a live pool worker (id or label) with no agent of its own")
    p.add_argument("--workspace", type=Path, default=None)
    p.add_argument("--lane", default=None, help="the core connection to act as, when --base-url matches several")
    p.add_argument("--name", default=None, help="the agent's name (default: from the worker's label); its id "
                                                "becomes @<owner>-<core>-<name>.agent")
    p.add_argument("--role", default=None, help="a line saying what the agent is for, stored with it")
    p.add_argument("--room", default="", help="a room the core is in, for the mention check: the run waits for you "
                                              "to invite the new agent there")
    p.add_argument("--single-claim", action="store_true", help="create one agent: skips the expired-claim and "
                                                               "different-caller checks")
    p.add_argument("--approval-timeout", type=float, default=900.0)
    p.add_argument("--mention-timeout", type=float, default=300.0)
    p.add_argument("--claim-ttl", type=float, default=300.0, help="used when the server names no expires_at")
    p.add_argument("--recollect-window", type=float, default=125.0)
    p.add_argument("--no-start", action="store_true", help="do not start the worker's lane after the redeem")
    p.add_argument("--repo", type=Path, default=None)
    p.add_argument("--logs-dir", type=Path, default=None)
    p.add_argument("--scan-root", action="append", default=[], help="one more directory the leak scan must cover")
    p.add_argument("--core-tmux", default="", help="SOCKET=SESSION of the core's tmux session")
    p.add_argument("--no-tmux", action="store_true", help="leave tmux scrollback out of the leak scan")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.workspace = Path(args.workspace if args.workspace is not None else resolve_workspace())
    targets = [] if args.no_tmux else default_tmux_targets(args.workspace, args.worker, args.core_tmux)
    return run(args, tmux_targets=targets)


if __name__ == "__main__":
    sys.exit(main())
