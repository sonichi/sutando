#!/usr/bin/env python3
"""`check_vendored_resolver_env` must be non-executing and fail honest.

Two properties, both from qingyun-wu's review of the first draft (#3892):

1. It must NOT execute discovered source. The first draft imported each copy in a
   subprocess; a subprocess is failure isolation, not a security boundary, so any
   checked-out skill could run import-time code with the health check's env. Their
   control was a copy whose top level wrote a marker — it existed before the probe
   returned. `test_a_marker_writing_copy_is_never_executed` is that control.

2. An unmeasurable copy must NOT read as clean. The first draft skipped timeouts
   and import errors, so `offenders == []` reported "none honour". The original
   test PINNED that false reassurance; it is now inverted.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hc_vre", _REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

_HONOURS = ("import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    p = os.environ.get('SUTANDO_WORKSPACE')\n"
            "    return Path(p)\n")


def _vendor(ws: Path, body: str, name: str = "someskill") -> Path:
    d = ws / "skill-repos" / name / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace_default.py").write_text(body)
    return d


class NonExecuting(unittest.TestCase):
    def test_a_marker_writing_copy_is_never_executed(self):
        """qingyun-wu's control: detection must not run what it inspects."""
        ws = Path(tempfile.mkdtemp())
        marker = ws / "side_effect_marker"
        _vendor(ws, f"open({str(marker)!r}, 'w').write('x')\n" + _HONOURS)
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertFalse(marker.exists(),
                         "the probe EXECUTED a discovered module — it wrote its marker")
        self.assertEqual(r["status"], "warn", "and it must still detect the defect statically")


class FailsHonest(unittest.TestCase):
    def test_an_unanalysable_copy_is_not_reported_clean(self):
        """Inverted from the first draft, which asserted `ok` here."""
        ws = Path(tempfile.mkdtemp())
        _vendor(ws, "this is not python(\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("could NOT be analysed", r["detail"])
        self.assertNotIn("none honour", r["detail"],
                         "an unmeasured copy was folded into a clean bill")

    def test_a_copy_without_the_function_is_also_unknown(self):
        ws = Path(tempfile.mkdtemp())
        _vendor(ws, "import os\nx = 1\n")
        self.assertEqual(hc.check_vendored_resolver_env(workspace_dir=ws)["status"], "warn")


class Verdicts(unittest.TestCase):
    def test_the_canonical_resolver_ignores_the_env(self):
        """Positive control: without it a detector that says 'ignores' for
        everything would pass every case below."""
        v, _ = hc._resolver_env_verdict(_REPO / "src" / "workspace_default.py")
        self.assertEqual(v, "ignores")

    def test_a_pre_v08_shape_is_flagged_and_named(self):
        ws = Path(tempfile.mkdtemp())
        _vendor(ws, _HONOURS)
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("workspace_default.py", r["detail"],
                      "the warn must NAME the offender, not just count it")

    def test_an_alias_through_a_local_still_counts(self):
        """`p = os.environ[...]` then `return Path(p)` — the shape the real copy uses."""
        ws = Path(tempfile.mkdtemp())
        _vendor(ws, "import os\nfrom pathlib import Path\n"
                    "def resolve_workspace():\n"
                    "    raw = os.environ['SUTANDO_WORKSPACE']\n"
                    "    q = raw\n"
                    "    return Path(q)\n")
        self.assertEqual(hc.check_vendored_resolver_env(workspace_dir=ws)["status"], "warn")

    def test_mentioning_the_name_without_returning_it_is_not_a_hit(self):
        """Negative control: the canonical file mentions the env only to warn."""
        ws = Path(tempfile.mkdtemp())
        _vendor(ws, "import os\nfrom pathlib import Path\n"
                    "def resolve_workspace():\n"
                    "    if os.environ.get('SUTANDO_WORKSPACE'):\n"
                    "        print('warning: no longer honored')\n"
                    "    return Path('/configured')\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "ok", r["detail"])


class EquivalentForms(unittest.TestCase):
    """Three spellings that honour the env without the two shapes the first
    analysis recognized. All three read 'ignores' at 12d5f2ad."""

    def _verdict(self, src):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(src)
        return hc._resolver_env_verdict(f)[0]

    def test_os_getenv_is_the_same_read(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('SUTANDO_WORKSPACE'))\n"), "honours")

    def test_a_module_level_environ_alias_is_the_same_read(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "env = os.environ\n"
            "def resolve_workspace():\n"
            "    raw = env.get('SUTANDO_WORKSPACE')\n"
            "    return Path(raw)\n"), "honours")

    def test_the_read_may_sit_in_another_function(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def legacy():\n"
            "    return os.environ.get('SUTANDO_WORKSPACE')\n"
            "def resolve_workspace():\n"
            "    return Path(legacy())\n"), "honours")

    def test_an_unresolvable_call_is_unknown_not_clean(self):
        """The general guard: what the analysis cannot follow is never 'ignores'."""
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(mystery())\n"), "unknown")

    def test_a_genuinely_clean_resolver_is_still_clean(self):
        """Negative control: the guard above must not make everything unknown."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.expanduser('~/w'))\n"), "ignores")


class UnresolvedIsNeverClean(unittest.TestCase):
    """Round 4: the fallback was applied only to the return expression and
    ignored dotted callees, so both shapes below read 'ignores' at 6a4ace97."""

    def _verdict(self, src):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(src)
        return hc._resolver_env_verdict(f)[0]

    def test_a_local_assigned_from_an_opaque_call_taints_the_return(self):
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    raw = mystery()\n"
            "    return Path(raw)\n"), "unknown")

    def test_a_dotted_callee_is_unresolved_too(self):
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(config.workspace())\n"), "unknown")

    def test_a_readable_same_name_delegate_is_analysed_not_guessed(self):
        """The canonical resolver delegates to a sibling module; that hop is
        taken, so a real file is not condemned as unknown."""
        d = Path(tempfile.mkdtemp())
        (d / "sutando_config.py").write_text(
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path('/configured')\n")
        (d / "workspace_default.py").write_text(
            "import sutando_config\n"
            "def resolve_workspace():\n"
            "    return sutando_config.resolve_workspace()\n")
        self.assertEqual(hc._resolver_env_verdict(d / "workspace_default.py")[0], "ignores")

    def test_the_delegate_carries_its_own_verdict_back(self):
        """Same hop, dirty delegate: the honours verdict must propagate."""
        d = Path(tempfile.mkdtemp())
        (d / "sutando_config.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.environ['SUTANDO_WORKSPACE'])\n")
        (d / "workspace_default.py").write_text(
            "import sutando_config\n"
            "def resolve_workspace():\n"
            "    return sutando_config.resolve_workspace()\n")
        self.assertEqual(hc._resolver_env_verdict(d / "workspace_default.py")[0], "honours")


class Coverage(unittest.TestCase):
    def test_zero_copies_reports_coverage_rather_than_going_silent(self):
        """Sutando-Mini on #3892: a probe that finds nothing must SAY so."""
        r = hc.check_vendored_resolver_env(workspace_dir=Path(tempfile.mkdtemp()))
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "ok")
        self.assertIn("zero copies scanned", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
