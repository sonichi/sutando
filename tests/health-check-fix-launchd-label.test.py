#!/usr/bin/env python3
"""
Regression tests for issue #1888 bug 1: `--fix` never auto-fixed
voice-agent / web-client because the dispatch only matched check names
starting with "com.sutando." while those checks are named by service.

Guards:
  a) LAUNCHD_BACKED_CHECKS maps both service names to their launchd labels
  b) fix_launchd() with a mapped label restarts the job — voice-agent through
     the GUARDED restart wrapper (amendment T4: never a direct
     `launchctl kickstart -k` of voice-agent; the wrapper runs the
     voice-lock.py takeover validation first), web-client via kickstart
     (verified via recorded subprocess calls against a temp LaunchAgents)
  c) fix_launchd() falls back to bootstrap when kickstart fails
  d) the --fix dispatch in main() actually routes LAUNCHD_BACKED_CHECKS
     names through fix_launchd (source-level guard — the dispatch is inline
     in main(), so this pins the branch against accidental removal)

Run: python3 tests/health-check-fix-launchd-label.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("health_check", SRC)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + label)
    if not cond:
        failures.append(label)


class _Recorder:
    """subprocess.run stand-in: records argv, scripted returncodes."""

    def __init__(self, kickstart_rc: int = 0, bootstrap_rc: int = 0):
        self.calls: list[list[str]] = []
        self.kickstart_rc = kickstart_rc
        self.bootstrap_rc = bootstrap_rc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        class R:
            stdout = "501\n"
            stderr = "scripted failure"
        r = R()
        if argv[0] == "/usr/bin/id":
            r.returncode = 0
        elif argv[:2] == ["/bin/launchctl", "kickstart"]:
            r.returncode = self.kickstart_rc
        elif argv[:2] == ["/bin/launchctl", "bootstrap"]:
            r.returncode = self.bootstrap_rc
        else:
            r.returncode = 0
        return r


def with_fake_home(tmp: str, make_plists: bool) -> None:
    la = Path(tmp) / "Library" / "LaunchAgents"
    la.mkdir(parents=True, exist_ok=True)
    if make_plists:
        for label in hc.LAUNCHD_BACKED_CHECKS.values():
            (la / f"{label}.plist").write_text("<plist/>")


def main() -> int:
    # a) the map covers exactly the two issue-#1888 services
    check(hc.LAUNCHD_BACKED_CHECKS.get("voice-agent") == "com.sutando.voice-agent",
          "voice-agent maps to com.sutando.voice-agent")
    check(hc.LAUNCHD_BACKED_CHECKS.get("web-client") == "com.sutando.web-client",
          "web-client maps to com.sutando.web-client")

    saved_home, saved_run = hc.Path.home, hc.subprocess.run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            hc.Path.home = staticmethod(lambda: Path(tmp))  # type: ignore[assignment]
            with_fake_home(tmp, make_plists=True)

            # b) restart path. voice-agent: NEVER a direct kickstart -k — the
            # repair goes through the guarded wrapper (amendment T4), which
            # runs the voice-lock.py takeover validation before its kickstart.
            rec = _Recorder(kickstart_rc=0)
            hc.subprocess.run = rec
            out = hc.fix_launchd(hc.LAUNCHD_BACKED_CHECKS["voice-agent"])
            kicks = [c for c in rec.calls if c[:2] == ["/bin/launchctl", "kickstart"]]
            wraps = [c for c in rec.calls
                     if any(str(part).endswith("restart-voice-agent.sh") for part in c)]
            check(out == "restarted com.sutando.voice-agent (guarded restart wrapper)"
                  and wraps and not kicks,
                  f"voice-agent repair uses the guarded wrapper, no direct kickstart (got {out!r}, kicks={kicks})")
            # web-client keeps the direct kickstart (its label never names
            # voice-agent, so the T4 gate does not apply).
            rec_wc = _Recorder(kickstart_rc=0)
            hc.subprocess.run = rec_wc
            out_wc = hc.fix_launchd(hc.LAUNCHD_BACKED_CHECKS["web-client"])
            kicks_wc = [c for c in rec_wc.calls if c[:2] == ["/bin/launchctl", "kickstart"]]
            check(out_wc == "restarted com.sutando.web-client"
                  and kicks_wc and kicks_wc[0][-1].endswith("/com.sutando.web-client"),
                  f"web-client mapped label kickstarts the launchd job (got {out_wc!r})")

            # c) bootstrap fallback
            rec2 = _Recorder(kickstart_rc=1, bootstrap_rc=0)
            hc.subprocess.run = rec2
            out2 = hc.fix_launchd(hc.LAUNCHD_BACKED_CHECKS["web-client"])
            boots = [c for c in rec2.calls if c[:2] == ["/bin/launchctl", "bootstrap"]]
            check(out2 == "bootstrapped com.sutando.web-client" and bool(boots),
                  f"kickstart failure falls back to bootstrap (got {out2!r})")

        # A KNOWN service whose plist is absent is not launchd-managed here —
        # startup.sh launches it directly. The old assertion accepted "no plist
        # found for com.sutando.voice-agent", which reads like a launchd failure
        # and names nothing the operator can run; a stale voice-agent survived
        # repeated --fix runs behind exactly that line.
        with tempfile.TemporaryDirectory() as tmp2:
            hc.Path.home = staticmethod(lambda: Path(tmp2))  # type: ignore[assignment]
            with_fake_home(tmp2, make_plists=False)
            rec3 = _Recorder()
            hc.subprocess.run = rec3
            out3 = hc.fix_launchd("com.sutando.voice-agent")
            check("not launchd-managed" in out3,
                  f"known service, no plist → says it is not launchd-managed (got {out3!r})")
            check("bash src/restart.sh" in out3,
                  f"...and names a runnable remedy (got {out3!r})")
            # The remedy must exist, or this message rots into a second dead end.
            check((REPO / "src" / "restart.sh").is_file(),
                  "the remedy the message names actually exists in the repo")
            check(not rec3.calls,
                  f"no launchctl call is attempted for an unmanaged service (got {rec3.calls})")

            # The genuinely-unknown label keeps the old wording: there is no
            # remedy to name because we do not know the job at all.
            out4 = hc.fix_launchd("com.sutando.not-a-real-job")
            check(out4 == "no plist found for com.sutando.not-a-real-job",
                  f"unknown label still reports plainly (got {out4!r})")
    finally:
        hc.Path.home = saved_home
        hc.subprocess.run = saved_run

    # d) dispatch guard: the inline --fix loop routes the map through
    # fix_launchd. Source-level because the loop lives inside main().
    src = SRC.read_text()
    fix_block = src.split("Attempting fixes...", 1)[-1]
    check('c["name"] in LAUNCHD_BACKED_CHECKS' in fix_block
          and 'fix_launchd(LAUNCHD_BACKED_CHECKS[c["name"]])' in fix_block,
          "--fix dispatch routes LAUNCHD_BACKED_CHECKS names through fix_launchd")

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
