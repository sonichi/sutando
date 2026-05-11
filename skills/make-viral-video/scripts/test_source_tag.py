#!/usr/bin/env python3
"""Unit tests for source_tag schema v1 auto-footer + license-gate logic.

Lifts the tested logic from render.py main() into pure functions so we can
exercise edge cases without firing TTS/ffmpeg. Run:

    python3 skills/make-viral-video/scripts/test_source_tag.py

Exits 0 on pass, non-zero on fail. No external deps.
"""
import sys
from typing import Any


def derive_auto_footer(manifest: list[dict[str, Any]]) -> str:
    """Replicates the auto-footer derivation from render.py:670-685.

    Returns the joined footer text, or empty string if manifest doesn't carry
    enough source_tag fields to derive a footer."""
    event_ids: list[str] = []
    shorts: list[str] = []
    for entry in manifest:
        st = entry.get("source_tag") or {}
        if isinstance(st, dict):
            ev = st.get("event_id")
            fs = st.get("footer_short")
            if ev and ev not in event_ids:
                event_ids.append(ev)
            if fs and fs not in shorts:
                shorts.append(fs)
    if event_ids and shorts:
        return " · ".join(event_ids + shorts)
    return ""


def license_gate_warnings(manifest: list[dict[str, Any]]) -> list[str]:
    """Replicates the license-gate logic from render.py:690-696. Returns the
    list of local_file names that trigger a warning (license=needs-license +
    empty attribution)."""
    warns = []
    for entry in manifest:
        st = entry.get("source_tag") or {}
        if isinstance(st, dict) and st.get("license") == "needs-license" and not st.get("attribution"):
            warns.append(entry.get("local_file", "?"))
    return warns


# -- Tests --

def test_empty_manifest():
    """No manifest entries → empty auto-footer, no warnings."""
    assert derive_auto_footer([]) == ""
    assert license_gate_warnings([]) == []


def test_manifest_without_source_tag():
    """Manifest with entries but no source_tag → falls back to empty."""
    manifest = [{"local_file": "x.jpg", "purpose": "hook"}]
    assert derive_auto_footer(manifest) == ""
    assert license_gate_warnings(manifest) == []


def test_single_event_full_chain():
    """Single event_id + 3 distinct footer_shorts → joined chain."""
    manifest = [
        {"source_tag": {"event_id": "ev1", "footer_short": "a"}},
        {"source_tag": {"event_id": "ev1", "footer_short": "b"}},
        {"source_tag": {"event_id": "ev1", "footer_short": "c"}},
    ]
    assert derive_auto_footer(manifest) == "ev1 · a · b · c"


def test_duplicate_footer_shorts_deduped():
    """Same footer_short on multiple entries → only listed once (dedup by value)."""
    manifest = [
        {"source_tag": {"event_id": "ev1", "footer_short": "a"}},
        {"source_tag": {"event_id": "ev1", "footer_short": "a"}},  # dupe
        {"source_tag": {"event_id": "ev1", "footer_short": "b"}},
    ]
    assert derive_auto_footer(manifest) == "ev1 · a · b"


def test_multi_event_bloat_pattern():
    """Multi-event manifest (Lucy's ShinyHunters case) → all event_ids land
    in chain. This is the v1.0 behavior that motivated the v1.1 `primary` flag."""
    manifest = [
        {"source_tag": {"event_id": "ev_canvas", "footer_short": "canvas"}},
        {"source_tag": {"event_id": "ev_ticketmaster", "footer_short": "ticket"}},
        {"source_tag": {"event_id": "ev_powerschool", "footer_short": "school"}},
        {"source_tag": {"event_id": "ev_raoult", "footer_short": "raoult"}},
    ]
    footer = derive_auto_footer(manifest)
    # All 4 event_ids + all 4 shorts present
    assert "ev_canvas" in footer
    assert "ev_raoult" in footer
    assert "canvas" in footer
    assert "raoult" in footer


def test_partial_source_tag_skipped():
    """Entry with source_tag but missing event_id or footer_short doesn't
    block the others. Only complete entries contribute."""
    manifest = [
        {"source_tag": {"event_id": "ev1", "footer_short": "a"}},
        {"source_tag": {"event_id": "ev1"}},  # missing footer_short
        {"source_tag": {"footer_short": "b"}},  # missing event_id (but b joins via dedup)
    ]
    footer = derive_auto_footer(manifest)
    assert "ev1" in footer
    assert "a" in footer
    assert "b" in footer  # b is collected even without an event_id since collection is per-field


def test_license_gate_warns_on_needs_license_no_attribution():
    manifest = [
        {"local_file": "ok.jpg", "source_tag": {"license": "public-domain", "attribution": "NASA"}},
        {"local_file": "bad.jpg", "source_tag": {"license": "needs-license"}},
        {"local_file": "ok-needs.jpg", "source_tag": {"license": "needs-license", "attribution": "via permission letter"}},
    ]
    warns = license_gate_warnings(manifest)
    assert warns == ["bad.jpg"], f"expected ['bad.jpg'], got {warns}"


def test_license_gate_silent_on_other_licenses():
    manifest = [
        {"local_file": "a.jpg", "source_tag": {"license": "fair-use"}},
        {"local_file": "b.jpg", "source_tag": {"license": "cc-by"}},
        {"local_file": "c.jpg", "source_tag": {"license": "editorial-only", "attribution": "press use only"}},
    ]
    assert license_gate_warnings(manifest) == []


def test_source_tag_none_handled_safely():
    """source_tag explicitly None (not just missing) should not crash."""
    manifest = [{"local_file": "x.jpg", "source_tag": None}]
    assert derive_auto_footer(manifest) == ""
    assert license_gate_warnings(manifest) == []


def main():
    tests = [
        ("empty_manifest", test_empty_manifest),
        ("manifest_without_source_tag", test_manifest_without_source_tag),
        ("single_event_full_chain", test_single_event_full_chain),
        ("duplicate_footer_shorts_deduped", test_duplicate_footer_shorts_deduped),
        ("multi_event_bloat_pattern", test_multi_event_bloat_pattern),
        ("partial_source_tag_skipped", test_partial_source_tag_skipped),
        ("license_gate_warns_on_needs_license_no_attribution", test_license_gate_warns_on_needs_license_no_attribution),
        ("license_gate_silent_on_other_licenses", test_license_gate_silent_on_other_licenses),
        ("source_tag_none_handled_safely", test_source_tag_none_handled_safely),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
