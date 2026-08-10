#!/usr/bin/env python3
"""The same-inode cleanup must not unlink a claim a peer created meanwhile.
Unlinking the SHARED pathname destroys whatever answers to it by then."""
import importlib.util
import os
import pathlib
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pr", REPO / "src" / "proactive_recovery.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)

PEER_BODY = "peer body that must survive"


def run() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        results = Path(tmp)
        txt = results / "proactive-race.txt"
        sending = results / "proactive-race.sending"
        txt.write_text("original body")
        os.link(txt, sending)  # half-made claim: one inode, two names

        real_stat = pathlib.Path.stat
        state = {"claim_stat_seen": False, "fired": False}

        def racing_stat(self, *a, **kw):
            result = real_stat(self, *a, **kw)
            # Fire between the inode comparison and the unlink. Anchor on the claim's
            # own stat: Path.exists() routes through Path.stat only on some versions.
            if self.name != "proactive-race.txt":
                state["claim_stat_seen"] = True
            elif state["claim_stat_seen"] and not state["fired"]:
                state["fired"] = True
                if sending.exists():
                    sending.unlink()
                sending.write_text(PEER_BODY)
            return result

        pathlib.Path.stat = racing_stat
        try:
            recovered = pr.recover_orphan_sending_files(results)
        finally:
            pathlib.Path.stat = real_stat

        if not state["fired"]:
            print("SETUP FAIL: the race was never injected — the window moved")
            return False
        survived = (
            (sending.exists() and sending.read_text() == PEER_BODY)
            or (txt.exists() and txt.read_text() == PEER_BODY)
        )
        print(f"recovered={recovered}")
        print(f"txt_exists={txt.exists()} sending_exists={sending.exists()}")
        print(f"peer_body_survived={survived}")
        return survived


if __name__ == "__main__":
    ok = run()
    print("PASS" if ok else "FAIL: the peer's body was destroyed by the same-inode unlink")
    sys.exit(0 if ok else 1)
