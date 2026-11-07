#!/usr/bin/env python3
"""Exercise receipt-sensitive adapter cases independently of quarantined timing suites."""
import os
import runpy
import sys
from pathlib import Path

if os.name == "nt":
    raise SystemExit(0)

TARGET = Path(__file__).resolve().parents[1] / "packages/ag2-sparrow/tests/test_claude_code_adapter.py"
sys.argv = [str(TARGET),
    'InboxCli.test_every_answer_in_the_harness_contract_is_what_the_real_verbs_answer',
    'InboxCli.test_a_mark_never_outlives_its_answer_even_when_the_task_file_is_gone',
    'InboxCli.test_an_unready_result_is_the_lifecycles_call_and_never_clears_a_mark',
]
runpy.run_path(str(TARGET), run_name="__main__")
