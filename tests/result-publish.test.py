#!/usr/bin/env python3
"""The one results/ publisher (src/result_publish.py) and delegation by every writer (#3956).

A result file is claimed on sight, so a writer that creates the name and fills it
afterwards hands the drain a zero-byte file or a prefix. The publisher stages beside
the target, fsyncs, and renames whole. The drills below call the PRODUCTION publisher
from forked processes while a reader polls the directory, and assert no reader ever
observes a size other than the full body. The delegation checks are structural, like
tests/bridge-marker-no-leak.test.py: importing each writer has side effects.

Run: python3 tests/result-publish.test.py
"""
from __future__ import annotations

import importlib
import io
import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import result_publish  # noqa: E402


def _load_vendored():
    sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
    return importlib.import_module("ag2_sparrow.result_publish")
from delivery.readiness import read_ready_result  # noqa: E402

BODY = "header line\n" + ("body text that follows the first paragraph boundary. " * 50)
FULL = len(BODY.encode("utf-8"))


def _publish_many(results: str, worker: int, rounds: int) -> None:
    for i in range(rounds):
        result_publish.publish_text(Path(results) / f"task-{worker}-{i}.txt", BODY)


def _poll_sizes(results: str, expected: int, stop_at: float, out) -> None:
    """Record every size a drain-shaped reader (glob task-*.txt, stat) can observe."""
    seen = set()
    while time.time() < stop_at or len(seen) < expected:
        for p in Path(results).glob("task-*.txt"):
            try:
                seen.add((p.name, p.stat().st_size))
            except FileNotFoundError:
                pass
        if len({n for n, _ in seen}) >= expected and time.time() >= stop_at:
            break
    out.put(sorted(seen))


