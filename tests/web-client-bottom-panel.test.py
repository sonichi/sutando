import json
import subprocess
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "src" / "web-client.ts").read_text()


def run_probe() -> dict:
    start = SOURCE.index("const PERSIST_KEY_BOTTOM_PANEL")
    end = SOURCE.index("try { setBottomPanelCollapsed", start)
    browser_code = SOURCE[start:end]
    probe = r"""
const classes = new Set();
const panel = { classList: {
  toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
  contains(name) { return classes.has(name); },
}};
const button = { attributes: {}, textContent: '' };
button.setAttribute = (name, value) => { button.attributes[name] = value; };
const elements = {'bottom-panel': panel, 'btn-panel-toggle': button};
const values = {};
globalThis.$ = (id) => elements[id];
globalThis.localStorage = { setItem(key, value) { values[key] = value; }, getItem() { return null; } };
globalThis.requestAnimationFrame = (callback) => callback();
let scrolls = 0;
globalThis.scrollTranscript = (force) => { if (force) scrolls += 1; };
""" + browser_code + r"""
setBottomPanelCollapsed(true);
const collapsed = {hidden: classes.has('collapsed'), expanded: button.attributes['aria-expanded'], label: button.textContent, stored: values[PERSIST_KEY_BOTTOM_PANEL], scrolls};
setBottomPanelCollapsed(false);
process.stdout.write(JSON.stringify({collapsed, expanded: {hidden: classes.has('collapsed'), expanded: button.attributes['aria-expanded'], label: button.textContent, stored: values[PERSIST_KEY_BOTTOM_PANEL], scrolls}}));
"""
    result = subprocess.run(
        ["node", "-e", probe],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


result = run_probe()
check(result["collapsed"] == {
    "hidden": True,
    "expanded": "false",
    "label": "Show transcript",
    "stored": "1",
    "scrolls": 0,
}, "collapsing the panel updates its DOM state and persistence")
check(result["expanded"] == {
    "hidden": False,
    "expanded": "true",
    "label": "Hide transcript",
    "stored": "0",
    "scrolls": 1,
}, "expanding the panel restores the transcript and scrolls to the latest entry")
print("web-client bottom-panel tests passed")
