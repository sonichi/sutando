"""Shared tmux pane-key argv and crash-safe send guard. Owns the socket mutex,
pending ticket record, revocation, uncertainty fence and explicit recovery."""
from pathlib import Path
import json
import os
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


def argv(socket, target, *keys, tmux="tmux"):
    return ["bash", str(SCRIPT), "--tmux", str(tmux), "-S", str(socket), "-t", target, "--", *keys]


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


def finish_guard(socket, completed=False):
    guard = guard_path(socket)
    if completed:
        (guard / "pending.json").unlink(missing_ok=True)
        (guard / "uncertain").unlink(missing_ok=True)
        return 0
    ticket = _pending_ticket(guard)
    if not completed and ticket is not None:
        try:
            ticket.unlink()
        except FileNotFoundError:
            if not ticket.with_name("ticket.orphaned").exists():
                (guard / "uncertain").touch()
                return UNCERTAIN
    if (guard / "uncertain").exists() and not completed:
        return UNCERTAIN
    (guard / "pending.json").unlink(missing_ok=True)
    return 0


def _lock_fd(fd):
    from file_lock import lock_fd

    try:
        lock_fd(fd, blocking=False)
    except BlockingIOError:
        return BUSY
    return 0


def acquire_guard(socket, fd):
    rc = _lock_fd(fd)
    if rc:
        return rc
    guard = guard_path(socket)
    if (guard / "uncertain").exists():
        return UNCERTAIN
    rc = finish_guard(socket)
    return rc


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
            if (guard / "uncertain").exists() or (
                    ticket is not None and not ticket.exists() and not ticket.with_name("ticket.orphaned").exists()):
                return "uncertain"
            return "clear"
    except (OSError, ValueError, KeyError, TypeError):
        return "uncertain"


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
            guard = guard_path(socket)
            if guard.is_dir() and not (guard / "mutex").exists():
                (guard / "uncertain").touch()
                (guard / "mutex").touch()
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
