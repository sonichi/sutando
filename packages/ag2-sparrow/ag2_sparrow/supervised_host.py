"""A host's declaration for running a local adapter supervised: the host's folders, its publish
contract and its stage hook, shared by every local adapter so each reads one schema.

Supervised mode is for a host that already owns the bridge, the identity, routing and restarts
(a worker pool, for one). The host writes one JSON file and starts the adapter with it; the
adapter then starts no bridge, takes its tasks from the host's inbox and publishes into the host's
results through the same staged rename `result_publish` performs. Stdlib only.

    {
      "schema": "ag2-sparrow.supervised-host/1",
      "inbox": "/abs/deliveries/<worker>",      task-*.txt entries the host delivers
      "payloads": "/abs/tasks",                 optional: an inbox entry names <payloads>/<entry>
      "results": "/abs/results",                results/<task id>.txt, published whole
      "state": "/abs/state/<adapter>",          the adapter's own threads, done ledger, metrics
      "publish": "result_publish",              the one publish contract this release honours
      "stage_command": ["/abs/py", "..."],      optional: + --task-id <id> --stage pending|done
      "cwd": "/abs/dir"                         optional: owner turns' working directory
    }

Without `payloads`, an inbox entry is the task file itself. `stage_command` is the host's claim
record: `pending` runs before a task's turn (a failure defers the task) and `done` after its result
is published (a failure is logged). A task the adapter dies on stays `pending` for the host to recover.
"""
from __future__ import annotations

import json
import os
import subprocess

from . import result_publish

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

    def result_path(self, entry):
        return os.path.join(self.results, entry)

    def publish(self, entry, text):
        """Publish one result body whole, as result_publish does for every in-repo writer."""
        return result_publish.publish_text(self.result_path(entry), text)

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
