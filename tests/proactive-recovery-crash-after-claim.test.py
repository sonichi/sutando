#!/usr/bin/env python3
"""A crash after the private rename must not strand the body.
The private name is unscanned by pollers, so recovery has to find it itself."""
import importlib.util
import os
import sys
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


results = [
    case("C1 crashed mid-recovery -> body recovered to .txt", crashed_mid_recovery, check_recovered),
    case("C2 live holder's private claim left untouched", live_holder, check_untouched),
    case("C3 an ordinary .sending claim still recovers", plain_claim_still_works, check_plain),
]
failed = [i for i, ok in enumerate(results, 1) if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
