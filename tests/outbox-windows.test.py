"""Native Windows acceptance tests over the canonical outbox implementation."""
import ctypes
import errno
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import outbox as ob


def drainer(root, barrier, results, release, reclaim):
    root = Path(root)
    if reclaim:
        assert ob.may_reclaim_delivery(root, "item", 1)
    barrier.wait(15)
    fn = ob.reclaim_delivery_claim if reclaim else ob.acquire_delivery_claim
    args = (root, "item", 1, str(os.getpid())) if reclaim else (root, "item", str(os.getpid()))
    results.put((os.getpid(), fn(*args)))
    release.wait(20)


def hold_lock(root, ready):
    with ob._item_lock(Path(root), "item"):
        ready.set()
        time.sleep(30)


@unittest.skipUnless(os.name == "nt", "native Windows contract")
class WindowsOutbox(unittest.TestCase):
    def known_dead_pid(self):
        with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
            self.assertEqual(child.wait(timeout=10), 0)
            pid = child.pid
            self.assertEqual(ob.process_identity(pid).state, ob.OwnerState.DEAD)
            return pid

    def child(self, code="import time; time.sleep(30)"):
        p = subprocess.Popen([sys.executable, "-c", code])
        def cleanup():
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
        self.addCleanup(cleanup)
        return p

    def test_original_four_imports(self):
        cases = [
            ("A", "import outbox", str(REPO / "src")),
            ("B", "import ag2_sparrow.outbox", str(REPO / "packages" / "ag2-sparrow")),
            ("C", "import ag2_sparrow.remote_gateway_bridge", str(REPO / "packages" / "ag2-sparrow")),
            ("D", "import runpy; runpy.run_path('src/remote-gateway-bridge.py', run_name='gateway_import_test')", None),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            env = {key: value for key, value in os.environ.items()
                   if key.upper() != "PYTHONPATH"
                   and not key.upper().startswith(("REMOTE_", "AG2_", "AGENT_CONNECT_"))}
            env.update(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=tmp, PYTHONUTF8="1",
                       CLAUDE_CONFIG_DIR=str(Path(tmp) / "config"), USERPROFILE=tmp,
                       REMOTE_TASK_TOKEN="a3-import-test", REMOTE_TASK_URL="https://example.invalid")
            for kind in ("TASK", "RESULT", "STATE"):
                env[f"AGENT_CONNECT_{kind}_DIR"] = str(Path(tmp) / kind.lower())
            for case, code, pythonpath in cases:
                with self.subTest(case=case, pythonpath=pythonpath):
                    case_env = env.copy()
                    if pythonpath is not None:
                        case_env["PYTHONPATH"] = pythonpath
                    r = subprocess.run([sys.executable, "-S", "-c", code], cwd=REPO,
                                       env=case_env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_current_and_child_tokens_stable(self):
        p = self.child()
        for pid in (os.getpid(), p.pid):
            first = ob.process_identity(pid)
            self.assertEqual(first.state, ob.OwnerState.ALIVE)
            self.assertIsInstance(first.start_usec, int)
            self.assertGreater(first.start_usec, 0)
            for _ in range(100):
                self.assertEqual(ob.process_identity(pid), first)

    def test_clean_exit(self):
        p = self.child("pass")
        p.wait(timeout=10)
        self.assertEqual(ob.process_identity(p.pid).state, ob.OwnerState.DEAD)

    def test_terminated_child(self):
        p = self.child()
        self.assertEqual(ob.process_identity(p.pid).state, ob.OwnerState.ALIVE)
        p.kill()
        p.wait(timeout=10)
        self.assertEqual(ob.process_identity(p.pid).state, ob.OwnerState.DEAD)

    def test_exit_code_259(self):
        p = self.child("import os; os._exit(259)")
        self.assertEqual(p.wait(timeout=10), 259)
        self.assertEqual(ob.process_identity(p.pid).state, ob.OwnerState.DEAD)

    def test_absent_and_protected_pid(self):
        self.assertEqual(ob.process_identity(self.known_dead_pid()).state, ob.OwnerState.DEAD)
        api = ob._windows_api()
        handle = api.OpenProcess(0x101000, False, 4)
        if handle:
            api.CloseHandle(handle)
            self.assertNotEqual(ob.process_identity(4).state, ob.OwnerState.DEAD)
        else:
            self.assertEqual(ctypes.get_last_error(), 5)
            self.assertEqual(ob.process_identity(4).state, ob.OwnerState.UNKNOWN)

    def test_input_validation_precedes_binding(self):
        with patch.object(ob, "_windows_api", side_effect=AssertionError) as api:
            for pid in (0, -1, 0x100000000, 1 << 80, "123", None):
                self.assertEqual(ob.process_identity(pid).state, ob.OwnerState.UNKNOWN)
            api.assert_not_called()

    def test_initialization_unavailable(self):
        for error in (OSError("unavailable"), AttributeError("missing API")):
            with patch.object(ob, "_windows_api", side_effect=error):
                self.assertEqual(ob.process_identity(os.getpid()).state, ob.OwnerState.UNKNOWN)

    def test_open_failures(self):
        for error, state in ((87, ob.OwnerState.DEAD), (5, ob.OwnerState.UNKNOWN),
                             (6, ob.OwnerState.UNKNOWN), (0, ob.OwnerState.UNKNOWN)):
            api = Mock()
            api.OpenProcess.return_value = None
            with patch.object(ob, "_windows_api", return_value=api), \
                    patch.object(ctypes, "get_last_error", return_value=error):
                self.assertEqual(ob.process_identity(123).state, state)
            api.OpenProcess.assert_called_once_with(0x101000, False, 123)
            api.CloseHandle.assert_not_called()

    def test_wait_and_times_failures_close_handles(self):
        for wait, state in ((0, ob.OwnerState.DEAD), (0xFFFFFFFF, ob.OwnerState.UNKNOWN),
                            (128, ob.OwnerState.UNKNOWN), (258, ob.OwnerState.UNKNOWN)):
            api = Mock()
            api.OpenProcess.return_value = 0x123456789
            api.WaitForSingleObject.return_value = wait
            api.GetProcessTimes.return_value = False
            with patch.object(ob, "_windows_api", return_value=api):
                self.assertEqual(ob.process_identity(123).state, state)
            api.WaitForSingleObject.assert_called_once_with(0x123456789, 0)
            api.CloseHandle.assert_called_once_with(0x123456789)
            if wait != 258:
                api.GetProcessTimes.assert_not_called()

    def test_exact_filetime_and_success_cleanup(self):
        from ctypes import wintypes
        api = Mock()
        api.OpenProcess.return_value = 0x123456789
        api.WaitForSingleObject.return_value = 258
        ticks = 116444736000000000 + 12345678901234567
        def times(handle, created, *unused):
            ft = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
            ft.dwHighDateTime, ft.dwLowDateTime = ticks >> 32, ticks & 0xFFFFFFFF
            return True
        api.GetProcessTimes.side_effect = times
        with patch.object(ob, "_windows_api", return_value=api):
            result = ob.process_identity(123)
        self.assertEqual(result, ob.ProcessIdentity(123, ob.OwnerState.ALIVE, 1234567890123456))
        api.CloseHandle.assert_called_once_with(0x123456789)

    def test_windows_dispatch_precedes_proc(self):
        with patch.object(ob, "_linux_process_identity", side_effect=AssertionError), \
                patch.object(ob, "_darwin_process_identity", side_effect=AssertionError):
            self.assertEqual(ob.process_identity(os.getpid()).state, ob.OwnerState.ALIVE)

    def test_binding_types_and_no_handle_growth(self):
        from ctypes import wintypes
        api = ob._windows_api()
        self.assertIs(ob._windows_api(), api)
        self.assertEqual(api.OpenProcess.argtypes, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
        self.assertIs(api.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(api.WaitForSingleObject.argtypes, [wintypes.HANDLE, wintypes.DWORD])
        self.assertIs(api.WaitForSingleObject.restype, wintypes.DWORD)
        self.assertEqual(api.GetProcessTimes.argtypes,
                         [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4)
        self.assertIs(api.GetProcessTimes.restype, wintypes.BOOL)
        self.assertEqual(api.CloseHandle.argtypes, [wintypes.HANDLE])
        self.assertIs(api.CloseHandle.restype, wintypes.BOOL)
        probe = ctypes.WinDLL("kernel32", use_last_error=True)
        probe.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        probe.GetProcessHandleCount.restype = wintypes.BOOL
        handle = api.OpenProcess(0x101000, False, os.getpid())
        self.assertTrue(handle)
        try:
            def count():
                n = wintypes.DWORD()
                self.assertTrue(probe.GetProcessHandleCount(handle, ctypes.byref(n)))
                return n.value
            before = count()
            for _ in range(1000):
                self.assertEqual(ob.process_identity(os.getpid()).state, ob.OwnerState.ALIVE)
            self.assertEqual(count(), before)
        finally:
            api.CloseHandle(handle)

    def test_claim_ttl_and_incarnation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(ob.acquire_delivery_claim(root, "item", "owner"))
            path = ob._claim_path(root, "item")
            record = json.loads(path.read_text())
            record["claimed_at"] = 0
            path.write_text(json.dumps(record))
            self.assertFalse(ob.may_reclaim_delivery(root, "item", 1))
            with patch.object(ob, "_windows_api", side_effect=OSError):
                self.assertFalse(ob.may_reclaim_delivery(root, "item", 1))
                self.assertFalse(ob.reclaim_delivery_claim(root, "item", 1, "other"))
            record["start_usec"] += 1
            path.write_text(json.dumps(record))
            self.assertTrue(ob.may_reclaim_delivery(root, "item", 1))
            record["pid"] = self.known_dead_pid()
            record["claimed_at"] = time.time()
            path.write_text(json.dumps(record))
            self.assertFalse(ob.may_reclaim_delivery(root, "item", 60))
            record["claimed_at"] = 0
            path.write_text(json.dumps(record))
            self.assertTrue(ob.may_reclaim_delivery(root, "item", 1))
            self.assertTrue(ob.reclaim_delivery_claim(root, "item", 1, "other"))

    def race(self, reclaim):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if reclaim:
                ob.acquire_delivery_claim(root, "item", "dead")
                path = ob._claim_path(root, "item")
                record = json.loads(path.read_text())
                record.update(pid=self.known_dead_pid(), claimed_at=0)
                path.write_text(json.dumps(record))
            barrier, results, release = ctx.Barrier(2), ctx.Queue(), ctx.Event()
            children = [ctx.Process(target=drainer, args=(tmp, barrier, results, release, reclaim))
                        for _ in range(2)]
            try:
                for child in children:
                    child.start()
                outcomes = [results.get(timeout=20) for _ in children]
                winners = [pid for pid, won in outcomes if won]
                self.assertEqual(len(winners), 1)
                self.assertEqual(ob.read_delivery_claim(root, "item").pid, winners[0])
            finally:
                release.set()
                for child in children:
                    if child.pid is not None:
                        child.join(10)
                        if child.is_alive():
                            child.kill()
                            child.join(10)
                results.close()
                results.join_thread()
            for child in children:
                self.assertEqual(child.exitcode, 0)
                child.close()

    def test_competing_drainers(self):
        self.race(False)

    def test_stale_observers_reread_under_lock(self):
        self.race(True)

    def test_killed_item_lock_holder(self):
        ctx = mp.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            ready = ctx.Event()
            p = ctx.Process(target=hold_lock, args=(tmp, ready))
            p.start()
            try:
                self.assertTrue(ready.wait(10))
                lock = Path(tmp) / ob.LOCKS_DIR / (ob._safe_key("item") + ".lock")
                fd = os.open(lock, os.O_RDWR)
                try:
                    with self.assertRaises(OSError) as raised:
                        ob.lock_fd(fd, blocking=False)
                    self.assertEqual(raised.exception.errno, errno.EACCES)
                    p.kill()
                    p.join(10)
                    ob.lock_fd(fd, blocking=False)
                finally:
                    os.close(fd)
                self.assertTrue(ob.acquire_delivery_claim(Path(tmp), "item", "successor"))
            finally:
                if p.is_alive():
                    p.kill()
                p.join(10)
                p.close()

    def test_torn_claim_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ob.acquire_delivery_claim(root, "item", "owner")
            path = ob._claim_path(root, "item")
            path.write_text("")
            self.assertEqual(ob.read_delivery_claim(root, "item").state, "UNKNOWN")
            self.assertFalse(ob.may_reclaim_delivery(root, "item", 0))
            self.assertFalse(ob.reclaim_delivery_claim(root, "item", 0, "other"))
            old = time.time() - 3600
            os.utime(path, (old, old))
            self.assertFalse(ob.may_reclaim_delivery(root, "item", 0))
            self.assertTrue(ob.reclaim_delivery_claim(root, "item", 0, "other"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
