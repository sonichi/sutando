#!/usr/bin/env python3
"""
Tests for skills/checklist-respond detector + view_builder.

Guards:
  - parse_checklist detects all three kinds (lettered, checklist, yesno)
  - [checklist] marker is stripped from output
  - apply_click correctly toggles / single-picks / votes
  - cid / parse_cid round-trip correctly
  - load/save state round-trip (tmp dir)

Run: python3 tests/checklist-respond.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "checklist-respond" / "scripts"))

from detector import parse_checklist, ChecklistSpec
from view_builder import cid, parse_cid, apply_click, load_state, save_state

PASS = 0
FAIL = 0


def ok(name: str):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fail(name: str, reason: str):
    global FAIL
    FAIL += 1
    print(f"  ✗ {name}: {reason}")


# ---------------------------------------------------------------------------
# detector tests
# ---------------------------------------------------------------------------

def test_no_marker_returns_none():
    spec = parse_checklist("Hello world, no checklist here")
    if spec is None:
        ok("no marker → None")
    else:
        fail("no marker → None", f"expected None, got {spec}")


def test_lettered_options():
    text = """[checklist]
Which episode should we post next?

a) Ep4 — park walk
b) Ep5 — barber chair
c) Ep6 — park demo"""
    spec = parse_checklist(text)
    if spec is None:
        fail("lettered options", "returned None"); return
    if spec.kind != "lettered":
        fail("lettered options", f"kind={spec.kind}"); return
    if len(spec.items) != 3:
        fail("lettered options", f"expected 3 items, got {len(spec.items)}"); return
    if spec.items[0].id != "a" or "park walk" not in spec.items[0].label:
        fail("lettered options", f"item 0 wrong: {spec.items[0]}"); return
    if "[checklist]" in spec.preamble:
        fail("lettered options", "marker not stripped from preamble"); return
    ok("lettered options — 3 items, kinds, marker stripped")


def test_lettered_uppercase():
    text = "[checklist]\nA. First option\nB. Second option\nC. Third option"
    spec = parse_checklist(text)
    if spec and spec.kind == "lettered" and len(spec.items) == 3:
        ok("lettered uppercase A. format")
    else:
        fail("lettered uppercase", f"got: {spec}")


def test_md_checklist():
    text = """[checklist]
PR review items:

- [ ] Tests cover all 4 steps
- [x] No dead imports
- [ ] SKILL.md updated"""
    spec = parse_checklist(text)
    if spec is None:
        fail("md checklist", "returned None"); return
    if spec.kind != "checklist":
        fail("md checklist", f"kind={spec.kind}"); return
    if len(spec.items) != 3:
        fail("md checklist", f"expected 3 items, got {len(spec.items)}"); return
    # Second item was [x] — should be pre-checked
    if spec.items[1].state != "checked":
        fail("md checklist", f"item 1 state should be 'checked', got {spec.items[1].state}"); return
    ok("md checklist — 3 items, checked state detected")


def test_yesno_fallback():
    text = "[checklist]\nPush feat/event-log-ts-1032 and open a PR?"
    spec = parse_checklist(text)
    if spec is None:
        fail("yesno", "returned None"); return
    if spec.kind != "yesno":
        fail("yesno", f"kind={spec.kind}"); return
    if len(spec.items) != 2:
        fail("yesno", f"expected 2 items, got {len(spec.items)}"); return
    ids = {it.id for it in spec.items}
    if ids != {"yes", "no"}:
        fail("yesno", f"expected yes/no items, got {ids}"); return
    ok("yesno fallback — 2 items yes/no")


def test_marker_case_insensitive():
    text = "[CHECKLIST]\nDo you agree?\n"
    spec = parse_checklist(text)
    if spec is not None:
        ok("marker case-insensitive")
    else:
        fail("marker case-insensitive", "returned None")


def test_preamble_preserved():
    text = """[checklist]
## WIRE Ep4 candidate selection

