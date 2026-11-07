#!/usr/bin/env python3
"""Exercise receipt-sensitive adapter cases independently of quarantined timing suites."""
import os
import runpy
import sys
from pathlib import Path

if os.name == "nt":
    raise SystemExit(0)

TARGET = Path(__file__).resolve().parents[1] / "packages/ag2-sparrow/tests/test_hatch_adapter.py"
sys.argv = [str(TARGET),
    'HookScript.test_closed_item_leaves_the_woken_record',
    'HookScript.test_closed_and_own_tasks_do_not_wake',
    'TaskFields.test_unpaired_result_does_not_settle_or_hide_a_task',
    'TaskFields.test_closed_unreadable_and_unattested_tasks',
    'Keepalive.test_an_old_open_task_is_abnormal_despite_a_fresh_keepawake',
    'Keepalive.test_a_hold_whose_room_line_never_delivers_reports_stuck',
    'Reply.test_never_overwrites_an_existing_result',
    'Reply.test_a_reply_landing_after_the_check_is_never_replaced',
    'Hold.test_every_send_outcome_releases_the_lock_and_keeps_its_inode',
    'Hold.test_a_refused_hold_leaves_a_line_an_earlier_hold_queued',
    'Hold.test_the_drain_withdraws_a_hold_line_its_record_does_not_arm',
    'Hold.test_an_answered_item_is_not_held',
    'Hold.test_a_line_being_sent_is_not_queued_again',
    'Hold.test_a_claim_handed_back_mid_read_is_found_on_the_rescan',
    'Hold.test_an_answer_landing_mid_hold_is_never_followed_by_its_line',
]
runpy.run_path(str(TARGET), run_name="__main__")
