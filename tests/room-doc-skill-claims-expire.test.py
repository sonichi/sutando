#!/usr/bin/env python3
"""A claim in the skill's text about what the SERVICE refuses must carry the
date it was last measured, and it expires.

The words "by design", "deliberate", "is refused", "cannot", "will not" turn
an observation into a rule. A rule sounds like a decision, so a reader stops
checking it — three agents skipped the one credential that worked because a
sentence written on one day was still there the day after the service
changed. A dated claim is an observation with a shelf life; an undated one is
a rule nobody owns.

Run: python3 tests/room-doc-skill-claims-expire.test.py  (exit 0 pass / 1 fail)
"""
import datetime as dt
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "room-doc" / "scripts"))

from skill_claims import MAX_AGE_DAYS, refusal_claims, stale_claims  # noqa: E402

SKILL = REPO / "skills" / "room-doc" / "SKILL.md"
FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


TODAY = dt.date(2026, 9, 20)


def test_a_refusal_claim_without_a_date_is_a_finding():
    text = "An AG2 ticket minted only to pull tasks is refused — that is deliberate."
    found = stale_claims(text, TODAY)
    assert len(found) == 1 and "undated" in found[0].problem, found


def test_a_dated_claim_within_the_window_passes():
    text = "The HTTP /doc/authz endpoint refuses agent bearers (verified 2026-09-20)."
    assert stale_claims(text, TODAY) == [], stale_claims(text, TODAY)


def test_a_dated_claim_past_the_window_is_a_finding():
    old = TODAY - dt.timedelta(days=MAX_AGE_DAYS + 1)
    text = f"Agent bearers are refused by design (verified {old.isoformat()})."
    found = stale_claims(text, TODAY)
    assert len(found) == 1 and "expired" in found[0].problem, found


def test_a_claim_on_the_last_allowed_day_still_passes():
    edge = TODAY - dt.timedelta(days=MAX_AGE_DAYS)
    text = f"Non-members are told 404 by design (verified {edge.isoformat()})."
    assert stale_claims(text, TODAY) == []


def test_ordinary_prose_is_not_a_claim():
    text = ("A refusal is raised, never returned as an empty document. "
            "The text commands refuse on a board rather than answering.")
    assert refusal_claims(text) == [], refusal_claims(text)


def test_the_refusal_table_rows_count_as_claims():
    text = "| 4403 | Refused or withdrawn: not authorized for documents. |"
    assert len(refusal_claims(text)) == 1


def test_the_live_skill_text_has_no_stale_or_undated_claims():
    """The point of the file: SKILL.md as it is checked in, against today."""
    found = stale_claims(SKILL.read_text(encoding="utf-8"), dt.date.today())
    assert found == [], "\n" + "\n".join(f"  line {c.line}: {c.problem}: {c.text[:80]}"
                                         for c in found)


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-doc skill claims expire: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-doc skill claims expire: ok")
