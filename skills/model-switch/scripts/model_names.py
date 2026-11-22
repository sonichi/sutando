#!/usr/bin/env python3
"""The one table from a model alias to the exact model a switch sets, and its display name.

switch-model.sh sends the id this table names for an alias, and a card labels its button with
the same entry, so the label and the switch cannot disagree. The table is the manifest's
MODEL_SWITCH_MODELS (`alias=id;...`), overridden by the same name in the environment.

    model_names.py resolve <alias>   <id>\\t<display>, exit 3 when the alias has no entry
    model_names.py table             {"<alias>": {"id": ..., "display": ...}} as JSON
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

MANIFEST = Path(__file__).resolve().parent.parent / "manifest.json"  # lint-workspace-resolution: allow-repo-root
_ID = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d+)*)$")


def _raw() -> str:
    if os.environ.get("MODEL_SWITCH_MODELS"):
        return os.environ["MODEL_SWITCH_MODELS"]
    try:
        return str(json.loads(MANIFEST.read_text(encoding="utf-8"))["config"]["MODEL_SWITCH_MODELS"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def display(model_id: str) -> Optional[str]:
    """`claude-opus-5-5` -> `Opus 5.5`, the name the CLI echoes when it accepts that id."""
    m = _ID.match(model_id or "")
    return f"{m.group(1).capitalize()} {m.group(2).replace('-', '.')}" if m else None


def table() -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for pair in _raw().split(";"):
        alias, _, model_id = pair.strip().partition("=")
        name = display(model_id.strip())
        if alias.strip() and name:
            out[alias.strip()] = {"id": model_id.strip(), "display": name}
    return out


def resolve(alias: str) -> Optional[Tuple[str, str]]:
    entry = table().get(alias)
    return (entry["id"], entry["display"]) if entry else None


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["table"]:
        print(json.dumps(table(), sort_keys=True))
        return 0
    if args[:1] == ["resolve"] and len(args) == 2:
        got = resolve(args[1])
        if got is None:
            return 3
        print(f"{got[0]}\t{got[1]}")
        return 0
    print(__doc__.strip().splitlines()[-2:][0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
