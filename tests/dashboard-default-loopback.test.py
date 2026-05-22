#!/usr/bin/env python3
"""Security regression guard: dashboard must default to loopback-only bind.

## Why this test exists

`src/dashboard.py` runs an HTTP server that exposes:

  - `GET /json` — score, health, activity, pending questions,
    system stats (full owner activity profile)
  - `GET /notes` — list of every owner note file (titles + slugs)
  - `GET /notes/<slug>` — full content of any owner note
  - `GET /avatar`, `GET /stand-identity` — owner avatar + identity

The pre-fix bind to `0.0.0.0` made all of this readable by any device
on the LAN with NO authentication. Notes can contain personal info,
work-in-progress thoughts, contacts, secrets — none of which should
be readable by a guest on the same Wi-Fi.

The fix defaults to `127.0.0.1` and lets users opt into LAN exposure
via `DASHBOARD_BIND=0.0.0.0` (same shape as `AGENT_API_BIND` in
`src/agent-api.py`). If they do opt in, the startup banner prints a
big warning so they remember the dashboard has no auth.

This test is source-grep + architectural assertion. We don't bind a
real server in the test (would conflict with a running dashboard on
the same machine) — we verify the source has the right shape.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "dashboard.py").read_text()


def test_default_bind_is_loopback_not_zero_zero():
    """The bare `0.0.0.0` bind that pre-fix existed must be gone."""
    bad = re.search(r'HTTPServer\(\s*\(\s*["\']0\.0\.0\.0["\']\s*,', SRC)
    assert bad is None, (
        "dashboard.py binds to 0.0.0.0 unconditionally — the dashboard "
        "exposes owner notes / activity / identity with NO authentication. "
        "Default to 127.0.0.1 and let users opt into LAN via DASHBOARD_BIND."
    )


def test_uses_dashboard_bind_env_with_loopback_default():
    """The fix's exact shape: `os.environ.get("DASHBOARD_BIND",
    "127.0.0.1")`. Pin so a future refactor that changes the env-var
    name or the default has to update this test deliberately."""
    assert re.search(
        r'os\.environ\.get\(\s*["\']DASHBOARD_BIND["\']\s*,\s*["\']127\.0\.0\.1["\']',
        SRC,
    ), (
        "dashboard.py must read the bind address from DASHBOARD_BIND with "
        "a default of 127.0.0.1."
    )


def test_warns_when_lan_exposure_is_enabled():
    """LAN-bind warning must remain so the auth gap is visible."""
    assert re.search(r"if\s+bind\s*!=\s*['\"]127\.0\.0\.1['\"]", SRC), (
        "dashboard.py must include `if bind != \"127.0.0.1\":` to surface "
        "the LAN-exposure warning."
    )
    assert "NO authentication" in SRC, (
        "dashboard.py must include the explicit 'NO authentication' "
        "string in the LAN-bind warning."
    )


def main():
    failures = []
    for fn in (
        test_default_bind_is_loopback_not_zero_zero,
        test_uses_dashboard_bind_env_with_loopback_default,
        test_warns_when_lan_exposure_is_enabled,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All dashboard-default-loopback tests passed.")


if __name__ == "__main__":
    main()
