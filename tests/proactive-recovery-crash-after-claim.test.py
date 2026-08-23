#!/usr/bin/env python3
"""A crash after the private rename must not strand the body.
The private name is unscanned by pollers, so recovery has to find it itself."""
import importlib.util
import os
import sys
from unittest import mock
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pr", REPO / "src" / "proactive_recovery.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)

BODY = "owner message that must survive a crash"
DEAD_PID = 999999  # no such process, so the claim is genuinely orphaned


def case(name, build, check):
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        build(d)
        recovered = pr.recover_orphan_sending_files(d)
        ok, detail = check(d, recovered)
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
        return ok


def crashed_mid_recovery(d):
    (d / f"proactive-crash.sending.recover-{DEAD_PID}-0").write_text(BODY)


def check_recovered(d, recovered):
    txt = d / "proactive-crash.txt"
    ok = txt.exists() and txt.read_text() == BODY
    leftovers = sorted(p.name for p in d.iterdir() if ".recover-" in p.name)
    return ok and not leftovers, f"recovered={recovered} txt={txt.exists()} leftover_private={leftovers}"


def live_holder(d):
    # Same shape, but the pid is THIS process: another worker is mid-recovery.
    (d / f"proactive-live.sending.recover-{os.getpid()}-0").write_text(BODY)


def check_untouched(d, recovered):
    private = d / f"proactive-live.sending.recover-{os.getpid()}-0"
    txt = d / "proactive-live.txt"
    ok = private.exists() and private.read_text() == BODY and not txt.exists()
    return ok, f"recovered={recovered} private_intact={private.exists()} txt_created={txt.exists()}"


def plain_claim_still_works(d):
    (d / "proactive-plain.sending").write_text(BODY)


def check_plain(d, recovered):
    txt = d / "proactive-plain.txt"
    ok = recovered == 1 and txt.exists() and txt.read_text() == BODY
    return ok, f"recovered={recovered} txt={txt.exists()}"


def two_bodies(d):
    # A prior crashed run left a private claim under the name this run will pick.
    (d / "proactive-x.sending").write_text("ordinary")
    (d / f"proactive-x.sending.recover-{os.getpid()}-0").write_text("private")


def check_both_survive(d, recovered):
    bodies = {p.read_text() for p in d.iterdir() if p.is_file()}
    ok = {"ordinary", "private"} <= bodies
    return ok, f"recovered={recovered} bodies={sorted(bodies)}"


def check_permission_holder(d, recovered):
    private = d / "proactive-perm.sending.recover-4242-0"
    return (private.exists() and not (d / "proactive-perm.txt").exists(),
            f"recovered={recovered} private_intact={private.exists()}")


def run_permission_case():
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "proactive-perm.sending.recover-4242-0").write_text(BODY)
        # A pid owned by another user answers signal 0 with EPERM: it is alive.
        with mock.patch.object(os, "kill", side_effect=PermissionError):
            recovered = pr.recover_orphan_sending_files(d)
        ok, detail = check_permission_holder(d, recovered)
        print(f"{'PASS' if ok else 'FAIL'}  C4 EPERM holder is treated as live\n      {detail}")
        return ok


def run_vanished_source_case():
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "proactive-gone.sending").write_text(BODY)
        real_unlink = Path.unlink
        state = {"first": True, "fired": False}

        def flaky_unlink(self, *a, **k):
            # A peer removes the source between our link and our unlink.
            if state["first"] and self.name == "proactive-gone.sending":
                state["first"] = False
                state["fired"] = True
                real_unlink(self, *a, **k)
                raise FileNotFoundError
            return real_unlink(self, *a, **k)

        with mock.patch.object(Path, "unlink", flaky_unlink):
            recovered = pr.recover_orphan_sending_files(d)
        txt = d / "proactive-gone.txt"
        if not state["fired"]:
            print("FAIL  C5 SETUP: the unlink race never fired — assertion would be vacuous")
            return False
        ok = txt.exists() and txt.read_text() == BODY
        print(f"{'PASS' if ok else 'FAIL'}  C5 source vanishing after the link still recovers\n      fired=True recovered={recovered} txt={txt.exists()}")
        return ok


def restore_collision(d):
    # A real collision (different inodes) AND the base claim name is occupied,
    # so the claim cannot be put back and must stay under its private name.
    (d / "proactive-y.txt").write_text("newer result")
    (d / "proactive-y.sending").write_text("base claim")
    (d / f"proactive-y.sending.recover-{DEAD_PID}-0").write_text("stranded body")


def check_restore_collision(d, recovered):
    bodies = {p.read_text() for p in d.iterdir() if p.is_file()}
    privates = [p.name for p in d.iterdir() if ".recover-" in p.name]
    ok = {"newer result", "base claim", "stranded body"} <= bodies and bool(privates)
    return ok, f"recovered={recovered} bodies={len(bodies)} kept_private={privates}"


results = [
    case("C6 unrestorable claim is kept, not dropped", restore_collision, check_restore_collision),
    case("C0 a colliding private claim is never overwritten", two_bodies, check_both_survive),
    case("C1 crashed mid-recovery -> body recovered to .txt", crashed_mid_recovery, check_recovered),
    case("C2 live holder's private claim left untouched", live_holder, check_untouched),
    case("C3 an ordinary .sending claim still recovers", plain_claim_still_works, check_plain),
    run_permission_case(),
    run_vanished_source_case(),
]
failed = [i for i, ok in enumerate(results, 1) if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
