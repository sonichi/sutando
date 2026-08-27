#!/usr/bin/env python3
"""Skill-usage telemetry — PostToolUse[Skill] hook.

Emits ONE anonymous ``feature_used {feature: "skill:<name>"}`` product-telemetry
event every time the core invokes a skill (the `Skill` tool). This is the
chokepoint that broadens feature-usage coverage from hand-instrumented scripts
(morning-briefing) to the *entire* skill surface without
touching each skill — the loop already runs dozens of skills (proactive-loop,
people-analysis, context-reconstruct, session-recap, task-orphan-check, …) and
none of them reported until now.

Why a hook, not per-skill calls: skills are markdown + scripts of every shape;
wiring `feature_used()` into each is unmaintainable and misses future skills. A
single PostToolUse matcher on the `Skill` tool captures all of them, for free,
the moment they run.

Privacy: sends ONLY the skill's short categorical name (prefixed ``skill:``).
Never the skill arguments, task content, prompts, or any PII — same contract as
`telemetry.feature_used`. Honors the same opt-out (DO_NOT_TRACK / telemetry-
disabled) because it routes through `telemetry.capture()`, which checks opt-out
on every call.

Fail-OPEN, ALWAYS. Telemetry must never break a tool: any error (bad stdin,
missing telemetry module, network) is swallowed and the hook exits 0 with no
output. A PostToolUse observability hook has no decision to make — it observes.

Registration: ``start-cli.sh`` passes this repo file's absolute path to
``build-core-settings.mjs``, which registers it under PostToolUse for the
``Skill`` matcher independently of the observability opt-in. Repo root is found
from this file's location, or via ``$SUTANDO_REPO_ROOT`` for tests.
"""
import sys
import os
import json


def _repo_src() -> str:
    """Locate the repo's src/ dir (telemetry.py lives there)."""
    override = os.environ.get("SUTANDO_REPO_ROOT")
    if override:
        return os.path.join(override, "src")
    # hooks/ is a sibling of src/ in the repo; from the deployed copy in
    # ~/.claude/hooks we fall back to $SUTANDO_REPO_ROOT (set at install).
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "src")


def main() -> int:
    # Fail-open around EVERYTHING — never let telemetry break the tool.
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)

        # Only the Skill tool is a "feature use" for this hook.
        if payload.get("tool_name") != "Skill":
            return 0

        tool_input = payload.get("tool_input") or {}
        skill = tool_input.get("skill")
        if not skill or not isinstance(skill, str):
            return 0
        # Guard against unbounded/garbage names getting into the property space.
        skill = skill.strip().lstrip("/")[:64]
        if not skill:
            return 0

        # Emit via a DETACHED subprocess, never in-process. This hook is
        # registered for EVERY Skill call, so it must return promptly: doing the
        # send here — even the bounded 1s flush path — would add a network RTT to
        # the tool run and compound across skill-heavy loops (CR #2254,
        # qingyun-wu). Instead we spawn `telemetry.py feature_used skill:<name>`
        # in its own session and DO NOT wait for it. The child (which the CLI
        # runs on the flush path, since it exits immediately) does the POST off
        # this hook's critical path; the hook forks-and-returns in ~ms.
        telemetry_py = os.path.join(_repo_src(), "telemetry.py")
        if not os.path.isfile(telemetry_py):
            return 0
        import subprocess
        subprocess.Popen(
            [sys.executable, telemetry_py, "feature_used", f"skill:{skill}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach: outlive this hook, don't get reaped with it
        )
    except Exception:
        # Swallow — observability must never surface an error to the tool run.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
