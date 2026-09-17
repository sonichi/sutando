#!/usr/bin/env python3
"""dispatch_task must bracket the AGENT path with the worker record.

`mark_done` had one reachable caller — the `--handler-runner` branch — so a
worker agent that reads its delivery, works and publishes a result produced no
done-flag, `_worker_of()` returned "", and no attribution ever left the host.

This pins the delegation, not end-to-end behaviour: driving the real watcher
needs fswatch and a live pool (see tests/watcher-stream.test.py for the harness
and its traps), so what is asserted here is that both stages are reachable from
the agent path at all, which is precisely what was missing.
"""
import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO / "src" / "watch-tasks-stream.sh").read_text()


def dispatch_task_body() -> str:
    m = re.search(r"^dispatch_task\(\) \{\n(.*?)^\}", SRC, re.S | re.M)
    assert m, "dispatch_task() not found — the test's anchor drifted, not the code"
    return m.group(1)


class AgentPathIsBracketed(unittest.TestCase):
    def test_pending_is_laid_before_the_work_is_dispatched(self):
        body = dispatch_task_body()
        self.assertIn("record_worker_done", body,
                      "dispatch_task never claims the task for a worker, so the agent "
                      "path produces no attribution")
        pend = body.index("record_worker_done")
        for emit in ("emit_dispatch_task_file", "queue_handler_task"):
            self.assertLess(pend, body.index(emit),
                            f"the claim must precede {emit}: a result the drain can "
                            "see must never exist without attribution beside it")

    def test_observing_the_answer_settles_the_record(self):
        body = dispatch_task_body()
        m = re.search(r"if handler_result_is_answer .*?\n(.*?)\n  fi", body, re.S)
        self.assertIsNotNone(m, "the already-answered guard moved")
        self.assertIn("settle_worker_record", m.group(1),
                      "on the agent path no handler runs, so the observed answer is the "
                      "only completion signal that can promote pending -> done")

    def test_the_claim_is_a_noop_off_a_worker(self):
        # The core calls dispatch_task too; the guard that keeps this inert lives
        # in record_worker_done, so assert it rather than trusting the call site.
        m = re.search(r"^record_worker_done\(\) \{\n(.*?)^\}", SRC, re.S | re.M)
        self.assertIsNotNone(m, "record_worker_done() not found")
        self.assertIn("SUTANDO_INSTANCE_ID", m.group(1))
        self.assertIn("SUTANDO_POOL_DELIVERY_SCRIPT", m.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