class PublisherContract(unittest.TestCase):
    def test_publishes_the_exact_body_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "results" / "task-1.txt"
            self.assertEqual(result_publish.publish_text(target, BODY), target)
            self.assertEqual(target.read_text(encoding="utf-8"), BODY)
            self.assertEqual([p.name for p in target.parent.iterdir()], ["task-1.txt"])

    def test_staged_name_matches_no_drain_glob(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "task-1.txt"
            tmp = result_publish.stage_text(target, BODY)
            try:
                self.assertTrue(tmp.name.startswith("."), tmp.name)
                self.assertTrue(tmp.name.endswith(result_publish.STAGED_SUFFIX), tmp.name)
                self.assertEqual(list(Path(td).glob("task-*.txt")), [])
                self.assertEqual(list(Path(td).glob("*.txt")), [])
                self.assertEqual(tmp.read_text(encoding="utf-8"), BODY)
            finally:
                tmp.unlink()

    def test_two_stagings_of_one_name_never_share_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "task-1.txt"
            a = result_publish.stage_text(target, "a")
            b = result_publish.stage_text(target, "b")
            self.assertNotEqual(a, b)
            self.assertEqual((a.read_text(), b.read_text()), ("a", "b"))

    def test_failed_publish_leaves_neither_target_nor_staging(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "task-1.txt"
            real = os.replace

            def boom(src, dst):
                raise OSError("simulated crash at publish")

            os.replace = boom
            try:
                with self.assertRaises(OSError):
                    result_publish.publish_text(target, BODY)
            finally:
                os.replace = real
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_published_file_honours_the_umask_like_a_plain_write(self):
        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "plain.txt"
            plain.write_text("x")
            published = result_publish.publish_text(Path(td) / "task-1.txt", "x")
            self.assertEqual(published.stat().st_mode & 0o777, plain.stat().st_mode & 0o777)

    def test_local_record_stays_owner_only(self):
        import local_record
        old = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as td:
                rec = local_record.write_whole(Path(td) / "q-1.json", {"a": 1})
                self.assertEqual(rec.stat().st_mode & 0o777, 0o600)
                body = local_record.write_text_whole(Path(td) / "proactive-1.txt", "x")
                self.assertEqual(body.stat().st_mode & 0o777, 0o600)
        finally:
            os.umask(old)

    def test_cli_publishes_stdin_whole(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "results" / "proactive-1.txt"
            proc = subprocess.run([sys.executable, str(REPO / "src" / "result_publish.py"), str(target)],
                                  input=BODY, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), BODY)
            self.assertEqual([p.name for p in target.parent.iterdir()], ["proactive-1.txt"])

    def test_cli_refuses_without_a_path(self):
        proc = subprocess.run([sys.executable, str(REPO / "src" / "result_publish.py")],
                              input="", text=True, capture_output=True)
        self.assertEqual(proc.returncode, 2)


class PublisherFailurePaths(unittest.TestCase):
    """Run against src and the vendored ag2-sparrow copy: the bridge imports the latter."""

    MODULES = (result_publish, _load_vendored())

    def test_failed_stage_leaves_no_staging_and_raises(self):
        for mod in self.MODULES:
            with self.subTest(mod=mod.__file__), tempfile.TemporaryDirectory() as td:
                with mock.patch.object(mod.os, "fsync", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        mod.stage_text(Path(td) / "task-1.txt", BODY)
                self.assertEqual(list(Path(td).iterdir()), [])

    def test_failed_publish_removes_its_staging(self):
        for mod in self.MODULES:
            with self.subTest(mod=mod.__file__), tempfile.TemporaryDirectory() as td:
                with mock.patch.object(mod.os, "replace", side_effect=OSError("crash at publish")):
                    with self.assertRaises(OSError):
                        mod.publish_text(Path(td) / "task-1.txt", BODY)
                self.assertEqual(list(Path(td).iterdir()), [])

    def test_main_publishes_stdin_and_refuses_a_flag_or_no_path(self):
        for mod in self.MODULES:
            with self.subTest(mod=mod.__file__), tempfile.TemporaryDirectory() as td:
                target = Path(td) / "proactive-1.txt"
                with mock.patch.object(mod.sys, "stdin", io.StringIO(BODY)):
                    self.assertEqual(mod.main(["result_publish.py", str(target)]), 0)
                self.assertEqual(target.read_text(encoding="utf-8"), BODY)
                with mock.patch.object(mod.sys, "stderr", io.StringIO()):
                    self.assertEqual(mod.main(["result_publish.py"]), 2)
                    self.assertEqual(mod.main(["result_publish.py", "--help"]), 2)


class ConcurrencyDrill(unittest.TestCase):
    """Forked writers through the production publisher; a polling reader sees whole bodies only."""

    def test_reader_never_observes_a_partial_body(self):
        workers, rounds = 4, 40
        ctx = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as td:
            results = str(Path(td) / "results")
            Path(results).mkdir()
            out = ctx.Queue()
            reader = ctx.Process(target=_poll_sizes,
                                 args=(results, workers * rounds, time.time() + 1.0, out))
            reader.start()
            writers = [ctx.Process(target=_publish_many, args=(results, w, rounds)) for w in range(workers)]
            for w in writers:
                w.start()
            for w in writers:
                w.join(60)
                self.assertEqual(w.exitcode, 0)
            seen = out.get(timeout=60)
            reader.join(60)
            names = {n for n, _ in seen}
            self.assertEqual(len(names), workers * rounds, "reader did not see every published file")
            partial = sorted({(n, s) for n, s in seen if s != FULL})
            self.assertEqual(partial, [], f"a reader observed a partial body: {partial[:5]}")
            self.assertEqual(len(list(Path(results).iterdir())), workers * rounds, "staged leftovers")

    def test_control_a_plain_write_is_what_the_reader_would_catch(self):
        """The probe is sensitive: a file created empty and filled later IS observed at 0."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "task-1.txt"
            with open(p, "w", encoding="utf-8") as fh:
                self.assertEqual(p.stat().st_size, 0)
                self.assertIsNone(read_ready_result(p), "an empty just-created result must not be claimed")
                fh.write(BODY)
            self.assertEqual(p.stat().st_size, FULL)


# Every in-repo writer of a results/ body, and how each one reaches the publisher.
WRITERS = {
    "src/morning-briefing.py": "from result_publish import publish_text",
    "src/friction-detector.py": "from result_publish import publish_text",
    "src/discord-bridge.py": "from result_publish import publish_text",
    "src/local_record.py": "from result_publish import publish_text",
    "skills/deal-finder/scripts/scan.py": "from result_publish import publish_text",
    "skills/pending-questions/scripts/pending_questions_remind.py": "from result_publish import publish_text",
    "skills/import-claude-context/scripts/finalize.py": "from result_publish import publish_text",
    "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py": "from .result_publish import",
    "src/notify.sh": "result_publish.py",
    "src/launchd/gateway-bridge-wrapper.sh": "result_publish.py",
    "src/launchd/channel-bridge-wrapper.sh": "result_publish.py",
    "src/voice-agent.ts": "publishResultFile(`proactive-voice-${c.category}-",
    "src/live-agent-runtime.ts": "publishResultFile(`proactive-voice-stuck-",
    "src/task-bridge.ts": "publishResultFile(`proactive-timeout-",
}
# Writers that publish through local_record.write_text_whole, which delegates.
VIA_LOCAL_RECORD = {
    "skills/pending-questions/scripts/pending_questions_ask.py": "write_text_whole(",
    "scripts/ask-owner.py": "write_text_whole(",
}
# The direct-write shapes each Python writer used to carry.
HAND_ROLLED = (
    "result_file.write_text(", "output_path.write_text(", "(RESULTS_DIR / name).write_text(",
    "tmp.write_text(part", 'f.write_text(clean_body', "path.write_text(\n        f\"You have",
)


_SH_TARGET = re.compile(r"(?<![0-9&])(?:>>?|\btee(?:\s+-a)?)\s*([^\s;|&)<>]+)")
_SH_ASSIGN = re.compile(r"^\s*(?:local\s+|export\s+|readonly\s+)?([A-Za-z_]\w*)=(\S*results/\S*)", re.M)
_TS_ASSIGN = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=;]+)?=\s*([^;]+);")
_TS_WRITE = re.compile(r"\b(?:writeFileSync|appendFileSync|writeFile|createWriteStream)\(\s*([^,)]+)")
_TS_RESULTS = re.compile(r"""['"`]results['"`/]|/results/|\bRESULTS?_DIR\b|\bresultsDir\b|\bresultDir\b""")
# Not result bodies a drain delivers: a smoke test's backdated fixture.
_SCAN_EXEMPT = {"skills/self-diagnose/scripts/test-gather.sh"}


def shell_results_writes(text: str) -> list:
    """`>`, `>>` or `tee` whose target is a results/ path, literally or via a variable."""
    names = {m.group(1) for m in _SH_ASSIGN.finditer(text)}
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in _SH_TARGET.finditer(line):
            target = m.group(1).strip("\"'")
            var = re.match(r"^\$\{?(\w+)\}?$", target)
            if "results/" in target or (var and var.group(1) in names):
                hits.append((n, line.strip()))
    return hits


def ts_results_writes(text: str) -> list:
    """A node fs write whose path argument is, or was assigned from, a results/ path."""
    names = {m.group(1) for m in _TS_ASSIGN.finditer(text) if _TS_RESULTS.search(m.group(2))}
    hits = []
    for m in _TS_WRITE.finditer(text):
        arg = m.group(1).strip()
        if _TS_RESULTS.search(arg) or arg in names:
            hits.append((text.count("\n", 0, m.start()) + 1, arg))
    return hits


def scan_results_writes(root: Path = REPO) -> list:
    hits = []
    for top in ("src", "skills", "scripts"):
        for p in sorted((root / top).rglob("*")):
            rel = p.relative_to(root).as_posix()
            if not p.is_file() or "node_modules" in p.parts or ".test." in p.name or rel in _SCAN_EXEMPT:
                continue
            if p.suffix in (".sh", ".bash"):
                found = shell_results_writes(p.read_text(encoding="utf-8", errors="replace"))
            elif p.suffix in (".ts", ".mts", ".js", ".mjs") and not p.name.endswith(".d.ts"):
                found = ts_results_writes(p.read_text(encoding="utf-8", errors="replace"))
            else:
                continue
            hits += [(rel, n, what) for n, what in found]
    return hits


# The pre-publisher shapes, verbatim from the writers this PR converted.
_OLD_GATEWAY_WRAPPER = (
    'printf \'%s\\n\' "The gateway bridge exited and was automatically restarted." > '
    '"$WORKSPACE/results/proactive-gateway-bridge-restarted-$NOW.txt"\n')
_OLD_CHANNEL_WRAPPER = (
    '  RESULT="$WORKSPACE/results/proactive-$CHANNEL-bridge-restarted-$NOW.txt"\n'
    '  printf \'%s\\n\' "The $CHANNEL bridge exited and was automatically restarted." > "$RESULT"\n')
_OLD_TS_VOICE = (
    "const path = join(WORKSPACE_DIR, 'results', `proactive-voice-${c.category}-${tsMs}.txt`);\n"
    "writeFileSync(path, body);\n")
_OLD_TS_TIMEOUT = (
    "const proactivePath = join(RESULT_DIR, `proactive-timeout-${taskId}-${proactiveTs}.txt`);\n"
    "writeFileSync(proactivePath, dmBody);\n")
_OLD_TS_CANCEL = "writeFileSync(join(resultsDir, `${targetId}.txt`), 'Cancelled.');\n"


class DelegationTest(unittest.TestCase):
    def test_every_writer_reaches_the_publisher(self):
        for rel, needle in {**WRITERS, **VIA_LOCAL_RECORD}.items():
            with self.subTest(writer=rel):
                src = (REPO / rel).read_text(encoding="utf-8")
                self.assertIn(needle, src, f"{rel}: does not publish through result_publish")

    def test_no_writer_hand_rolls_the_result_write(self):
        for rel in list(WRITERS) + list(VIA_LOCAL_RECORD):
            if not rel.endswith(".py"):
                continue
            src = (REPO / rel).read_text(encoding="utf-8")
            for shape in HAND_ROLLED:
                self.assertNotIn(shape, src, f"{rel}: writes a result body directly ({shape!r})")

    def test_no_shell_or_ts_file_writes_into_results_directly(self):
        hits = [f"{rel}:{n}: {what}" for rel, n, what in scan_results_writes()]
        self.assertEqual(hits, [], "writes into results/ that bypass the publisher:\n" + "\n".join(hits))

    def test_scan_flags_the_pre_publisher_shapes(self):
        # Controls: each shape a converted writer used to have must be caught.
        self.assertTrue(shell_results_writes(_OLD_GATEWAY_WRAPPER))
        self.assertTrue(shell_results_writes(_OLD_CHANNEL_WRAPPER))
        self.assertTrue(ts_results_writes(_OLD_TS_VOICE))
        self.assertTrue(ts_results_writes(_OLD_TS_TIMEOUT))
        self.assertTrue(ts_results_writes(_OLD_TS_CANCEL))
        self.assertFalse(shell_results_writes(
            'printf x | python3 "$REPO/src/result_publish.py" "$WORKSPACE/results/proactive-1.txt"\n'
            'echo hi >&2; cmd 2>/dev/null\n'))
        self.assertFalse(ts_results_writes("publishResultFile(`proactive-1.txt`, body);\n"
                                           "writeFileSync(join(TASK_DIR, `${id}.txt`), body);\n"))

    def test_bridge_stage_and_publish_delegate(self):
        src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text(encoding="utf-8")
        self.assertIn("return _stage_text(path, text)", src)
        self.assertIn("_publish_staged_whole(tmp, path)", src)
        self.assertNotIn("uuid.uuid4().hex}.tmp\")\n        with open(tmp", src)

    def test_ts_cancel_result_goes_through_publishResultFile(self):
        src = (REPO / "src" / "inline-tools.ts").read_text(encoding="utf-8")
        self.assertIn("publishResultFile(`${targetId}.txt`, 'Cancelled.')", src)
        self.assertNotIn("writeFileSync(join(resultsDir", src)

    def test_sparrow_bundle_matches_src(self):
        pkg = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_publish.py"
        self.assertTrue(pkg.exists(), "result_publish.py not bundled into ag2-sparrow")
        self.assertEqual(pkg.read_text(), (REPO / "src" / "result_publish.py").read_text(),
                         "ag2-sparrow copy drifted from src/ — run tools/sync_from_src.py")

    def test_publisher_is_dependency_light(self):
        src = (REPO / "src" / "result_publish.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.startswith(("import ", "from ")) and "__future__" not in line:
                self.assertIn(line.split()[1].split(".")[0], ("os", "secrets", "sys", "pathlib"), line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
