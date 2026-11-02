"""FirstActContract: the first-act rules every holder_lock caller must keep, driven through its CLI.

A consumer subclasses it with unittest.TestCase and supplies `caller(root, *extra)`: the argv
(a Python script and its arguments) that starts the caller on a work root and keeps it holding
until its stdin closes, printing `acquired` once it holds. `module` is the holder_lock module the
caller uses. The mixin drives contention, no-trace, a missing root, a replaced lock file, a live
legacy pidfile, publish-before-spawn (no child process before the record exists; `ps` is exempt),
a root replaced right after Acquired (the caller's first act must land in the original inode,
through owner.root_fd, never on the new directory at the path), and SIGTERM (the caller ends within
the bound, its record removed and the lock free: its handler sets a flag and the main loop releases;
holder_lock is never called from a signal handler). Every wait is bounded.

Run as a script, this file is the spawn guard: `holder_lock_contract.py guard <report> <root>
<module kind> <script> [args...]` runs the script with every child spawn checked; `... replace
<moved> <root> <module kind> <script> [args...]` runs it with the root renamed to <moved> and a new
directory put at <root> right after Acquired.
"""
import os
import runpy
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WAIT = 10.0
HERE = Path(__file__).resolve()


def snapshot(path):
    """Every file and directory under `path` with its bytes; a refusal must leave it equal."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames:
            out[os.path.relpath(os.path.join(dirpath, name), path) + "/"] = None
        for name in filenames:
            p = os.path.join(dirpath, name)
            with open(p, "rb") as f:
                out[os.path.relpath(p, path)] = f.read()
    return out


class FirstActContract:
    module = None
    fa_quiet = False  # a negative control, expected to fail, prints no FAIL-DIAG line

    def caller(self, root, *extra):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self._fa_tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._fa_tmp, True)
        self._fa_children = []
        self._fa_ran = getattr(self, "_fa_ran", [])
        self.addCleanup(self._fa_stop)

    def _fa_root(self, name="work"):
        root = os.path.join(self._fa_tmp, name)

        def drop():
            if os.path.isdir(root):
                shutil.rmtree(self.module.lock_dir(root), True)
        self.addCleanup(drop)
        return root

    def _fa_stop(self):
        for child in self._fa_children:
            if child.poll() is None:
                try:
                    child.stdin.close()
                    child.wait(WAIT)
                except (OSError, subprocess.TimeoutExpired):
                    child.kill()
                    child.wait(WAIT)
            text = (getattr(child, "_fa_seen", b"") + getattr(child, "_fa_buf", b"")).decode("utf-8", "replace")
            for stream in (child.stdout, child.stderr):
                try:
                    text += os.read(stream.fileno(), 65536).decode("utf-8", "replace") if select.select(
                        [stream], [], [], 0)[0] else ""
                except (OSError, ValueError):
                    pass
            self._fa_ran.append((child.returncode, text))
            for stream in (child.stdin, child.stdout, child.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

    def _fa_start(self, argv):
        child = subprocess.Popen([sys.executable] + list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self._fa_children.append(child)
        return child

    def _fa_line(self, child):
        """One line, read with os.read on the raw fd (a buffered readline after select can block
        when two lines arrive in one read)."""
        fd, buf = child.stdout.fileno(), getattr(child, "_fa_buf", b"")
        deadline = time.monotonic() + WAIT
        while b"\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                self.fail("no line from the caller within %.0fs" % WAIT)
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            buf += chunk
        line, _, child._fa_buf = buf.partition(b"\n")
        child._fa_seen = getattr(child, "_fa_seen", b"") + line + b"\n"
        return line.decode().strip()

    def _fa_run(self, argv):
        r = subprocess.run([sys.executable] + list(argv), capture_output=True, text=True, timeout=WAIT * 3,
                           stdin=subprocess.DEVNULL)
        self._fa_ran.append((r.returncode, r.stdout + r.stderr))
        return r.returncode, r.stdout.strip()

    def run(self, result=None):
        """A failing case also prints one `FAIL-DIAG:` stdout line per caller it ran (exit code and
        the head of its output), and appends the same lines to the failure's message."""
        self._fa_ran = []
        before = len(result.failures) + len(result.errors) if result is not None else 0
        out = super().run(result)
        result = result if result is not None else out
        diag = ["FAIL-DIAG: %s rc=%s %s" % (self.id(), rc, " ".join(text.split())[:300] or "(no output)")
                for rc, text in self._fa_ran]
        if result is None or len(result.failures) + len(result.errors) == before:
            return out
        if not self.fa_quiet:
            print("\n".join(diag or ["FAIL-DIAG: %s (no caller ran)" % self.id()]), flush=True)
        for bucket in (result.failures, result.errors):
            if bucket and bucket[-1][0] is self:
                bucket[-1] = (self, bucket[-1][1] + "\n" + "\n".join(diag))
        return out

    def _fa_hold(self, root):
        holder = self._fa_start(self.caller(root))
        self.assertEqual(self._fa_line(holder), "acquired")
        return holder

    def test_first_act_contention_refuses_with_exit_3_and_leaves_no_trace(self):
        root = self._fa_root()
        self._fa_hold(root)
        before, record = snapshot(root), self.module.read_holder(root)
        rc, out = self._fa_run(self.caller(root))
        self.assertEqual(rc, 3, out)
        self.assertEqual(snapshot(root), before, "the loser leaves the root as it was")
        self.assertEqual(self.module.read_holder(root), record, "and the record")

    def test_first_act_a_missing_root_is_created_and_acquired(self):
        root = self._fa_root("absent/work")
        self.assertFalse(os.path.exists(root))
        self._fa_hold(root)
        self.assertTrue(os.path.isdir(root))

    def test_first_act_a_replaced_lock_file_still_refuses(self):
        root = self._fa_root()
        self._fa_hold(root)
        lock = os.path.join(self.module.lock_dir(root), "lock")
        fresh = lock + ".fresh"
        open(fresh, "w").close()
        os.replace(fresh, lock)
        rc, out = self._fa_run(self.caller(root))
        self.assertEqual(rc, 3, "the record names the live holder: %s" % out)

    def test_first_act_a_live_legacy_pidfile_refuses(self):
        root = self._fa_root()
        live = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (live.kill(), live.wait(WAIT)))
        os.makedirs(os.path.join(root, "state"))
        with open(os.path.join(root, "state", "connector.pid"), "w") as f:
            f.write("%d\n" % live.pid)
        rc, out = self._fa_run(self.caller(root))
        self.assertEqual((rc, out.split()[0]), (3, "LegacyRunning"), out)

    def test_first_act_publish_comes_before_any_child_is_spawned(self):
        root = self._fa_root()
        report = os.path.join(self._fa_tmp, "spawns.txt")
        script, *args = self.caller(root)
        holder = self._fa_start([str(HERE), "guard", report, root, self.module_kind, script] + args)
        self.assertEqual(self._fa_line(holder), "acquired")
        with open(report, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertTrue(lines, "the guard saw the caller's own child after publish")
        self.assertEqual([line for line in lines if not line.startswith("after-publish ")], [],
                         "a child was spawned before the record existed")


    def test_first_act_lands_on_the_held_root_when_the_path_is_replaced(self):
        root = self._fa_root()
        moved = os.path.join(self._fa_tmp, "moved")

        def drop_moved():
            if os.path.isdir(moved):
                shutil.rmtree(self.module.lock_dir(moved), True)
        self.addCleanup(drop_moved)
        script, *args = self.caller(root)
        holder = self._fa_start([str(HERE), "replace", moved, root, self.module_kind, script] + args)
        self.assertEqual(self._fa_line(holder), "acquired")
        self.assertEqual(os.listdir(root), [], "nothing on the new directory at the path")
        self.assertTrue(os.listdir(moved), "the first act landed in the original inode")


    def test_first_act_sigterm_ends_the_caller_and_frees_the_lock(self):
        root = self._fa_root()
        holder = self._fa_hold(root)
        pid = self.module.read_holder(root).record["pid"]
        os.kill(pid, signal.SIGTERM)
        try:
            holder.wait(WAIT)
        except subprocess.TimeoutExpired:
            self.fail("the caller did not end on SIGTERM within %.0fs" % WAIT)
        self.assertIsInstance(self.module.read_holder(root), self.module.Missing, "its record is removed")
        rc, out = self._fa_run(self.caller(root))
        self.assertEqual(rc, 0, "the lock is free again: %s" % out)


def _load(kind):
    repo = HERE.parents[2]
    if kind == "src":
        sys.path.insert(0, str(repo / "src"))
        import holder_lock as hl
    else:
        sys.path.insert(0, str(repo / "packages" / "ag2-sparrow"))
        from ag2_sparrow import holder_lock as hl
    return hl


def _replace(moved, root, kind, script, args):
    """Run `script` with its root renamed away and a new directory put at the path after Acquired."""
    hl = _load(kind)
    real = hl.acquire

    def acquire_then_replace(*a, **k):
        got = real(*a, **k)
        if isinstance(got, hl.Acquired):
            os.rename(root, moved)
            os.mkdir(root)
        return got
    hl.acquire = acquire_then_replace
    sys.argv = [script] + list(args)
    runpy.run_path(script, run_name="__main__")


def _guard(report, root, kind, script, args):
    """Run `script` with every child spawn recorded as before- or after-publish."""
    hl = _load(kind)

    def note(argv):
        name = os.path.basename(str(argv[0] if isinstance(argv, (list, tuple)) else argv).split()[0])
        if name == "ps":
            return
        prior = hl.read_holder(root)
        ours = isinstance(prior, hl.Valid) and prior.record["pid"] == os.getpid()
        with open(report, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % ("after-publish" if ours else "BEFORE-PUBLISH", name))
    real_init = subprocess.Popen.__init__

    def init(self, args, *a, **k):
        note(args)
        real_init(self, args, *a, **k)
    subprocess.Popen.__init__ = init
    for name in ("posix_spawn", "posix_spawnp", "execv", "execve"):
        real = getattr(os, name, None)
        if real is not None:
            setattr(os, name, (lambda r: lambda path, *a, **k: (note([path]), r(path, *a, **k))[1])(real))
    sys.argv = [script] + list(args)
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__" and len(sys.argv) > 5 and sys.argv[1] in ("guard", "replace"):
    try:
        (_guard if sys.argv[1] == "guard" else _replace)(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5],
                                                          sys.argv[6:])
    except SystemExit as e:
        sys.exit(e.code)