a) Park walk video
b) Barber chair clip"""
    spec = parse_checklist(text)
    if spec and "WIRE Ep4" in spec.preamble:
        ok("preamble preserved in spec")
    else:
        fail("preamble preserved", f"preamble: {spec.preamble if spec else 'None'}")


# ---------------------------------------------------------------------------
# view_builder tests
# ---------------------------------------------------------------------------

def test_cid_round_trip():
    custom_id = cid("1234567890123456789", "a")
    result = parse_cid(custom_id)
    if result == ("1234567890123456789", "a"):
        ok("cid/parse_cid round-trip")
    else:
        fail("cid round-trip", f"expected ('1234567890123456789', 'a'), got {result}")


def test_cid_unknown_prefix_returns_none():
    result = parse_cid("unknown_prefix_button")
    if result is None:
        ok("unknown cid prefix → None")
    else:
        fail("unknown cid", f"expected None, got {result}")


def test_apply_click_lettered_single_pick():
    state = {"kind": "lettered", "voters": {}}
    # User picks 'b'
    apply_click(state, "b", "U1", "alice")
    if "U1" in state["voters"].get("b", []):
        ok("lettered click — vote recorded")
    else:
        fail("lettered click", f"vote not recorded: {state['voters']}")

    # User picks 'a' — should remove from 'b'
    apply_click(state, "a", "U1", "alice")
    if "U1" in state["voters"].get("a", []) and "U1" not in state["voters"].get("b", []):
        ok("lettered click — single-pick (switched from b to a)")
    else:
        fail("lettered single-pick", f"state: {state['voters']}")


def test_apply_click_checklist_toggle():
    state = {"kind": "checklist", "voters": {}}
    apply_click(state, "0", "U1", "alice")
    if "U1" in state["voters"].get("0", []):
        ok("checklist click — item checked")
    else:
        fail("checklist toggle check", str(state["voters"]))

    apply_click(state, "0", "U1", "alice")
    if "U1" not in state["voters"].get("0", []):
        ok("checklist click — item unchecked on second click")
    else:
        fail("checklist toggle uncheck", str(state["voters"]))


def test_apply_click_yesno_exclusive():
    state = {"kind": "yesno", "voters": {}}
    apply_click(state, "yes", "U1", "alice")
    if "U1" in state["voters"].get("yes", []):
        ok("yesno click — yes recorded")
    else:
        fail("yesno yes", str(state["voters"]))

    apply_click(state, "no", "U1", "alice")
    if "U1" in state["voters"].get("no", []) and "U1" not in state["voters"].get("yes", []):
        ok("yesno click — switched yes→no, exclusive")
    else:
        fail("yesno exclusive", str(state["voters"]))


def test_state_load_save_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "checklists"
        state = {"kind": "lettered", "task_id": "task-1234", "voters": {"a": ["U1"]}}
        save_state(state_dir, "9999000000000001", state)
        loaded = load_state(state_dir, "9999000000000001")
        if loaded == state:
            ok("state load/save round-trip")
        else:
            fail("state load/save", f"loaded: {loaded}")


def test_load_state_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        result = load_state(Path(tmp), "nonexistent_msg_id")
        if result is None:
            ok("load_state missing → None")
        else:
            fail("load_state missing", f"expected None, got {result}")


def test_save_state_non_fatal_bad_dir():
    import os
    with tempfile.TemporaryDirectory() as tmp:
        ro = Path(tmp) / "readonly"
        ro.mkdir()
        os.chmod(ro, 0o555)
        try:
            save_state(ro / "checklists", "msg1", {"x": 1})
            ok("save_state non-fatal on bad dir")
        except Exception as e:
            fail("save_state non-fatal", f"raised: {e}")
        finally:
            os.chmod(ro, 0o755)


def test_multi_user_lettered():
    state = {"kind": "lettered", "voters": {}}
    apply_click(state, "a", "U1", "alice")
    apply_click(state, "b", "U2", "bob")
    if "U1" in state["voters"].get("a", []) and "U2" in state["voters"].get("b", []):
        ok("multi-user lettered — each user can pick different option")
    else:
        fail("multi-user lettered", str(state["voters"]))


# ---------------------------------------------------------------------------
# Bridge structural test — on_interaction handler wired
# ---------------------------------------------------------------------------

def test_bridge_has_on_interaction_handler():
    src = (REPO / "src" / "discord-bridge.py").read_text()
    if "on_interaction" in src and "checklist" in src.lower():
        ok("discord-bridge.py has on_interaction handler + checklist wiring")
    else:
        # Not yet wired — this test will fail until bridge integration is done
        fail("discord-bridge on_interaction", "handler or checklist wiring missing (implement bridge integration)")


if __name__ == "__main__":
    print("checklist-respond skill tests")
    print()

    print("detector:")
    test_no_marker_returns_none()
    test_lettered_options()
    test_lettered_uppercase()
    test_md_checklist()
    test_yesno_fallback()
    test_marker_case_insensitive()
    test_preamble_preserved()

    print()
    print("view_builder:")
    test_cid_round_trip()
    test_cid_unknown_prefix_returns_none()
    test_apply_click_lettered_single_pick()
    test_apply_click_checklist_toggle()
    test_apply_click_yesno_exclusive()
    test_state_load_save_round_trip()
    test_load_state_missing_returns_none()
    test_save_state_non_fatal_bad_dir()
    test_multi_user_lettered()

    print()
    print("bridge wiring (structural):")
    test_bridge_has_on_interaction_handler()

    print()
    if FAIL == 0:
        print(f"━━━ checklist-respond tests: {PASS} passed / 0 failed ━━━")
    else:
        print(f"━━━ FAILED: {FAIL} failed / {PASS} passed ━━━")
        sys.exit(1)
