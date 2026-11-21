#!/usr/bin/env python3
"""A proactive body redirected away from the owner's conversation is guarded at
the destination's tier; the same body to the owner is delivered unchanged (#4971).

Contract for `guard_proactive_for_destination` in src/policy/egress/result.py,
the one owner every proactive adapter delegates to.

Run: python3 tests/proactive-destination-guard.test.py
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import policy.egress.result as guard  # noqa: E402

OWNER_DM = "D-OWNER"
FAILURES: list[str] = []


class _Scan:
    def __init__(self, detected):
        self.detected = detected
        self.secret_types = ["aws_key"] if detected else []


def _clean(_body):
    return _Scan(False)


def _leaky(_body):
    return _Scan(True)


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else ' ' + detail}")
    if not cond:
        FAILURES.append(name)


def run(body, tier, scan=_clean):
    # The adapter routes; the shared parser names the destination as an adapter would read it.
    return guard.guard_proactive_for_destination(
        body, guard.redirect_destination(body), OWNER_DM, tier, REPO, secret_filter=scan)


def main() -> int:
    leaky_bodies = {
        "errno": "copy failed: [Errno 2] No such file",
        "winerror": "copy failed: [WinError 5] Access is denied",
        "posix home path": "wrote /Users/someone/notes/plan.md",
        "windows home path": "wrote C:\\Users\\someone\\plan.md",
        "tmp path": "see /tmp/sutando-run/out.log",
        "root path": "see /root/.cache/x",
    }
    for label, text in leaky_bodies.items():
        body = f"[channel: D-OTHER]\n{text}"
        safe, reason = run(body, "other")
        check(f"redirected to a non-owner: {label} is withheld with the standard notice",
              reason is not None and safe == guard.TEAM_LEAK_RESULT, f"{reason!r} {safe!r}")
        safe, reason = run(text, "other")
        check(f"the same body to the owner DM is unchanged: {label}",
              reason is None and safe == text, f"{reason!r} {safe!r}")

    body = "[channel: D-OTHER]\n[Errno 2] at /Users/someone/x"
    check("a redirect naming the owner DM itself is the owner's conversation",
          run(body.replace("D-OTHER", OWNER_DM), "other") == (body.replace("D-OTHER", OWNER_DM), None))
    check("a redirect to an owner-tier destination is delivered unchanged",
          run(body, "owner") == (body, None))
    check("an unknown destination tier guards (fail closed)",
          run(body, None)[1] is not None and run(body, "")[1] is not None)
    check("[dm-only] cancels the redirect, so the body stays the owner's",
          run("[dm-only]\n" + body, "other") == ("[dm-only]\n" + body, None))

    clean = "[channel: D-OTHER]\nhello\n[attach: /Users/someone/report.png]"
    for tier in ("other", "team", "guest", None, ""):
        check(f"a redirected [file:] to a guarded destination is withheld (tier={tier!r})",
              run(clean, tier) == (guard.TEAM_LEAK_RESULT_MARKER, "result delivery control marker"),
              repr(run(clean, tier)))
    check("a redirected [file:] to an owner-tier destination keeps its markers",
          run(clean, "owner") == (clean, None), repr(run(clean, "owner")))
    check("the owner DM keeps its [file:] markers",
          run(clean.replace("D-OTHER", OWNER_DM), "other") == (clean.replace("D-OTHER", OWNER_DM), None))
    check("[dm-only] cancels the redirect, so a [file:] stays the owner's",
          run("[dm-only]\n" + clean, "other") == ("[dm-only]\n" + clean, None))
    check("a clean redirected body with no [file:] is delivered as written",
          run("[channel: D-OTHER]\nhello", "other") == ("[channel: D-OTHER]\nhello", None))
    safe, reason = run("[channel: D-OTHER]\ntoken inside", "team", scan=_leaky)
    check("a secret in a redirected body is withheld", reason is not None and safe == guard.TEAM_LEAK_RESULT)
    check("the secret scan is not applied to the owner DM",
          run("token inside", "other", scan=_leaky) == ("token inside", None))

    check("the destination the adapter routed to decides, not the body's first marker",
          guard.guard_proactive_for_destination(body, OWNER_DM, OWNER_DM, "other", REPO,
                                                secret_filter=_clean) == (body, None)
          and guard.guard_proactive_for_destination("[Errno 2] x", "!room:x", OWNER_DM, "other", REPO,
                                                    secret_filter=_clean)[1] is not None)
    check("redirect_destination reads the target through the shared parser",
          guard.redirect_destination("[channel: C123]\nx") == "C123"
          and guard.redirect_destination("x\n[channel: C123]") is None
          and guard.redirect_destination("[dm-only]\n[channel: C123]\nx") is None)

    name = guard.attachment_notice_name
    check("an attachment notice names the full path only for the owner",
          name("/Users/someone/x/report.png", "owner") == "/Users/someone/x/report.png"
          and name("/Users/someone/x/report.png", "team") == "report.png"
          and name("C:\\Users\\someone\\report.png", "guest") == "report.png"
          and name("/tmp/run/out.log", None) == "out.log"
          and name("", "other") == "(unnamed file)")

    print()
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s)")
        return 1
    print("PASS: redirected proactive bodies are guarded at the destination's tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
