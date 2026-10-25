"""Shared tmux pane-key argv and crash-safe send guard. Owns the socket mutex,
pending ticket record, revocation, uncertainty fence and explicit recovery.
Stdlib only; vendored byte-identical into ag2-sparrow beside its shell script."""
from pathlib import Path
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

SCRIPT = Path(__file__).resolve().parent / "tmux-pane-keys.sh"
# One bounded operation (5 s + 1 s TERM grace), the guard's start-up and polling, with margin.
TIMEOUT_S = 22
BUSY = 75
UNCERTAIN = 125
# Exit of a fenced action: 0 sent, 1 predicate false (the ticket ends .orphaned, nothing sent),
# UNCERTAIN for a ticket neither sent nor orphaned, BUSY for another sender.
WITHHELD = 1
# The script's own orphan command, substituted by the script for the ticket it claimed.
ORPHAN = "$ORPHAN"
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_ID_RE = re.compile(r"^\$\d+$")
TITLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def argv(socket, target, *keys, tmux="tmux"):
    """The guarded send; the script takes this interpreter explicitly, never a repo helper."""
    return ["bash", str(SCRIPT), "--tmux", str(tmux), "--python", sys.executable, "-S", str(socket),
            "-t", target, "--", *keys]


def fence_argv(socket, target_pane, predicate_format, action_cmd, tmux="tmux"):
    """The guarded fenced action: the script builds fenced(target_pane, predicate_format, action_cmd)
    with its own ORPHAN and runs it through the ticket claim, so a false predicate sends nothing."""
    return ["bash", str(SCRIPT), "--tmux", str(tmux), "--python", sys.executable, "-S", str(socket),
            "-t", target_pane, "--fence", predicate_format, action_cmd]


def tmux_quote(value):
    """A tmux single-quoted word, the script's rule: literal; an embedded ' closes, is double-quoted, reopens."""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def predicate(nonce, pane_pid, session_id, session_created, title_token=None):
    """The fence condition the server evaluates against the pane `-t` selects: the exact pane pid,
    session id and creation time, the generation nonce matched by glob against the pane's own start
    command (`hold --generation=<hex> --`, never a path in the format), and the pane title when a
    token is given. No pane_id term: the target already names the pane."""
    if not NONCE_RE.match(str(nonce)):
        raise ValueError("the generation nonce is 32 hex characters")
    if not SESSION_ID_RE.match(str(session_id)):
        raise ValueError("a tmux session id is $<n>")
    terms = ["#{==:#{pane_pid},%d}" % int(pane_pid), "#{==:#{session_id},%s}" % session_id,
             "#{==:#{session_created},%d}" % int(session_created),
             "#{m:*--generation=%s --*,#{pane_start_command}}" % nonce]
    if title_token is not None:
        if not TITLE_TOKEN_RE.match(str(title_token)):
            raise ValueError("a title token is 1-64 of [A-Za-z0-9_-]")
        terms.append("#{==:#{pane_title},%s}" % title_token)
    out = terms[-1]
    for term in reversed(terms[:-1]):
        out = "#{&&:%s,%s}" % (term, out)
    return out


def fenced(target_pane, predicate_format, action_cmd, orphan=ORPHAN):
    """The one server command of a fenced action, exactly as the script builds it: the action runs
    only while the predicate holds; otherwise the ticket is orphaned, which the script exits 1 on."""
    false_branch = tmux_quote("run-shell " + tmux_quote(orphan))
    return "if-shell -F -t %s %s %s %s" % (tmux_quote(target_pane), tmux_quote(predicate_format),
                                            tmux_quote(action_cmd), false_branch)


def guard_path(socket):
    return Path(str(socket) + ".pane-keys-lock")


def prepare_guard(socket):
    guard = guard_path(socket)
    if not guard.exists():
        prepared = Path(tempfile.mkdtemp(prefix=guard.name + ".", dir=guard.absolute().parent))
        (prepared / "mutex").touch()
        try:
            guard.symlink_to(prepared)
        except FileExistsError:
            shutil.rmtree(prepared)
    if not (guard / "mutex").is_file():
        raise ValueError("legacy fence requires explicit recovery")
    return guard / "mutex"


def _pending_ticket(guard):
    pending = guard / "pending.json"
    if not pending.exists():
        return None
    if pending.stat().st_size > 4096:
        raise ValueError("oversized pending record")
    ticket = Path(json.loads(pending.read_text())["ticket"])
    if not ticket.is_absolute() or ticket.name != "ticket" or not ticket.parent.name.startswith("tmux-pane-keys."):
        raise ValueError("invalid pending ticket")
    return ticket


