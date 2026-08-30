#!/usr/bin/env python3
"""Regression guard for task-time normalization and display formatting."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "src" / "web-client.ts").read_text()


def test_task_time_uses_one_normalized_formatter():
    assert "function formatTaskAge(time)" in SOURCE
    assert "Math.max(0, Math.floor((Date.now() - timestamp) / 1000))" in SOURCE
    assert SOURCE.count("formatTaskAge(t.time)") == 2
    assert "Math.round((Date.now() - t.time) / 1000)" not in SOURCE


def test_task_rendering_is_change_driven():
    assert "function taskRenderSignature()" in SOURCE
    assert "if (signature === lastTaskRenderSignature)" in SOURCE
    assert "if (signature === lastDynamicTaskRenderSignature)" in SOURCE
    assert "refreshTaskTimes(container);" in SOURCE


def test_opening_transcript_scrolls_to_latest_message():
    assert "if (!collapsed) requestAnimationFrame(() => scrollTranscript(true));" in SOURCE


if __name__ == "__main__":
    test_task_time_uses_one_normalized_formatter()
    print("web-client task-time tests passed")