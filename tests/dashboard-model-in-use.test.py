#!/usr/bin/env python3
"""The Quota tile names the model in use, from the proxy's `last_request` stamp.

Owner ask (2026-09-03, repeated 2026-09-04): display the model in use next to
the quota in the dashboard. The source is what the credential proxy saw on the
wire — a launch-time marker goes stale on a mid-session /model switch. Absence
renders as an honest "model —", never as a guess.
"""
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dash", REPO / "src" / "dashboard.py")
dash = importlib.util.module_from_spec(spec)
sys.modules["dash"] = dash
spec.loader.exec_module(dash)

fails = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + label + ("" if cond else f" — {detail}"))
    if not cond:
        fails.append(label)


def render_with(quota):
    real = dash.get_system_stats
    dash.get_system_stats = lambda: {"disk_free": "53GB", "battery": "42%", "charging": False, "quota": quota}
    try:
        return dash.render_dashboard()
    finally:
        dash.get_system_stats = real


def quota_tile(html):
    i = html.index(">Quota<br>")
    return html[i:html.index("</div></div>", i)]


LIVE = {"available": True, "age_h": 0.1, "stale": False,
        "headers": {"anthropic-ratelimit-unified-5h-utilization": "0.13"},
        "last_request": {"model": "claude-fable-5-1", "at": "2026-09-04T16:20:08Z"}}

check("L1 helper returns the stamped model", dash._quota_model_label(LIVE) == "claude-fable-5-1")
tile = quota_tile(render_with(LIVE))
check("L2 the Quota tile shows the model", "claude-fable-5-1" in tile, tile)
check("L3 the age label still follows it", "ago" in tile or "no data" in tile or "h old" in tile, tile)

for label, quota in (
    ("A1 no stamp (proxy predates it)", {**LIVE, "last_request": None}),
    ("A2 stamp key absent", {k: v for k, v in LIVE.items() if k != "last_request"}),
    ("A3 malformed stamp", {**LIVE, "last_request": "claude-x"}),
    ("A4 empty model", {**LIVE, "last_request": {"model": "  ", "at": "T"}}),
):
    check(f"{label} renders 'model —'", dash._quota_model_label(quota) == "model —", dash._quota_model_label(quota))

tile = quota_tile(render_with({**LIVE, "last_request": None}))
check("A5 absence is visible in the tile, not a blank", "model —" in tile, tile)
check("A6 absence never borrows a model from elsewhere", "claude-" not in tile, tile)

# --- the value is attacker-reachable: any local caller can put markup in a request body
EVIL = {**LIVE, "last_request": {"model": "<img src=x onerror=alert(1)>", "at": "T"}}
tile = quota_tile(render_with(EVIL))
check("S1 markup in the model is escaped at the sink", "&lt;img src=x onerror=alert(1)&gt;" in tile, tile)
check("S2 ...and never rendered raw", "<img src=x" not in tile, tile)
LONG = {**LIVE, "last_request": {"model": "claude-" + "x" * 500, "at": "T"}}
check("S3 a pathological model string is capped", len(dash._quota_model_label(LONG)) <= 64, str(len(dash._quota_model_label(LONG))))

print()
if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails)); sys.exit(1)
print("all checks pass")
