#!/usr/bin/env python3
"""Execute the documented boot gate with production ownership logic and fixture process snapshots."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parents[1]


class DocumentedGate(unittest.TestCase):
    def test_each_checkout_uses_its_own_tree_and_keeps_unknown(self):
        doc = (REPO / "skills/schedule-crons/SKILL.md").read_text()
        section = doc.split("1.5. **Start the streaming task watcher", 1)[1]
        gate = textwrap.dedent(section.split("```bash\n", 1)[1].split("```", 1)[0])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            driver = root / "driver.py"
            driver.write_text('''import importlib.util, json, os, sys
real_spec = importlib.util.spec_from_file_location

def fixture_spec(name, path, *args, **kwargs):
    spec = real_spec(name, path, *args, **kwargs)
    if name == "hc":
        real_exec = spec.loader.exec_module
        def load(module):
            real_exec(module)
            module.REPO_DIR = os.environ["FIXTURE_REPO"]
            module._ps_snapshot = lambda: json.loads(os.environ["FIXTURE_PS"])
        spec.loader.exec_module = load
    return spec

importlib.util.spec_from_file_location = fixture_spec
assert sys.argv[1] == "-c"
exec(compile(sys.argv[2], "<documented watcher gate>", "exec"))
''')
            shim = root / "python3"
            shim.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                            shlex.quote(str(driver)) + ' "$@"\n')
            shim.chmod(0o755)
            a, b = root / "a", root / "b"
            own = f"999999100 1 /bin/bash {a}/src/watch-tasks-stream.sh\n"
            foreign = f"999999200 1 /bin/bash {b}/src/watch-tasks-stream.sh\n"
            cases = [(a, foreign, "start"), (b, own, "start"),
                     (a, own + foreign, "skip"), (b, own + foreign, "skip"),
                     (a, "", "start"), (a, None, "UNKNOWN:"),
                     (a, "999999300 1 /bin/bash src/watch-tasks-stream.sh --tasks /fixture\n", "skip")]
            for repo, snapshot, expected in cases:
                with self.subTest(repo=repo.name, snapshot=snapshot):
                    env = {"PATH": str(root) + os.pathsep + os.defpath, "HOME": tmp,
                           "FIXTURE_REPO": str(repo), "FIXTURE_PS": json.dumps(snapshot)}
                    out = subprocess.run(["/bin/bash", "-c", gate], cwd=REPO, env=env,
                                         capture_output=True, text=True, timeout=20)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertTrue(out.stdout.strip().startswith(expected), out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
