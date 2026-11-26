"""The host declaration a Codex app-server seat is started with: its folders and its stage hook.

A deliberate minimal copy of `ag2_sparrow.supervised_host` from #6474, which is parked: only what
`seat.py` uses (the loader, the validated `Host`, the stage hook). The schema name and the keys
are #6474's, so a declaration written here stays readable by the shared module. Replace this
file with an import of that module if it lands. Stdlib only.

    {
      "schema": "ag2-sparrow.supervised-host/1",
      "inbox": "/abs/deliveries/<worker>",      task-*.txt entries the host delivers
      "payloads": "/abs/tasks",                 optional: an inbox entry names <payloads>/<entry>
      "results": "/abs/results",                results/<task id>.txt, written by the agent
      "state": "/abs/state/<seat>",             the seat's own thread id, records, lock
      "publish": "result_publish",              validated, unused: the seat writes no result
      "stage_command": ["/abs/py", "..."],      optional: + --task-id <id> --stage pending|done
      "cwd": "/abs/dir"                         optional: the turns' working directory
    }
"""
from __future__ import annotations

import json
import os
import subprocess

SCHEMA = "ag2-sparrow.supervised-host/1"
PUBLISH_CONTRACTS = ("result_publish",)
STAGES = ("pending", "done")
STAGE_TIMEOUT_S = 30
DECLARATION_MAX = 64 * 1024
REQUIRED = ("schema", "inbox", "results", "state", "publish")
OPTIONAL = ("payloads", "stage_command", "cwd")


class DeclarationError(ValueError):
    """The declaration cannot be used; the message is the one line to print."""


class Host:
    """A validated declaration. Paths are absolute; `payloads`, `stage_command` and `cwd` may be None."""

    def __init__(self, path, inbox, results, state, publish, payloads=None, stage_command=None, cwd=None):
        self.path, self.inbox, self.results, self.state = path, inbox, results, state
        self.publish_contract, self.payloads, self.stage_command, self.cwd = publish, payloads, stage_command, cwd

    def task_path(self, entry):
        """The task file an inbox entry stands for: itself, or the payload it names under `payloads`."""
        return os.path.join(self.payloads, entry) if self.payloads else os.path.join(self.inbox, entry)

    def stage(self, task_id, stage, run=subprocess.run):
        """'' when the host recorded `stage` for `task_id` (or declares no stage hook), else why not."""
        if stage not in STAGES:
            raise ValueError("unknown stage %r" % stage)
        if not self.stage_command:
            return ""
        argv = list(self.stage_command) + ["--task-id", task_id, "--stage", stage]
        try:
            r = run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=STAGE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            return "stage %s did not run (%s)" % (stage, type(e).__name__)
        if r.returncode != 0:
            tail = ((r.stderr or "").strip().splitlines() or [""])[-1][:200]
            return "stage %s exited %d%s" % (stage, r.returncode, (": " + tail) if tail else "")
        return ""


def _abs_dir(decl, key, must_exist=True):
    value = decl.get(key)
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise DeclarationError("%s must be an absolute path" % key)
    value = os.path.normpath(value)
    if must_exist and not os.path.isdir(value):
        raise DeclarationError("%s %s is not a directory" % (key, value))
    return value


def load(path):
    """The Host `path` declares, or DeclarationError naming the first thing wrong with it."""
    try:
        with open(path, "rb") as f:
            raw = f.read(DECLARATION_MAX + 1)
    except OSError as e:
        raise DeclarationError("cannot read the host declaration %s (%s)" % (path, e.strerror or e))
    if len(raw) > DECLARATION_MAX:
        raise DeclarationError("the host declaration %s is larger than %d bytes" % (path, DECLARATION_MAX))
    try:
        decl = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise DeclarationError("the host declaration %s is not JSON (%s)" % (path, e))
    if not isinstance(decl, dict):
        raise DeclarationError("the host declaration must be a JSON object")
    unknown = sorted(set(decl) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        raise DeclarationError("unknown key in the host declaration: %s" % unknown[0])
    missing = [k for k in REQUIRED if k not in decl]
    if missing:
        raise DeclarationError("the host declaration has no %s" % missing[0])
    if decl["schema"] != SCHEMA:
        raise DeclarationError("schema %r is not %s" % (decl["schema"], SCHEMA))
    if decl["publish"] not in PUBLISH_CONTRACTS:
        raise DeclarationError("publish %r is not one of %s" % (decl["publish"], ", ".join(PUBLISH_CONTRACTS)))
    inbox, results = _abs_dir(decl, "inbox"), _abs_dir(decl, "results")
    state = _abs_dir(decl, "state", must_exist=False)
    payloads = _abs_dir(decl, "payloads") if decl.get("payloads") is not None else None
    cwd = _abs_dir(decl, "cwd") if decl.get("cwd") is not None else None
    if len({os.path.realpath(p) for p in (inbox, results, state)}) != 3:
        raise DeclarationError("inbox, results and state must be three different directories")
    stage = decl.get("stage_command")
    if stage is not None and (not isinstance(stage, list) or not stage
                              or not all(isinstance(a, str) and a for a in stage) or not os.path.isabs(stage[0])):
        raise DeclarationError("stage_command must be a list of strings starting with an absolute executable")
    os.makedirs(state, mode=0o700, exist_ok=True)
    return Host(os.path.abspath(path), inbox, results, state, decl["publish"], payloads, stage, cwd)
