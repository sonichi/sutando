#!/usr/bin/env python3
"""A pin must SURVIVE a migration intact — the sources are paths, not hosts.

`sutando-migrate.sh` moves per-user state from LEGACY LOCATIONS to the canonical
M0 path, on ONE machine. So the pinned pid is still running at the destination
and the record still means what it said.

Three classifications fail, each in its own direction, and none is visible to a
class-only assertion because all three "preserve the bytes":

  skip-ephemeral     destroys a lone live pin mid-migration — the exact loss the
                     pin exists to prevent.
  structural         collision sidecars one copy to `.legacy-*`; the only runtime
                     reader (`process_pins.load_pins`) opens the canonical path
                     and never a sidecar, so the pin lands where nothing reads it.
  union-json-array   `pins` is a COMPLETE MUTABLE SNAPSHOT: absence from a newer
                     array IS the release operation. A union cannot tell
                     "released" from "never present" and re-arms the old pin.
                     A real union would need stable record ids plus tombstones,
                     which this schema does not have.

`newest-mtime` is the honest class: one canonical file, no merge, newest wins.

These drive the real migration script and then the real reader — the only thing
that separates "the bytes survived" from "the decision survived".

Run: python3 tests/process-pins-migration-visibility.test.py
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import process_pins as pp  # noqa: E402

SERVICE = "discord-bridge"
LIVE_PID, LIVE_LSTART = "222", "Sun Aug 24 09:11:02 2026"
DEAD_PID = "111"
FUTURE = "2099-01-01T00:00:00Z"


_NOW = int(time.time())
OLDER, NEWER = _NOW - 7200, _NOW - 3600   # both well past INFLIGHT_GUARD_SEC=60


def pin(pid, lstart, reason):
    return {"service": SERVICE, "pid": int(pid), "lstart": lstart,
            "reason": reason, "expires_at": FUTURE}


class TraceContractError(AssertionError):
    """Raised when the post-result record contract is not met."""


class PinMigrationVisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pin-migrate-"))
        for leaf in ("src/a", "src/b", "src/c", "dest", "home"):
            (self.tmp / leaf).mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, leaf, pins, mtime):
        d = self.tmp / leaf / "state"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "process-pins.json"
        f.write_text(json.dumps({"pins": pins}, indent=2))
        os.utime(f, (mtime, mtime))
        return f

    def _migrate(self, extra_env=None, *, expect_rc=0):
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        env.update(HOME=str(self.tmp / "home"),
                   SUTANDO_MIGRATE_DEST=str(self.tmp / "dest"),
                   SUTANDO_MIGRATE_SRC_A=str(self.tmp / "src/a"),
                   SUTANDO_MIGRATE_SRC_B=str(self.tmp / "src/b"),
                   SUTANDO_MIGRATE_SRC_C=str(self.tmp / "src/c"))
        r = subprocess.run(
            ["bash", str(REPO / "scripts" / "sutando-migrate.sh"), "--commit",
             "--no-confirm", "--no-claude-import", "--no-hook-bridge",
             "--no-channel-bridge"],
            capture_output=True, text=True, env=env, timeout=180)
        # expect_rc=None defers the check so a row asserts its own property
        # first; otherwise every defect fails on the exit code and stops discriminating.
        if expect_rc == 0:
            self.assertEqual(r.returncode, 0, f"migrate failed:\n{r.stdout}\n{r.stderr}")
        elif expect_rc == "nonzero":
            self.assertNotEqual(
                r.returncode, 0,
                f"migrate was required to refuse and exited 0:\n{r.stdout}\n{r.stderr}")
        return r

    def test_RECORDER_is_live_independent_oracle_plus_falsification(self) -> None:
        """Prove the recorder can emit at all, before trusting any silence.

        Two observations of one call, each compared to a literal fixed here in
        advance: neither the expected stdout nor the expected record is derived
        from the other, so a dead recorder cannot be certified by a live
        subprocess (or vice versa).
        """
        import json
        import subprocess
        import uuid
        call_id = uuid.uuid4().hex[:12]
        known = self._write("src/a", [], 1700000000)
        bin_ = self._gnu_stat_shim()
        run_id, trace = self._run_id, self._trace
        self.assertEqual(trace.read_text(), "", "bind-check must leave the trace empty")
        env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
        argv = [f"--sutando-shim-probe={call_id}", str(known)]
        cp = subprocess.run(["stat", *argv], capture_output=True, text=True, env=env)

        expected_stdout = f"SHIM-LIVE-{call_id}\n"          # parent-defined
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout, expected_stdout)
        self.assertEqual(cp.stderr, "")

        expected = self._probe_record(
            run_id=run_id, call_id=call_id, argv=argv,
            operand=str(known.resolve()), stdout=expected_stdout)
        self._require_trace(expected, run_id=run_id)

    def test_TRACE_READER_rejects_a_corrupt_line(self) -> None:
        """A corrupt record and an absent one must not look alike. The old
        per-site readers skipped malformed lines, making them indistinguishable."""
        self._gnu_stat_shim()
        self._trace.write_text('{"v": 1, "kind": "result"\n')       # truncated JSON
        with self.assertRaises(AssertionError) as caught:
            self._read_trace()
        self.assertIn("not JSON", str(caught.exception))

        self._trace.write_text(json.dumps({"v": 1, "kind": "result"}) + "\n")
        with self.assertRaises(AssertionError) as caught:
            self._read_trace()                                       # well-formed, wrong schema
        self.assertIn("schema mismatch", str(caught.exception))

    def _one_valid_record(self, **over):
        rec = {"v": 1, "kind": "result", "run_id": self._run_id, "call_id": "c" * 12,
               "argv": ["-c", "%Y", "/x"], "operand": "/x",
               "rc": 0, "stdout": "", "stderr": ""}
        rec.update(over)
        return rec

    def test_TRACE_READER_rejects_each_permissive_shape(self) -> None:
        """Equality conflates True/1.0 with 1, json.loads keeps the last of a
        duplicate key, and a missing final newline can mean a truncated write."""
        self._gnu_stat_shim()
        cases = [
            ("v is bool", json.dumps(self._one_valid_record(v=True)) + "\n", "v is bool"),
            ("rc is bool", json.dumps(self._one_valid_record(rc=False)) + "\n", "rc is bool"),
            ("rc is float", json.dumps(self._one_valid_record(rc=0.0)) + "\n", "rc is float"),
            ("argv not str", json.dumps(self._one_valid_record(argv=[1, 2, 3])) + "\n", "argv must be"),
            ("dup key", '{"v": 1, "v": 1, "kind": "result", "run_id": "r", "call_id": "c",'
                        ' "argv": [], "operand": "/x", "rc": 0, "stdout": "", "stderr": ""}\n',
             "duplicate key"),
            ("no final newline", json.dumps(self._one_valid_record()), "does not end in a newline"),
            ("blank line", "\n" + json.dumps(self._one_valid_record()) + "\n", "is blank"),
        ]
        for label, payload, needle in cases:
            with self.subTest(shape=label):
                self._trace.write_text(payload)
                with self.assertRaises(AssertionError) as caught:
                    self._read_trace()
                self.assertIn(needle, str(caught.exception))

    def test_TRACE_READER_frames_on_LF_only(self) -> None:
        """read_text()+splitlines() treat bare CR and U+2028 as separators, so a
        writer that never emits them could still be framed by them."""
        self._gnu_stat_shim()
        one = json.dumps(self._one_valid_record())
        # Pin the REASON, not just "some AssertionError": with the utf-8 handler
        # removed, json.loads still raises and a generic assertRaises passes.
        for label, payload, needle in (
                ("terminal bare CR", (one + "\r").encode(),
                 "does not end in a newline"),
                ("CR-separated pair", (one + "\r" + one + "\n").encode(),
                 "is not JSON"),
                ("U+2028-separated pair", (one + "\u2028" + one + "\n").encode(),
                 "is not JSON"),
                ("invalid UTF-8", b"\xff\n", "is not UTF-8"),
        ):
            with self.subTest(framing=label):
                self._trace.write_bytes(payload)
                with self.assertRaises(AssertionError) as caught:
                    self._read_trace()
                self.assertIn(needle, str(caught.exception))

        # CRLF stays acceptable: json tolerates the trailing \r as whitespace.
        # Compare the RECORD, not the count — a dummy single row satisfies len==1.
        self._trace.write_bytes((one + "\r\n").encode())
        self.assertEqual(self._read_trace(), [self._one_valid_record()])

    def test_TRACE_CONTRACT_rejects_a_same_length_wrong_record(self) -> None:
        """Calibrates the equal-length branch. A validator that returns on any
        non-empty trace passes both recorder controls but fails this one."""
        self._gnu_stat_shim()
        expected = [self._one_valid_record()]
        self._trace.write_text(json.dumps(self._one_valid_record(rc=1)) + "\n")
        with self.assertRaisesRegex(TraceContractError, r"^post-result record mismatch$"):
            self._require_trace(expected, run_id=self._run_id)

    def test_RECORDER_liveness_check_can_FAIL(self) -> None:
        """Falsification control. The mutant is a FLAG, not a text edit: rc and
        streams must stay identical, so a pass cannot be a shim that never ran."""
        import json
        import subprocess
        import uuid
        call_id = uuid.uuid4().hex[:12]
        known = self._write("src/a", [], 1700000000)
        bin_ = self._gnu_stat_shim()
        trace = self._trace
        argv = ["stat", f"--sutando-shim-probe={call_id}", str(known)]
        env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}

        live = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(
            len([l for l in trace.read_text().splitlines() if l.strip()]), 1,
            "unmutated shim must record exactly one result")

        shim = bin_ / "stat"
        src = shim.read_text()
        needle = "DROP_RESULTS = False"
        self.assertEqual(src.count(needle), 1,
                         f"mutant needle absent ({needle!r}) - control would be inert")
        shim.write_text(src.replace(needle, "DROP_RESULTS = True"))
        shim.chmod(0o755)
        trace.write_text("")

        dead = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(
            (dead.returncode, dead.stdout, dead.stderr),
            (live.returncode, live.stdout, live.stderr),
            "mutant changed observable behavior - it is not a recorder-only control")
        # Fail through the SAME contract that certified the live record: zero
        # records is this test's passing value, so asserting it proves nothing.
        expected = self._probe_record(
            run_id=self._run_id, call_id=call_id, argv=argv[1:],
            operand=str(known.resolve()), stdout=f"SHIM-LIVE-{call_id}\n")
        with self.assertRaisesRegex(
                TraceContractError,
                r"^expected 1 post-result record; found 0$"):
            self._require_trace(expected, run_id=self._run_id)

    def _require_trace(self, expected: list[dict], *, run_id: str | None = None) -> None:
        """The post-result contract. Both the live probe and the dead-writer
        control go through this, so weakening it breaks the control too."""
        actual = self._read_trace(run_id=run_id)
        if len(actual) != len(expected):
            raise TraceContractError(
                f"expected {len(expected)} post-result record; found {len(actual)}")
        if actual != expected:
            raise TraceContractError("post-result record mismatch")

    @staticmethod
    def _probe_record(*, run_id, call_id, argv, operand, stdout):
        """Parent-authored expected record. Never built from an observed trace."""
        return [{"v": 1, "kind": "result", "run_id": run_id, "call_id": call_id,
                 "argv": argv, "operand": operand,
                 "rc": 0, "stdout": stdout, "stderr": ""}]

    _TRACE_KEYS = {"v", "kind", "run_id", "call_id", "argv",
                   "operand", "rc", "stdout", "stderr"}

    @staticmethod
    def _no_dup_keys(pairs):
        """json.loads keeps the LAST duplicate key silently; refuse instead."""
        seen = [k for k, _ in pairs]
        if len(seen) != len(set(seen)):
            raise ValueError(f"duplicate key(s): {sorted({k for k in seen if seen.count(k) > 1})}")
        return dict(pairs)

    def _read_trace(self, *, run_id: str | None = None) -> list[dict]:
        """Sole reader of the trace. Malformed lines RAISE rather than skip: a
        corrupt record and an absent one must not look alike to any caller."""
        # BYTES, not read_text(): universal-newline translation plus
        # str.splitlines() treat bare CR and U+2028 as record separators.
        blob = self._trace.read_bytes() if self._trace.exists() else b""
        if blob and not blob.endswith(b"\n"):
            raise AssertionError("trace does not end in a newline - final record may be truncated")
        out = []
        for n, raw_ln in enumerate(blob.split(b"\n")[:-1] if blob else [], 1):
            try:
                ln = raw_ln.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AssertionError(f"trace line {n} is not UTF-8 ({exc})")
            if not ln.strip():
                raise AssertionError(f"trace line {n} is blank")
            try:
                rec = json.loads(ln, object_pairs_hook=self._no_dup_keys)
            except ValueError as exc:
                raise AssertionError(f"trace line {n} is not JSON ({exc}): {ln[:120]!r}")
            if not isinstance(rec, dict) or set(rec) != self._TRACE_KEYS:
                raise AssertionError(
                    f"trace line {n} schema mismatch: {sorted(rec) if isinstance(rec, dict) else type(rec).__name__}")
            # `type(...) is int` not `== 1`: True == 1 and 1.0 == 1 in Python,
            # so an equality test admits bools and floats into an int field.
            for key, want in (("v", int), ("rc", int)):
                if type(rec[key]) is not want:
                    raise AssertionError(f"trace line {n}: {key} is {type(rec[key]).__name__}, want int")
            for key in ("kind", "run_id", "call_id", "operand", "stdout", "stderr"):
                if type(rec[key]) is not str:
                    raise AssertionError(f"trace line {n}: {key} is {type(rec[key]).__name__}, want str")
            if type(rec["argv"]) is not list or any(type(a) is not str for a in rec["argv"]):
                raise AssertionError(f"trace line {n}: argv must be a list of str")
            if rec["v"] != 1 or rec["kind"] != "result":
                raise AssertionError(f"trace line {n}: v={rec['v']!r} kind={rec['kind']!r}")
            if run_id is not None and rec["run_id"] != run_id:
                continue
            out.append(rec)
        return out

    @staticmethod
    def _disable_subsecond(src: str) -> str:
        """Force the %Y fallback. Asserts the patch applied: a .replace() that
        matches nothing silently returns the input and the test passes anyway."""
        needle = 'if fmt == "%.9Y":'
        assert src.count(needle) == 1, f"disable-subsecond needle not found ({needle!r})"
        return src.replace(needle, "if False:")

    def _gnu_stat_shim(self, comma: bool = True, synth: dict | None = None,
                       poison: dict | None = None,
                       synth9: dict | None = None, exhaust=None) -> Path:
        """GNU-ish `stat` whose ONE writer serves both production and the probe.

        A reserved --sutando-shim-probe argv takes the same finish() path, so a
        dead production writer cannot be certified live by a probe-only writer.
        """
        import uuid
        run_id = uuid.uuid4().hex
        self._run_id = run_id
        bin_ = self.tmp / "shimbin"
        bin_.mkdir(exist_ok=True)
        self._trace = self.tmp / "stat-trace.log"

        tmpl = r"""#!/usr/bin/env python3
