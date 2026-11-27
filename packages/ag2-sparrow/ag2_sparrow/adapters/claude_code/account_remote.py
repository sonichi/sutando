"""claude-local's switch to a saved account, asked from the owner's phone and done by the desktop app.

The contract with the app (v2), all in the agent's own CLAUDE_CONFIG_DIR:

  request  `.ag2-switch-request.json` {"v": 2, "key", "nonce", "at"}, 0600, written by this module only
           when no switch is unresolved for the agent, and created exclusively (a temp file linked in).
  claimed  the app renames the request to `.ag2-switch-claimed-<nonce>.json` while it switches.
  result   then writes `.ag2-switch-result-<nonce>.json` {"v": 2, "nonce", "ok", "label" | "error"} and
           removes the claimed file.

Each request is kept, until its outcome is told, as `state/account-remote/<nonce>.json` (the card tapped,
its room, the address asked for). A detached watcher per nonce (`await`, holding `<nonce>.lock`) tells the
outcome in place of the card: switched or not from the result; a request still unclaimed after
UNCLAIMED_S is withdrawn (only while it is still this nonce's) and said not picked up; a claimed one is
said still in progress, and after GIVE_UP_S said untold (never failed) with the nonce kept, so a late
result is still told: `settle_late`, on the next card or tap, and `recover`, which restarts a watcher a
restart left dead. The app's answer is kept in that record before its receipt is removed, and the
record is dropped only once the card telling it is queued; an answer the app could not finish knowing
(`unknown`: it restarted mid-switch) says the account in use now, never that the switch failed.

    python3 -m ag2_sparrow.adapters.claude_code.account_remote await --root ROOT --nonce NONCE
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ... import account_switch, card_desk, claude_local_records as records, connector_cards
from ...bounded_file_read import read_regular, unreadable
from ...file_lock import locked_file, try_lock_fd, unlock_fd

REQUEST = ".ag2-switch-request.json"
CLAIMED = ".ag2-switch-claimed-%s.json"
RESULT = ".ag2-switch-result-%s.json"
FILE_MAX = 16 << 10
VERSION = 2
PENDING = "account-remote"
UNCLAIMED_S, GIVE_UP_S, POLL_S = 90.0, 600.0, 1.0
NONCE_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
BUSY = "A switch is already running for this agent."
UNCLAIMED = "The app on this computer did not pick this up — is it running? Nothing was changed."
WORKING = "Switching to %s… the app on this computer is still working on it."
UNVERIFIED = ("Switched to %s. The new sign-in hasn't been tried yet; if the next reply fails, sign in again from "
              "the account card.")
RESTARTED_ON = "The app restarted during the switch. This agent is now on %s."
RESTARTED = "The app restarted during the switch; check the account card to see which account is in use."
UNTOLD = "The app has not said how the switch ended; check the account card again in a moment."
_popen = subprocess.Popen


def _log(*parts):
    print("account-remote:", *parts, file=sys.stderr)


# ── this agent's own record of the switches it asked for ─────────────────────────────────────────────

def folder(root) -> Path:
    return records.state_dir(root) / PENDING


def load(root, nonce: str):
    if not NONCE_RE.fullmatch(str(nonce or "")):
        return None
    text, _ = read_regular(str(folder(root) / (nonce + ".json")), FILE_MAX, follow=False)
    try:
        rec = json.loads(text) if text else None
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("nonce") == nonce and rec.get("config_dir") else None


def save(root, rec: dict) -> None:
    d = folder(root)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path, tmp = d / (rec["nonce"] + ".json"), d / (rec["nonce"] + ".json.tmp")
    with os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    os.replace(str(tmp), str(path))


def drop(root, nonce: str) -> None:
    (folder(root) / (nonce + ".json")).unlink(missing_ok=True)


def outstanding(root) -> list:
    """The switches asked for and not yet told, oldest first."""
    try:
        names = sorted(p.name[:-5] for p in folder(root).iterdir() if p.name.endswith(".json"))
    except OSError:
        return []
    found = [r for r in (load(root, n) for n in names) if r]
    return sorted(found, key=lambda r: float(r.get("at") or 0))


def _lock(root):
    folder(root).mkdir(mode=0o700, parents=True, exist_ok=True)
    return locked_file(folder(root) / ".lock", create_mode=0o600)


def watcher_alive(root, nonce: str) -> bool:
    """Whether a watcher holds this nonce's lock now."""
    try:
        fd = os.open(str(folder(root) / (nonce + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        if try_lock_fd(fd):
            unlock_fd(fd)
            return False
        return True
    finally:
        os.close(fd)


# ── the app's files ──────────────────────────────────────────────────────────────────────────────────

def request_nonce(config_dir):
    """The nonce the request file holds; '' for one that cannot be read; None for no request file."""
    text, why = read_regular(str(Path(config_dir) / REQUEST), FILE_MAX, follow=False)
    if text is None:
        return "" if why else None
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    return str(data.get("nonce") or "") if isinstance(data, dict) else ""


def claimed(config_dir, nonce: str) -> bool:
    return os.path.lexists(str(Path(config_dir) / (CLAIMED % nonce)))


def claimed_any(config_dir) -> bool:
    try:
        return any(n.startswith(".ag2-switch-claimed-") and n.endswith(".json") for n in os.listdir(str(config_dir)))
    except OSError:
        return False


def _parsed(text, nonce: str) -> dict:
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("nonce") != nonce:
        return {"nonce": nonce, "ok": False, "error": "the app's answer could not be read"}
    return {k: data.get(k) for k in ("nonce", "ok", "label", "error", "unknown", "verified") if k in data}


RETRY = "retry"     # receive(): a receipt there that could not be read or kept just now; both files stay


def receive(root, rec: dict):
    """`rec` with the app's answer to it kept in sparrow's own record ("outcome"); None when there is no
    receipt; RETRY when there is one that could not be read (an I/O error) or whose answer could not be
    saved just now: the receipt and the record are left as they are, nothing is told, and it is read
    again later. Only a receipt read whole whose content is not a usable answer is kept as "could not be
    read". Called under the switch lock only. The answer is saved (a whole write) before the receipt is
    removed: a stop between any two steps leaves the receipt or the saved answer, never neither."""
    path = Path(rec["config_dir"]) / (RESULT % rec["nonce"])
    if not isinstance(rec.get("outcome"), dict):
        text, why = read_regular(str(path), FILE_MAX, follow=False)
        if text is None and not why:
            return None
        if text is None and unreadable(why):
            return RETRY
        rec = dict(rec, outcome=_parsed(text, rec["nonce"]))
        try:
            save(root, rec)
        except OSError:
            return RETRY
    try:
        path.unlink()
    except OSError:
        pass        # gone already, or removed on the next look; the answer is kept either way
    return rec


def write_request(config_dir, key: str, nonce: str, now: float) -> None:
    """The request, whole and owner-only, linked in only where there is none (two writers cannot both win)."""
    path = Path(config_dir) / REQUEST
    tmp = path.with_name("%s.%s.tmp" % (REQUEST, nonce))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"v": VERSION, "key": key, "nonce": nonce, "at": int(now)}))
            f.flush()
            os.fsync(f.fileno())
        os.link(str(tmp), str(path))
    finally:
        tmp.unlink(missing_ok=True)


