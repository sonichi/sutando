#!/usr/bin/env python3
"""Sibling plugin hooks are discovered only from explicit plugin directories."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-discord-sibling-skills-")
_discord_config = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_discord_config.mkdir(parents=True, exist_ok=True)
(_discord_config / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")
(_discord_config / ".env").write_text("DISCORD_BOT_TOKEN=test-stub-token\n", encoding="utf-8")
SOURCE = REPO / "src" / "discord-bridge.py"
tree = ast.parse(SOURCE.read_text())
loader_node = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "_load_plugin_message_hooks"
)

failures = []


def check(label, condition):
    if not condition:
        failures.append(label)


with tempfile.TemporaryDirectory(prefix="discord-sibling-skills-") as temp:
    base = Path(temp)
    host = base / "host"
    sibling = base / "sibling"
    skills = sibling / "skills"
    skill = skills / "sibling-probe"
    skill.mkdir(parents=True)
    (host / "src").mkdir(parents=True)
    marker = base / "imported"
    (skill / "manifest.json").write_text(json.dumps({
        "name": "sibling-probe",
        "enabled": True,
        "message_hook": {
            "module": "hook.py",
            "matches": "matches",
            "handle": "handle",
        },
    }))
    (skill / "hook.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['SIBLING_MARKER']).write_text('loaded')\n"
        "def matches(text): return False\n"
        "def handle(message, self_user_id): return ''\n"
    )

    namespace = {
        "Path": Path,
        "REPO": host,
        "__file__": str(host / "src" / "discord-bridge.py"),
        "json": json,
        "os": os,
        "sys": sys,
    }
    exec(compile(ast.Module(body=[loader_node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    load_hooks = namespace["_load_plugin_message_hooks"]
    previous_dirs = os.environ.pop("SUTANDO_EXTERNAL_PLUGIN_DIRS", None)
    previous_marker = os.environ.get("SIBLING_MARKER")
    os.environ["SIBLING_MARKER"] = str(marker)
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hooks = load_hooks()
        check("unlisted sibling hook did not import", not marker.exists())
        check("unlisted sibling warning emitted",
              "not loading" in output.getvalue() and "sibling" in output.getvalue())
        check("unlisted sibling hook absent", not hooks)

        os.environ["SUTANDO_EXTERNAL_PLUGIN_DIRS"] = str(skills)
        hooks = load_hooks()
        check("explicit sibling plugin imports", marker.exists())
        check("explicit sibling hook is returned", len(hooks) == 1)

        marker.unlink(missing_ok=True)
        os.environ["SUTANDO_EXTERNAL_PLUGIN_DIRS"] = str(sibling)
        hooks = load_hooks()
        # Module-level marker can't re-fire (sys.modules cache); the returned
        # hook is the proof the root's skills/ dir was scanned.
        check("checkout-root entry scans its skills dir", len(hooks) == 1)
    finally:
        if previous_dirs is None:
            os.environ.pop("SUTANDO_EXTERNAL_PLUGIN_DIRS", None)
        else:
            os.environ["SUTANDO_EXTERNAL_PLUGIN_DIRS"] = previous_dirs
        if previous_marker is None:
            os.environ.pop("SIBLING_MARKER", None)
        else:
            os.environ["SIBLING_MARKER"] = previous_marker

if failures:
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    raise SystemExit(1)
print("PASS: discord bridge sibling skills")
