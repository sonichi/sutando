#!/usr/bin/env python3
"""A dedup requeue re-renders the SKILL INSTRUCTIONS prelude; it never copies it.

The prelude names the task's credential lane and result path. Copying it
verbatim resurrects whatever text the task was BORN with — after any
prelude-affecting fix, a requeue keeps instructing handlers with superseded
text (issue #3613; observed live: a `source: dev-ag2space` task requeued
post-#3610 still said `channel-env.sh ag2space`). The template now has one
owner (result_markers.render_skill_prelude); build_requeued_task strips the
stored block and re-renders from the task's own header, where `source:` names
the lane (lane-authoritative stamping).

Run: python3 tests/requeue-prelude-rebuild.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.result_markers import (  # noqa: E402
    build_requeued_task,
    render_skill_prelude,
)

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def stored_task(source="dev-ag2space", stale_dir="ag2space", prelude=True):
    head = (
        "id: task-dev~task-orig111\n"
        "task: hello\n"
        f"source: {source}\n"
        "channel_id: !room1:dev.ag2.space\n"
        "user_id: @u:dev.ag2.space\n"
        "access_tier: owner\n"
    )
    if not prelude:
        return head
    # The BIRTH prelude, rendered with a superseded channel dir — the #3613 shape.
    stale = "\n".join(
        render_skill_prelude("!room1:dev.ag2.space", stale_dir, "task-dev~task-orig111")
    )
    return head + stale + "\n"


def main() -> int:
    # --- the observed defect: stale lane dir is replaced by the header's own ---
    out = build_requeued_task(stored_task(), "task-dev~task-req222", 1,
                              "!other:dev.ag2.space", "task-holder")
    check("channel-env.sh dev-ag2space)" in out,
          "requeued prelude names the task's OWN lane (from source:)")
    check("channel-env.sh ag2space)" not in out,
          "the birth prelude's superseded lane dir is gone")
    check(out.count("===SKILL INSTRUCTIONS") == 1,
          "exactly one prelude block after requeue")
    check("results/task-dev~task-req222.txt" in out,
          "re-rendered prelude binds the NEW task id's result path")
    check("results/task-dev~task-orig111.txt" not in out,
          "the old id's result path does not survive")
    check("===SUTANDO SYSTEM INSTRUCTIONS" in out,
          "the trusted requeue note is still appended")
    check("dedup_requeue_count: 1" in out.split("===SKILL INSTRUCTIONS")[0],
          "requeue count stays in the header region, before the prelude")

    # --- addressed_to survives the re-render ---
    addressed = stored_task().replace(
        "user_id:", "addressed_to: @peer:ag2.space\nuser_id:")
    out2 = build_requeued_task(addressed, "task-dev~task-req333", 1,
                               "!other:dev.ag2.space", "task-holder")
    check("ADDRESSING: this message replies to @peer:ag2.space" in out2,
          "addressed_to from the header is re-rendered into the fresh prelude")

    # --- a task born without a prelude must not gain one ---
    out3 = build_requeued_task(stored_task(prelude=False), "task-req444", 1,
                               "!other:dev.ag2.space", "task-holder")
    check("===SKILL INSTRUCTIONS" not in out3,
          "a prelude-less (non-owner) task requeues without gaining a prelude")
    check("===SUTANDO SYSTEM INSTRUCTIONS" in out3,
          "prelude-less requeue still carries the trusted note")

    # --- renderer is the single owner: the bridge carries no inline template ---
    bridge = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
              / "remote_gateway_bridge.py").read_text()
    check("CONTEXT-FIRST (unconditional)" not in bridge,
          "bridge holds no inline copy of the prelude template")
    check("render_skill_prelude" in bridge,
          "bridge delegates to the shared renderer")

    print(("FAIL: " + "; ".join(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