import os, sys, time, json as _j
a = sys.argv[1:]
TRACE, RUN_ID = __TRACE__, __RUNID__
_POISON, _SYNTH, _SYNTH9 = __POISON__, __SYNTH__, __SYNTH9__
_EXHAUST = __EXHAUST__
DROP_RESULTS = False

def record_result(rec):
    if DROP_RESULTS:
        return
    with open(TRACE, "a") as fh:
        fh.write(_j.dumps(rec) + "\n")
        fh.flush(); os.fsync(fh.fileno())

def finish(argv, operand, call_id, rc, out, err):
    record_result({"v": 1, "kind": "result", "run_id": RUN_ID,
                   "call_id": call_id, "argv": argv, "operand": operand,
                   "rc": rc, "stdout": out, "stderr": err})
    sys.stdout.write(out); sys.stderr.write(err)
    raise SystemExit(rc)

def _key(pth):
    return os.path.basename(os.path.dirname(os.path.dirname(pth)))

def dispatch(a):
    if a and a[0].startswith("--sutando-shim-probe="):
        _cid = a[0].split("=", 1)[1]
        return os.path.realpath(a[1]), _cid, 0, "SHIM-LIVE-" + _cid + "\n", ""
    if not a or a[0] == "-f":
        if len(a) > 2 and a[1] == "%Fm" and _key(a[2]) in _POISON:
            return os.path.realpath(a[2]), "", 1, _POISON[_key(a[2])] + "\n", ""
        _op = os.path.realpath(a[2]) if len(a) > 2 else ""
        return (_op, "", 1, '  File: "x"\n    ID: 0 Namelen: 255\n',
                "stat: cannot read file system information\n")
    if a[0] != "-c":
        return "", "", 1, "", ""
    fmt, f = a[1], a[2]
    if _key(f) in _EXHAUST:
        # Empty stdout + nonzero on every -c lane; the two -f lanes already
        # fail, so mtime_ns exhausts exactly as it does on a real I/O error.
        return (os.path.realpath(f), "", 1, "",
                "stat: cannot stat '" + f + "': Input/output error\n")
    st = os.stat(f)
    sep = "," if os.environ.get("LC_ALL") != "C" else "."
    _k, op = _key(f), os.path.realpath(f)
    if fmt == "%.9Y":
        if _k in _SYNTH9:
            _w, _n = _SYNTH9[_k]
        elif _k in _SYNTH:
            _w, _n = _SYNTH[_k]
        else:
            _w, _n = int(st.st_mtime), st.st_mtime_ns % 1000000000
        out = "%d%s%09d\n" % (_w, sep, _n)
    elif fmt == "%Y":
        out = str(_SYNTH[_k][0] if _k in _SYNTH else int(st.st_mtime)) + "\n"
    elif fmt in ("%s", "%z"):
        out = str(st.st_size) + "\n"
    elif fmt == "%y":
        out = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)) + "\n"
    else:
        return op, "", 1, "", ""
    return op, "", 0, out, ""

