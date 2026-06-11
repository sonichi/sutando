"""Roll-up + Telegram reporting for the voice-agent test harness.

Aggregation (pass rate, p50/p95 latency, clarity mean) is real. Delivery writes
a result file the existing bridge polls (results/proactive-*.txt), so the report
reaches the owner's Telegram with no new transport.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def summarize(tests: list[dict]) -> dict:
    """Suite roll-up over per-test rows."""
    hard = [t for t in tests if not t.get("soft")]
    passed = sum(1 for t in hard if t.get("accuracy") == "pass")
    latencies = [t["latency_ms"] for t in tests if t.get("latency_ms") is not None]
    clarities = [t["clarity"] for t in tests if t.get("clarity") is not None]
    no_resp = sum(1 for t in tests if t.get("no_response"))
    return {
        "pass": passed,
        "hard_total": len(hard),
        "total": len(tests),
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "clarity_mean": round(sum(clarities) / len(clarities), 2) if clarities else None,
        "no_response": no_resp,
    }


def _fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "n/a"
    return f"{ms/1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def render(run: dict, regressions: list[str], date: str) -> str:
    s = run["summary"]
    head = (
        f"🎙️ Voice suite — {date}\n"
        f"Pass {s['pass']}/{s['hard_total']} · "
        f"p50 {_fmt_ms(s['p50_latency_ms'])} · p95 {_fmt_ms(s['p95_latency_ms'])} · "
        f"clarity {s['clarity_mean']}/5"
    )
    if s.get("no_response"):
        head += f" · ⛔ {s['no_response']} no-response"
    real = [r for r in regressions if not r.startswith("(")]
    if not real:
        body = "\n✅ No regressions vs baseline."
    else:
        body = f"\n⚠️ Regressions ({len(real)}):\n" + "\n".join(f"  • {r}" for r in real)
    return head + body + f"\nFull: results/voice-test/{date}.json"


def deliver(text: str, workspace: str | None = None) -> str:
    """Write a result file the bridge delivers to Telegram. Returns the path."""
    ws = workspace or os.environ.get("SUTANDO_WORKSPACE") or os.path.expanduser("~/.sutando/workspace")
    results = Path(ws) / "results"
    results.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    path = results / f"proactive-{ts}.txt"
    path.write_text(text)
    return str(path)


if __name__ == "__main__":  # manual: re-render + deliver an existing run file
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("run_json")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--deliver", action="store_true")
    args = ap.parse_args()
    run = json.loads(Path(args.run_json).read_text())
    import baseline as bl
    regr = bl.diff(run, bl.load(bl.BASELINE_PATH))
    msg = render(run, regr, args.date)
    print(msg)
    if args.deliver:
        print("→", deliver(msg))
