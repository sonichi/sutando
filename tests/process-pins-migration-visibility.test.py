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

    def _migrate(self, extra_env=None):
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
        self.assertEqual(r.returncode, 0, f"migrate failed:\n{r.stdout}\n{r.stderr}")
        return r

    def _gnu_stat_shim(self, comma: bool = True, synth: dict | None = None) -> Path:
        """A faithful-enough GNU `stat` on PATH: -f is --file-system (fails),
        -c formats succeed, and fractions use the locale's decimal separator."""
        import uuid
        nonce = f"SHIM-BOUND-{uuid.uuid4().hex[:12]}"
        bin_ = self.tmp / "shimbin"
        bin_.mkdir(exist_ok=True)
        self._trace = self.tmp / "stat-trace.log"
        self._nonce = nonce
        (bin_ / "stat").write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "a = sys.argv[1:]\n"
            "import json as _j\n"
            f"open({str(self._trace)!r}, 'a').write("
            f"_j.dumps(['{nonce}'] + a) + '\\n')\n"
            "if not a or a[0] == '-f':\n"
            "    sys.stderr.write('stat: cannot read file system information\\n')\n"
            f"    sys.stderr.write('{nonce}\\n')\n"
            "    print('  File: \"x\"'); print('    ID: 0 Namelen: 255'); sys.exit(1)\n"
            "if a[0] != '-c':\n"
            "    sys.exit(1)\n"
            "fmt, f = a[1], a[2]\n"
            "st = os.stat(f)\n"
            "comma = os.environ.get('LC_ALL') != 'C'\n"
            "sep = ',' if comma else '.'\n"
            f"_SYNTH = {synth or {}!r}\n"
            "_k = os.path.basename(os.path.dirname(os.path.dirname(f)))\n"
            "if fmt == '%.9Y':\n"
            "    if _k in _SYNTH:\n"
            "        _w, _n = _SYNTH[_k]\n"
            "        print(f'{_w}{sep}{_n:09d}')\n"
            "    else:\n"
            "        print(f'{int(st.st_mtime)}{sep}{st.st_mtime_ns % 1000000000:09d}')\n"
            "elif fmt == '%Y':\n"
            "    print(_SYNTH[_k][0] if _k in _SYNTH else int(st.st_mtime))\n"
            "elif fmt in ('%s', '%z'):  print(st.st_size)\n"
            "elif fmt == '%y':  print(time.strftime('%Y-%m-%d %H:%M:%S',\n"
            "                          time.localtime(st.st_mtime)))\n"
            "else: sys.exit(1)\n")
        (bin_ / "stat").chmod(0o755)
        self._assert_shim_binds(bin_, nonce)
        return bin_

    def _assert_shim_binds(self, bin_: Path, nonce: str) -> None:
        """Executable-on-disk is not bound-on-PATH.

        Assert on a NONCE, never on nonzero-exit + `File:` -- host GNU stat
        emits exactly that for `-f` (it is --file-system), so that predicate
        is satisfied by the host on the one platform it must discriminate on.
        """
        import subprocess
        env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
        r = subprocess.run(["stat", "-f", "%m", str(bin_)],
                           capture_output=True, text=True, env=env)
        self.assertIn(nonce, r.stderr,
                      f"shim did NOT bind: host stat answered (stderr={r.stderr[:80]!r})")
        # Truncate: this probe's own entry must not satisfy the production
        # assertion, which asks whether the MIGRATOR subprocess used the shim.
        self._trace.write_text("")

    def _assert_migrator_used_shim(self, *, fallback: bool = False) -> None:
        """Bind the trace to the EXACT operands, both of them.

        A root prefix accepts a decoy under that root -- and startswith() is
        string prefix, not path containment, so root src/a also admits
        src/attacker/... Require (flag, fmt, exact path) for every required
        format against BOTH files the comparator must stat.
        """
        import json
        seen = self._trace.read_text() if self._trace.exists() else ""
        calls = set()
        for ln in seen.splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if not rec or rec[0] != self._nonce or len(rec) < 4:
                continue
            # rec == [nonce, flag, fmt, path]; macOS resolves /var -> /private/var
            calls.add((rec[1], rec[2], str(Path(rec[3]).resolve())))
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
        """Absence from the NEWER array is the release. A union would re-arm it."""
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
                body = shim.read_text().replace("if fmt == '%.9Y':", "if False:")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