def finish_guard(socket, completed=False, sweep=False):
    guard = guard_path(socket)
    if completed:
        (guard / "pending.json").unlink(missing_ok=True)
        (guard / "uncertain").unlink(missing_ok=True)
        return 0
    ticket = _pending_ticket(guard)
    if ticket is not None:
        try:
            ticket.unlink()
        except FileNotFoundError:
            if not ticket.with_name("ticket.orphaned").exists():
                (guard / "uncertain").touch()
                return UNCERTAIN
            # An orphaned claim is terminal proof that this generation sent nothing.
            (guard / "uncertain").unlink(missing_ok=True)
        if sweep:
            shutil.rmtree(ticket.parent, ignore_errors=True)
    if (guard / "uncertain").exists():
        return UNCERTAIN
    (guard / "pending.json").unlink(missing_ok=True)
    return 0


def _lock_fd(fd):
    try:
        from .file_lock import lock_fd    # the vendored package layout
    except ImportError:
        from file_lock import lock_fd     # the repo's src/ on sys.path

    try:
        lock_fd(fd, blocking=False)
    except BlockingIOError:
        return BUSY
    return 0


def acquire_guard(socket, fd):
    rc = _lock_fd(fd)
    if rc:
        return rc
    return finish_guard(socket, sweep=True)


def begin_guard(socket, ticket):
    guard = guard_path(socket)
    ticket = Path(ticket).absolute()
    ticket.touch()
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=guard)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"ticket": str(ticket)}, stream)
        os.replace(name, guard / "pending.json")
    finally:
        Path(name).unlink(missing_ok=True)


def fence_status(socket):
    guard = guard_path(socket)
    if not guard.exists() and not guard.is_symlink():
        return "clear"
    try:
        with (guard / "mutex").open("rb") as stream:
            if _lock_fd(stream.fileno()) == BUSY:
                return "busy"
            ticket = _pending_ticket(guard)
            if ticket is not None and ticket.with_name("ticket.orphaned").exists():
                return "clear"  # the next sender reconciles it
            if (guard / "uncertain").exists() or (ticket is not None and not ticket.exists()):
                return "uncertain"
            return "clear"
    except (OSError, ValueError, KeyError, TypeError):
        return "uncertain"


def replace_unreadable_guard(socket):
    """Recovery only: a guard with no lockable mutex file has no live holder, so its shape is replaced."""
    guard = guard_path(socket)
    mutex = guard / "mutex"
    if mutex.is_file():
        return
    if mutex.is_dir() and not mutex.is_symlink():
        shutil.rmtree(mutex)
    elif mutex.is_symlink() or mutex.exists():
        mutex.unlink()
    if guard.is_dir():
        mutex.touch()
    elif guard.is_symlink() or guard.exists():
        guard.unlink()


def run_guarded(socket, args):
    with prepare_guard(socket).open("ab") as mutex:
        rc = acquire_guard(socket, mutex.fileno())
        if rc:
            return rc
        work = Path(tempfile.mkdtemp(prefix="tmux-pane-keys."))
        completed = False
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        def interrupted(signum, _frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            begin_guard(socket, work / "ticket")
            with (work / "stdout").open("wb") as out, (work / "stderr").open("wb") as err:
                result = subprocess.run(["bash", str(SCRIPT), "--guarded", str(work), *args],
                                        stdout=out, stderr=err, close_fds=True)
            rc = result.returncode
            claimed = (work / "ticket.claimed").exists()
            if rc == 0 and not claimed:
                rc = 1 if (work / "ticket").exists() or (work / "ticket.orphaned").exists() else UNCERTAIN
                print("tmux-pane-keys: tmux did not send the keys (ticket unclaimed, or pane held in a mode);"
                      " delivery not confirmed",
                      file=sys.stderr)
            # Only a clean claimed send is proven; any other claimed outcome keeps the fence.
            completed = rc == 0 and claimed
            finish_rc = finish_guard(socket, completed=completed)
            sys.stdout.buffer.write((work / "stdout").read_bytes())
            sys.stderr.buffer.write((work / "stderr").read_bytes())
            return finish_rc or rc
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if finish_guard(socket, completed=completed) == 0:
                shutil.rmtree(work, ignore_errors=True)


def _main():
    action, socket, *args = sys.argv[1:]
    try:
        if action == "run":
            rc = run_guarded(socket, args)
        elif action == "recover":
            replace_unreadable_guard(socket)
            with prepare_guard(socket).open("ab") as stream:
                rc = _lock_fd(stream.fileno())
                if rc != BUSY:
                    rc = finish_guard(socket, completed=True)
        else:
            return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"tmux-pane-keys: guard unreadable; recovery required: {exc}", file=sys.stderr)
        return UNCERTAIN
    if rc == BUSY:
        print("tmux-pane-keys: another sender is busy; retry later", file=sys.stderr)
    elif rc == UNCERTAIN:
        print(f"tmux-pane-keys: uncertain send; sends blocked by {guard_path(socket)} until recovery", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(_main())
