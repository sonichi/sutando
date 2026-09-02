#!/usr/bin/env python3
"""The Signal Room lane alone carries codex's network allowance (its image-generation
wrapper calls a provider); every other lane's delegation text is unchanged."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from policy.guardrail import sandboxed_delegation_lines, sandboxed_delegation_text  # noqa: E402
from signal_room_tasks import delegation_lines, worker_output_root  # noqa: E402

FLAG = "-c sandbox_workspace_write.network_access=true"


def main() -> int:
    block = "\n".join(delegation_lines("task-signal-abc", "/tmp/results"))
    assert FLAG in block, "Signal Room delegation must carry the network allowance"
    assert "NETWORK access solely" in block and "NO NETWORK" not in block
    root = worker_output_root("/tmp/results", "task-signal-abc")
    assert "--sandbox workspace-write" in block and f"-C {root}" in block
    default = sandboxed_delegation_text()
    assert FLAG not in default and "NO NETWORK" in default, "the read-only default is unchanged"
    other = "\n".join(sandboxed_delegation_lines("Slack", "GUEST tier", "results/x.txt", "scope"))
    assert FLAG not in other and "NO NETWORK" in other, "other lanes never gain network"
    print("PASS — Signal Room network allowance is lane-scoped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
