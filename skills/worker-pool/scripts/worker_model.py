#!/usr/bin/env python3
"""The model a worker runs on, when the owner picked one for it.

`/model` in a worker's session saves the pick into the Claude folder it runs in, which is the core's
unless the worker has its own: the core and every other worker would start on it next. So a worker's
model is kept here instead (`state/workers/<worker id>/model`, one line) and handed to its CLI at
launch as `--model`; the pick takes effect when the worker is next started. No pick: the folder's own.

  worker_model.py get <workspace> <worker id>           # prints the pick, or nothing
  worker_model.py set <workspace> <worker id> <model>   # keeps it; an empty model drops the pick
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")   # an alias or id; it becomes a CLI argument


def path(workspace, worker_id: str) -> Path:
    if not _ID.match(str(worker_id or "")):
        raise ValueError("not a worker id")
    return Path(workspace) / "state" / "workers" / str(worker_id) / "model"


def get(workspace, worker_id: str) -> str:
    """The owner's pick for this worker, '' when there is none or it is not a model name."""
    try:
        said = path(workspace, worker_id).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""
    return said if _MODEL.match(said) else ""


def set(workspace, worker_id: str, model: str) -> str:  # noqa: A001 - the verb the command line names
    """Keep `model` as this worker's pick ('' drops it); returns what is now kept."""
    file, model = path(workspace, worker_id), str(model or "").strip()
    if not model:
        file.unlink(missing_ok=True)
        return ""
    if not _MODEL.match(model):
        raise ValueError("not a model name")
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(model + "\n", encoding="utf-8")
    os.replace(tmp, file)
    return model


def main(argv) -> int:
    try:
        if len(argv) == 4 and argv[1] == "get":
            out = get(argv[2], argv[3])
        elif len(argv) == 5 and argv[1] == "set":
            out = set(argv[2], argv[3], argv[4])
        else:
            print("usage: worker_model.py get <workspace> <worker id> | set <workspace> <worker id> <model>", file=sys.stderr)
            return 2
    except (ValueError, OSError) as e:
        print("worker_model: %s" % e, file=sys.stderr)
        return 1
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
