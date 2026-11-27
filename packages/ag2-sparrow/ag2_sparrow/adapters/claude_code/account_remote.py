"""claude-local's switch to a saved account, asked from the owner's phone and done by the desktop app.

The contract with the app (v2), all in the agent's own CLAUDE_CONFIG_DIR:

  request  `.ag2-switch-request.json` {"v": 2, "key", "nonce", "at"}, 0600, written by this module only
           when no switch is unresolved for the agent, and created exclusively (a temp file linked in).
  claimed  the app renames the request to `.ag2-switch-claimed-<nonce>.json` while it switches.
  result   then writes `.ag2-switch-result-<nonce>.json` {"v": 2, "nonce", "ok", "label" | "error",
           "verified", "unknown"} and removes the claimed file. `verified` false (or absent): switched, the
           new sign-in not tried yet. `unknown` true: the app restarted mid-switch and cannot say.

Each request is kept, until its outcome is told, as `state/account-remote/<nonce>.json` (the card tapped,
its room, the address asked for). A detached watcher per nonce (`await`, holding `<nonce>.lock`) tells the
outcome in place of the card: switched or not from the result; a request still unclaimed after
UNCLAIMED_S is withdrawn (only while it is still this nonce's) and said not picked up; a claimed one is
said still in progress, and after GIVE_UP_S said untold (never failed) with the nonce kept, so a late
result is still told: `settle_late`, on the next card or tap, and `recover`, which restarts a watcher a
restart left dead. The app's answer is kept in that record before its receipt is removed, and the
record is dropped only once the card telling it is queued; an answer the app could not finish knowing
(`unknown`: it restarted mid-switch) and a receipt read whole that is not an answer say the account in use
now, never that the switch failed. Sparrow never removes a claim: only the app releases one (at its next
launch it answers a claim it left with `unknown`). A claim unanswered STALE_CLAIM_S after the request is told
to the owner once as not known yet (`told_uncertain`, journalled in the record), the record kept so a late
receipt is still shown ("… after all"); while the claim is there the agent stays busy and a tap is told that
reopening the app resolves it. A request file that is not a whole request (empty, unparseable or unreadable)
and older than JUNK_REQUEST_S is not one sparrow wrote: it stops counting as busy and is removed while it is
still the same file. The watcher's log is owner-only, like every file kept here.

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
STALE_CLAIM_S = 900.0       # a claim with no result this long after the request: told once as not known yet
JUNK_REQUEST_S = 120.0      # a request file that is not a whole request, this old: never sparrow's, and stale
LOG = "account-remote.log"
NONCE_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
BUSY = "A switch is already running for this agent."
UNCLAIMED = "The app on this computer did not pick this up — is it running? Nothing was changed."
WORKING = "Switching to %s… the app on this computer is still working on it."
UNVERIFIED = ("Switched to %s. The new sign-in hasn't been tried yet; if the next reply fails, sign in again from "
              "the account card.")
RESTARTED_ON = "The app restarted during the switch. This agent is now on %s."
RESTARTED = "The app restarted during the switch; check the account card to see which account is in use."
UNREAD_ON = "The app's answer about this switch could not be read. This agent is now on %s."
UNREAD = "The app's answer about this switch could not be read; check the account card to see which account is in use."
UNCERTAIN = ("It isn't known yet how the switch to %s ended; check the account card. If the app on that computer "
             "was closed or crashed, opening it again will finish this.")
UNRESOLVED = ("A previous switch for this agent is still unresolved; opening the app on the computer it runs on "
              "will resolve it.")
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
        if hasattr(os, "fchmod"):           # a leftover temp keeps its mode through O_TRUNC
            os.fchmod(f.fileno(), 0o600)
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


def claim_age(config_dir, nonce: str, now=None):
    """Seconds since the claim of `nonce` last changed; None when there is none."""
    try:
        at = os.lstat(str(Path(config_dir) / (CLAIMED % nonce))).st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - at


def claims(config_dir) -> list:
    """The nonces of the claims the app holds (each stays until the app itself answers it)."""
    try:
        names = os.listdir(str(config_dir))
    except OSError:
        return []
    return [n[len(".ag2-switch-claimed-"):-len(".json")] for n in names
            if n.startswith(".ag2-switch-claimed-") and n.endswith(".json")]


def claimed_any(config_dir) -> bool:
    return bool(claims(config_dir))


def junk_request(config_dir, now=None):
    """The request file's (inode, mtime) when it is not a whole request (empty, unparseable, unreadable) and
    at least JUNK_REQUEST_S old: sparrow writes requests whole, so it is not one of sparrow's. None otherwise."""
    if request_nonce(config_dir) != "":
        return None
    try:
        st = os.lstat(str(Path(config_dir) / REQUEST))
    except OSError:
        return None
    if (time.time() if now is None else now) - st.st_mtime < JUNK_REQUEST_S:
        return None
    return st.st_ino, st.st_mtime


def clear_junk_request(config_dir, now=None) -> bool:
    """Remove a junk request (`junk_request`) only while it is the same file: moved aside, compared by inode
    and mtime, put back when it is not. Whether one was removed."""
    seen = junk_request(config_dir, now)
    if seen is None:
        return False
    path = Path(config_dir) / REQUEST
    aside = path.with_name(REQUEST + ".junk")
    try:
        os.rename(str(path), str(aside))
    except OSError:
        return False
    try:
        st = os.lstat(str(aside))
        if (st.st_ino, st.st_mtime) == seen:
            return True
        try:
            os.link(str(aside), str(path))      # another file since: left as it was
        except OSError:
            pass
        return False
    finally:
        aside.unlink(missing_ok=True)