def withdraw(config_dir, nonce: str) -> bool:
    """Take back the request while it is still this nonce's and unclaimed: True when it was taken back."""
    path = Path(config_dir) / REQUEST
    aside = path.with_name("%s.withdrawn-%s" % (REQUEST, nonce))
    try:
        os.rename(str(path), str(aside))
    except OSError:
        return False        # the app claimed it meanwhile
    try:
        text, _ = read_regular(str(aside), FILE_MAX, follow=False)
        try:
            mine = isinstance(json.loads(text or ""), dict) and json.loads(text).get("nonce") == nonce
        except ValueError:
            mine = False
        if not mine:
            try:
                os.link(str(aside), str(path))      # not ours: put back, unless a newer one is there
            except OSError:
                pass
        return mine
    finally:
        aside.unlink(missing_ok=True)


# ── telling the owner ────────────────────────────────────────────────────────────────────────────────

def note(state: str, text: str):
    """A card with no buttons, shown in place of the account card the owner tapped."""
    agent_card, _ = connector_cards._core()
    return agent_card.build(account_switch.NOTE, state, title="Account", body="Account of Claude Code", text=text)


def one_line(text, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def current_account(rec: dict) -> str:
    """The account the agent is on now, as its account card names it; '' when it cannot be read."""
    from . import account_card
    try:
        return account_card.label({"CLAUDE_CONFIG_DIR": rec["config_dir"], "CLAUDE": rec.get("claude") or "claude"})
    except Exception:  # noqa: BLE001 - said as unknown
        return ""


def told(data, label: str, current=lambda: "") -> tuple:
    """(state, words) for the app's answer. One the app could not finish knowing (it restarted during the
    switch, `unknown`) is never said failed: it says the account in use now, as `current()` reads it."""
    if data.get("unknown") is True:
        now = current()
        return "info", (RESTARTED_ON % now if now else RESTARTED)
    if data.get("ok") is True:
        name = account_switch.shown(str(data.get("label") or "").split(" (")[0])
        if data.get("verified") is False:      # switched, its sign-in not tried yet; an older app sends none
            return "done", UNVERIFIED % (name or label)
        return "done", "Switched to %s." % (name or label)
    return "error", "Could not switch: %s" % (one_line(data.get("error")) or "the app gave no reason")


def tell(rec: dict, state: str, words: str, phase: str, log=_log) -> bool:
    """Queue `words` in place of the card tapped; whether it was queued."""
    if card_desk.post(rec.get("bridge_state") or "", rec.get("room") or "", note(state, words), log,
                      key="account-remote:%s:%s" % (rec["nonce"], phase), at_ns=time.time_ns(),
                      edit=str(rec.get("edit") or "")):
        return True
    log("not posted:", words)
    return False


def settle(root, rec: dict, log=_log) -> str:
    """Tell the answer kept in `rec`; the record is dropped only once its card is queued, else it stays,
    answer and all, for the next try (`settle_late`)."""
    state, words = told(rec["outcome"], str(rec.get("label") or "that account"), lambda: current_account(rec))
    if tell(rec, state, words, "result", log):
        drop(root, rec["nonce"])
    return words


def settle_late(root, log=_log) -> list:
    """Tell what came of switches no watcher is telling: an answer kept but not yet told, a late result, or a
    request the app no longer holds in any form. What was told."""
    with _lock(root):
        return _settle_late(root, log)


def _settle_late(root, log) -> list:
    said = []
    for rec in outstanding(root):
        nonce, config_dir = rec["nonce"], rec["config_dir"]
        got = receive(root, rec)
        if got is RETRY:
            continue
        if got is not None:
            said.append(settle(root, got, log))
            continue
        if watcher_alive(root, nonce) or request_nonce(config_dir) == nonce or claimed(config_dir, nonce):
            continue
        got = receive(root, rec)       # written just before the claimed file was removed
        if got is RETRY:
            continue
        if got is not None:
            said.append(settle(root, got, log))
            continue
        if not rec.get("gave_up"):
            tell(rec, "info", UNTOLD, "lost", log)
            said.append(UNTOLD)
        drop(root, nonce)
    return said


def busy(root, config_dir) -> bool:
    return request_nonce(config_dir) is not None or claimed_any(config_dir) or bool(outstanding(root))


def publish(root, config_dir, key: str, label: str, room: str, edit: str, bridge_state: str, log=_log, now=None,
            claude: str = ""):
    """Ask the app for the switch: the nonce, or None when a switch is unresolved for this agent (nothing
    written). Raises OSError when the request cannot be written; nothing is then kept."""
    now = time.time() if now is None else now
    with _lock(root):
        _settle_late(root, log)
        if busy(root, config_dir):
            return None
        nonce = str(uuid.uuid4())
        save(root, {"nonce": nonce, "label": label, "room": room, "edit": edit, "config_dir": str(config_dir),
                    "bridge_state": str(bridge_state), "at": now, "claude": claude})
        try:
            write_request(config_dir, key, nonce, now)
        except OSError:
            drop(root, nonce)
            raise
    return nonce


def start(root, nonce: str, popen=None) -> bool:
    """The watcher for `nonce`, detached."""
    try:
        with open(records.state_dir(root) / "account-remote.log", "ab") as fh:
            (popen or _popen)([sys.executable, "-m", __name__, "await", "--root", str(root), "--nonce", nonce],
                              stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        _log("watcher not started:", "%s: %s" % (type(e).__name__, str(e)[:200]))
        return False
    return True


def recover(root, popen=None, log=_log) -> int:
    """Tell what is owed (`settle_late`: an answer whose card could not be queued, a late result), then restart
    the watcher of each switch still being watched for when a restart ended its watcher."""
    settle_late(root, log)
    started = 0
    for rec in outstanding(root):
        if not rec.get("gave_up") and not watcher_alive(root, rec["nonce"]):
            started += start(root, rec["nonce"], popen)
    return started


def watch(root, nonce: str, *, sleep=time.sleep, clock=time.time, log=_log) -> str:
    """The detached watcher of one request: what it told; '' when another watcher has it or it is settled."""
    if load(root, nonce) is None:
        return ""
    fd = os.open(str(folder(root) / (nonce + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not try_lock_fd(fd):
            return ""
        return _watch(root, nonce, sleep, clock, log)
    finally:
        os.close(fd)


def _watch(root, nonce, sleep, clock, log) -> str:
    while True:
        with _lock(root):
            rec = load(root, nonce)
            if rec is None:
                return ""
            config_dir, at = rec["config_dir"], float(rec.get("at") or 0)
            now = clock()
            got = receive(root, rec)
            if got is RETRY:
                if now >= at + GIVE_UP_S:       # still unreadable: said untold, kept for a later look
                    return _give_up(root, rec, log)
            elif got is not None:
                return settle(root, got, log)
            elif request_nonce(config_dir) == nonce:
                if now >= at + UNCLAIMED_S and withdraw(config_dir, nonce):
                    tell(rec, "error", UNCLAIMED, "unclaimed", log)
                    drop(root, nonce)
                    return UNCLAIMED
            elif claimed(config_dir, nonce):
                if now >= at + GIVE_UP_S:
                    return _give_up(root, rec, log)
                if now >= at + UNCLAIMED_S and not rec.get("working"):
                    tell(rec, "pending", WORKING % rec.get("label"), "working", log)
                    save(root, dict(rec, working=True))
            else:
                got = receive(root, rec)
                if got is not None and got is not RETRY:
                    return settle(root, got, log)
                if got is None:
                    tell(rec, "info", UNTOLD, "lost", log)
                    drop(root, nonce)
                    return UNTOLD
        sleep(POLL_S)


def _give_up(root, rec: dict, log) -> str:
    """Stop waiting without saying how it ended; the record stays, so a later answer is still told."""
    tell(rec, "info", UNTOLD, "untold", log)
    save(root, dict(rec, gave_up=True))
    return UNTOLD


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="account_remote", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("await")
    a.add_argument("--root", required=True)
    a.add_argument("--nonce", required=True)
    args = p.parse_args(argv)
    print(watch(args.root, args.nonce) or "nothing to watch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
