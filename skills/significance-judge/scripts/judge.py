#!/usr/bin/env python3
"""significance-judge — score life-replay events by spawning a local agent subagent.

Bridges sutando-life's significance step (its ``significance.local.json``
``agent_command``) to the host's local Sutando agent CLI. The judgment runs in
a FRESH child process of the agent CLI — a subagent — never inline in a core
session.

stdin (from the caller, one JSON object)::

    {
      "schema_version": 1,
      "instructions": "<the caller's task contract>",
      "events": [
        {"id", "ts", "source", "kind", "actor_id", "title", "detail",
         "place", "url"}
      ]
    }

stdout (ONLY, on success)::

    [{"event_id": "<an input event id>",
      "significance_score": 0.0-1.0,
      "reason": "<short non-empty string>"}]

Any input, subprocess, or output-validation failure exits non-zero with the
detail on stderr and NOTHING on stdout — the caller treats the response as
all-or-nothing, so a partial or unparseable judgment must never reach stdout.

Environment overrides:
  SIGNIFICANCE_JUDGE_CMD      argv (shell-split) for the agent CLI; the prompt
                              is written to its stdin. Default: claude -p
                              --output-format text
  SIGNIFICANCE_JUDGE_TIMEOUT  subprocess timeout in seconds (default 110 —
                              under the caller's default 120s budget)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

SCHEMA_VERSION = 1
DEFAULT_AGENT_CMD = ["claude", "-p", "--output-format", "text"]
DEFAULT_TIMEOUT_SECONDS = 110.0
MAX_EVENTS = 1000
MAX_FIELD_CHARS = 500
MAX_PROMPT_BYTES = 1_000_000
MAX_REASON_CHARS = 500
EVENT_FIELDS = ("id", "ts", "source", "kind", "actor_id", "title", "detail", "place", "url")

RUBRIC = (
    "Score each event's significance from 0 to 1 for the owner's life-replay "
    "timeline. Score HIGH: concrete impact on the project, cross-agent "
    "coordination moments, first-time events, owner decisions, and shipped "
    "milestones. Score LOW or omit entirely: routine chatter, status noise, "
    "heartbeats, and repetitive mechanical activity. "
    "Respond with ONLY a JSON array (no prose, no code fence) of objects "
    '{"event_id": <an input event id>, "significance_score": <number 0-1>, '
    '"reason": <short explanation>}. Never invent event ids.'
)

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n(.*)\n```$", re.DOTALL)


class JudgeError(Exception):
    pass


def fail(message: str):
    print(f"significance-judge: {message}", file=sys.stderr)
    sys.exit(1)


def read_request(raw: str) -> dict:
    """Validate the caller's stdin object; reject anything off-contract."""
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"stdin is not valid JSON: {exc}") from exc
    if not isinstance(request, dict):
        raise JudgeError("stdin must be a JSON object")
    if request.get("schema_version") != SCHEMA_VERSION:
        raise JudgeError(f"unsupported schema_version (expected {SCHEMA_VERSION})")
    events = request.get("events")
    if not isinstance(events, list):
        raise JudgeError("events must be a JSON array")
    rows = []
    for row in events:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise JudgeError("each event must be an object with a non-empty string id")
        rows.append(row)
    if not rows:
        raise JudgeError("events is empty — nothing to judge")
    instructions = request.get("instructions")
    return {
        "instructions": instructions if isinstance(instructions, str) else "",
        "events": rows,
    }


def project_events(events: list) -> list:
    """Bound the batch and truncate each field so the prompt stays sized."""
    projected = []
    for row in events[:MAX_EVENTS]:
        projected.append({
            key: str(row.get(key) or "")[:MAX_FIELD_CHARS] for key in EVENT_FIELDS
        })
    return projected


def build_prompt(instructions: str, events: list) -> str:
    """Compose the bounded judgment prompt; drop oldest events to fit the cap."""
    while True:
        prompt = "\n\n".join(part for part in (
            instructions.strip(),
            RUBRIC,
            "Events (newest first):\n" + json.dumps(events, ensure_ascii=False, indent=1),
        ) if part)
        if len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES or len(events) <= 1:
            return prompt
        events = events[: max(1, len(events) // 2)]


def agent_command() -> list:
    override = os.environ.get("SIGNIFICANCE_JUDGE_CMD", "").strip()
    command = shlex.split(override) if override else list(DEFAULT_AGENT_CMD)
    if not command or shutil.which(command[0]) is None:
        raise JudgeError(f"agent CLI not found on PATH: {command[0] if command else '(empty)'}")
    return command


def judge_timeout() -> float:
    raw = os.environ.get("SIGNIFICANCE_JUDGE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise JudgeError("SIGNIFICANCE_JUDGE_TIMEOUT must be a number of seconds") from exc
    if value <= 0:
        raise JudgeError("SIGNIFICANCE_JUDGE_TIMEOUT must be positive")
    return value


def spawn_subagent(command: list, prompt: str, timeout_seconds: float) -> str:
    """The child process IS the subagent — a fresh agent CLI run per judgment."""
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise JudgeError(f"subagent timed out after {timeout_seconds:g}s") from exc
    except OSError as exc:
        raise JudgeError(f"subagent could not be spawned: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:2000]
        raise JudgeError(f"subagent exited {result.returncode}: {detail}")
    return result.stdout


def parse_judgments(output: str, batch_ids: set) -> list:
    """Strict output contract; a code fence is unwrapped, everything else must parse."""
    text = output.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"subagent output is not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise JudgeError("subagent output must be a JSON array")
    judgments = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"event_id", "significance_score", "reason"}:
            raise JudgeError("each judgment must be {event_id, significance_score, reason}")
        event_id = row["event_id"]
        if not isinstance(event_id, str) or event_id not in batch_ids:
            raise JudgeError("judgment references an unknown event_id")
        if event_id in seen:
            raise JudgeError("duplicate event_id in subagent output")
        score = row["significance_score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise JudgeError("significance_score must be a number from 0 to 1")
        reason = row["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise JudgeError("reason must be a non-empty string")
        seen.add(event_id)
        judgments.append({
            "event_id": event_id,
            "significance_score": float(score),
            "reason": reason.strip()[:MAX_REASON_CHARS],
        })
    return judgments


def main() -> int:
    try:
        request = read_request(sys.stdin.read())
        events = project_events(request["events"])
        prompt = build_prompt(request["instructions"], events)
        command = agent_command()
        output = spawn_subagent(command, prompt, judge_timeout())
        judgments = parse_judgments(output, {row["id"] for row in events})
    except JudgeError as exc:
        fail(str(exc))
    json.dump(judgments, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
