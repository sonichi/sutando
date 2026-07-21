#!/usr/bin/env python3
"""Regression tests for proactive Slack owner-recipient resolution."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("slack_owner", REPO / "src" / "slack_owner.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
resolve = module.resolve_proactive_owner_id


def main():
    current_live_shape = {
        "allowFrom": ["team-first", "rui-owner", "team-second"],
        "tierMap": {
            "team-first": "team",
            "rui-owner": "owner",
            "team-second": "team",
        },
        "tofuOwner": "rui-owner",
    }
    assert resolve(current_live_shape) == "rui-owner"

    # TOFU remains authoritative when multiple owner-tier users exist.
    assert resolve({
        "allowFrom": ["owner-a", "owner-b"],
        "tierMap": {"owner-a": "owner", "owner-b": "owner"},
        "tofuOwner": "owner-b",
    }) == "owner-b"

    # Removed or demoted TOFU owners cannot receive owner notifications.
    assert resolve({
        "allowFrom": ["former-owner", "current-owner"],
        "tierMap": {"former-owner": "team", "current-owner": "owner"},
        "tofuOwner": "former-owner",
    }) == "current-owner"

    # Legacy access files predate tierMap; allowFrom entries default to owner.
    assert resolve({"allowFrom": ["legacy-owner", "second"]}) == "legacy-owner"
    assert resolve({"allowFrom": ["team"], "tierMap": {"team": "team"}}) is None
    assert resolve({"allowFrom": []}) is None
    print("PASS: proactive Slack notifications resolve only to the configured owner")


if __name__ == "__main__":
    main()
