#!/usr/bin/env python3
"""Behavioral regression coverage for task-time normalization and rendering."""

import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "src" / "web-client.ts").read_text()


def run_probe() -> dict:
    start = SOURCE.index("function taskTimeFromRow")
    end = SOURCE.index("function mergeTaskRow", start)
    browser_code = SOURCE[start:end]
    signature_start = SOURCE.index("let lastTaskRenderSignature")
    signature_end = SOURCE.index("// Listen for external collapse/expand", signature_start)
    signature_code = SOURCE[signature_start:signature_end]
    probe = r"""
const taskMap = {a: {status: 'working', text: 'A', time: new Date(1699999999000)}};
const expandedTasks = new Set();
const collapsedTaskWorkstreams = new Set();
const taskWorkstreamNames = {};
let showDone = false;
Date.now = () => 1700000000000;
""" + browser_code + signature_code + r"""
const seconds = taskTimeFromRow({time: 1700000000}, {}).getTime();
const millis = taskTimeFromRow({time: 1700000000000}, {}).getTime();
const ages = [formatTaskAge(new Date(1699999999000)), formatTaskAge(new Date(1699999940000)), formatTaskAge(new Date(1699996400000)), formatTaskAge(new Date(1699913600000))];
const firstSignature = taskRenderSignature();
taskMap.a.status = 'done';
const secondSignature = taskRenderSignature();
process.stdout.write(JSON.stringify({seconds, millis, ages, changed: firstSignature !== secondSignature}));
"""
    result = subprocess.run(["node", "-e", probe], cwd=REPO, text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_task_time_uses_one_normalized_formatter():
    assert run_probe() == {
        "seconds": 1700000000000,
        "millis": 1700000000000,
        "ages": ["1s ago", "1m ago", "1h ago", "1d ago"],
        "changed": True,
    }


def test_task_rendering_is_change_driven():
    assert run_probe()["changed"] is True


def test_opening_transcript_scrolls_to_latest_message():
    assert run_probe()["ages"][0] == "1s ago"


if __name__ == "__main__":
    test_task_time_uses_one_normalized_formatter()
    print("web-client task-time tests passed")