finish(a, *dispatch(a))
"""
        for token, value in (("__TRACE__", repr(str(self._trace))),
                             ("__RUNID__", repr(run_id)),
                             ("__POISON__", repr(poison or {})),
                             ("__SYNTH__", repr(synth or {})),
                             ("__SYNTH9__", repr(synth9 or {})),
                             ("__EXHAUST__", repr(sorted(exhaust or [])))):
            tmpl = tmpl.replace(token, value)
        (bin_ / "stat").write_text(tmpl)
        (bin_ / "stat").chmod(0o755)
        self._assert_shim_binds(bin_)
        return bin_

    def _assert_shim_binds(self, bin_: Path) -> None:
        """Executable-on-disk is not bound-on-PATH.

        Binds through the RESERVED probe argv, not the production `-f %m`
        shape, so the BSD lane can own that shape and return success.
        """
        import subprocess
        import uuid
        cid = f"bind-{uuid.uuid4().hex[:12]}"
        argv = [f"--sutando-shim-probe={cid}", str(bin_)]
        stdout = f"SHIM-LIVE-{cid}\n"
        expected = self._probe_record(
            run_id=self._run_id, call_id=cid, argv=argv,
            operand=str(bin_.resolve()), stdout=stdout)

        self._trace.write_bytes(b"")
        self.assertEqual(self._trace.read_bytes(), b"")
        env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
        cp = subprocess.run(["stat", *argv], capture_output=True,
                            text=True, env=env)
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr),
                         (0, stdout, ""), "shim did not bind through PATH")
        self._require_trace(expected, run_id=self._run_id)
        self._trace.write_bytes(b"")
        self.assertEqual(self._trace.read_bytes(), b"",
                         "bind must leave no record for migration assertions to see")

    def _assert_migrator_used_shim(self, *, fallback: bool = False) -> None:
        """Bind the trace to the EXACT operands, both of them.

        A root prefix accepts a decoy under that root -- and startswith() is
        string prefix, not path containment, so root src/a also admits
        src/attacker/... Require (flag, fmt, exact path) for every required
        format against BOTH files the comparator must stat.
        """
        calls = set()
        for rec in self._read_trace(run_id=self._run_id):
            argv = rec["argv"]
            # the reserved probe carries 2 argv elements; stat calls carry 3
            if len(argv) < 3 or not rec["operand"]:
                continue
            # macOS resolves /var -> /private/var, so compare resolved operands
            calls.add((argv[0], argv[1], str(Path(rec["operand"]).resolve())))
        operands = [
            str((self.tmp / "src/a" / "state" / "process-pins.json").resolve()),
            str((self.tmp / "dest" / "state" / "process-pins.json").resolve()),
        ]
        fmts = [("-f", "%Fm"), ("-c", "%.9Y")]
        if fallback:
            fmts.append(("-c", "%Y"))
        want = {(fl, fm, op) for fl, fm in fmts for op in operands}
        missing = sorted(want - calls)
        self.assertFalse(
            missing,
            f"comparator did not stat these on the shim: {missing}\n"
            f"traced: {sorted(calls)[:10]}")

    def _verdict(self, pins_path):
        """Drive the real reader exactly as check_bridges does."""
        results = pp.evaluate(pp.load_pins(pins_path), SERVICE,
                              {LIVE_PID: LIVE_LSTART}, now_ts=0.0)
        return pp.stale_verdict(results, age_min=42)

    # ---- controls first: without these, every assertion below could pass on a
    # harness that never exercised the mechanism it claims to protect. ----

    def test_POSITIVE_CONTROL_a_local_armed_pin_still_suppresses_the_restart(self) -> None:
        """The pin mechanism itself is untouched by the reclassification."""
        f = self._write("src/c", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], OLDER)
        status, detail = self._verdict(f)
        self.assertEqual(status, "warn", detail)
        self.assertIn(f"DO NOT RESTART {SERVICE} pid {LIVE_PID}", detail)

    def test_NEGATIVE_CONTROL_a_released_record_prescribes_a_restart(self) -> None:
        """An empty pin set is the operator saying 'you may restart now'."""
        f = self._write("src/a", [], NEWER)
        status, detail = self._verdict(f)
        self.assertEqual(status, "stale", detail)
        self.assertIn("restart needed", detail)

    # ---- the contract ----

    def test_a_lone_live_pin_ARRIVES_at_the_canonical_path(self) -> None:
        """Same-host move: the pid is still live, so the pin must survive."""
        self._write("src/c", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], OLDER)
        self._migrate()
        canonical = self.tmp / "dest" / "state" / "process-pins.json"
        self.assertTrue(canonical.exists(), "a live pin was destroyed by the migration")
        status, detail = self._verdict(canonical)
        self.assertEqual(status, "warn", detail)
        self.assertIn(f"DO NOT RESTART {SERVICE} pid {LIVE_PID}", detail)

    def test_no_pin_is_stranded_in_an_unread_sidecar(self) -> None:
        """The canonical path is the only thing load_pins opens."""
        self._write("src/a", [pin(DEAD_PID, "Sat Aug 23 01:00:00 2026", "stale witness")], NEWER)
        self._write("src/c", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], OLDER)
        self._migrate()
        state = self.tmp / "dest" / "state"
        strays = sorted(q.name for q in state.glob("process-pins.json.*"))
        self.assertEqual(strays, [], f"pin written to a path no reader opens: {strays}")

    def test_migration_does_not_resurrect_a_RELEASED_pin(self) -> None:
        """A released pin on a process that is no longer running stays released:
        the union keeps an older-only pin only while its pid is live with a
        matching lstart, and this pid is not."""
        self._write("src/a", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], OLDER)
        self._write("src/c", [], NEWER)          # newer: released
        self._migrate()
        canonical = self.tmp / "dest" / "state" / "process-pins.json"
        self.assertTrue(canonical.exists())
        status, detail = self._verdict(canonical)
        self.assertEqual(status, "stale", detail)
        self.assertIn("restart needed", detail)

    def test_newest_wins_in_BOTH_directions(self) -> None:
        """Whichever source is newer owns the record — not whichever is scanned first."""
        for newer_is_release in (True, False):
            with self.subTest(newer_is_release=newer_is_release):
                self.setUp()
                armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
                self._write("src/a", armed if not newer_is_release else [], NEWER)
                self._write("src/c", [] if not newer_is_release else armed, OLDER)
                self._migrate()
                status, _ = self._verdict(self.tmp / "dest" / "state" / "process-pins.json")
                self.assertEqual(status, "stale" if newer_is_release else "warn")


    def test_THREE_SOURCE_newest_extension_survives_intermediate_union(self) -> None:
        """C -> A -> B: the first union must not out-date B by carrying the
        migration's write time. B holds the newest extension of C's identity;
        A adds only an unrelated pin. Both must reach the canonical file."""
        me = os.getpid()
        my_lstart = subprocess.run(["ps", "-o", "lstart=", "-p", str(me)],
                                   capture_output=True, text=True).stdout.strip()
        one_lstart = subprocess.run(["ps", "-o", "lstart=", "-p", "1"],
                                    capture_output=True, text=True).stdout.strip()
        old = {"service": SERVICE, "pid": me, "lstart": my_lstart,
               "reason": "old witness", "expires_at": "2027-01-01T00:00:00Z"}
        ext = dict(old, reason="extended witness", expires_at=FUTURE)
        unrelated = {"service": "telegram-bridge", "pid": 1, "lstart": one_lstart,
                     "reason": "unrelated", "expires_at": FUTURE}
        self._write("src/c", [old], OLDER)
        self._write("src/a", [unrelated], OLDER + 600)
        self._write("src/b", [ext], OLDER + 1200)
        self._migrate()
        pins = pp.load_pins(self.tmp / "dest" / "state" / "process-pins.json")
        got = sorted((p["reason"], p["expires_at"]) for p in pins)
        self.assertEqual(got, [("extended witness", FUTURE), ("unrelated", FUTURE)], got)

    def test_FAILED_stat_call_printing_a_number_is_NOT_a_successful_answer(self) -> None:
        """Numeric OUTPUT is not a successful CALL.

        `-f %Fm` exits nonzero here while printing a plausible mtime. Honouring
        rc, the loop must discard it and fall through to `-c %.9Y`. Ignoring rc
        (the `|| true` this replaced), it takes the poisoned value and picks the
        other winner.
        """
        SEC = 1700000000
        synth = {"a": (SEC, 100000000), "dest": (SEC, 900000000)}   # -> dest wins
        poison = {"a": f"{SEC + 99}.000000000"}                     # -> a wins, if trusted
        bin_ = self._gnu_stat_shim(synth=synth, poison=poison)
        armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
        self._write("src/a", armed, SEC + 5)
        self._write("dest", [], SEC)
        self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}",
                                 "LC_ALL": "de_DE.UTF-8"})
        status, _ = self._verdict(self.tmp / "dest" / "state" / "process-pins.json")
        self.assertEqual(
            status, "stale",
            "a FAILED `-f %Fm` that printed a number was accepted as the answer: "
            "the loop broke on numeric output instead of on a successful call")

    _MTIME_LANES = (["-f", "%Fm"], ["-c", "%.9Y"], ["-f", "%m"], ["-c", "%Y"])

    def _mtime_probes(self, operand):
        """Ordered mtime-lane probes for one operand. Size/format reads excluded
        so 'was it compared' cannot be answered by an unrelated stat."""
        return [r for r in self._read_trace()
                if r["operand"] == str(Path(operand).resolve())
                and r["argv"][:2] in [list(x) for x in self._MTIME_LANES]]

    def _sentinels(self):
        return sorted(p.name for p in (self.tmp / "dest" / "state").glob(".migrated-from-*"))

    def _collision(self, sec):
        """One armed source/dest pair, returning both paths and their bytes."""
        armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
        a = self._write("src/a", armed, sec + 5)
        d = self._write("dest", [], sec)
        return a, d, a.read_bytes(), d.read_bytes()

    @staticmethod
    def _extract_shipped_fn(name):
        """The SHIPPED function text, not a restatement of it. Refuses on a
        count other than one so a rename cannot silently test nothing."""
        src = (REPO / "scripts" / "sutando-migrate.sh").read_text()
        blocks = re.findall(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", src,
                            re.S | re.M)
        if len(blocks) != 1:
            raise AssertionError(f"{name}(): found {len(blocks)} definitions, want 1")
        return blocks[0]

    def test_MTIME_NS_GRAMMAR_rows_against_the_shipped_function(self) -> None:
        """Epoch zero, malformed values and exhaustion, driven through the
        shipped mtime_ns. Epoch zero must stay a usable answer; exhaustion must
        be distinguishable from it -- that collapse is what wrote the sentinel."""
        fn = self._extract_shipped_fn("mtime_ns")
        rows = [
            # stat output,        expect_rc, expect_stdout
            ("0",                  0, "0000000000"),
            ("0.000000000",        0, "0000000000"),
            ("1700000000",         0, "1700000000000000000"),
            ("1700000000.5",       0, "1700000000500000000"),
            ("1700000000.123456789", 0, "1700000000123456789"),
            (".",                  1, ""),
            ("1.2.3",              1, ""),
            (".5",                 1, ""),
            ("7.",                 1, ""),
            ("abc",                1, ""),
            ("",                   1, ""),          # exhaustion: empty on every lane
        ]
        binn = self.tmp / "grammarbin"
        binn.mkdir()
        for value, want_rc, want_out in rows:
            with self.subTest(stat_returns=repr(value)):
                # A stat that answers every lane with one fixture value; empty
                # value also exits nonzero, which is what exhaustion looks like.
                (binn / "stat").write_text(
                    "#!/bin/sh\n"
                    f"printf '%s' {shlex.quote(value + ('' if value == '' else chr(10)))}\n"
                    f"exit {0 if value else 1}\n")
                (binn / "stat").chmod(0o755)
                r = subprocess.run(
                    ["bash", "-c", f"set -euo pipefail\n{fn}\nmtime_ns /nonexistent"],
                    capture_output=True, text=True,
                    env={**os.environ, "PATH": f"{binn}:{os.environ['PATH']}"})
                self.assertEqual(r.returncode, want_rc,
                                 f"{value!r}: rc {r.returncode}, want {want_rc}")
                self.assertEqual(r.stdout.strip(), want_out,
                                 f"{value!r}: stdout {r.stdout.strip()!r}, want {want_out!r}")

    def test_EXHAUSTION_WITNESS_unarmed_control_migrates(self) -> None:
        """Control for the two exhaustion rows: same fixture, nothing armed.

        Without it, 'no sentinel and unchanged bytes' is also what a migrator
        that never ran produces -- the arming would certify nothing.
        """
        SEC = 1700000000
        bin_ = self._gnu_stat_shim(synth9={"a": (SEC, 900000000),
                                           "dest": (SEC, 100000000)})
        a, d, a_bytes, _ = self._collision(SEC)
        r = self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 0)
        canonical = self.tmp / "dest" / "state" / "process-pins.json"
        self.assertEqual(canonical.read_bytes(), a_bytes,
                         "control: the newer source did not win, so the "
                         "comparator never ran and the arming proves nothing")
        self.assertTrue(self._sentinels(), "control: no sentinel was written")
        self.assertTrue(self._mtime_probes(a), "control: source was never probed")
        self.assertTrue(self._mtime_probes(d), "control: dest was never probed")
        self.assertNotIn("AMBIGUOUS", r.stderr)

    def test_EXHAUSTION_WITNESS_source_refuses_before_probing_dest(self) -> None:
        """Source exhaustion: named refusal, nonzero rc, nothing written, and
        the destination is never probed -- the short-circuit is observed, not read."""
        SEC = 1700000000
        bin_ = self._gnu_stat_shim(synth9={"a": (SEC, 900000000),
                                           "dest": (SEC, 100000000)},
                                   exhaust=["a"])
        a, d, a_bytes, d_bytes = self._collision(SEC)
        r = self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}"},
                          expect_rc=None)
        self.assertIn("AMBIGUOUS", r.stderr)
        self.assertIn(str(a.resolve()), r.stderr,
                      "the refusal must name the operand it could not read")
        self.assertIn("source", r.stderr.split("AMBIGUOUS", 1)[1].split("\n")[0])
        self.assertEqual(a.read_bytes(), a_bytes, "source bytes changed on a refusal")
        self.assertEqual(d.read_bytes(), d_bytes, "dest bytes changed on a refusal")
        self.assertEqual(self._sentinels(), [],
                         "a sentinel was written despite the refusal: the next "
                         "run would skip this source entirely")
        self.assertTrue(self._mtime_probes(a), "source was never probed at all")
        self.assertEqual(
            self._mtime_probes(d), [],
            "destination was probed after the source read failed: the "
            "comparison is not short-circuiting on the source")
        self.assertNotEqual(r.returncode, 0, "refused but exited 0")

    def test_EXHAUSTION_WITNESS_dest_refuses_after_source_succeeds(self) -> None:
        """Destination exhaustion: the source probe COMPLETES first, then the
        destination read exhausts. Ordering is asserted from the trace."""
        SEC = 1700000000
        bin_ = self._gnu_stat_shim(synth9={"a": (SEC, 900000000),
                                           "dest": (SEC, 100000000)},
                                   exhaust=["dest"])
        a, d, a_bytes, d_bytes = self._collision(SEC)
        r = self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}"},
                          expect_rc=None)
        self.assertIn("AMBIGUOUS", r.stderr)
        self.assertIn(str(d.resolve()), r.stderr)
        self.assertIn("destination", r.stderr.split("AMBIGUOUS", 1)[1].split("\n")[0])
        self.assertEqual(a.read_bytes(), a_bytes)
        self.assertEqual(d.read_bytes(), d_bytes)
        self.assertEqual(self._sentinels(), [], "sentinel written despite refusal")
        src_ok = [r_ for r_ in self._mtime_probes(a) if r_["rc"] == 0]
        self.assertTrue(src_ok, "source never completed a successful probe")
        dest_bad = [r_ for r_ in self._mtime_probes(d) if r_["rc"] != 0]
        self.assertTrue(dest_bad, "destination never exhausted")
        full = self._read_trace()
        self.assertLess(full.index(src_ok[-1]), full.index(dest_bad[0]),
                        "destination was probed before the source succeeded")
        self.assertNotEqual(r.returncode, 0, "refused but exited 0")

    def test_EXHAUSTION_WITNESS_refusal_halts_remaining_sources(self) -> None:
        """C refuses; A and B must not proceed. Measured, not reasoned: errexit
        is exempt inside a containing conditional, and a walk that continued
        would write the later sources' sentinels and still exit 0."""
        SEC = 1700000000
        bin_ = self._gnu_stat_shim(synth9={"c": (SEC, 900000000),
                                           "a": (SEC, 800000000),
                                           "dest": (SEC, 100000000)},
                                   exhaust=["c"])
        armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
        self._write("src/c", armed, SEC + 5)
        a = self._write("src/a", armed, SEC + 5)
        d = self._write("dest", [], SEC)
        d_bytes = d.read_bytes()
        r = self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}"},
                          expect_rc=None)
        self.assertIn("AMBIGUOUS", r.stderr)
        self.assertEqual(self._sentinels(), [],
                         "a later source wrote its sentinel after C refused")
        self.assertEqual(
            self._mtime_probes(a), [],
            "source A was compared after C refused: the refusal did not halt "
            "the walk, so a partial migration reports success")
        self.assertEqual(d.read_bytes(), d_bytes)
        self.assertNotEqual(r.returncode, 0, "refused but exited 0")

    def test_WHOLE_SECOND_fallback_RECIPROCAL_and_byte_checked(self) -> None:
        """Reciprocity on the %Y path, or an always-keep-destination impl passes.

        A single dest-winning row is satisfied by mtime_ns returning constants,
        by dropping `stat -f %m`, and by trying %Y before %m -- destination is
        pre-seeded with the expected bytes, so no-op looks correct.
        """
        SEC = 1700000000
        armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
        rows = [
            # label, real_a, real_d, Y_a, Y_d, nine_a, nine_d, expected
            ("fallback-D-wins", SEC + 3, SEC + 0, SEC + 1, SEC + 2,
             SEC + 9, SEC + 0, "stale"),
            ("fallback-A-wins", SEC + 0, SEC + 3, SEC + 2, SEC + 1,
             SEC + 0, SEC + 9, "warn"),
        ]
        for label, ra, rd, ya, yd, na, nd, expected in rows:
            with self.subTest(row=label):
                self.setUp()
                bin_ = self._gnu_stat_shim(
                    synth={"a": (ya, 0), "dest": (yd, 0)},
                    synth9={"a": (na, 0), "dest": (nd, 0)})
                shim = bin_ / "stat"
                shim.write_text(
                    self._disable_subsecond(shim.read_text()))
                shim.chmod(0o755)
                a_path = self._write("src/a", armed, ra)
                d_path = self._write("dest", [], rd)
                a_bytes, d_bytes = a_path.read_bytes(), d_path.read_bytes()
                self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}",
                                         "LC_ALL": "de_DE.UTF-8"})
                canonical = self.tmp / "dest" / "state" / "process-pins.json"
                status, _ = self._verdict(canonical)
                self.assertEqual(
                    status, expected,
                    f"{label}: %Y table names {expected}; host or mixed pairs "
                    f"name the other, and a constant impl cannot satisfy both rows")
                want = d_bytes if expected == "stale" else a_bytes
                self.assertEqual(canonical.read_bytes(), want,
                                 f"{label}: surviving bytes are not the expected candidate's")

    def test_INTERLEAVED_only_BOTH_shim_answers_pick_the_shim_winner(self) -> None:
        """Reciprocal + interleaved, so a mixed host/shim pair cannot pass.

        A single non-interleaved row is satisfied by `always keep destination`,
        and ordinary reciprocal values can still be satisfied by consuming ONE
        operand's shim answer and one host answer. Interleaving makes every
        mixed pair agree with the HOST winner, so only reading both shim values
        selects the shim winner:

            shim-D-wins: real_D < shim_A < shim_D < real_A
            shim-A-wins: real_A < shim_D < shim_A < real_D

        All four land inside one whole second, so `%Y` ties and only the
        successful `%.9Y` answers can decide.
        """
        SEC = 1700000000
        armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
        rows = [
            # (label, real_a, real_d, shim_a, shim_d, expected)
            ("shim-D-wins", 0.9, 0.1, 400000000, 600000000, "stale"),
            ("shim-A-wins", 0.1, 0.9, 600000000, 400000000, "warn"),
        ]
        for label, ra, rd, sa, sd, expected in rows:
            with self.subTest(row=label):
                self.setUp()
                bin_ = self._gnu_stat_shim(
                    synth={"a": (SEC, sa), "dest": (SEC, sd)})
                a_path = self._write("src/a", armed, SEC + ra)
                d_path = self._write("dest", [], SEC + rd)
                a_bytes, d_bytes = a_path.read_bytes(), d_path.read_bytes()
                self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}",
                                         "LC_ALL": "de_DE.UTF-8"})
                canonical = self.tmp / "dest" / "state" / "process-pins.json"
                status, _ = self._verdict(canonical)
                self.assertEqual(
                    status, expected,
                    f"{label}: expected the SHIM winner ({expected}); a host or "
                    f"mixed host/shim pair yields the other verdict")
                # BYTES, not the verdict: load_pins() maps truncated/malformed/
                # absent alike to [] -> "stale", hiding dest-branch corruption.
                want = d_bytes if expected == "stale" else a_bytes
                self.assertEqual(
                    canonical.read_bytes(), want,
                    f"{label}: verdict matched but the surviving BYTES are not "
                    f"the expected candidate's")

    def test_GNU_migrator_CONSUMES_shim_values_not_host_stat(self) -> None:
        """Prove the shim's ANSWER drove the decision, not merely that it ran.

        Every earlier witness was blind here: the shim answered from os.stat on
        the same fixtures, so host and shim always named the same winner. Here
        they DISAGREE on the pair the migrator actually compares (src vs DEST),
        and the surviving content shows which answer production consumed.
        """
        SEC = 1700000000
        # host stat: src/a is 5s newer than dest -> src would win, dest overwritten.
        # shim:      same second, dest has the larger fraction -> dest must survive.
        synth = {"a": (SEC, 100000000), "dest": (SEC, 900000000)}
        bin_ = self._gnu_stat_shim(synth=synth)
        armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
        self._write("src/a", armed, SEC + 5)
        self._write("dest", [], SEC)
        self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}",
                                 "LC_ALL": "de_DE.UTF-8"})
        status, _ = self._verdict(self.tmp / "dest" / "state" / "process-pins.json")
        self.assertEqual(
            status, "stale",
            "migration followed HOST stat (src newer -> dest overwritten with the "
            "armed pin -> warn) instead of the shim's answer (dest newer -> "
            "preserved -> stale): shim invoked, value NOT consumed")

    def test_COMMA_LOCALE_real_migrator_resolves_subsecond_on_dest_newer(self) -> None:
        """Drive the REAL migrator under emulated GNU stat in a comma locale.

        Same second, different subsecond: without LC_ALL=C the fraction is
        rejected, both sides tie, and the tie branch aborts AMBIGUOUS (rc 1).
        Both runs keep c newer -- this swaps WHICH SIDE is armed, not the
        scan direction; direction is covered by test_newest_wins_in_BOTH_directions.
        """
        SEC = 1700000000
        for newer_is_release in (True, False):
            with self.subTest(newer_is_release=newer_is_release):
                self.setUp()
                bin_ = self._gnu_stat_shim()
                armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
                fa = self._write("src/a", armed if not newer_is_release else [], SEC)
                fc = self._write("src/c", [] if not newer_is_release else armed, SEC)
                # a OLDER than c by 800ms, inside the same whole second.
                os.utime(fa, ns=(SEC * 10**9 + 100_000_000,) * 2)
                os.utime(fc, ns=(SEC * 10**9 + 900_000_000,) * 2)
                self.assertEqual(int(fa.stat().st_mtime), int(fc.stat().st_mtime),
                                 "fixture must sit in ONE second or the tie never arises")
                self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}",
                                         "LC_ALL": "de_DE.UTF-8"})
                self._assert_migrator_used_shim()
                status, _ = self._verdict(self.tmp / "dest" / "state" / "process-pins.json")
                # c is newer in both runs, so c's content decides both times.
                self.assertEqual(status, "warn" if newer_is_release else "stale")

    def test_COMMA_LOCALE_whole_second_FALLBACK_is_exercised(self) -> None:
        """Older GNU without %.9Y must still migrate correctly in a comma locale.

        Covers the whole-second fallback candidates, which the subsecond test
        never reaches because %.9Y answers first.
        """
        SEC = 1700000000
        for newer_is_release in (True, False):
            with self.subTest(newer_is_release=newer_is_release):
                self.setUp()
                bin_ = self._gnu_stat_shim()
                shim = bin_ / "stat"
                body = self._disable_subsecond(shim.read_text())
                shim.write_text(body)
                shim.chmod(0o755)
                armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
                self._write("src/a", armed if not newer_is_release else [], SEC)
                self._write("src/c", [] if not newer_is_release else armed, SEC + 5)
                self._migrate(extra_env={"PATH": f"{bin_}:{os.environ['PATH']}",
                                         "LC_ALL": "de_DE.UTF-8"})
                self._assert_migrator_used_shim(fallback=True)
                status, _ = self._verdict(self.tmp / "dest" / "state" / "process-pins.json")
                self.assertEqual(status, "warn" if newer_is_release else "stale")

    def test_COMMA_LOCALE_stat_still_yields_subsecond_ordering(self) -> None:
        """A comma-decimal locale must not collapse mtime to whole seconds.

        GNU stat prints localeconv()->decimal_point, so `stat -c %.9Y` returns
        "sec,nsec" under e.g. de_DE. The validator rejects the comma, degrades to
        integer %Y, and two distinct writes tie -- a false AMBIGUOUS abort.
        """
        import os
        import pathlib
        import re
        import subprocess
        import tempfile
        src = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sutando-migrate.sh"
        body = re.search(r"^mtime_ns\(\) \{.*?^\}", src.read_text(), re.S | re.M)
        self.assertIsNotNone(body, "mtime_ns not found -- test cannot bind its subject")
        fixed = body.group(0)
        broken = fixed.replace("LC_ALL=C ", "")
        self.assertNotEqual(fixed, broken, "no LC_ALL=C present: control cannot discriminate")

        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td); bin_ = td / "bin"; bin_.mkdir()
            shim = bin_ / "stat"
            # The shim must NOT call host stat: GNU treats -f as --file-system and
            # emits the very non-numeric report this patch exists to reject.
            shim.write_text(
                '#!/bin/sh\n'
                'case "$1" in -f) exit 1 ;; -c) fmt="$2"; f="$3" ;; *) exit 1 ;; esac\n'
                'case "${f##*/}" in a) ns=1700000000.100000000 ;; '
                'b) ns=1700000000.900000000 ;; *) exit 1 ;; esac\n'
                'case "$fmt" in\n'
                '  %.9Y) sep=","; [ "$LC_ALL" = "C" ] && sep="."\n'
                '        printf \'%s%s%s\\n\' "${ns%%.*}" "$sep" '
                '"$(printf \'%s\' "${ns#*.}000000000" | cut -c1-9)" ;;\n'
                '  %Y)   printf \'%s\\n\' "${ns%%.*}" ;;\n'
                'esac\n')
            shim.chmod(0o755)
            self.assertTrue(os.access(shim, os.X_OK), "shim not executable -- invalid control")

            a, b = td / "a", td / "b"
            a.write_text("A")
            os.utime(a, ns=(1_700_000_000_100_000_000, 1_700_000_000_100_000_000))
            b.write_text("B")
            os.utime(b, ns=(1_700_000_000_900_000_000, 1_700_000_000_900_000_000))

            def probe(fn_src: str, target: pathlib.Path) -> str:
                lib = td / "lib.sh"; lib.write_text(fn_src)
                env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
                       "LC_ALL": "de_DE.UTF-8"}
                r = subprocess.run(["bash", "-c", f'. "{lib}"; mtime_ns "{target}"'],
                                   capture_output=True, text=True, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
                return r.stdout.strip()

            # Negative half: without LC_ALL=C the comma is rejected and both tie.
            self.assertEqual(probe(broken, a), probe(broken, b),
                             "control is inert: pre-fix form did not tie under a comma locale")
            # Positive half: the shipped form orders, in BOTH directions.
            fa, fb = probe(fixed, a), probe(fixed, b)
            self.assertNotEqual(fa, fb, "comma locale still collapsed subsecond mtime")
            self.assertLess(int(fa), int(fb))
            self.assertGreater(int(fb), int(fa))

    def test_SUB_SECOND_ordering_decides_not_scan_order(self) -> None:
        """Two writes in the same second must still order by their real mtime.

        Integer-second `stat` collapses them and the comparator then falls back
        to scan order, which resurrects whichever copy happens to be scanned
        last — a released pin coming back, or a live one being dropped.
        """
        base = _NOW - 3600
        for release_is_newer in (True, False):
            with self.subTest(release_is_newer=release_is_newer):
                self.setUp()
                armed = [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")]
                a_pins, c_pins = ([], armed) if release_is_newer else (armed, [])
                fa = self._write("src/a", a_pins, base)
                fc = self._write("src/c", c_pins, base)
                # Same SECOND, different sub-second instant. A is the newer one.
                os.utime(fc, ns=(int(base * 1e9) + 100_000_000,) * 2)
                os.utime(fa, ns=(int(base * 1e9) + 900_000_000,) * 2)
                self._migrate()
                status, detail = self._verdict(
                    self.tmp / "dest" / "state" / "process-pins.json")
                self.assertEqual(status, "stale" if release_is_newer else "warn", detail)


class PinMigrationUnionTest(PinMigrationVisibilityTest):
    """Independent sources are not one history (keweichen, #3356): a newer
    snapshot missing a pin proves nothing about a LIVE pin an older one carries.
    Positive control uses a real child process so liveness is measured, not
    injected; negative controls keep the release semantics for dead pids."""

    # Reuse the fixture only: the parent's cases must not run a second time here.
    for _name in [n for n in dir(PinMigrationVisibilityTest) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def _live_child(self):
        import subprocess
        child = subprocess.Popen(["sleep", "300"])
        self.addCleanup(child.kill)
        ls = subprocess.run(["ps", "-o", "lstart=", "-p", str(child.pid)],
                            capture_output=True, text=True).stdout.strip()
        self.assertTrue(ls, "ps returned no lstart for the live child")
        return str(child.pid), ls

    def _pins_at(self, path):
        return {(p["service"], str(p["pid"]), p["lstart"]) for p in pp.load_pins(path)}

    def test_a_LIVE_older_only_pin_survives_a_newer_unrelated_snapshot(self) -> None:
        """The blocker as reported: an unrelated newer Telegram pin must not
        drop a still-live Discord veto."""
        cpid, cls = self._live_child()
        self._write("src/a", [pin(cpid, cls, "#2604 witness armed")], OLDER)
        self._write("src/c", [{"service": "telegram-bridge", "pid": 333,
                               "lstart": "Sat Aug 23 01:00:00 2026",
                               "reason": "unrelated", "expires_at": FUTURE}], NEWER)
        self._migrate()
        got = self._pins_at(self.tmp / "dest" / "state" / "process-pins.json")
        self.assertIn((SERVICE, cpid, cls), got, f"live veto dropped: {got}")
        self.assertIn(("telegram-bridge", "333", "Sat Aug 23 01:00:00 2026"), got)
        # the production reader still vetoes the live one
        res = pp.evaluate(pp.load_pins(self.tmp / "dest" / "state" / "process-pins.json"),
                          SERVICE, {cpid: cls}, time.time())
        self.assertEqual([v for v, _p, _d in res], [pp.ARMED], res)

    def test_a_DEAD_older_only_pin_is_still_dropped(self) -> None:
        """Dead pid + unrelated newer snapshot: nothing live to keep."""
        self._write("src/a", [pin(DEAD_PID, "Sat Aug 23 01:00:00 2026", "stale witness")], OLDER)
        self._write("src/c", [{"service": "telegram-bridge", "pid": 333,
                               "lstart": "Sat Aug 23 01:00:00 2026",
                               "reason": "unrelated", "expires_at": FUTURE}], NEWER)
        self._migrate()
        got = self._pins_at(self.tmp / "dest" / "state" / "process-pins.json")
        self.assertNotIn((SERVICE, DEAD_PID, "Sat Aug 23 01:00:00 2026"), got, got)
        self.assertEqual(len(got), 1, got)

    def test_an_EXPIRED_live_older_only_pin_is_dropped(self) -> None:
        cpid, cls = self._live_child()
        expired = {"service": SERVICE, "pid": int(cpid), "lstart": cls,
                   "reason": "old", "expires_at": "2000-01-01T00:00:00Z"}
        self._write("src/a", [expired], OLDER)
        self._write("src/c", [], NEWER)
        self._migrate()
        self.assertEqual(self._pins_at(self.tmp / "dest" / "state" / "process-pins.json"), set())

    def test_the_same_pin_in_both_snapshots_is_kept_ONCE(self) -> None:
        cpid, cls = self._live_child()
        self._write("src/a", [pin(cpid, cls, "armed (a)")], OLDER)
        self._write("src/c", [pin(cpid, cls, "armed (c)")], NEWER)
        self._migrate()
        pins = pp.load_pins(self.tmp / "dest" / "state" / "process-pins.json")
        self.assertEqual(len(pins), 1, pins)
        self.assertEqual(pins[0]["reason"], "armed (c)", "the newer snapshot's copy wins")

    def test_direction_does_not_matter_for_a_live_pin(self) -> None:
        """Scan order and which side is newer both leave the live pin present."""
        cpid, cls = self._live_child()
        unrelated = {"service": "telegram-bridge", "pid": 333,
                     "lstart": "Sat Aug 23 01:00:00 2026", "reason": "x", "expires_at": FUTURE}
        for live_is_newer in (True, False):
            with self.subTest(live_is_newer=live_is_newer):
                self.setUp()
                self._write("src/a", [pin(cpid, cls, "armed")], NEWER if live_is_newer else OLDER)
                self._write("src/c", [unrelated], OLDER if live_is_newer else NEWER)
                self._migrate()
                got = self._pins_at(self.tmp / "dest" / "state" / "process-pins.json")
                self.assertIn((SERVICE, cpid, cls), got, got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
