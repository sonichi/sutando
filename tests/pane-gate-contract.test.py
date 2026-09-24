#!/usr/bin/env python3
"""Contract for src/delivery/pane_gate.py — the shared pane idle-gate + delivery.

Run: python3 tests/pane-gate-contract.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from delivery import pane_gate as pg  # noqa: E402
import cli_wedge as wedge  # noqa: E402

FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
CODEX_DIM_IDLE = "\x1b[1m›\x1b[0m \x1b[2mImprove documentation in @filename\x1b[0m\n"
CODEX_PICKER = "  Select Model and Effort\n› 4. gpt-5.5 (current)  Proven previous-generation model\n"
CODEX_IDLE = f"\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n{FOOTER}\n"
CLAUDE_IDLE = f"❯ \n{FOOTER}\n"
# (gate kind, prose a finished turn may print, the live dialog of that kind)
BROAD_SIGNATURES = [
    ("selection", "Select the best file for the user.",
     "Select an option:\n❯ 1. main.py\n  2. util.py\n  Enter to select · Esc to cancel\n"),
    ("permission", "I do not have permission to read that file.",
     "  Do you want to proceed?\n  Allow this action\n  Esc to cancel\n"),
    ("folder-trust", "Do you trust the summary above?",
     "Do you trust the files in this folder?\n❯ 1. Yes, proceed\n  2. No, exit\n  Enter to confirm · Esc to cancel\n"),
    ("press-enter", "The dialog said Press Enter to continue.",
     "  Login\n  Logged in as x@example.com\n  Login successful. Press Enter to continue…\n"),
    ("bypass-permissions", "Bypass Permissions mode is already on, so no prompt appeared.",
     "WARNING: Claude Code running in Bypass Permissions mode\n❯ 1. No, exit\n  2. Yes, I accept\n"
     "  Enter to confirm · Esc to cancel\n"),
    ("login", "The Select login method dialog did not appear.",
     "  Select login method:\n  ❯ 1. Claude account with subscription\n    2. Anthropic Console account\n"
     "  Esc to cancel\n"),
    ("fable-limit-unfocused", "You've reached your Fable limit, the dialog said, so I switched.",
     "  You've reached your Fable limit\n  You've used your included Fable usage for this week.\n"
     "    Switch to Opus 5 and continue\n  ❯ Continue with Fable 5.1\n  Esc to cancel\n"),
]


def state(text, runtime):
    return pg.classify_pane(text, pg.ADAPTERS[runtime]).state


class ClaudeClassification(unittest.TestCase):
    def test_idle_footer_with_empty_prompt_is_idle_ready(self):
        v = pg.classify_pane(f"❯ \n{FOOTER}\n", pg.CLAUDE)
        self.assertEqual(v.state, "idle-ready")
        self.assertEqual(v.pending, "")

    def test_working_marker_is_busy(self):
        self.assertEqual(state(f"✻ Thinking… (12s · esc to interrupt)\n❯ \n{FOOTER}\n", "claude"), "busy")

    def test_text_at_the_prompt_is_pending_with_the_text(self):
        v = pg.classify_pane(f"❯ half typed\n{FOOTER}\n", pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("pending", "half typed"))

    def test_permission_prompt_is_busy_not_pending(self):
        v = pg.classify_pane("Do you want to proceed?\n❯ 1. Yes\n  2. No\n", pg.CLAUDE)
        self.assertEqual(v.state, "busy")
        self.assertIn(v.reason, ("permission", "selection"))
        self.assertIsNone(v.pending)

    def test_last_prompt_line_wins_over_scrollback(self):
        self.assertEqual(pg.pending_text("❯ old\n…\n❯ \n", pg.CLAUDE), "")
        self.assertEqual(pg.pending_text("❯ \n…\n❯ new\n", pg.CLAUDE), "new")

    def test_a_grey_ghost_suggestion_is_idle_ready_not_pending(self):
        """Live incident, 2026-09-17T02:22Z: the model-switch menu was refused three
        times because Claude's own inline suggestion ("merge 4269", never typed) read
        as pending text -- Claude captures had no attributes at all before this fix,
        so a styled ghost line and typed text were indistinguishable."""
        ghost = "\x1b[38;5;246m❯\xa0\x1b[39m\x1b[38;5;246mmerge 4269\x1b[39m\n"
        v = pg.classify_pane(ghost, pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("idle-ready", ""))

    def test_typed_text_before_a_ghost_completion_still_refuses_the_typed_part(self):
        line = "\x1b[38;5;246m❯\xa0\x1b[39mmerge\x1b[38;5;246m 4269\x1b[39m\n"
        v = pg.classify_pane(line, pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("pending", "merge"))

    def test_a_colour_cube_foreground_is_REAL_input_not_a_ghost(self):
        """A 256-colour foreground is only a hint when it is the GREYSCALE RAMP
        (232-255). 200-231 are colour-cube entries a CLI may use for real input, and
        reading one as a hint empties the composer -- so a pane holding typed text
        reports idle-ready and the sender types over it. Fails closed on the cube."""
        line = "\x1b[39m❯ \x1b[38;5;208mdeploy\x1b[0m\n"
        v = pg.classify_pane(line, pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("pending", "deploy"))

    def test_a_ghost_run_that_WRAPS_stays_ghost_on_its_continuation_rows(self):
        """A dim placeholder longer than the pane wraps onto rows carrying no SGR of
        their own. Stripping ghost per-row leaves that tail as pending text, so Codex's
        EMPTY composer reads as occupied on a narrow pane and delivery refuses forever."""
        row1 = "\x1b[1m\u203a\x1b[0m \x1b[2mAsk Codex to do anythi"
        row2 = "ng in @filename\x1b[0m"
        width = len("\u203a Ask Codex to do anythi")
        # Fixture precondition, asserted rather than assumed: unless the prompt row is at least
        # `width` wide the continuation loop never runs and this passes without testing the wrap.
        self.assertGreaterEqual(len(pg._SGR.sub("", row1)), width,
                                "fixture no longer reaches the wrap path — widen row1 or lower width")
        self.assertEqual(pg.pending_text(row1 + "\n" + row2 + "\n", pg.CODEX, width), "")

    def test_wrapped_REAL_input_is_still_pending(self):
        """Control for the ghost-wrap case: an unstyled draft that wraps must survive."""
        cap = "\u203a deploy the whole thing\nto production now\n"
        width = len("\u203a deploy the whole thing")
        self.assertGreaterEqual(len("\u203a deploy the whole thing"), width,
                                "fixture no longer reaches the wrap path")
        self.assertEqual(
            pg.pending_text(cap, pg.CODEX, width), "deploy the whole thingto production now")

    def test_an_SGR2_ghost_suggestion_is_idle_ready(self):
        """Verbatim `capture-pane -p -e` from Claude Code v2.1.276 (Sutando-Pro's
        witness): the suggested reply is SGR 2, not a 256-colour grey."""
        v = pg.classify_pane("\x1b[39m❯ \x1b[2myes\x1b[0m\n", pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("idle-ready", ""))

    def test_a_typed_draft_from_the_same_capture_is_pending(self):
        """Its control, captured the same way: no styled run, so it stays a draft."""
        v = pg.classify_pane("\x1b[39m❯\xa0hello draft\n", pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("pending", "hello draft"))

    def test_typed_text_beside_a_dim_span_is_pending(self):
        """The undimmed remainder is what decides, not where the span sits."""
        v = pg.classify_pane("\x1b[39m❯ hi \x1b[2mghost\x1b[0m\n", pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("pending", "hi"))

    def test_a_grey_glyph_with_no_content_reads_as_an_EMPTY_composer(self):
        """The glyph itself is drawn in the ramp, so the ramp rule must not eat the
        prompt and lose the line. Composer-level only: an empty composer carries no
        idle affordance by itself, so classify_pane stays `unknown` without a footer."""
        self.assertEqual(pg.pending_text("\x1b[38;5;246m❯\xa0\x1b[39m\n", pg.CLAUDE), "")

    def test_unanchored_for_agents_in_prose_is_not_idle(self):
        """keweichen round-4 (review 5231933087): the old CLAUDE_IDLE pattern matched
        "for agents\\b" anywhere, so ordinary prose mentioning it (no prompt line at
        all) read as idle-ready. Anchored to the real footer's "<- for agents" shape;
        bare prose no longer matches."""
        v = pg.classify_pane("This guide is for agents who review code.\n", pg.CLAUDE)
        self.assertEqual(v.state, "unknown")

    def test_the_real_footers_for_agents_still_matches(self):
        self.assertEqual(state(f"❯ \n{FOOTER}\n", "claude"), "idle-ready")


class CodexClassification(unittest.TestCase):
    def test_dim_placeholder_is_idle_ready_not_pending(self):
        v = pg.classify_pane(CODEX_DIM_IDLE, pg.CODEX)
        self.assertEqual((v.state, v.pending), ("idle-ready", ""))

    def test_an_UNSTYLED_literal_matching_the_placeholder_text_is_PENDING_not_idle(self):
        """keweichen/qingyun-wu, 04:07-04:23Z: the exact-text fallback let someone who
        actually TYPED this exact phrase (unstyled, e.g. a plain capture without -e) be
        read as an empty composer, and worse, let the phrase ANYWHERE in the tail (prose,
        old transcript) authorize idle-ready with no current-prompt line at all. Every
        real capture is taken with -e now (pane_gate.observe always passes escapes=True),
        so an unstyled match is never the genuine placeholder -- it fails closed as typed
        text, and prose with no glyph line stays unknown."""
        v = pg.classify_pane("› Ask Codex to do anything\n", pg.CODEX)
        self.assertEqual((v.state, v.pending), ("pending", "Ask Codex to do anything"))

    def test_the_placeholder_phrase_in_PROSE_with_no_prompt_line_is_UNKNOWN_not_idle(self):
        v = pg.classify_pane("some earlier transcript said Ask Codex to do anything once\n", pg.CODEX)
        self.assertEqual(v.state, "unknown")

    def test_the_REAL_styled_placeholder_is_still_idle_ready(self):
        v = pg.classify_pane(CODEX_DIM_IDLE, pg.CODEX)
        self.assertEqual((v.state, v.pending), ("idle-ready", ""))

    def test_shared_footer_after_bare_glyph_is_idle_ready(self):
        # The launcher's pinned stale-running recovery pane.
        self.assertEqual(state(f"›\n{FOOTER}\n", "codex"), "idle-ready")

    def test_typed_text_is_pending(self):
        v = pg.classify_pane("\x1b[1m›\x1b[0m half typed\n", pg.CODEX)
        self.assertEqual((v.state, v.pending), ("pending", "half typed"))

    def test_picker_row_is_busy(self):
        v = pg.classify_pane(CODEX_PICKER, pg.CODEX)
        self.assertEqual(v.state, "busy")
        self.assertEqual(v.reason, "selection")
        self.assertEqual(state("› 4. gpt-5.5 (current)\n", "codex"), "busy")

    def test_working_marker_is_busy(self):
        self.assertEqual(state("◦ Working (2m • esc to interrupt)\n", "codex"), "busy")

    def test_claude_glyph_is_not_the_codex_prompt(self):
        self.assertIsNone(pg.pending_text("❯ half typed\n", pg.CODEX))
        self.assertNotEqual(state("❯ half typed\n", "codex"), "pending")

    def test_a_gate_appearing_BELOW_a_stale_empty_composer_is_still_busy(self):
        """keweichen round-4, 2026-09-17 (review 5231933087): the composer/footer can
        be genuinely idle-shaped in the capture, and a gate can render UNDER it in the
        SAME capture (composer drawn first, dialog printed right after) -- the composer
        being empty is not proof nothing below it is asking for input. after_prompt()
        must be checked, not just the empty/placeholder composer line. OLD behaviour:
        idle-ready (wrong -- would send into the gate). NEW: busy/"press-enter"."""
        capture = f"{CODEX_IDLE}Login successful. Press Enter to continue\u2026\n"
        v = pg.classify_pane(capture, pg.CODEX)
        self.assertEqual((v.state, v.reason), ("busy", "press-enter"))


class GateSignaturesNeedACurrentAffordance(unittest.TestCase):
    """A finished turn's prose above the runtime's empty composer is history; only a dialog that
    shows how to answer it (caret on a numbered row, an Enter/Esc hint) holds the pane."""

    def test_select_prose_above_the_codex_composer_is_idle_ready(self):
        # The reviewer's repro; "Choose" is the control that never matched a signature.
        for prose in ("Select the best file for the user.", "Choose the best file for the user."):
            v = pg.classify_pane(f"• {prose}\n\n{CODEX_IDLE}", pg.CODEX)
            self.assertEqual((v.state, v.pending), ("idle-ready", ""), prose)

    def test_a_real_codex_picker_or_approval_stays_busy(self):
        v = pg.classify_pane(CODEX_PICKER + "  Press enter to confirm or esc to go back\n", pg.CODEX)
        self.assertEqual((v.state, v.reason), ("busy", "selection"))
        approval = ("Would you like to run the following command?\n  $ rm -rf build\n"
                    "› 1. Yes, proceed\n  2. No, and tell Codex what to do differently (esc)\n")
        self.assertEqual(pg.classify_pane(approval, pg.CODEX).state, "busy")

    def test_select_prose_above_the_claude_composer_is_idle_ready_and_a_row_picker_is_busy(self):
        self.assertEqual(state(f"● Select the best file for the user.\n{CLAUDE_IDLE}", "claude"), "idle-ready")
        self.assertEqual(state("● Select the best file.\n❯ 1. main.py\n  2. util.py\n", "claude"), "busy")

    def test_prose_above_typed_text_is_pending_not_busy(self):
        v = pg.classify_pane(f"● Select the best file for the user.\n❯ half typed\n{FOOTER}\n", pg.CLAUDE)
        self.assertEqual((v.state, v.pending), ("pending", "half typed"))

    def test_every_broad_signature_is_prose_above_an_empty_composer(self):
        for kind, prose, _ in BROAD_SIGNATURES:
            self.assertEqual(state(f"● {prose}\n{CLAUDE_IDLE}", "claude"), "idle-ready", kind)
            self.assertEqual(state(f"• {prose}\n{CODEX_IDLE}", "codex"), "idle-ready", kind)

    def test_every_broad_signature_is_a_gate_in_its_live_dialog(self):
        for kind, _, dialog in BROAD_SIGNATURES:
            v = pg.classify_pane(dialog, pg.CLAUDE)
            self.assertEqual((v.state, v.reason), ("busy", kind))

    def test_focused_fable_switch_row_is_the_fable_gate(self):
        dialog = ("  You've reached your Fable limit\n  ❯ Switch to Opus 5 and continue\n"
                  "    Continue with Fable 5.1\n  Esc to cancel\n")
        self.assertEqual(pg.classify_pane(dialog, pg.CLAUDE).reason, "fable-limit")

    def test_an_unlisted_dialog_with_a_hint_is_unknown_never_idle(self):
        novel = "  Overwrite the existing config file?\n  (Enter to confirm · Esc to cancel)\n"
        for runtime in pg.ADAPTERS:
            self.assertEqual(state(novel, runtime), "unknown", runtime)

    def test_an_unlisted_dialog_BELOW_a_stale_idle_composer_is_not_idle_ready(self):
        """The dialog-alone case already passed; the defect needed the stale composer above it.
        keweichen/round-4: a styled empty composer + footer flipped the verdict to idle-ready."""
        novel = "  Overwrite the existing config file?\n  (Enter to confirm \u00b7 Esc to cancel)\n"
        v = pg.classify_pane(f"{CLAUDE_IDLE}{novel}", pg.CLAUDE)
        self.assertNotEqual(v.state, "idle-ready", f"stale composer vouched for a live dialog: {v}")
        self.assertIn(v.state, pg.UNSAFE_STATES, v)
        # the control: a genuinely idle composer with nothing under it must still deliver
        self.assertEqual(pg.classify_pane(CLAUDE_IDLE, pg.CLAUDE).state, "idle-ready")

    def test_an_empty_composer_outside_the_tail_window_vouches_for_nothing(self):
        far = "\n".join(f"line {i}" for i in range(20))
        dialog = "  Login successful. Press Enter to continue…\n"
        self.assertEqual(pg.classify_pane(f"❯ \n{far}\n{dialog}", pg.CLAUDE).reason, "press-enter")


class SharedStates(unittest.TestCase):
    def test_abnormal_patterns_win_over_gate_text(self):
        self.assertEqual(state("Compacting context…\n", "codex"), "abnormal")
        v = pg.classify_pane(f"You've hit your session limit\n❯ \n{FOOTER}\n", pg.CLAUDE)
        self.assertEqual(v.state, "abnormal")
        self.assertIn("quota-limit", v.reason)

    def test_empty_or_failed_capture_is_unknown_never_idle(self):
        for runtime in pg.ADAPTERS:
            for text in (None, "", "\n\n", "   \n"):
                self.assertEqual(state(text, runtime), "unknown", (runtime, text))

    def test_prose_without_affordance_is_unknown(self):
        self.assertEqual(state("Reading the file now.\nDone.\n", "claude"), "unknown")

    def test_classify_reads_the_same_tail_window_as_the_monitor(self):
        far = "\n".join(f"line {i}" for i in range(40))
        self.assertEqual(state(f"❯ \n{FOOTER}\n{far}\n", "claude"), "unknown")


class WrappedComposerIsStillTheComposer(unittest.TestCase):
    """A composer wider than the pane continues onto glyph-less rows.

    Read without `width`, those rows look like content sitting BELOW an empty
    prompt, which inverts both answers: the pending text comes back truncated,
    and the wrapped tail gets fingerprinted as a gate beneath the composer.
    """

    # "\u203a abcdefgh" is exactly 10 columns, so the row is full and wraps.
    WRAPPED = "\u203a abcdefgh\nijklm\nGATE?\n"

    def test_a_full_width_prompt_row_absorbs_the_rows_that_wrapped_from_it(self):
        self.assertEqual(pg.pending_text(self.WRAPPED, pg.CODEX, 10), "abcdefghijklm")

    def test_without_a_width_the_wrapped_tail_is_lost(self):
        # Why every caller must pass its real pane width, not 0.
        self.assertEqual(pg.pending_text(self.WRAPPED, pg.CODEX), "abcdefgh")

    def test_a_row_short_of_the_width_did_not_wrap_so_nothing_is_absorbed(self):
        self.assertEqual(pg.pending_text("\u203a short\ntrailing\n", pg.CODEX, 40), "short")

    def test_a_following_prompt_glyph_ends_the_continuation(self):
        # The next composer is a new prompt, never a continuation of the last.
        self.assertEqual(pg.pending_text("\u203a abcdefgh\n\u203a second\n", pg.CODEX, 10), "second")

    def test_after_prompt_starts_below_the_wrapped_rows_not_inside_them(self):
        self.assertEqual(pg.after_prompt(self.WRAPPED, pg.CODEX, 10), "GATE?")
        # Without the width the composer's own tail is misread as content below it.
        self.assertEqual(pg.after_prompt(self.WRAPPED, pg.CODEX), "ijklm\nGATE?")

    def test_after_prompt_keeps_unwrapped_rows_below_a_short_prompt(self):
        self.assertEqual(pg.after_prompt("\u203a hi\nGATE?\n", pg.CODEX, 40), "GATE?")


class ClassifyPaneSeesPastTheTailTruncation(unittest.TestCase):
    """classify_pane read only the last TAIL_LINES rows before searching for the
    prompt glyph, so a draft with more continuation rows than that lost its own
    glyph line to truncation -- the footer beneath it then read as an empty,
    idle composer, and a notifier would type the next task over the unsubmitted
    draft and press Enter (keweichen, #4320 review, live capture with 20+ rows).
    """

    def test_a_draft_wrapped_past_the_tail_window_is_still_pending_not_idle(self):
        self.assertGreater(24, pg.TAIL_LINES,
                            "fixture no longer exceeds TAIL_LINES \u2014 it would no longer reach the bug")
        rows = "\n".join(f"continuation row {i}" for i in range(24))
        capture = f"\u203a owner draft starts\n{rows}\n{FOOTER}\n"
        v = pg.classify_pane(capture, pg.CODEX)
        self.assertEqual(v.state, "pending")
        self.assertEqual(v.pending, "owner draft starts")

    def test_control_the_same_capture_truncated_to_the_tail_loses_the_glyph(self):
        # Proves the fixture actually exercises truncation, not some other path.
        rows = "\n".join(f"continuation row {i}" for i in range(24))
        capture = f"\u203a owner draft starts\n{rows}\n{FOOTER}\n"
        lines = pg._tail_lines(capture)
        self.assertIsNone(pg.prompt_line("\n".join(lines), pg.CODEX),
                           "control invalid: the truncated tail still contains the glyph line")


class ComposerTextStripsTheWholeFooterNotJustOneRow(unittest.TestCase):
    """composer_text() (moved here from core-input-watch.py's `_composer_text`,
    which now aliases it) parses task-notifier.sh's EXACT-equality staging
    checks. A live Claude Code footer can render the idle-footer row AND a
    separate rotating "Tip: ..." row beneath it; the old implementation popped
    only one non-border trailing row, so the tip leaked straight into staged
    text and a real prompt never compared equal to itself (keweichen/qingyun,
    #4320 round 5 -- a live incident, not a hypothetical: 47 minutes of a
    Claude task-notifier refusing every delivery with "composer holds <task>'s
    prompt with other text").
    """

    def test_a_footer_with_a_status_row_and_a_tip_row_still_strips_clean(self):
        capture = f"❯ Sutando task ready: task-x.txt\n{FOOTER}\nTip: Use /btw to send feedback\n"
        self.assertEqual(pg.composer_text(capture), "Sutando task ready: task-x.txt")

    def test_control_the_single_row_footer_already_stripped_clean(self):
        # Proves the fixture reaches the leak path for the right reason: the
        # ONE-row footer was already clean before this fix.
        capture = f"❯ Sutando task ready: task-x.txt\n{FOOTER}\n"
        self.assertEqual(pg.composer_text(capture), "Sutando task ready: task-x.txt")

    def test_an_owner_row_reading_tip_survives_the_strip(self):
        # Popping the real tip row must not re-classify an interior row: only
        # the LAST matching row is ever popped, same guarantee as the idle row.
        capture = f"❯ Sutando task ready: task-x.txt\nTip: this is what I typed\n{FOOTER}\n"
        self.assertEqual(pg.composer_text(capture),
                          "Sutando task ready: task-x.txtTip: this is what I typed")

    def test_no_prompt_line_is_none_not_a_leaked_footer(self):
        self.assertIsNone(pg.composer_text("no prompt line here\njust text"))

    def test_composer_text_cli_subcommand_matches_the_pending_contract(self):
        out, err = io.StringIO(), io.StringIO()
        stdin = f"❯ Sutando task ready: task-x.txt\n{FOOTER}\nTip: Use /btw to send feedback\n"
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pg.main(["composer-text", "--runtime", "claude"])
        self.assertEqual((code, out.getvalue().strip()), (0, "Sutando task ready: task-x.txt"))

    def test_composer_text_cli_refuses_with_no_prompt_line(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("just prose, no glyph\n")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pg.main(["composer-text", "--runtime", "claude"])
        self.assertEqual(code, pg.EXIT_UNSAFE)
        self.assertEqual(out.getvalue(), "")


class TheAbnormalVerdictIsCliWedgesNotTheGatesOwn(unittest.TestCase):
    """The gate asks cli_wedge one question -- frame_abnormal -- and ranks nothing
    itself. A gate that composed the detectors and ordered them by hand once put
    the interrupt affordance above a retry, against the rule the window classifier
    already held (abnormal text outranks motion). Pinned by substitution: whatever
    cli_wedge answers is the verdict, and without its answer no banner is seen."""

    RETRY = "  ⎿  Connection error. Retrying in 2 seconds…"
    PARKED = "API Error: 529 Overloaded"
    INTERRUPT = "✻ Thinking… (12s · esc to interrupt)"

    def test_cli_wedges_answer_is_the_verdict_even_over_an_idle_footer(self):
        stub = wedge.FrameAbnormal("abnormal", ("stubbed-family",), False)
        with mock.patch.object(pg, "frame_abnormal", return_value=stub):
            v = pg.classify_pane(CLAUDE_IDLE, pg.CLAUDE)
        self.assertEqual((v.state, v.reason), ("abnormal", "stubbed-family"))

    def test_without_cli_wedges_answer_the_gate_sees_no_banner_at_all(self):
        with mock.patch.object(pg, "frame_abnormal", return_value=None):
            for text in (self.RETRY, self.PARKED):
                with self.subTest(text=text):
                    self.assertEqual(state(f"{text}\n{CLAUDE_IDLE}", "claude"), "idle-ready")

    def test_a_parked_line_beside_the_affordance_is_abnormal_as_the_window_rule_says(self):
        # cli_wedge: moving + abnormal is a warning; the affordance is motion, not health.
        v = pg.classify_pane(f"{self.INTERRUPT}\n{self.PARKED}\n{CLAUDE_IDLE}", pg.CLAUDE)
        self.assertEqual((v.state, v.reason, pg.accepts_input(v)), ("abnormal", "api-error", False))

    def test_a_named_dialog_still_outranks_a_parked_line_that_is_its_own_text(self):
        dialog = ("  You've reached your Fable limit\n  ❯ Switch to Opus 5 and continue\n"
                  "    Continue with Fable 5.1\n  Esc to cancel\n")
        self.assertIsNotNone(wedge.frame_abnormal(dialog))   # control: the line IS parked text
        self.assertEqual(pg.classify_pane(dialog, pg.CLAUDE).reason, "fable-limit")

    def test_a_retry_outranks_a_named_dialog(self):
        dialog = f"{self.RETRY}\n  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n  Esc to cancel\n"
        v = pg.classify_pane(dialog, pg.CLAUDE)
        self.assertEqual(v.state, "abnormal")
        self.assertIn("retry:retrying", v.reason)


class ANamedLoginGateOutranksCliWedgesNewCoarserLabel(unittest.TestCase):
    """cli_wedge now recognises the login dialog's title as abnormal/needs-login
    (a narrower fix, this repo). The gate must still report the SPECIFIC named
    kind -- HITL's kind-string contract depends on "login", not "needs-login"."""

    def test_the_named_gate_wins(self):
        dialog = ("Select login method\n  \u276f 1. Log in with browser\n"
                  "    2. Paste code manually\n  Esc to cancel\n")
        v = pg.classify_pane(dialog, pg.CLAUDE)
        self.assertEqual((v.state, v.reason), ("busy", "login"))


class ARetryIsAbnormalEvenInsideARunningTurn(unittest.TestCase):
    """Owner's rule: retry means abnormal. The interrupt affordance stays on screen
    while the CLI retries, so "esc to interrupt" cannot vouch for a served turn --
    a line typed then queues into a turn that is not being served. Pinned in both
    orderings because the first draft ranked the affordance above the banner."""

    RETRY = "  ⎿  Connection error. Retrying in 2 seconds…"
    INTERRUPT = "✻ Thinking… (12s · esc to interrupt)"

    def test_control_the_affordance_alone_is_a_running_turn_that_accepts_input(self):
        v = pg.classify_pane(f"{self.INTERRUPT}\n{CLAUDE_IDLE}", pg.CLAUDE)
        self.assertEqual((v.state, v.reason, pg.accepts_input(v)), ("busy", "working", True))

    def test_a_retry_beside_the_affordance_holds_whichever_comes_first(self):
        for order in ((self.INTERRUPT, self.RETRY), (self.RETRY, self.INTERRUPT)):
            with self.subTest(order=order):
                v = pg.classify_pane("\n".join(order) + f"\n{CLAUDE_IDLE}", pg.CLAUDE)
                self.assertEqual(v.state, "abnormal")
                self.assertIn("retry:retrying", v.reason)
                self.assertFalse(pg.accepts_input(v))


class AbnormalIsBothOfCliWedgesFamilies(unittest.TestCase):
    """The gate is the ONE caller of cli_wedge's single-capture detectors. Before
    this it read only the line-anchored parked patterns, so a live retry banner
    ("Retrying in 2s") classified idle-ready and a caller typed into it; the Claude
    notifier compensated with its own second detector in a second place."""

    RETRY = "  ⎿  Connection error. Retrying in 2 seconds…"
    PARKED = "API Error: 529 Overloaded"
    PROSE = (
        "⏺ I once saw a Connection error. Retrying was the fix.",
        "  ⎿  Connection error. Retrying was the fix.",
        "⏺ API Error handling is covered by tests.",
        "  ⎿  Connection error. The fix was retrying",
        "⏺ I verified the docs that say\n  Connection error handling is covered by tests.",
    )

    def test_a_live_retry_banner_is_abnormal(self):
        v = pg.classify_pane(f"{self.RETRY}\n{CLAUDE_IDLE}", pg.CLAUDE)
        self.assertEqual(v.state, "abnormal")
        self.assertIn("retry:retrying", v.reason)

    def test_control_the_same_retry_banner_was_invisible_to_the_parked_patterns(self):
        # Proves the fixture reaches the family only the banner grammar reads.
        self.assertEqual(wedge.matched_abnormal([self.RETRY]), [])

    def test_a_parked_api_error_banner_is_abnormal(self):
        v = pg.classify_pane(f"{self.PARKED}\n{CLAUDE_IDLE}", pg.CLAUDE)
        self.assertEqual(v.state, "abnormal")
        self.assertIn("api-error", v.reason)

    def test_prose_about_errors_and_retries_stays_idle_ready(self):
        for line in self.PROSE:
            with self.subTest(line=line):
                self.assertEqual(state(f"{line}\n{CLAUDE_IDLE}", "claude"), "idle-ready")

    def test_accepts_input_is_idle_a_draft_or_a_running_turn_and_nothing_else(self):
        yes = [pg.Verdict("idle-ready", "idle footer or empty composer", ""),
               pg.Verdict("pending", "text at the prompt", "draft"),
               pg.Verdict("busy", "working")]
        no = [pg.Verdict("busy", "permission"), pg.Verdict("busy", "unlisted"),
              pg.Verdict("abnormal", "retry:retrying"), pg.Verdict("unknown", "no capture")]
        for v in yes:
            self.assertTrue(pg.accepts_input(v), v)
        for v in no:
            self.assertFalse(pg.accepts_input(v), v)

    def _healthy(self, stdin):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pg.main(["healthy", "--runtime", "claude"])
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_healthy_cli_exits_zero_for_a_running_turn_and_an_idle_composer(self):
        busy = f"✻ Thinking… (12s · esc to interrupt)\n❯ \n{FOOTER}\n"
        self.assertEqual(self._healthy(busy)[:2], (0, "busy"))
        self.assertEqual(self._healthy(CLAUDE_IDLE)[:2], (0, "idle-ready"))

    def test_healthy_cli_refuses_a_retry_banner_a_dialog_and_an_empty_capture(self):
        for cap in (f"{self.RETRY}\n{CLAUDE_IDLE}",
                    "Do you want to proceed?\n❯ 1. Yes\n  2. No\n", ""):
            with self.subTest(cap=cap[:30]):
                code, out, err = self._healthy(cap)
                self.assertEqual(code, pg.EXIT_UNSAFE)
                self.assertEqual(out, "", "a refusal must print no state on stdout")
                self.assertIn("not accepting input", err)


class CliExitCodesAreTheContract(unittest.TestCase):
    """Callers are shell. They branch on the exit code, so each one is pinned here."""

    def _run(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pg.main(argv)
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_safe_states_lists_exactly_the_safe_set_and_needs_no_runtime(self):
        code, out, _ = self._run(["safe-states"])
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), [s for s in pg.STATES if not pg.state_is_unsafe(s)])

    def test_safe_states_omits_every_unsafe_state(self):
        # Membership is fail-closed: a caller matching against this list must
        # refuse anything absent from it, including `unknown`.
        _, out, _ = self._run(["safe-states"])
        for unsafe in pg.UNSAFE_STATES:
            self.assertNotIn(unsafe, out.split())

    def test_safe_prints_the_state_and_exits_zero_on_an_idle_composer(self):
        code, out, err = self._run(["safe", "--runtime", "codex"], "\u203a \n\u2190 for agents\n")
        self.assertEqual((code, out, err), (0, "idle-ready", ""))

    def test_safe_refuses_a_pending_composer_with_the_unsafe_code(self):
        code, out, err = self._run(["safe", "--runtime", "codex"], "\u203a half typed\n\u2190 for agents\n")
        self.assertEqual(code, pg.EXIT_UNSAFE)
        self.assertEqual(out, "", "a refusal must print no state on stdout")
        self.assertIn("pending", err)

    def test_safe_refuses_an_unreadable_capture_rather_than_calling_it_safe(self):
        code, _, err = self._run(["safe", "--runtime", "codex"], "")
        self.assertEqual(code, pg.EXIT_UNSAFE)
        self.assertIn("unknown", err)


class UnknownIsUnsafe(unittest.TestCase):
    """`unknown` is an absence of evidence, not evidence of safety.

    It authorized injection at three sites: the `pending` CLI collapsed None into "",
    tmux-send-line.sh's `[ -n "$PENDING" ]` therefore sent under --refuse-if-pending,
    and the notifier kept its own list that omitted it.
    """

    def test_unknown_is_in_the_unsafe_set(self):
        self.assertIn("unknown", pg.UNSAFE_STATES)
        self.assertTrue(pg.state_is_unsafe("unknown"))

    def test_only_idle_ready_is_safe_among_known_states(self):
        safe = [s for s in pg.STATES if not pg.state_is_unsafe(s)]
        self.assertEqual(safe, ["idle-ready"])

    def test_an_unrecognised_state_refuses_too(self):
        self.assertTrue(pg.state_is_unsafe("something-new"))
        self.assertTrue(pg.state_is_unsafe(""))

    def test_the_notifier_does_not_keep_its_own_copy_of_the_set(self):
        """One owner. A second list is how `unknown` stayed permissive after this
        module already classified the pane as unreadable."""
        src = (REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh").read_text()
        self.assertNotIn("pending|busy|abnormal", src,
                         "task-notifier.sh restates the unsafe set instead of asking for it")
        self.assertIn("safe-states", src)

    def test_the_notifiers_predicate_actually_RUNS(self):
        """Extract pane_is_unsafe from the script and execute it on every state.

        The grep above is decorative on its own: the first version of this delegation
        omitted the sys.modules registration that dataclasses needs on 3.9, so the import
        raised, python exited 1, the shell read 1 as "not unsafe", and EVERY state --
        pending and abnormal included -- reported safe-to-type. The text assertion passed
        throughout. A predicate is only pinned by running it.
        """
        import re as _re
        script = (REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh").read_text()
        m = _re.search(r'^pane_is_unsafe\(\) \{.*?^\}', script, _re.S | _re.M)
        self.assertTrue(m, "pane_is_unsafe not found in task-notifier.sh")
        gate = REPO / "src" / "delivery" / "pane_gate.py"
        with tempfile.TemporaryDirectory() as d:
            sh = Path(d) / "probe.sh"
            sh.write_text(f'NOTIFIER_PY={sys.executable}\nPANE_GATE_PY="{gate}"\n{m.group(0)}\n'
                          'if pane_is_unsafe "$1" 2>/dev/null; then echo UNSAFE; else echo SAFE; fi\n')
            def verdict(state):
                return subprocess.run(["bash", str(sh), state],
                                      capture_output=True, text=True).stdout.strip()
            self.assertEqual(verdict("idle-ready"), "SAFE")
            for state in ("unknown", "pending", "busy", "abnormal", "not-a-state"):
                self.assertEqual(verdict(state), "UNSAFE", f"{state} must refuse")
            # "" was SAFE here to stop a blank pane starving delivery; the real cause was
            # a fixture never staging sutando_platform.py, so every classify died on import.
            self.assertEqual(verdict(""), "UNSAFE",
                             "an unreadable pane may hold an unsent draft; absence of "
                             "evidence is not evidence of safety")


class ClaudeConstantsHaveOneOwner(unittest.TestCase):
    def test_core_input_watch_aliases_the_gate_constants(self):
        spec = importlib.util.spec_from_file_location("ciw", REPO / "src" / "core-input-watch.py")
        ciw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ciw)
        self.assertIs(ciw._IDLE, pg.CLAUDE_IDLE)
        self.assertIs(ciw._AWAIT_HINT, pg.AWAIT_HINT)
        self.assertIs(ciw._FABLE_TEXT, pg.FABLE_TEXT)
        self.assertEqual(ciw._SIGNATURES, pg.CLAUDE_GATE_SIGNATURES)
        self.assertTrue(ciw._is_idle_ready(f"❯ \n{FOOTER}\n"))


class VerdictShape(unittest.TestCase):
    def test_as_dict_is_exactly_the_three_fields(self):
        self.assertEqual(pg.Verdict("pending", "text at the prompt", "half typed").as_dict(),
                         {"state": "pending", "reason": "text at the prompt", "pending": "half typed"})
        self.assertEqual(pg.Verdict("unknown", "no capture").as_dict(),
                         {"state": "unknown", "reason": "no capture", "pending": None})


class Observe(unittest.TestCase):
    def _runner(self, calls, pane):
        def run(argv, **kw):
            calls.append(argv)
            if "list-windows" in argv:
                return SimpleNamespace(returncode=0, stdout="0\n")
            return SimpleNamespace(returncode=0, stdout=pane)
        return run

    def test_both_runtimes_capture_WITH_attributes(self):
        """Claude ghost suggestions and Codex's dim placeholder are both styled;
        prompt_line() needs attributes on either runtime to tell them from typed text."""
        calls = []
        v = pg.observe("/x.sock", "core", pg.CODEX, runner=self._runner(calls, CODEX_DIM_IDLE))
        self.assertEqual(v.state, "idle-ready")
        cap = [c for c in calls if "capture-pane" in c][0]
        self.assertEqual(cap[cap.index("capture-pane") + 1:cap.index("-t")], ["-e", "-p"])
        self.assertEqual(cap[cap.index("-t") + 1], "=core:0")
        calls.clear()
        pg.observe("/x.sock", "core", pg.CLAUDE, runner=self._runner(calls, f"❯ \n{FOOTER}\n"))
        cap = [c for c in calls if "capture-pane" in c][0]
        self.assertEqual(cap[cap.index("capture-pane") + 1:cap.index("-t")], ["-e", "-p"])

    def test_no_core_window_is_unknown(self):
        def run(argv, **kw):
            return SimpleNamespace(returncode=1, stdout="")
        self.assertEqual(pg.observe("/x.sock", "core", pg.CODEX, runner=run).state, "unknown")


class DeliverDelegates(unittest.TestCase):
    def test_calls_the_one_sender_with_runtime_and_maps_its_exit_codes(self):
        for code, status in ((0, "sent"), (3, "no-session"), (4, "no-tmux"), (5, "pending"),
                             (6, "queued"), (7, "unknown"), (2, "rejected"), (1, "failed")):
            calls = []

            def run(argv, **kw):
                calls.append(argv)
                return SimpleNamespace(returncode=code, stdout="", stderr=f"rc {code}")
            out = pg.deliver("hello", "core", "codex", "/s.sock", refuse_if_pending=True,
                             skip_if_queued="watcher", runner=run)
            self.assertEqual(len(calls), 1, "deliver must make exactly one call: the sender")
            argv = calls[0]
            self.assertEqual(argv[:4], ["bash", str(REPO / "scripts" / "tmux-send-line.sh"), "core", "hello"])
            self.assertEqual(argv[argv.index("--runtime") + 1], "codex")
            self.assertIn("--refuse-if-pending", argv)
            self.assertEqual(argv[argv.index("--skip-if-queued") + 1], "watcher")
            self.assertNotIn("tmux", argv)
            self.assertEqual((out.status, out.code, out.message), (status, code, f"rc {code}"))

    def test_dry_run_forwards_only_the_flags_it_was_given(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="would send: hello\n", stderr="")
        out = pg.deliver("hello", "core", "claude", dry_run=True, runner=run)
        argv = calls[0]
        self.assertIn("--dry-run", argv)
        for absent in ("--socket", "--refuse-if-pending", "--skip-if-queued"):
            self.assertNotIn(absent, argv)
        self.assertEqual((out.status, out.code, out.message), ("sent", 0, "would send: hello"))

    def test_unknown_runtime_is_rejected_before_any_call(self):
        with self.assertRaises(ValueError):
            pg.deliver("x", "core", "agy", runner=lambda *a, **k: self.fail("must not run"))

    def test_gate_owns_no_send_or_capture_of_its_own(self):
        src = (REPO / "src" / "delivery" / "pane_gate.py").read_text()
        # As argv tokens, not prose: the docstring names the sender it does not call.
        self.assertNotIn('"send-keys"', src)
        self.assertNotIn('"capture-pane"', src)


class EndToEndThroughTheRealScript(unittest.TestCase):
    """The real tmux-send-line.sh, a PATH tmux shim, and the gate's `pending` parse it calls back into."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "tmux.log"
        shim = self.bin / "tmux"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$TMUX_LOG\"\n"
            "case \" $* \" in\n"
            "  *' capture-pane '*)\n"
            "    # After the payload is typed, a real terminal echoes it back on re-read --\n"
            "    # the sender's post-delay recheck (see tmux-send-line.sh) depends on that.\n"
            "    if grep -q -- \"-l hello\" \"$TMUX_LOG\" 2>/dev/null; then printf '\u203a hello\\n'\n"
            "    else printf '%b' \"$TMUX_PANE_TEXT\"; fi ;;\n"
            "esac\n"
            "exit 0\n")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, pane, **kw):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", TMUX_LOG=str(self.log),
                   TMUX_PANE_TEXT=pane)
        return pg.deliver("hello", "probe", "codex", f"{self.tmp.name}/s.sock", refuse_if_pending=True,
                          runner=lambda argv, **k: subprocess.run(argv, env=env, **k), **kw)

    def test_dim_placeholder_sends_and_typed_text_refuses(self):
        out = self._run(CODEX_DIM_IDLE)
        self.assertEqual((out.status, out.code), ("sent", 0), out.message)
        log = self.log.read_text()
        self.assertIn("capture-pane -e -p -t probe", log)
        self.assertIn("send-keys -t probe -l hello", log)
        self.log.write_text("")
        out = self._run("\\033[1m›\\033[0m half typed\\n")
        self.assertEqual((out.status, out.code), ("pending", 5), out.message)
        self.assertIn("half typed", out.message)
        self.assertNotIn("send-keys", self.log.read_text())

    def test_picker_row_refuses(self):
        out = self._run(CODEX_PICKER.replace("\n", "\\n"))
        self.assertEqual((out.status, out.code), ("pending", 5), out.message)


class Cli(unittest.TestCase):
    def _cli(self, args, stdin):
        return subprocess.run([sys.executable, str(REPO / "src" / "delivery" / "pane_gate.py"), *args],
                              input=stdin, capture_output=True, text=True)

    def test_classify_prints_the_state(self):
        self.assertEqual(self._cli(["classify", "--runtime", "codex"], CODEX_DIM_IDLE).stdout.strip(), "idle-ready")
        self.assertEqual(self._cli(["classify", "--runtime", "codex"], "").stdout.strip(), "unknown")
        self.assertEqual(self._cli(["classify", "--runtime", "claude"], f"❯ x\n{FOOTER}\n").stdout.strip(), "pending")
        r = self._cli(["classify", "--runtime", "codex", "--json"], "◦ Working (esc to interrupt)\n")
        self.assertIn('"state": "busy"', r.stdout)

    def test_pending_prints_the_prompt_text_or_empty(self):
        self.assertEqual(self._cli(["pending", "--runtime", "codex"], CODEX_DIM_IDLE).stdout, "\n")
        self.assertEqual(self._cli(["pending", "--runtime", "claude"], "❯ half typed\n").stdout, "half typed\n")

    def test_pending_refuses_rather_than_printing_empty_when_no_prompt_is_found(self):
        """A readably-empty composer and an unparseable pane are OPPOSITE answers.

        This case previously asserted stdout == "\\n", i.e. it pinned the collapse as
        intended: a shell caller's `[ -n "$PENDING" ]` then authorized a send into a
        pane nobody could parse. Retargeted, not relaxed.
        """
        empty = self._cli(["pending", "--runtime", "claude"], "❯ \n")
        unknown = self._cli(["pending", "--runtime", "claude"], "no prompt here\n")
        self.assertEqual((empty.returncode, empty.stdout), (0, "\n"))
        self.assertEqual(unknown.returncode, pg.EXIT_UNSAFE)
        self.assertNotEqual(empty.returncode, unknown.returncode,
                            "empty composer and unknown pane must be distinguishable by exit code")
        self.assertIn("unknown", unknown.stderr)

    def test_safe_refuses_every_unsafe_state_and_admits_idle_ready(self):
        self.assertEqual(self._cli(["safe", "--runtime", "codex"], CODEX_DIM_IDLE).returncode, 0)
        for capture in ("", "no prompt here\n", "◦ Working (esc to interrupt)\n"):
            self.assertEqual(self._cli(["safe", "--runtime", "codex"], capture).returncode,
                             pg.EXIT_UNSAFE, f"should refuse: {capture!r}")

    def test_after_prints_everything_below_the_prompt_line(self):
        self.assertEqual(self._cli(["after", "--runtime", "codex"], CODEX_DIM_IDLE).stdout, "\n")
        self.assertEqual(self._cli(["after", "--runtime", "codex"],
                                    "› hello\nLogin successful. Press Enter to continue\n").stdout,
                          "Login successful. Press Enter to continue\n")
        self.assertEqual(self._cli(["after", "--runtime", "codex"], "no prompt line here\n").stdout, "\n")

    def test_unknown_runtime_is_rejected(self):
        self.assertNotEqual(self._cli(["classify", "--runtime", "agy"], "").returncode, 0)


class CliInProcess(unittest.TestCase):
    """main() called directly — the surface Cli pins through a subprocess, but visible to the
    instrumented run. stdin is a StringIO (no reconfigure(), so _read_stdin's fallback runs too)."""

    def _main(self, args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = pg.main(args)
            except SystemExit as e:  # argparse rejections
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_classify_prints_the_state(self):
        self.assertEqual(self._main(["classify", "--runtime", "codex"], CODEX_DIM_IDLE), (0, "idle-ready\n", ""))
        self.assertEqual(self._main(["classify", "--runtime", "codex"], ""), (0, "unknown\n", ""))
        self.assertEqual(self._main(["classify", "--runtime", "claude"], f"❯ x\n{FOOTER}\n"), (0, "pending\n", ""))

    def test_classify_json_is_the_verdict_dict(self):
        rc, out, err = self._main(["classify", "--runtime", "codex", "--json"], "◦ Working (esc to interrupt)\n")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out), {"state": "busy", "reason": "working", "pending": None})
        rc, out, _ = self._main(["classify", "--runtime", "codex", "--json"], "\x1b[1m›\x1b[0m half typed\n")
        self.assertEqual(json.loads(out), {"state": "pending", "reason": "text at the prompt", "pending": "half typed"})

    def test_pending_prints_the_prompt_text_or_empty(self):
        self.assertEqual(self._main(["pending", "--runtime", "codex"], CODEX_DIM_IDLE), (0, "\n", ""))
        self.assertEqual(self._main(["pending", "--runtime", "claude"], "❯ half typed\n"), (0, "half typed\n", ""))

    def test_pending_refuses_rather_than_printing_empty_when_no_prompt_is_found(self):
        """Second copy of the collapse assertion — the subprocess Cli class held one too,
        so the defect was pinned in two places and either alone would have re-admitted it."""
        rc, out, err = self._main(["pending", "--runtime", "claude"], "no prompt here\n")
        self.assertEqual((rc, out), (pg.EXIT_UNSAFE, ""))
        self.assertIn("unknown", err)

    def test_after_prints_everything_below_the_prompt_line(self):
        self.assertEqual(self._main(["after", "--runtime", "codex"], CODEX_DIM_IDLE), (0, "\n", ""))
        self.assertEqual(self._main(["after", "--runtime", "codex"],
                                     "› hello\nLogin successful. Press Enter to continue\n"),
                          (0, "Login successful. Press Enter to continue\n", ""))
        self.assertEqual(self._main(["after", "--runtime", "codex"], "no prompt line here\n"), (0, "\n", ""))

    def test_unknown_runtime_is_rejected_by_the_parser(self):
        rc, out, err = self._main(["classify", "--runtime", "agy"], "")
        self.assertEqual((rc, out), (2, ""))
        # argparse quotes the choices on some Pythons and not on others: assert the
        # rejection, the rejected value and the offered choices, never the punctuation.
        rejection = [ln for ln in err.splitlines() if "invalid choice" in ln]
        self.assertEqual(len(rejection), 1, err)
        for word in ("agy", "claude", "codex"):
            self.assertIn(word, rejection[0])

    def _deliver_via_main(self, args, returncode, stdout="", stderr=""):
        """main() has no runner seam: deliver's default runner is bound at definition, so stub
        pg.deliver with the REAL deliver given a fake runner. Records the sender argv; touches no tmux."""
        calls = []
        real = pg.deliver

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

        def stubbed(*a, **kw):
            return real(*a, runner=run, **kw)
        with mock.patch.object(pg, "deliver", stubbed), mock.patch.dict(os.environ):
            os.environ.pop("SUTANDO_TMUX_SOCKET", None)
            rc, out, err = self._main(["deliver", *args])
        self.assertEqual(len(calls), 1, "main must reach the one sender exactly once")
        return rc, out, err, calls[0]

    def test_deliver_dry_run_reaches_the_sender_with_the_flag_and_reports_on_stdout(self):
        rc, out, err, argv = self._deliver_via_main(["core", "hello", "--runtime", "codex", "--dry-run"], 0)
        self.assertEqual(argv[:4], ["bash", str(REPO / "scripts" / "tmux-send-line.sh"), "core", "hello"])
        self.assertEqual(argv[argv.index("--runtime") + 1], "codex")
        self.assertIn("--dry-run", argv)
        self.assertNotIn("--socket", argv)
        self.assertEqual((rc, out, err), (0, "sent\n", ""))

    def test_deliver_forwards_socket_and_policy_flags(self):
        rc, out, err, argv = self._deliver_via_main(
            ["core", "hello", "--runtime", "claude", "--socket", "/s.sock", "--refuse-if-pending",
             "--skip-if-queued", "watcher", "--dry-run"], 0, stdout="would send\n")
        self.assertEqual(argv[argv.index("--socket") + 1], "/s.sock")
        self.assertIn("--refuse-if-pending", argv)
        self.assertEqual(argv[argv.index("--skip-if-queued") + 1], "watcher")
        self.assertEqual((rc, out, err), (0, "sent: would send\n", ""))

    def test_deliver_nonzero_exit_is_the_return_code_and_goes_to_stderr(self):
        rc, out, err, _ = self._deliver_via_main(["core", "hello", "--runtime", "codex"], 5,
                                                 stderr="pending: half typed\n")
        self.assertEqual((rc, out), (5, ""))
        self.assertTrue(err.startswith("pending: "), err)
        self.assertIn("half typed", err)



class AStaleLimitBannerYieldsToTheProxyRecord(unittest.TestCase):
    """A weekly-limit row that has scrolled into the transcript reads as a live
    limit for as long as it stays in the tail window, so the hold outlives the
    reset. The provider's own record, written by the credential proxy on every
    request, outranks that screenshot -- and ONLY when it is fresh, says allowed,
    and the banner is the sole abnormality. Everything else still holds."""

    BANNER = "  ⎿  You've hit your weekly limit · resets Sep 27 at 2am (America/Los_Angeles)"
    GHOST = "❯ \x1b[2mcheck the watcher's still running\x1b[0m"
    # The seat measured on 2026-09-24: banner 7 non-blank rows from the bottom, then
    # an idle footer under an empty composer holding a dimmed CLI suggestion.
    PANE = (
        "⏺ Re-armed. Holding.\n"
        "✻ Worked for 10s · done 4:26 AM\n"
        "⏺ Monitor event: \"worker task watcher on its own delivery inbox\"\n"
        f"{BANNER}\n"
        "✻ Crunched for 1s · done 4:56 AM\n"
        "                    0% until auto-compact\n"
        "──────────────────────────── sutando-worker ─\n"
        f"{GHOST}\n"
        "─────────────────────────────────────────────\n"
        f"  {FOOTER} · 1 feedback draft\n"
    )
    MENU = (
        "⏺ Writing the result file.\n"
        f"{BANNER}\n"
        "✻ Churned for 49s · done 4:37 AM\n"
        "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
        "   What do you want to do?\n"
        "   ❯ 1. Stop and wait for limit to reset\n"
        "     2. Wait here, then continue automatically at Sep 27 at 2am\n"
        "     3. Switch to usage credits\n"
        "   Enter to confirm · Esc to cancel\n"
    )

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.record = self.ws / "state" / "quota-state.json"

    def _write(self, allowed=True, age_s=30):
        from datetime import datetime, timezone
        when = datetime.fromtimestamp(__import__("time").time() - age_s, tz=timezone.utc)
        status = "allowed" if allowed else "rejected"
        self.record.write_text(json.dumps({
            "available": allowed,
            "last_checked": when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "headers": {
                "anthropic-ratelimit-unified-status": "allowed",
                "anthropic-ratelimit-unified-5h-status": "allowed",
                "anthropic-ratelimit-unified-7d-status": status,
            },
        }))

    def _verdict(self, pane):
        return pg.classify_pane(pane, pg.CLAUDE, self.ws)

    def test_the_fixture_itself_reads_as_a_quota_only_limit(self):
        # Control: without a record the banner holds, and for exactly the family named.
        v = self._verdict(self.PANE)
        self.assertEqual((v.state, v.reason), ("abnormal", "quota-limit"))
        self.assertFalse(pg.accepts_input(v))

    def test_a_fresh_allowed_record_makes_the_banner_scrollback(self):
        self._write(allowed=True, age_s=30)
        v = self._verdict(self.PANE)
        self.assertEqual(v.state, "idle-ready", v)
        self.assertTrue(pg.accepts_input(v))

    def test_a_stale_record_does_not_vouch(self):
        self._write(allowed=True, age_s=3600)
        self.assertEqual(self._verdict(self.PANE).reason, "quota-limit")

    def test_a_record_that_says_rejected_keeps_the_hold(self):
        self._write(allowed=False, age_s=30)
        self.assertEqual(self._verdict(self.PANE).reason, "quota-limit")

    def test_an_unreadable_record_keeps_the_hold(self):
        self.record.write_text("{not json")
        self.assertEqual(self._verdict(self.PANE).reason, "quota-limit")

    def test_a_workspace_that_does_not_exist_keeps_the_hold(self):
        self._write(allowed=True, age_s=30)
        v = pg.classify_pane(self.PANE, pg.CLAUDE, Path(self._t.name) / "nowhere")
        self.assertEqual(v.reason, "quota-limit")

    def test_the_record_never_overrides_a_second_abnormal_family(self):
        self._write(allowed=True, age_s=30)
        pane = self.PANE.replace("✻ Crunched for 1s · done 4:56 AM\n",
                                 "✻ Crunched for 1s · done 4:56 AM\nSession expired\n")
        v = self._verdict(pane)
        self.assertEqual(v.state, "abnormal")
        self.assertIn("needs-login", v.reason)
        self.assertIn("quota-limit", v.reason)

    def test_the_record_never_overrides_a_retry(self):
        self._write(allowed=True, age_s=30)
        pane = self.PANE.replace("✻ Crunched for 1s · done 4:56 AM\n",
                                 "  ⎿  Connection error. Retrying in 2 seconds…\n")
        v = self._verdict(pane)
        self.assertEqual(v.state, "abnormal")
        self.assertIn("retry:retrying", v.reason)

    def test_the_record_never_overrides_a_live_limit_dialog(self):
        # The menu IS the limit, live and waiting on a key; the gate reads it first.
        self._write(allowed=True, age_s=30)
        v = self._verdict(self.MENU)
        self.assertEqual(v.state, "busy", v)
        self.assertFalse(pg.accepts_input(v))

    def test_a_plain_capture_still_passes_healthy_as_pending(self):
        # The notifier hands `healthy` a colour-stripped capture, where the dimmed
        # suggestion reads as typed text: pending, and pending accepts input.
        self._write(allowed=True, age_s=30)
        plain = pg._SGR.sub("", self.PANE)
        v = self._verdict(plain)
        self.assertEqual(v.state, "pending", v)
        self.assertTrue(pg.accepts_input(v))

    def _healthy(self, stdin, *extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pg.main(["healthy", "--runtime", "claude", *extra])
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_healthy_cli_honours_workspace_end_to_end(self):
        code, out, err = self._healthy(self.PANE, "--workspace", str(self.ws))
        self.assertEqual(code, pg.EXIT_UNSAFE, (out, err))
        self.assertIn("quota-limit", err)
        self._write(allowed=True, age_s=30)
        code, out, err = self._healthy(self.PANE, "--workspace", str(self.ws))
        self.assertEqual((code, out), (0, "idle-ready"), err)

    def test_healthy_cli_without_workspace_resolves_the_configured_one(self):
        # No --workspace: the configured workspace is consulted, and its record
        # (whatever it holds) must never turn a live dialog into a pass.
        with mock.patch.object(pg, "resolve_workspace", return_value=self.ws):
            self._write(allowed=True, age_s=30)
            self.assertEqual(self._healthy(self.PANE)[:2], (0, "idle-ready"))
            self.assertEqual(self._healthy(self.MENU)[0], pg.EXIT_UNSAFE)

    def test_a_resolver_that_raises_keeps_the_hold(self):
        with mock.patch.object(pg, "resolve_workspace", side_effect=RuntimeError("no config")):
            v = pg.classify_pane(self.PANE, pg.CLAUDE)
        self.assertEqual(v.reason, "quota-limit")

if __name__ == "__main__":
    unittest.main()