def _parsed(text, nonce: str) -> dict:
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("nonce") != nonce or not usable(data):
        return {"nonce": nonce, "unreadable": True}      # not an answer: how the switch ended is not known
    return {k: data.get(k) for k in ("nonce", "ok", "label", "error", "unknown", "verified") if k in data}


def usable(data: dict) -> bool:
    """Whether a receipt's answer has the contract's shape: `unknown`, when there, a bool, and true is an answer
    by itself; else `ok` a bool, a success naming its account (`label`, a string), `verified` when there a bool.
    A success without `verified` (an older app) is usable, and shown as not yet tried."""
    if "unknown" in data and not isinstance(data["unknown"], bool):
        return False
    if data.get("unknown") is True:
        return True
    if not isinstance(data.get("ok"), bool):
        return False
    if "verified" in data and not isinstance(data["verified"], bool):
        return False
    return not data["ok"] or (isinstance(data.get("label"), str) and bool(data["label"].strip()))


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


def told(data, label: str, current=lambda: "", after_all: bool = False) -> tuple:
    """(state, words) for the app's answer. One that does not say how the switch ended (the app restarted
    during it, `unknown`; a receipt that is not an answer, `unreadable`) is never said failed: it says the
    account in use now, as `current()` reads it. `after_all`: the owner was told it was not known yet."""
    for flag, on, plain in (("unknown", RESTARTED_ON, RESTARTED), ("unreadable", UNREAD_ON, UNREAD)):
        if data.get(flag) is True:
            now = current()
            return "info", (on % now if now else plain)
    if data.get("ok") is True:
        name = (account_switch.shown(str(data.get("label") or "").split(" (")[0]) or label) + (
            " after all" if after_all else "")
        if data.get("verified") is not True:      # switched; its sign-in not tried yet, or not said (older app)
            return "done", UNVERIFIED % name
        return "done", "Switched to %s." % name
    return "error", "Could not switch: %s" % (one_line(data.get("error")) or "the app gave no reason")


def tell(rec: dict, state: str, words: str, phase: str, log=_log, at_ns: int = 0) -> bool:
    """Queue `words` in place of the card tapped; whether it was queued. With the `at_ns` first decided (kept
    in the record) a repeat is the same keyed card, never a second one."""
    if card_desk.post(rec.get("bridge_state") or "", rec.get("room") or "", note(state, words), log,
                      key="account-remote:%s:%s" % (rec["nonce"], phase), at_ns=at_ns or time.time_ns(),
                      edit=str(rec.get("edit") or "")):
        return True
    log("not posted:", words)
    return False


def settle(root, rec: dict, log=_log) -> str:
    """Tell the answer kept in `rec`; the record is dropped only once its card is queued, else it stays,
    answer and all, for the next try (`settle_late`)."""
    state, words = told(rec["outcome"], str(rec.get("label") or "that account"), lambda: current_account(rec),
                        after_all=bool(rec.get("told_uncertain")))
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
        if watcher_alive(root, nonce) or request_nonce(config_dir) == nonce:
            continue
        if claimed(config_dir, nonce):
            # Only the app releases a claim. Past STALE_CLAIM_S: told once, journalled first (`uncertain_at`,
            # so a repeat is the same keyed card), then marked told; the record stays for the late receipt.
            if not rec.get("told_uncertain") and time.time() - float(rec.get("at") or 0) >= STALE_CLAIM_S:
                try:
                    if not rec.get("uncertain_at"):
                        rec = dict(rec, uncertain_at=time.time_ns())
                        save(root, rec)
                    words = UNCERTAIN % (rec.get("label") or "that account")
                    if tell(rec, "info", words, "uncertain", log, at_ns=int(rec["uncertain_at"])):
                        said.append(words)
                        save(root, dict(rec, told_uncertain=True))
                except OSError as e:
                    log("not journalled:", "%s: %s" % (type(e).__name__, str(e)[:200]))
            continue
        got = receive(root, rec)       # written just before the claimed file was removed
        if got is RETRY:
            continue
        if got is not None:
            said.append(settle(root, got, log))
            continue
        if not rec.get("gave_up") and not rec.get("told_uncertain"):
            tell(rec, "info", UNTOLD, "lost", log)
            said.append(UNTOLD)
        drop(root, nonce)
    return said


def busy(root, config_dir) -> bool:
    """Whether a switch is unresolved for this agent: a request (but junk past JUNK_REQUEST_S), a claim the
    app holds, or a switch of sparrow's not yet told."""
    request = request_nonce(config_dir) is not None and junk_request(config_dir) is None
    return request or claimed_any(config_dir) or bool(outstanding(root))


def busy_words(root, config_dir) -> str:
    """What a tap is told while `busy`: that a previous switch is unresolved when a claim has waited past
    STALE_CLAIM_S (the app was likely closed mid-switch), else that one is running."""
    if any((claim_age(config_dir, n) or 0) >= STALE_CLAIM_S for n in claims(config_dir)) or \
            any(r.get("told_uncertain") for r in outstanding(root)):
        return UNRESOLVED
    return BUSY


def publish(root, config_dir, key: str, label: str, room: str, edit: str, bridge_state: str, log=_log, now=None,
            claude: str = ""):
    """Ask the app for the switch: the nonce, or None when a switch is unresolved for this agent (nothing
    written). Raises OSError when the request cannot be written; nothing is then kept."""
    now = time.time() if now is None else now
    with _lock(root):
        _settle_late(root, log)
        clear_junk_request(config_dir)
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


def private_log(root):
    """The watchers' log, opened for appending and owner-only whatever the umask or an earlier mode: what it
    says may name an account."""
    fd = os.open(str(records.state_dir(root) / LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "ab")


def start(root, nonce: str, popen=None) -> bool:
    """The watcher for `nonce`, detached."""
    try:
        with private_log(root) as fh:
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
