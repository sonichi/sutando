#!/usr/bin/env python3
"""`docs/sutando-config.schema.json` must describe the post-gate form the
resolver actually executes — including the map, and its required `*`.

The schema is the editor autocomplete + validation contract
(`docs/workspace-config.md`), so a shape the runtime supports but the schema
rejects is reported invalid to whoever is writing the config. It declared
`discord_post_gate` as a bare string while `resolve_validator` accepted a
{channel_id: path} map, which is the drift this pins.

Each fixture asserts BOTH verdicts, then two cross-invariants over the table:
no schema-VALID config may refuse every send, and no config that gates
normally may be schema-INVALID. Those are the two ways the pair can lie.

Run: python3 tests/discord-post-gate-schema.test.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ.pop("SUTANDO_DISCORD_POST_GATE", None)

from channels.discord.post_gate import resolve_validator  # noqa: E402

SCHEMA = json.loads((REPO / "docs/sutando-config.schema.json").read_text())
GATE = SCHEMA["properties"]["bridges"]["properties"]["discord_post_gate"]

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


# Narrow on purpose: `jsonschema` is absent on this repo's runners, and an
# over-general validator written here would be the thing under test.
def valid(schema: dict, value) -> bool:
    if "oneOf" in schema:
        return sum(1 for s in schema["oneOf"] if valid(s, value)) == 1
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        py = {"string": str, "object": dict, "null": type(None)}
        if not any(isinstance(value, py[t]) for t in types if t in py):
            return False
        if "null" in types and value is None:
            return True
    if isinstance(value, str):
        pat = schema.get("pattern")
        return pat is None or re.search(pat, value) is not None
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                return False
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties")
        for k, v in value.items():
            sub = props.get(k, addl)
            if isinstance(sub, dict) and not valid(sub, v):
                return False
        return True
    return True


# The validator must be able to say NO, or every "valid" below is vacuous.
check("validator control: rejects an int", not valid(GATE, 7))
check("validator control: accepts a plain path", valid(GATE, "gates/all.py"))


# --- runtime outcome, measured through the real resolver -------------------
ALLOW = "def validate(channel_id, payload):\n    return None\n"


def runtime(value, tmp: Path) -> str:
    """'ungated' | 'gated' | 'refuse-all', from resolve_validator itself."""
    def materialize(v):
        if not isinstance(v, str) or not v.strip():
            return v
        f = tmp / f"p{abs(hash(v))}.py"
        f.write_text(ALLOW)
        return str(f)

    if isinstance(value, dict):
        cfg = {k: materialize(v) for k, v in value.items()}
    else:
        cfg = materialize(value)
    import sutando_config
    orig = sutando_config.load_config
    sutando_config.load_config = lambda root=None: {
        "bridges": {"discord_post_gate": cfg}}
    try:
        v = resolve_validator()
    finally:
        sutando_config.load_config = orig
    if v is None:
        return "ungated"
    probes = ["111", "222", "*"]
    if all(v(c, {"content": "x"}) for c in probes):
        return "refuse-all"
    return "gated"


CASES = [
    ("legacy single path", "gates/all.py", True, "gated"),
    ("map with only `*`", {"*": "gates/all.py"}, True, "gated"),
    ("map, id + `*`", {"111": "gates/dev.py", "*": "gates/all.py"},
     True, "gated"),
    ("map, null entry beside `*`", {"111": None, "*": "gates/all.py"},
     True, "gated"),
    ("map WITHOUT `*`", {"111": "gates/dev.py"}, False, "refuse-all"),
    ("empty map", {}, False, "refuse-all"),
    ("blank `*`", {"111": "gates/dev.py", "*": "   "}, False, "refuse-all"),
    ("blank string (legacy unconfigured)", "   ", True, "ungated"),
    ("empty string (legacy unconfigured)", "", True, "ungated"),
    ("wrong type", 7, False, "refuse-all"),
]

table = []
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    for label, value, want_valid, want_runtime in CASES:
        got_valid = valid(GATE, value)
        got_runtime = runtime(value, tmp)
        table.append((label, got_valid, got_runtime))
        check(f"schema {label}: valid={want_valid}", got_valid == want_valid)
        check(f"runtime {label}: {want_runtime}", got_runtime == want_runtime)

# The two ways schema and runtime can disagree. The second is the reported bug:
# `{"*": "gates/all.py"}` gated correctly while the schema called it invalid.
check("no schema-VALID config refuses every send",
      not [l for l, v, r in table if v and r == "refuse-all"])
# Widened after review: `gated` was too narrow. `ungated` is a SUPPORTED runtime
# outcome too, so rejecting it is the same schema/runtime drift, pointed the other way.
check("no runtime-supported config is schema-INVALID",
      not [l for l, v, r in table if not v and r != "refuse-all"])

# `*` is required by the schema, not merely mentioned in prose.
obj = [s for s in GATE["oneOf"] if s.get("type") == "object"]
check("schema declares exactly one object branch", len(obj) == 1)
check("object branch requires `*`", obj and obj[0].get("required") == ["*"])

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
