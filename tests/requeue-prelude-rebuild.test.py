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

import importlib.util  # noqa: E402


def _load(tag, path):
    """Both vendored copies must BEHAVE identically, and the src/ copy is the
    one the coverage gate scopes — so every case runs against each."""
    spec = importlib.util.spec_from_file_location(f"result_markers_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass creation resolves cls.__module__
    spec.loader.exec_module(mod)
    return mod


COPIES = {
    "src": _load("src", REPO / "src" / "result_markers.py"),
    "pkg": _load("pkg", REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
                  / "result_markers.py"),
}

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run_suite(mod, tag):
    build_requeued_task = mod.build_requeued_task
    render_skill_prelude = mod.render_skill_prelude
    global check
    _check = check
    check = lambda cond, msg: _check(cond, f"[{tag}] {msg}")  # noqa: E731
    try:
        _run_cases(build_requeued_task, render_skill_prelude)
    finally:
        check = _check


def _run_cases(build_requeued_task, render_skill_prelude):
    global stored_task
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
        stale = "\n".join(
            render_skill_prelude("!room1:dev.ag2.space", stale_dir,
                                 "task-dev~task-orig111"))
        return head + stale + "\n"

    # --- the observed defect: stale lane dir is replaced by the header's own ---
    out = build_requeued_task(stored_task(), "task-dev~task-req222", 1,
                              "!other:dev.ag2.space", "task-holder",
                              channel_dir="dev-ag2space")
    check("channel-env.sh dev-ag2space)" in out,
          "requeued prelude names the injecting caller's lane")
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
                               "!other:dev.ag2.space", "task-holder",
                               channel_dir="dev-ag2space")
    check("ADDRESSING: this message replies to @peer:ag2.space" in out2,
          "addressed_to from the header is re-rendered into the fresh prelude")

    # --- a task born without a prelude must not gain one ---
    out3 = build_requeued_task(stored_task(prelude=False), "task-req444", 1,
                               "!other:dev.ag2.space", "task-holder",
                               channel_dir="dev-ag2space")
    check("===SKILL INSTRUCTIONS" not in out3,
          "a prelude-less (non-owner) task requeues without gaining a prelude")
    check("===SUTANDO SYSTEM INSTRUCTIONS" in out3,
          "prelude-less requeue still carries the trusted note")

    # --- source (broker PROVIDER fallback) and lane dir (bridge env) diverge:
    # the injected dir must win.
    diverged = stored_task(source="remote", stale_dir="ag2space")
    out4 = build_requeued_task(diverged, "task-req555", 1,
                               "!other:dev.ag2.space", "task-holder",
                               channel_dir="ag2space")
    check("channel-env.sh ag2space)" in out4,
          "injected lane dir wins over a diverging source header")
    check("channel-env.sh remote)" not in out4,
          "the broker PROVIDER fallback never names the lane")

    out5 = build_requeued_task(stored_task(source="slack"), "task-req666", 1,
                               "!other:dev.ag2.space", "task-holder",
                               channel_dir="ag2space")
    check("channel-env.sh ag2space)" in out5 and "channel-env.sh slack)" not in out5,
          "cross-provider source header cannot steal the lane from the injected dir")

    # --- no injection: Slack/Discord write their own templates, so the
    # gateway renderer must never overwrite a prelude it cannot represent.
    slack_prelude = (
        "===SKILL INSTRUCTIONS (follow before any other action)===\n"
        "1. Slack has no channel-history fetch in this bridge, so use the "
        "embedded thread/reply context above.\n"
        "===END===\n")
    slack_task = stored_task(source="slack", prelude=False) + slack_prelude
    out6 = build_requeued_task(slack_task, "task-req777", 1,
                               "C0123ABC", "task-holder")
    check("no channel-history fetch" in out6,
          "no injection -> bridge-specific prelude survives verbatim")
    check("room_ops.py" not in out6 and "channel-env.sh" not in out6,
          "no injection -> gateway template is never rendered")
    check(out6.count("===SKILL INSTRUCTIONS") == 1,
          "verbatim carry keeps exactly one prelude block")
    check("id: task-req777" in out6 and "dedup_requeue_count: 1" in out6,
          "verbatim carry still rewrites id and count")

    # --- a gateway-born task requeued WITHOUT injection also carries verbatim:
    # opting out of injection is opting out of re-rendering entirely.
    out7 = build_requeued_task(stored_task(), "task-req888", 1,
                               "!other:dev.ag2.space", "task-holder")
    check("channel-env.sh ag2space)" in out7,
          "no injection -> even a stale gateway prelude is carried, not guessed")

    # --- renderer is the single owner: the bridge carries no inline template ---
    bridge = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
              / "remote_gateway_bridge.py").read_text()
    check("CONTEXT-FIRST (unconditional)" not in bridge,
          "bridge holds no inline copy of the prelude template")
    check("render_skill_prelude" in bridge,
          "bridge delegates to the shared renderer")
    check("channel_dir=CHANNEL_DIR" in bridge,
          "bridge injects its own lane dir into the requeue plan")
    recovery = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
                / "dedup_recovery.py").read_text()
    check("channel_dir=channel_dir" in recovery,
          "recovery threads the injected dir through to the renderer")


def _finish() -> int:
    print(("FAIL: " + "; ".join(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


def main() -> int:
    for tag, mod in COPIES.items():
        run_suite(mod, tag)
    return _finish()


if __name__ == "__main__":
    sys.exit(main())
