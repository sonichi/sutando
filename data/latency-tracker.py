#!/usr/bin/env python3
"""
Task latency tracker — measures time from task creation to result delivery.
Scans tasks/ and results/ directories, logs latency to data/latency.json.
Run: python3 data/latency-tracker.py
"""
import json, os, re
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).parent.parent
RESULTS = REPO / "results"
LATENCY_FILE = REPO / "data" / "latency.json"

def load_latency():
    if LATENCY_FILE.exists():
        return json.loads(LATENCY_FILE.read_text())
    return {"measurements": [], "stats": {}}

def measure_from_results():
    """Scan result files and extract latency from task timestamp vs file mtime."""
    data = load_latency()
    seen = {m["task_id"] for m in data["measurements"]}
    
    for f in RESULTS.glob("task-*.txt"):
        task_id = f.stem
        if task_id in seen:
            continue
        # Result file mtime = when result was written
        result_time = f.stat().st_mtime
        # Extract timestamp from task_id (epoch ms after "task-" prefix or from content)
        match = re.search(r'(\d{13})', task_id)
        if match:
            task_time = int(match.group(1)) / 1000
            latency_s = result_time - task_time
            if 0 < latency_s < 3600:  # sanity: under 1 hour
                data["measurements"].append({
                    "task_id": task_id,
                    "task_time": task_time,
                    "result_time": result_time,
                    "latency_s": round(latency_s, 1),
                })
    
    # Compute stats
    if data["measurements"]:
        latencies = [m["latency_s"] for m in data["measurements"]]
        data["stats"] = {
            "count": len(latencies),
            "mean_s": round(sum(latencies) / len(latencies), 1),
            "median_s": round(sorted(latencies)[len(latencies)//2], 1),
            "max_s": round(max(latencies), 1),
            "min_s": round(min(latencies), 1),
            "last_updated": datetime.now().isoformat(),
        }
    
    LATENCY_FILE.write_text(json.dumps(data, indent=2))
    return data["stats"]

if __name__ == "__main__":
    stats = measure_from_results()
    if stats:
        print(f"Latency: mean={stats['mean_s']}s median={stats['median_s']}s max={stats['max_s']}s ({stats['count']} tasks)")
    else:
        print("No measurements yet.")
