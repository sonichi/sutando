#!/usr/bin/env python3
"""Executed ring controls (#3499 review): the page's OWN JS draws the ring —
run it under node against fixture payloads and pin the arc, tick, and color.

Not a mirror: the JS under test is extracted verbatim from dashboard.py's
_QUOTA_SPARK_JS, so a drawing change that breaks these pins is a change to
the shipped code, not to a reimplementation.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _spark_js() -> str:
    src = (REPO / "src" / "dashboard.py").read_text()
    m = re.search(r'_QUOTA_SPARK_JS = """<script>\n(.*?)</script>"""', src, re.S)
    assert m, "spark JS block not found"
    return m.group(1)


def _run_ring(points, projected_end):
    payload = {"windows": {"5h": {"segments": [
        {"current": True, "points": points, "projected_end": projected_end}]},
        "7d": {"segments": []}}}
    harness = """
const payload = %s;
global.fetch = async () => ({ json: async () => payload });
const els = {};
for (const id of ['qs-5h','qs-7d','qr-5h','qr-7d'])
  els[id] = { innerHTML: '' };
global.document = { getElementById: id => els[id] || null };
%s
setTimeout(() => {
  console.log(JSON.stringify({ ring: els['qr-5h'].innerHTML, chart: els['qs-5h'].innerHTML }));
}, 50);
""" % (json.dumps(payload), _spark_js())
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> None:
    if shutil.which("node") is None:
        print("dashboard-ring-controls: SKIP (node unavailable)")
        return

    # under pace: usage 0.30 at elapsed 0.60 -> green arc, tick past the arc end
    under = _run_ring([{"x": 0.2, "y": 0.1}, {"x": 0.6, "y": 0.3}], 0.5)
    assert 'stroke="#4ecca3"' in under["ring"], "under-pace arc must be green"
    assert "<text" in under["ring"] and "30%" in under["ring"], "ring text shows usage pct"
    assert under["ring"].count("<line") == 1, "exactly one pace tick"

    # over pace: usage 0.80 at elapsed 0.50 -> red arc
    over = _run_ring([{"x": 0.5, "y": 0.8}], 1.6)
    assert 'stroke="#e94560"' in over["ring"], "over-pace arc must be red"

    # arc sweep is proportional to usage: end-angle for u=0.25 vs u=0.75 differ;
    # extract the arc path's end coordinates and check quadrant placement
    q1 = _run_ring([{"x": 0.9, "y": 0.25}], 0.28)
    q3 = _run_ring([{"x": 0.9, "y": 0.75}], 0.83)
    def arc_end(ring_html):
        m = re.search(r'A [\d.]+ [\d.]+ 0 \d 1 ([\d.-]+) ([\d.-]+)"', ring_html)
        assert m, "usage arc path missing"
        return float(m.group(1)), float(m.group(2))
    x25, y25 = arc_end(q1["ring"]); x75, y75 = arc_end(q3["ring"])
    # center is (22,22): u=0.25 ends at (44,22)-ish right; u=0.75 at (0,22)-ish left
    assert x25 > 22 > x75, f"sweep not proportional: 25%% ends x={x25}, 75%% ends x={x75}"

    # tick angle follows PACE not usage: same usage, different pace -> tick moves
    t1 = _run_ring([{"x": 0.3, "y": 0.2}], 0.6)
    t2 = _run_ring([{"x": 0.9, "y": 0.2}], 0.22)
    def tick_x(ring_html):
        m = re.search(r'<line x1="([\d.-]+)"', ring_html)
        return float(m.group(1))
    assert tick_x(t1["ring"]) != tick_x(t2["ring"]), "pace tick must track elapsed fraction"

    # The hydrated ring must degrade the SAME way the server tile does. The
    # server clamps at 999%+ and em-dashes non-finite or negative values.
    huge = _run_ring([{"x": 0.9, "y": 1e308}], 1.0)
    assert "Infinity%" not in huge["ring"], "hydrated ring must not render Infinity%"
    assert "999%+" in huge["ring"], f"huge usage must clamp like the server: {huge['ring'][-90:]}"

    for bad, why in ((float("nan"), "NaN"), (float("-inf"), "-inf"), (-0.5, "negative")):
        r = _run_ring([{"x": 0.9, "y": bad}], 1.0)
        assert "NaN" not in r["ring"], f"{why} leaked NaN into the ring: {r['ring'][-90:]}"
        assert "\u2014" in r["ring"], f"{why} must render an em dash like the server tile"

    # Fractional parity with the server tile. Every other fixture here is
    # invariant under truncation vs rounding, so none of them can fail.
    import dashboard
    for u in (0.999, 0.9999, 0.307, 0.5, 0.019):
        ring = _run_ring([{"x": 0.9, "y": u}], 1.0)["ring"]
        want = dashboard._quota_tile_pct({"utilization_5h": u}, "5h")
        assert f">{want}<" in ring, (
            f"u={u}: hydrated ring must render {want} like _quota_tile_pct; "
            f"got {ring[-90:]}")

    print("dashboard-ring-controls: PASS — arc/tick/color executed via the page's own JS")


if __name__ == "__main__":
    main()
