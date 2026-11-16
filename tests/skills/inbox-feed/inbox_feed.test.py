#!/usr/bin/env python3
"""skills/inbox-feed/scripts/inbox_feed.py: mapping, priority rules, dedupe, Inbox-room
selection and the digest text — the deterministic half of the inbox-feed skill (the
live composio_exec / room.event.send calls are the agent's own, not scripted here;
see SKILL.md). Stdlib only, no network.

Run: python3 tests/skills/inbox-feed/inbox_feed.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills" / "inbox-feed" / "scripts"))
sys.path.insert(0, str(REPO / "src"))
import inbox_feed as ib  # noqa: E402

# A real GitHub search hit (ag2-space/cinny-webclient#4402, review-requested:@me),
# trimmed to the fields the mapper reads.
GITHUB_REVIEW_REQUEST_FIXTURE = {
    "number": 4402,
    "title": "fix(timeline): a forward page keeps the rows the back prefetch would fetch again (V254)",
    "html_url": "https://github.com/ag2-space/cinny-webclient/pull/4402",
    "state": "open",
    "created_at": "2026-11-16T04:27:46Z",
    "updated_at": "2026-11-16T07:21:08Z",
    "repository_url": "https://api.github.com/repos/ag2-space/cinny-webclient",
    "pull_request": {"merged_at": None},
    "user": {
        "login": "liususan091219",
        "avatar_url": "https://avatars.githubusercontent.com/u/5702457?v=4",
    },
}

GITHUB_MERGED_PR_FIXTURE = {
    **GITHUB_REVIEW_REQUEST_FIXTURE,
    "number": 4403,
    "state": "closed",
    "pull_request": {"merged_at": "2026-11-16T08:00:00Z"},
}

GMAIL_UNREAD_FIXTURE = {
    "thread_id": "18c9f1a2b3d4e5f6",
    "from_email": "mark@example.com",
    "from_name": "Mark",
    "subject": "Re: scheduler promotion — hold until I'm back",
    "snippet": "quick note before you merge...",
    "last_message_ts": 1795000000000,  # ms epoch
    "is_unread": True,
}

SLACK_DM_FIXTURE = {
    "channel": "D0123456",
    "ts": "1795000000.000100",
    "user_id": "U0ABCDEF",
    "user_name": "chi",
    "text": "can you take a look at the release PR?",
    "permalink": "https://ag2.slack.com/archives/D0123456/p1795000000000100",
}


class RulesTest(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self.assertEqual(ib.load_rules(ws), ib.DEFAULT_RULES)

    def test_init_writes_defaults_once_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self.assertTrue(ib.init_rules(ws))
            self.assertFalse(ib.init_rules(ws))  # second call: already there
            self.assertEqual(ib.load_rules(ws), ib.DEFAULT_RULES)

    def test_a_bad_rules_file_still_falls_back_rather_than_breaking_polling(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ib.rules_path(ws).write_text("not json")
            self.assertEqual(ib.load_rules(ws), ib.DEFAULT_RULES)

    def test_classify_priority_first_match_wins_in_order(self):
        rules = [
            {"app": "github", "kind": "mentioned", "priority": "needs_you"},
            {"app": "*", "kind": "*", "priority": "fyi"},
        ]
        self.assertEqual(ib.classify_priority("github", "mentioned", rules), "needs_you")
        self.assertEqual(ib.classify_priority("github", "pr_merged", rules), "fyi")
        self.assertEqual(ib.classify_priority("slack", "dm", rules), "fyi")

    def test_classify_priority_with_no_matching_rule_at_all_defaults_fyi(self):
        self.assertEqual(ib.classify_priority("linear", "issue_assigned", []), "fyi")

    def test_editing_the_workspace_file_changes_classification_without_code(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ib.init_rules(ws)
            rules = json.loads(ib.rules_path(ws).read_text())
            rules.insert(0, {"app": "github", "kind": "mentioned", "priority": "needs_you"})
            ib.rules_path(ws).write_text(json.dumps(rules))
            self.assertEqual(ib.classify_priority("github", "mentioned", ib.load_rules(ws)), "needs_you")


class MapGithubTest(unittest.TestCase):
    def test_review_requested_maps_every_field(self):
        item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
        self.assertEqual(item["schema_version"], 1)
        self.assertEqual(item["external_id"], "github:ag2-space/cinny-webclient#4402")
        self.assertEqual(item["id"], "github:ag2-space/cinny-webclient#4402:review_requested")
        self.assertEqual(item["source"], {"app": "github"})
        self.assertEqual(item["actor"]["id"], "liususan091219")
        self.assertEqual(item["actor"]["avatar_url"], GITHUB_REVIEW_REQUEST_FIXTURE["user"]["avatar_url"])
        self.assertEqual(item["what"]["kind"], "review_requested")
        self.assertIn("forward page", item["what"]["summary"])
        self.assertEqual(item["link"], GITHUB_REVIEW_REQUEST_FIXTURE["html_url"])
        self.assertEqual(item["priority"], "needs_you")
        self.assertEqual(item["thread_key"], item["link"])
        self.assertEqual(item["created_at"], "2026-11-16T04:27:46Z")
        self.assertEqual(item["state"], "open")

    def test_merged_pr_is_state_resolved_and_priority_fyi(self):
        item = ib.map_github(GITHUB_MERGED_PR_FIXTURE, "pr_merged", ib.DEFAULT_RULES)
        self.assertEqual(item["state"], "resolved")
        self.assertEqual(item["priority"], "fyi")

    def test_account_is_included_only_when_given(self):
        bare = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
        self.assertNotIn("account", bare["source"])
        tagged = ib.map_github(
            GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES, account="qingyun"
        )
        self.assertEqual(tagged["source"]["account"], "qingyun")

    def test_long_title_is_clamped_to_one_line(self):
        raw = {**GITHUB_REVIEW_REQUEST_FIXTURE, "title": "x" * 500}
        item = ib.map_github(raw, "mentioned", ib.DEFAULT_RULES)
        self.assertLessEqual(len(item["what"]["summary"]), ib.SUMMARY_MAX)
        self.assertTrue(item["what"]["summary"].endswith("…"))


class MapGmailTest(unittest.TestCase):
    def test_unread_thread_maps_needs_you_by_default_rules(self):
        item = ib.map_gmail(GMAIL_UNREAD_FIXTURE, "thread_needs_reply", ib.DEFAULT_RULES)
        self.assertEqual(item["external_id"], "gmail:18c9f1a2b3d4e5f6")
        self.assertEqual(item["id"], "gmail:18c9f1a2b3d4e5f6:thread_needs_reply")
        self.assertEqual(item["source"], {"app": "gmail"})
        self.assertEqual(item["actor"]["display_name"], "Mark")
        self.assertIn("scheduler promotion", item["what"]["summary"])
        self.assertEqual(item["priority"], "needs_you")
        self.assertEqual(item["state"], "open")
        self.assertTrue(item["created_at"].startswith("2026-"))

    def test_read_thread_state_is_read(self):
        raw = {**GMAIL_UNREAD_FIXTURE, "is_unread": False}
        item = ib.map_gmail(raw, "new_mail", ib.DEFAULT_RULES)
        self.assertEqual(item["state"], "read")
        self.assertEqual(item["priority"], "fyi")

    def test_missing_from_name_falls_back_to_email(self):
        raw = {**GMAIL_UNREAD_FIXTURE, "from_name": None}
        item = ib.map_gmail(raw, "new_mail", ib.DEFAULT_RULES)
        self.assertEqual(item["actor"]["display_name"], "mark@example.com")


class MapSlackTest(unittest.TestCase):
    def test_dm_maps_needs_you(self):
        item = ib.map_slack(SLACK_DM_FIXTURE, "dm", ib.DEFAULT_RULES)
        self.assertEqual(item["external_id"], "slack:D0123456:1795000000.000100")
        self.assertEqual(item["id"], "slack:D0123456:1795000000.000100:dm")
        self.assertEqual(item["thread_key"], "slack:D0123456:1795000000.000100")
        self.assertEqual(item["actor"]["display_name"], "chi")
        self.assertEqual(item["priority"], "needs_you")
        self.assertEqual(item["link"], SLACK_DM_FIXTURE["permalink"])

    def test_threaded_message_groups_under_thread_ts_not_its_own_ts(self):
        raw = {**SLACK_DM_FIXTURE, "ts": "1795000005.000200", "thread_ts": "1795000000.000100"}
        item = ib.map_slack(raw, "mention", ib.DEFAULT_RULES)
        # Groups with the thread root (not this message's own ts)...
        self.assertEqual(item["thread_key"], "slack:D0123456:1795000000.000100")
        # ...but its own identity is still its own message, not the thread root's.
        self.assertEqual(item["external_id"], "slack:D0123456:1795000005.000200")

    def test_channel_activity_without_mention_is_fyi(self):
        item = ib.map_slack(SLACK_DM_FIXTURE, "channel_activity", ib.DEFAULT_RULES)
        self.assertEqual(item["priority"], "fyi")


class DedupeTest(unittest.TestCase):
    def test_new_item_passes_through_once(self):
        item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
        self.assertEqual(ib.dedupe_new([item], {}), [item])

    def test_dedupe_cli_never_writes_anything_itself(self):
        # dedupe has no --live at all any more: selection and delivery are split
        # (review P2 — marking "sent" must happen only after a confirmed send).
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            items_file = ws / "items.json"
            items_file.write_text(json.dumps([item]))
            ib.main(["--workspace", str(ws), "dedupe", "--file", str(items_file)])
            self.assertFalse(ib.sent_ids_path(ws).exists())

    def test_already_sent_same_revision_is_dropped(self):
        item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
        sent_ids = {item["id"]: {"seen_at": "2026-11-15T00:00:00Z", "revision": item["updated_at"]}}
        self.assertEqual(ib.dedupe_new([item], sent_ids), [])

    def test_a_newer_revision_of_an_already_sent_id_resurfaces(self):
        # A later reply/status-change on the SAME (object, kind) must not be
        # permanently swallowed just because an earlier revision was already sent.
        item_v1 = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
        later_raw = {**GITHUB_REVIEW_REQUEST_FIXTURE, "updated_at": "2026-11-17T00:00:00Z"}
        item_v2 = ib.map_github(later_raw, "review_requested", ib.DEFAULT_RULES)
        sent_ids = {item_v1["id"]: {"seen_at": "2026-11-16T00:00:00Z", "revision": item_v1["updated_at"]}}
        self.assertEqual(ib.dedupe_new([item_v2], sent_ids), [item_v2])

    def test_a_different_kind_on_the_same_object_is_not_a_duplicate(self):
        # The exact review repro: review_requested already sent must not swallow a
        # later checks_failed on the SAME PR — they are different facts.
        pr_raw = GITHUB_REVIEW_REQUEST_FIXTURE
        review_requested = ib.map_github(pr_raw, "review_requested", ib.DEFAULT_RULES)
        later_raw = {**pr_raw, "updated_at": "2026-11-17T00:00:00Z"}
        checks_failed = ib.map_github(later_raw, "checks_failed", ib.DEFAULT_RULES)
        self.assertNotEqual(review_requested["id"], checks_failed["id"])
        self.assertEqual(review_requested["thread_key"], checks_failed["thread_key"])
        sent_ids = {
            review_requested["id"]: {
                "seen_at": "2026-11-16T00:00:00Z",
                "revision": review_requested["updated_at"],
            }
        }
        self.assertEqual(ib.dedupe_new([checks_failed], sent_ids), [checks_failed])

    def test_two_hits_sharing_an_id_in_one_batch_collapse_to_the_newest(self):
        pr_raw = GITHUB_REVIEW_REQUEST_FIXTURE
        stale = ib.map_github(pr_raw, "review_requested", ib.DEFAULT_RULES)
        fresh_raw = {**pr_raw, "updated_at": "2026-11-18T00:00:00Z", "title": "retitled"}
        fresh = ib.map_github(fresh_raw, "review_requested", ib.DEFAULT_RULES)
        new_items = ib.dedupe_new([stale, fresh], {})
        self.assertEqual(len(new_items), 1)
        self.assertEqual(new_items[0]["updated_at"], "2026-11-18T00:00:00Z")

    def test_prune_drops_entries_older_than_the_window(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        old = (now - timedelta(days=ib.SENT_IDS_MAX_AGE_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sent_ids = {"old-id": {"seen_at": old, "revision": "x"}, "fresh-id": {"seen_at": fresh, "revision": "x"}}
        pruned = ib.prune_sent_ids(sent_ids, now=now)
        self.assertEqual(set(pruned), {"fresh-id"})

    def test_a_legacy_bare_timestamp_entry_is_tolerated_and_resurfaces_once(self):
        # A legacy bare-string entry must not crash load/prune; it resurfaces once
        # (revision "" < any real updated_at), then self-heals into the new shape.
        item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
        legacy_sent_ids = {item["id"]: "2026-11-15T00:00:00Z"}
        self.assertEqual(ib.dedupe_new([item], legacy_sent_ids), [item])


class MarkSentTest(unittest.TestCase):
    def test_record_sent_is_pure_and_only_covers_what_it_is_given(self):
        item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
        updated = ib.record_sent({}, [item])
        self.assertIn(item["id"], updated)
        self.assertEqual(updated[item["id"]]["revision"], item["updated_at"])

    def test_selection_alone_never_marks_anything_sent_the_review_p2_repro(self):
        # Exact review repro: dedupe --live used to mark "sent" with zero sends
        # in between. Now dedupe writes nothing; only mark-sent does.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            items_file = ws / "items.json"
            items_file.write_text(json.dumps([item]))
            ib.main(["--workspace", str(ws), "dedupe", "--file", str(items_file)])
            # No room.event.send ever happened (nothing here ever calls it). A retry
            # must still see the item as new.
            retried = ib.dedupe_new([item], ib.load_sent_ids(ws))
            self.assertEqual(retried, [item])

    def test_mark_sent_dry_run_does_not_persist(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            items_file = ws / "items.json"
            items_file.write_text(json.dumps([item]))
            ib.main(["--workspace", str(ws), "mark-sent", "--file", str(items_file)])
            self.assertFalse(ib.sent_ids_path(ws).exists())

    def test_mark_sent_live_persists_and_then_dedupe_drops_it(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            items_file = ws / "items.json"
            items_file.write_text(json.dumps([item]))
            ib.main(["--workspace", str(ws), "mark-sent", "--file", str(items_file), "--live"])
            saved = json.loads(ib.sent_ids_path(ws).read_text())
            self.assertIn(item["id"], saved["ids"])
            self.assertEqual(ib.dedupe_new([item], ib.load_sent_ids(ws)), [])

    def test_an_unconfirmed_item_left_out_of_mark_sent_stays_eligible(self):
        # Simulating a partial batch: only one of two sent items is confirmed.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            sent_ok = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            unconfirmed = ib.map_github(GITHUB_MERGED_PR_FIXTURE, "pr_merged", ib.DEFAULT_RULES)
            confirmed_file = ws / "confirmed.json"
            confirmed_file.write_text(json.dumps([sent_ok]))  # unconfirmed deliberately left out
            ib.main(["--workspace", str(ws), "mark-sent", "--file", str(confirmed_file), "--live"])
            remaining = ib.dedupe_new([sent_ok, unconfirmed], ib.load_sent_ids(ws))
            self.assertEqual(remaining, [unconfirmed])


class FindInboxRoomTest(unittest.TestCase):
    OWNER = "@qingyun:s.space"

    def test_state_match_in_a_joined_room_wins(self):
        rooms = [
            {"room_id": "!a:s.space", "joined": True, "state_events": []},
            {
                "room_id": "!inbox:s.space",
                "joined": True,
                "state_events": [{"type": "space.ag2.inbox", "content": {"owner": self.OWNER, "version": 1}}],
            },
        ]
        result = ib.find_inbox_room(rooms, self.OWNER)
        self.assertEqual(result, {"status": "found", "room_id": "!inbox:s.space", "matched_by": "state"})

    def test_state_match_but_not_a_member_reports_not_member_not_found(self):
        rooms = [
            {
                "room_id": "!inbox:s.space",
                "joined": False,
                "state_events": [{"type": "space.ag2.inbox", "content": {"owner": self.OWNER}}],
            }
        ]
        result = ib.find_inbox_room(rooms, self.OWNER)
        self.assertEqual(result["status"], "not_member")
        self.assertEqual(result["room_id"], "!inbox:s.space")

    def test_topic_match_with_no_readable_state_needs_confirmation_not_found(self):
        # A weak match must never auto-qualify as "found" — SKILL.md requires asking
        # the owner before any --live send on this status (review P1 remediation).
        rooms = [
            {"room_id": "!a:s.space", "joined": True, "topic": "general chat"},
            {"room_id": "!inbox:s.space", "joined": True, "topic": "Your private Inbox. Ask your agent..."},
        ]
        result = ib.find_inbox_room(rooms, self.OWNER)
        self.assertEqual(
            result, {"status": "needs_confirmation", "room_id": "!inbox:s.space", "matched_by": "topic"}
        )

    def test_name_match_with_no_readable_state_needs_confirmation_not_found(self):
        rooms = [{"room_id": "!inbox:s.space", "joined": True, "name": "Inbox"}]
        result = ib.find_inbox_room(rooms, self.OWNER)
        self.assertEqual(result, {"status": "needs_confirmation", "room_id": "!inbox:s.space", "matched_by": "name"})

    def test_a_room_named_inbox_for_an_unrelated_reason_is_never_auto_found(self):
        # Exact repro from the review: a team's own generic triage room happens to be
        # named "Inbox", carries no space.ag2.inbox state at all. Must not be `found`.
        rooms = [{"room_id": "!team-triage:s.space", "joined": True, "name": "Inbox"}]
        result = ib.find_inbox_room(rooms, self.OWNER)
        self.assertNotEqual(result["status"], "found")
        self.assertEqual(result["status"], "needs_confirmation")

    def test_a_same_owner_different_agent_is_not_fooled_by_a_different_owner(self):
        rooms = [
            {
                "room_id": "!someone-elses:s.space",
                "joined": True,
                "state_events": [{"type": "space.ag2.inbox", "content": {"owner": "@someone-else:s.space"}}],
            }
        ]
        self.assertEqual(ib.find_inbox_room(rooms, self.OWNER), {"status": "not_found"})

    def test_a_room_with_contradicting_owner_state_is_never_matched_via_its_topic(self):
        # The topic contains the marker text, but the room's OWN state names a
        # different owner — that contradiction must win, not even needs_confirmation.
        rooms = [
            {
                "room_id": "!someone-elses-inbox:s.space",
                "joined": True,
                "topic": "Your private Inbox for someone else entirely",
                "state_events": [{"type": "space.ag2.inbox", "content": {"owner": "@someone-else:s.space"}}],
            }
        ]
        self.assertEqual(ib.find_inbox_room(rooms, self.OWNER), {"status": "not_found"})

    def test_a_room_with_contradicting_owner_state_is_never_matched_via_its_name(self):
        rooms = [
            {
                "room_id": "!someone-elses-inbox:s.space",
                "joined": True,
                "name": "Inbox",
                "state_events": [{"type": "space.ag2.inbox", "content": {"owner": "@someone-else:s.space"}}],
            }
        ]
        self.assertEqual(ib.find_inbox_room(rooms, self.OWNER), {"status": "not_found"})

    def test_a_genuine_owner_state_room_still_wins_outright_over_a_weak_match(self):
        rooms = [
            {"room_id": "!decoy:s.space", "joined": True, "name": "Inbox"},
            {
                "room_id": "!real-inbox:s.space",
                "joined": True,
                "state_events": [{"type": "space.ag2.inbox", "content": {"owner": self.OWNER, "version": 1}}],
            },
        ]
        result = ib.find_inbox_room(rooms, self.OWNER)
        self.assertEqual(result, {"status": "found", "room_id": "!real-inbox:s.space", "matched_by": "state"})

    def test_weak_match_not_joined_is_not_member_not_needs_confirmation(self):
        rooms = [{"room_id": "!inbox:s.space", "joined": False, "name": "Inbox"}]
        result = ib.find_inbox_room(rooms, self.OWNER)
        self.assertEqual(result["status"], "not_member")

    def test_nothing_matches_anything(self):
        rooms = [{"room_id": "!a:s.space", "joined": True, "name": "random"}]
        self.assertEqual(ib.find_inbox_room(rooms, self.OWNER), {"status": "not_found"})


class DigestTest(unittest.TestCase):
    def _item(self, priority, hours_ago, summary="thing"):
        from datetime import datetime, timedelta, timezone

        when = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "source": {"app": "github"},
            "what": {"kind": "mentioned", "summary": summary},
            "link": "https://example.com",
            "priority": priority,
            "updated_at": when,
        }

    def test_empty_window_says_so(self):
        self.assertIn("Nothing new", ib.digest_text([], 12))

    def test_groups_by_priority_and_counts(self):
        items = [self._item("needs_you", 1, "a"), self._item("needs_you", 2, "b"), self._item("fyi", 3, "c")]
        text = ib.digest_text(items, 12)
        self.assertIn("Needs you** (2)", text)
        self.assertIn("FYI** (1)", text)
        self.assertIn("- [github] a — https://example.com", text)

    def test_items_outside_the_window_are_excluded(self):
        items = [self._item("needs_you", 1), self._item("fyi", 48)]
        text = ib.digest_text(items, 12)
        self.assertIn("Needs you** (1)", text)
        self.assertNotIn("FYI", text)


class RotateLogTest(unittest.TestCase):
    def test_small_log_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log.jsonl"
            path.write_text("short\n")
            ib.rotate_log(path)
            self.assertEqual(path.read_text(), "short\n")

    def test_oversized_log_is_capped_to_its_tail(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log.jsonl"
            path.write_text("x" * (ib.LOG_MAX_BYTES + 1000) + "TAIL_MARKER")
            ib.rotate_log(path)
            self.assertLessEqual(path.stat().st_size, ib.LOG_KEEP_BYTES)
            self.assertTrue(path.read_text().endswith("TAIL_MARKER"))

    def test_missing_log_is_not_an_error(self):
        ib.rotate_log(Path(tempfile.mkdtemp()) / "missing.jsonl")  # does not raise


class CliCursorsTest(unittest.TestCase):
    def test_set_without_live_does_not_persist(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ib.main(["--workspace", str(ws), "cursors", "set", "--source", "github", "--value", "x"])
            self.assertFalse(ib.cursors_path(ws).exists())

    def test_set_with_live_persists_and_get_reads_it_back(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ib.main(
                ["--workspace", str(ws), "cursors", "set", "--source", "github", "--value", "2026-11-16", "--live"]
            )
            saved = json.loads(ib.cursors_path(ws).read_text())
            self.assertEqual(saved["github"], "2026-11-16")

    def test_get_on_an_unset_source_answers_null(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rc = ib.main(["--workspace", str(ws), "cursors", "get", "--source", "slack"])
            self.assertEqual(rc, 0)


class HelperEdgeCasesTest(unittest.TestCase):
    def test_as_iso_passes_through_a_string_unchanged(self):
        self.assertEqual(ib._as_iso("2026-11-16T00:00:00Z"), "2026-11-16T00:00:00Z")

    def test_as_iso_none_is_empty(self):
        self.assertEqual(ib._as_iso(None), "")

    def test_as_iso_passes_a_string_through_even_if_it_looks_wrong(self):
        # By design: a string is assumed already-ISO (e.g. a GitHub `created_at`) and
        # is never run through the float-epoch parser.
        self.assertEqual(ib._as_iso("not-a-number"), "not-a-number")

    def test_as_iso_unparseable_non_string_value_is_empty_not_an_error(self):
        self.assertEqual(ib._as_iso(["not", "a", "number"]), "")

    def test_slack_ts_to_iso_bad_value_is_empty_not_an_error(self):
        self.assertEqual(ib._slack_ts_to_iso("not-a-ts"), "")

    def test_repo_full_name_prefers_the_repository_object(self):
        raw = {"repository": {"full_name": "a/b"}, "repository_url": "https://api.github.com/repos/x/y"}
        self.assertEqual(ib._repo_full_name(raw), "a/b")

    def test_gmail_and_slack_include_account_only_when_given(self):
        gmail_item = ib.map_gmail(GMAIL_UNREAD_FIXTURE, "new_mail", ib.DEFAULT_RULES, account="qingyun")
        self.assertEqual(gmail_item["source"]["account"], "qingyun")
        slack_item = ib.map_slack(SLACK_DM_FIXTURE, "dm", ib.DEFAULT_RULES, account="qingyun")
        self.assertEqual(slack_item["source"]["account"], "qingyun")

    def test_load_sent_ids_tolerates_a_non_dict_ids_field(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ib.sent_ids_path(ws).write_text(json.dumps({"ids": "not-a-dict"}))
            self.assertEqual(ib.load_sent_ids(ws), {})

    def test_load_sent_ids_tolerates_a_non_dict_top_level(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ib.sent_ids_path(ws).write_text(json.dumps(["not", "a", "dict"]))
            self.assertEqual(ib.load_sent_ids(ws), {})

    def test_load_rules_tolerates_a_non_list_top_level(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ib.rules_path(ws).write_text(json.dumps({"not": "a list"}))
            self.assertEqual(ib.load_rules(ws), ib.DEFAULT_RULES)

    def test_prune_skips_an_unparseable_timestamp_rather_than_crashing(self):
        sent_ids = {
            "bad-id": {"seen_at": "not-a-timestamp", "revision": "x"},
            "good-id": {"seen_at": "2026-11-16T00:00:00Z", "revision": "x"},
        }
        pruned = ib.prune_sent_ids(sent_ids)
        self.assertNotIn("bad-id", pruned)
        self.assertIn("good-id", pruned)

    def test_digest_recent_check_tolerates_a_missing_or_bad_timestamp(self):
        item = {
            "source": {"app": "github"},
            "what": {"kind": "mentioned", "summary": "x"},
            "link": "https://example.com",
            "priority": "fyi",
            "updated_at": "not-a-timestamp",
        }
        self.assertIn("Nothing new", ib.digest_text([item], 12))

    def test_rotate_log_on_a_directory_in_place_of_a_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            # A directory: the mocked size passes the guard, read_bytes() then raises
            # IsADirectoryError, an OSError subclass.
            path = Path(td) / "oversized-dir"
            path.mkdir()
            with patch.object(ib.Path, "stat") as mock_stat:
                mock_stat.return_value.st_size = ib.LOG_MAX_BYTES + 1
                ib.rotate_log(path)  # does not raise


class CliOtherSubcommandsTest(unittest.TestCase):
    def test_rules_show_and_init_via_cli(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self.assertEqual(ib.main(["--workspace", str(ws), "rules", "init"]), 0)
            self.assertTrue(ib.rules_path(ws).exists())
            self.assertEqual(ib.main(["--workspace", str(ws), "rules", "show"]), 0)

    def test_map_via_cli_reads_a_file_and_prints_mapped_items(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            raw_file = Path(td) / "raw.json"
            raw_file.write_text(json.dumps([GITHUB_REVIEW_REQUEST_FIXTURE]))
            rc = ib.main(
                [
                    "--workspace",
                    str(ws),
                    "map",
                    "--source",
                    "github",
                    "--kind",
                    "review_requested",
                    "--file",
                    str(raw_file),
                ]
            )
            self.assertEqual(rc, 0)

    def test_map_via_cli_accepts_a_single_object_not_just_an_array(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            raw_file = Path(td) / "raw.json"
            raw_file.write_text(json.dumps(GITHUB_REVIEW_REQUEST_FIXTURE))
            rc = ib.main(
                ["--workspace", str(ws), "map", "--source", "github", "--kind", "mentioned", "--file", str(raw_file)]
            )
            self.assertEqual(rc, 0)

    def test_find_room_via_cli(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rooms_file = Path(td) / "rooms.json"
            rooms_file.write_text(json.dumps([{"room_id": "!a:s.space", "joined": True, "name": "Inbox"}]))
            rc = ib.main(["--workspace", str(ws), "find-room", "--file", str(rooms_file), "--owner", "@x:s.space"])
            self.assertEqual(rc, 0)

    def test_log_sent_dry_run_then_live_via_cli(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            items_file = Path(td) / "items.json"
            items_file.write_text(json.dumps([item]))
            ib.main(["--workspace", str(ws), "log-sent", "--file", str(items_file)])
            self.assertFalse(ib.sent_log_path(ws).exists())
            ib.main(["--workspace", str(ws), "log-sent", "--file", str(items_file), "--live"])
            self.assertTrue(ib.sent_log_path(ws).exists())
            logged = ib.sent_log_path(ws).read_text().strip().splitlines()
            self.assertEqual(len(logged), 1)

    def test_digest_via_cli_with_explicit_file(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            items_file = Path(td) / "items.json"
            items_file.write_text(json.dumps([item]))
            rc = ib.main(["--workspace", str(ws), "digest", "--file", str(items_file), "--hours", "999999"])
            self.assertEqual(rc, 0)

    def test_digest_via_cli_reads_the_local_sent_log_when_no_file_given(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            item = ib.map_github(GITHUB_REVIEW_REQUEST_FIXTURE, "review_requested", ib.DEFAULT_RULES)
            items_file = Path(td) / "items.json"
            items_file.write_text(json.dumps([item]))
            ib.main(["--workspace", str(ws), "log-sent", "--file", str(items_file), "--live"])
            # A garbled line in the log must not break the digest.
            with ib.sent_log_path(ws).open("a") as fh:
                fh.write("not json\n")
            rc = ib.main(["--workspace", str(ws), "digest", "--hours", "999999"])
            self.assertEqual(rc, 0)

    def test_digest_via_cli_with_no_log_and_no_file_says_nothing_new(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            rc = ib.main(["--workspace", str(ws), "digest"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
