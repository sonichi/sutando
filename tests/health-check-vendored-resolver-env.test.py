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
    def test_the_canonical_resolver_does_not_honour_the_env(self):
        """The canonical file reads `unknown`, and that is the honest verdict.

        It used to read `ignores`, but only because a callee's verdict was
        dropped when the call was held in a local: the wrapper stores
        `sutando_config.resolve_workspace()` in `target` and returns it, and
        that delegate is itself `unknown` (its resolver flows through `get()`).
        So the old `ignores` was the false clean this analysis exists to refuse.
        What must hold is that it never reads `honours`.
        """
        v, why = hc._resolver_env_verdict(_REPO / "src" / "workspace_default.py")
        self.assertNotEqual(v, "honours", why)
        self.assertEqual(v, "unknown", why)

    def test_a_clean_resolver_still_reads_ignores(self):
        """The positive control the canonical file no longer provides: without
        it, a detector that answers `unknown` for everything would pass."""
        d = Path(tempfile.mkdtemp())
        (d / "workspace_default.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n    return Path(os.getenv('HOME'))\n")
        self.assertEqual(hc._resolver_env_verdict(d / "workspace_default.py")[0], "ignores")

    def test_a_delegate_verdict_survives_being_held_in_a_local(self):
        """keweichen's round-14 finding 2: `t = sib.resolve_workspace(); return t`
        dropped the callee's verdict, so a dirty delegate read clean."""
        d = Path(tempfile.mkdtemp())
        (d / "sutando_config.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.environ['SUTANDO_WORKSPACE'])\n")
        (d / "workspace_default.py").write_text(
            "import sutando_config\n"
            "def resolve_workspace():\n"
            "    target = sutando_config.resolve_workspace()\n"
            "    return target\n")
        v, why = hc._resolver_env_verdict(d / "workspace_default.py")
        self.assertEqual(v, "honours", why)

    def test_a_clean_delegate_held_in_a_local_stays_ignores(self):
        """The negative: propagation must not taint every held call."""
        d = Path(tempfile.mkdtemp())
        (d / "sutando_config.py").write_text(
            "from pathlib import Path\n"
            "def resolve_workspace():\n    return Path('/configured')\n")
        (d / "workspace_default.py").write_text(
            "import sutando_config\n"
            "def resolve_workspace():\n"
            "    target = sutando_config.resolve_workspace()\n"
            "    return target\n")
        self.assertEqual(hc._resolver_env_verdict(d / "workspace_default.py")[0], "ignores")

    def test_an_alias_that_is_not_os_is_never_trusted(self):
        """keweichen's round-14 finding 1: `import helper as os` at module scope
        while a nested `import os` supplies the identity to a file-wide scan."""
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\ndef getenv(k):\n    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "import helper as os\nfrom pathlib import Path\n"
            "def unrelated():\n    import os\n"
            "def resolve_workspace():\n    return Path(os.getenv('HOME'))\n")
        self.assertEqual(hc._resolver_env_verdict(d / "workspace_default.py")[0], "unknown")

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


class TheHopIsBudgeted(unittest.TestCase):
    """Round 5: the one-hop limit was documented in prose and unenforced, so
    mutual delegates recursed to RecursionError instead of failing closed."""

    def test_mutual_delegates_fail_closed_instead_of_recursing(self):
        d = Path(tempfile.mkdtemp())
        (d / "workspace_default.py").write_text(
            "import b\ndef resolve_workspace():\n    return b.resolve_workspace()\n")
        (d / "b.py").write_text(
            "import workspace_default\n"
            "def resolve_workspace():\n    return workspace_default.resolve_workspace()\n")
        v, why = hc._resolver_env_verdict(d / "workspace_default.py")
        self.assertEqual(v, "unknown")
        self.assertIn("b.resolve_workspace", why)

    def test_a_self_delegating_file_also_fails_closed(self):
        d = Path(tempfile.mkdtemp())
        (d / "workspace_default.py").write_text(
            "import workspace_default\n"
            "def resolve_workspace():\n    return workspace_default.resolve_workspace()\n")
        self.assertEqual(hc._resolver_env_verdict(d / "workspace_default.py")[0], "unknown")

    def test_one_real_hop_still_resolves(self):
        """Negative control: budgeting the hop must not disable it."""
        d = Path(tempfile.mkdtemp())
        (d / "sutando_config.py").write_text(
            "from pathlib import Path\ndef resolve_workspace():\n    return Path('/c')\n")
        (d / "workspace_default.py").write_text(
            "import sutando_config\n"
            "def resolve_workspace():\n    return sutando_config.resolve_workspace()\n")
        self.assertEqual(hc._resolver_env_verdict(d / "workspace_default.py")[0], "ignores")


class TheKeyAndTheBindingAreDataflowToo(unittest.TestCase):
    """Round 6: reads_env() recognized a literal key only and the taint pass
    modelled Name targets only, so an env read reached the return unseen.
    qingyun-wu's three controls all read 'ignores' at 2ca05484."""

    def _verdict(self, src):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(src)
        return hc._resolver_env_verdict(f)[0]

    def test_getenv_through_a_named_key_is_not_clean(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "KEY = 'SUTANDO_WORKSPACE'\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv(KEY))\n"), "unknown")

    def test_subscript_through_a_named_key_is_not_clean(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "KEY = 'SUTANDO_WORKSPACE'\n"
            "def resolve_workspace():\n"
            "    return Path(os.environ[KEY])\n"), "unknown")

    def test_a_tuple_target_carries_the_taint(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    raw, other = os.getenv('SUTANDO_WORKSPACE'), None\n"
            "    return Path(raw)\n"), "honours")

    def test_a_with_binding_is_modelled_too(self):
        """The two shapes above are instances; an unmodelled binding form is
        the class. `with ... as` bound a name the old loop never visited."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    with open(os.environ['SUTANDO_WORKSPACE']) as raw:\n"
            "        return Path(raw)\n"), "honours")

    def test_a_literal_key_for_another_variable_stays_clean(self):
        """Negative control: only an UNRESOLVED key is unknown. A resolved key
        naming some other variable is analysed, and analysed means clean."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_the_untainted_half_of_a_tuple_stays_clean(self):
        """Negative control: element-wise pairing, not blanket over-tainting —
        otherwise every tuple mentioning the env would read 'honours'."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    a, b = os.getenv('SUTANDO_WORKSPACE'), '/configured'\n"
            "    return Path(b)\n"), "ignores")


class EveryBindingFormIsModelled(unittest.TestCase):
    """One test per binding form _bind_sites() enumerates. An untested branch
    here IS the defect this round fixed: a form the pass does not reach leaves
    its names clean."""

    def _verdict(self, body):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text("import os\nfrom pathlib import Path\n"
                     "def resolve_workspace():\n" + body)
        return hc._resolver_env_verdict(f)[0]

    def test_an_annotated_binding(self):
        self.assertEqual(self._verdict(
            "    raw: str = os.environ['SUTANDO_WORKSPACE']\n"
            "    return Path(raw)\n"), "honours")

    def test_an_augmented_binding(self):
        self.assertEqual(self._verdict(
            "    raw = ''\n"
            "    raw += os.environ['SUTANDO_WORKSPACE']\n"
            "    return Path(raw)\n"), "honours")

    def test_a_walrus_binding(self):
        self.assertEqual(self._verdict(
            "    if (raw := os.environ['SUTANDO_WORKSPACE']):\n"
            "        return Path(raw)\n"
            "    return Path('/configured')\n"), "honours")

    def test_a_loop_binding(self):
        self.assertEqual(self._verdict(
            "    for raw in [os.environ['SUTANDO_WORKSPACE']]:\n"
            "        return Path(raw)\n"
            "    return Path('/configured')\n"), "honours")

    def test_a_starred_target_binds_the_whole_value(self):
        """No element-wise pairing is possible across a star, so every name it
        binds takes the whole value — over-tainting, which is the safe side."""
        self.assertEqual(self._verdict(
            "    first, *rest = os.environ['SUTANDO_WORKSPACE'], '/a'\n"
            "    return Path(first)\n"), "honours")

    def test_a_subscript_target_taints_its_container(self):
        self.assertEqual(self._verdict(
            "    holder = {}\n"
            "    holder['w'] = os.environ['SUTANDO_WORKSPACE']\n"
            "    return Path(holder['w'])\n"), "honours")

    def test_an_env_read_with_no_key_is_unknown(self):
        self.assertEqual(self._verdict(
            "    return Path(os.environ.get())\n"), "unknown")

    def test_a_subscript_on_something_else_is_not_an_env_read(self):
        """Negative control: the key guard keys on the BASE, so an ordinary
        dict lookup must not turn a clean resolver unknown."""
        self.assertEqual(self._verdict(
            "    d = {'w': '/configured'}\n"
            "    return Path(d['w'])\n"), "ignores")


class BindingsOutsideTheBody(unittest.TestCase):
    """Round 7: the taint pass walked the FUNCTION BODY only, and the alias
    collector kept a private rule that never learned the new binding forms.
    qingyun-wu's three controls all read 'ignores' at 757e2c90; the fourth is
    the same class, found by asking what else binds a name before the body."""

    def _verdict(self, src):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(src)
        return hc._resolver_env_verdict(f)[0]

    def test_a_module_scope_binding_is_a_taint_source(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "RAW = os.getenv('SUTANDO_WORKSPACE')\n"
            "def resolve_workspace():\n"
            "    return Path(RAW)\n"), "honours")

    def test_a_default_argument_is_a_binding(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace(raw=os.environ.get('SUTANDO_WORKSPACE')):\n"
            "    return Path(raw)\n"), "honours")

    def test_an_annotated_module_alias_is_still_an_alias(self):
        """The alias collector now shares _bind_sites with the taint pass, so a
        form one side learns cannot be missing from the other."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "env: object = os.environ\n"
            "def resolve_workspace():\n"
            "    return Path(env['SUTANDO_WORKSPACE'])\n"), "honours")

    def test_a_caller_supplied_parameter_is_unknown(self):
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "def resolve_workspace(base):\n"
            "    return Path(base)\n"), "unknown")

    def test_a_starargs_parameter_is_unknown_too(self):
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "def resolve_workspace(*parts, **kw):\n"
            "    return Path(parts[0])\n"), "unknown")

    def test_a_keyword_only_default_is_analysed(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace(*, raw=os.getenv('SUTANDO_WORKSPACE')):\n"
            "    return Path(raw)\n"), "honours")

    def test_a_name_bound_nowhere_is_unknown(self):
        """The backstop: 'no binding this analysis resolved' is unknown, not
        clean. Without it every future scope gap defaults to a clean bill."""
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(NOWHERE)\n"), "unknown")

    def test_a_module_constant_stays_clean(self):
        """Negative control: module scope became a taint SOURCE, not a taint."""
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "DEFAULT = '/configured'\n"
            "def resolve_workspace():\n"
            "    return Path(DEFAULT)\n"), "ignores")

    def test_a_clean_default_argument_stays_clean(self):
        """Negative control: a default is analysed, and analysed means clean."""
        self.assertEqual(self._verdict(
            "from pathlib import Path\n"
            "def resolve_workspace(raw='/configured'):\n"
            "    return Path(raw)\n"), "ignores")

    def test_the_probe_reports_it_too_not_only_the_verdict(self):
        """qingyun ran both levels: a verdict that never reaches the probe's
        detail is a fix nobody sees."""
        ws = Path(tempfile.mkdtemp())
        _vendor(ws, "import os\nfrom pathlib import Path\n"
                    "RAW = os.getenv('SUTANDO_WORKSPACE')\n"
                    "def resolve_workspace():\n"
                    "    return Path(RAW)\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("still honour", r["detail"])


class OsPathIsNotAPureNamespace(unittest.TestCase):
    """Round 8: unresolved_call() exempted `os.path.*` wholesale, and that
    namespace contains expandvars(), which reads the environment. qingyun-wu's
    control read 'ignores' at 1cca14b2, verdict AND probe detail."""

    def _verdict(self, src):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(src)
        return hc._resolver_env_verdict(f)[0]

    def test_expandvars_naming_the_removed_variable(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.expandvars('$SUTANDO_WORKSPACE'))\n"), "honours")

    def test_the_braced_spelling_counts_too(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.expandvars('${SUTANDO_WORKSPACE}/w'))\n"), "honours")

    def test_a_direct_import_of_expandvars_counts_too(self):
        self.assertEqual(self._verdict(
            "from os.path import expandvars\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(expandvars('$SUTANDO_WORKSPACE'))\n"), "honours")

    def test_an_unresolvable_expandvars_argument_is_unknown(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "T = '$SUTANDO_WORKSPACE'\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.expandvars(T))\n"), "unknown")

    def test_an_unlisted_os_path_member_is_unknown(self):
        """The class, not the case: a member-by-member allowlist means a helper
        nobody has classified fails closed instead of inheriting the namespace."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.some_future_helper('x'))\n"), "unknown")

    def test_expanduser_stays_clean(self):
        """Negative control: expanduser reads $HOME, which structurally cannot
        yield the retired variable, so it stays on the pure list."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.expanduser('~/w'))\n"), "ignores")

    def test_expandvars_naming_another_variable_stays_clean(self):
        """Negative control: the literal PROVES which variable it expands, so
        this is analysed-and-clean, not unknown."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.expandvars('$HOME/w'))\n"), "ignores")

    def test_a_listed_os_path_helper_stays_clean(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.path.join(os.path.expanduser('~'), 'w'))\n"), "ignores")


class ProvenanceNotSuffix(unittest.TestCase):
    """Round 9: the env APIs were matched by SUFFIX, so `helper.getenv(...)` and
    `helper.environ[...]` — arbitrary imported code — were treated as understood
    OS reads and exempted. qingyun-wu's controls read 'ignores' at cf974224."""

    def _verdict(self, src):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(src)
        return hc._resolver_env_verdict(f)[0]

    def test_a_lookalike_dotted_getenv_is_unknown(self):
        self.assertEqual(self._verdict(
            "import helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(helper.getenv('HOME'))\n"), "unknown")

    def test_a_lookalike_environ_subscript_is_unknown(self):
        """No Call node exists here, so unresolved_call cannot see it; the
        subscript itself has to be judged."""
        self.assertEqual(self._verdict(
            "import helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(helper.environ['HOME'])\n"), "unknown")

    def test_a_bare_getenv_from_a_foreign_module_is_unknown(self):
        """`from helper import getenv` and `from os import getenv` produce an
        identical call site; only the import statement tells them apart."""
        self.assertEqual(self._verdict(
            "from helper import getenv\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(getenv('HOME'))\n"), "unknown")

    def test_a_lookalike_expandvars_is_unknown(self):
        self.assertEqual(self._verdict(
            "import helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(helper.path.expandvars('$HOME'))\n"), "unknown")

    def test_real_os_getenv_stays_analysed(self):
        """Negative control: provenance must not make the genuine article
        unanalysable, or the probe reports unknown for every resolver."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_an_os_module_alias_is_followed(self):
        self.assertEqual(self._verdict(
            "import os as _o\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(_o.getenv('HOME'))\n"), "ignores")

    def test_from_os_import_getenv_is_followed(self):
        self.assertEqual(self._verdict(
            "from os import getenv\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(getenv('HOME'))\n"), "ignores")

    def test_and_it_still_catches_the_removed_variable_through_them(self):
        """Both directions on the same import shape: provenance decides whether
        the read is analysed, the KEY decides the verdict."""
        self.assertEqual(self._verdict(
            "from os import getenv\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(getenv('SUTANDO_WORKSPACE'))\n"), "honours")


class ProvenanceIsBindingAware(unittest.TestCase):
    """Round 10: provenance was collected into unordered sets and never
    invalidated, so a REBOUND trusted name kept its proof. qingyun-wu's two
    controls read 'ignores' at 5ffd704b."""

    def _verdict(self, src):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(src)
        return hc._resolver_env_verdict(f)[0]

    def test_reassigning_the_os_name_revokes_its_proof(self):
        self.assertEqual(self._verdict(
            "import os, helper\nfrom pathlib import Path\n"
            "os = helper\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "unknown")

    def test_a_later_colliding_import_revokes_it_too(self):
        self.assertEqual(self._verdict(
            "from os import getenv\nfrom helper import getenv\n"
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(getenv('HOME'))\n"), "unknown")

    def test_a_shadowed_bare_environ_subscript_too(self):
        """A bare name has no dotted root, and a Subscript has no Call node, so
        this needed the subscript check widened rather than provenance alone."""
        self.assertEqual(self._verdict(
            "from os import environ\nfrom helper import environ\n"
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(environ['HOME'])\n"), "unknown")

    def test_an_unshadowed_import_keeps_its_proof(self):
        """Negative control: revocation must key on REBINDING, not on the name
        appearing twice in the file for any reason."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_an_os_environ_alias_still_works(self):
        """Negative control: `env = os.environ` binds env, not os — the alias
        must not be mistaken for a rebinding of the trusted name."""
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "env = os.environ\n"
            "def resolve_workspace():\n"
            "    return Path(env['HOME'])\n"), "ignores")

    def test_and_the_removed_variable_is_still_caught(self):
        self.assertEqual(self._verdict(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('SUTANDO_WORKSPACE'))\n"), "honours")


class ProvenanceCertifiesAProvenShape(unittest.TestCase):
    """`ignores` is asserted for a shape this analysis can prove, and refused for
    everything else. The prior rule enumerated what to EXCLUDE, which is a
    blacklist over an open set: `import *` binds names it cannot list, and a
    file-wide walk counted a nested import as module-scope proof."""

    def _v(self, body, helper=None):
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(helper or (
            "import os\ndef getenv(key):\n"
            "    return os.environ['SUTANDO_WORKSPACE']\n"))
        (d / "workspace_default.py").write_text(body)
        return hc._resolver_env_verdict(d / "workspace_default.py")[0]

    def test_a_star_import_is_never_proven(self):
        """qingyun-wu's round-13 control: the star binds `os`, the nested import
        is what the file-wide walk counted."""
        self.assertEqual(self._v(
            "from helper import *\nfrom pathlib import Path\n"
            "def unrelated():\n    import os\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "unknown")

    def test_a_nested_import_does_not_prove_a_module_scope_name(self):
        """The scope half, on its own — no star import in sight."""
        self.assertEqual(self._v(
            "from pathlib import Path\n"
            "def unrelated():\n    import os\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "unknown")

    def test_a_module_scope_import_is_still_proven(self):
        """The negative that keeps this a rule and not a refusal-of-everything."""
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_a_real_read_is_still_detected(self):
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.environ['SUTANDO_WORKSPACE'])\n"), "honours")

    def test_the_helper_reports_star_as_unanalysable(self):
        self.assertIsNone(hc._import_provenance(
            __import__("ast").parse("from helper import *\n")))


class AGlobalRebindingIsABinding(unittest.TestCase):
    """qingyun-wu's round-15 control: `global os; import helper as os` in a
    function called at module init REPLACES the module binding, and CPython
    records it as imported+global in that scope — never `assigned`. So a
    revocation reading only is_assigned()/is_parameter() misses it."""

    def _v(self, body):
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\ndef getenv(key):\n"
            "    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(body)
        return hc._resolver_env_verdict(d / "workspace_default.py")[0]

    def test_a_nested_global_import_revokes_the_module_binding(self):
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def poison():\n    global os\n    import helper as os\n"
            "poison()\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "unknown")

    def test_a_global_READ_does_not_revoke(self):
        """The discriminating negative: revoking on the mere presence of
        `global` would make any module that reads a global unanalysable."""
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def helper_fn():\n    global os\n    return os\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_symtable_reports_the_shape_this_relies_on(self):
        """Pin the CPython fact the rule rests on, so a semantics change is
        caught here rather than as a silent false clean."""
        import symtable
        st = symtable.symtable(
            "import os\ndef poison():\n    global os\n    import helper as os\n",
            "m.py", "exec")
        # By NAME, not index: 3.12+ emits an __annotate__ child scope first.
        child = next(c for c in st.get_children() if c.get_name() == "poison")
        sym = next(s for s in child.get_symbols() if s.get_name() == "os")
        self.assertTrue(sym.is_declared_global())
        self.assertTrue(sym.is_imported())
        self.assertFalse(sym.is_assigned())


class BindingRevocationIsNotEnumerated(unittest.TestCase):
    """Six rounds each shipped a binding form the next round found, so the
    enumeration itself was the defect. CPython's symbol table is the language's
    own answer and cannot omit a construct; these cases are evidence of that
    property, not a list to extend when a seventh form appears."""

    def _v(self, body):
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\ndef getenv(key):\n"
            "    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(body)
        return hc._resolver_env_verdict(d / "workspace_default.py")[0]

    def test_a_comprehension_target_is_not_proven(self):
        """qingyun-wu's round-12 control. An earlier round DELETED the
        comprehension branch as 'unreachable'; the value escapes via [0]."""
        self.assertEqual(self._v(
            "import os, helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path([os.getenv('HOME') for os in [helper]][0])\n"), "unknown")

    def test_a_with_as_rebinding_is_not_proven(self):
        """Never named by any round — covered because symtable covers it."""
        self.assertEqual(self._v(
            "import os, helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    with open('/x') as os:\n        pass\n"
            "    return Path(os.getenv('HOME'))\n"), "unknown")

    def test_a_walrus_rebinding_is_not_proven(self):
        """Likewise never named."""
        self.assertEqual(self._v(
            "import os, helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path((os := helper).getenv('HOME'))\n"), "unknown")

    def test_colliding_imports_still_revoke(self):
        """symtable calls both `imported` and neither `assigned`, so imports stay
        AST-checked — a CLOSED set of two node types, unlike binding forms."""
        self.assertEqual(self._v(
            "from os import getenv\nfrom helper import getenv\n"
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(getenv('HOME'))\n"), "unknown")

    def test_a_source_symtable_refuses_is_unknown_not_clean(self):
        """The refusal must not degrade to an empty set, which reads as clean."""
        self.assertIsNone(hc._rebound_names("def (:\n"))

    def test_an_unshadowed_import_is_still_proven(self):
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_an_alias_binds_the_alias_not_the_module(self):
        """`env = os.environ` binds env; revoking os here would make every
        aliasing resolver unanalysable."""
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "env = os.environ\n"
            "def resolve_workspace():\n"
            "    return Path(env['HOME'])\n"), "ignores")

    def test_a_real_read_is_still_detected(self):
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.environ['SUTANDO_WORKSPACE'])\n"), "honours")


class EveryBindingSourceRevokes(unittest.TestCase):
    """Round 10 revoked provenance for imports and _bind_sites; parameters were a
    SECOND enumerator it never consulted, so a parameter default could shadow a
    trusted name and keep the module-level proof. Two enumerators of the same
    question is the defect — one left out is one false clean per form."""

    def _v(self, body):
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\ndef getenv(key):\n"
            "    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(body)
        return hc._resolver_env_verdict(d / "workspace_default.py")[0]

    def test_a_parameter_default_shadowing_os_is_not_proven(self):
        """qingyun-wu's round-11 control, verbatim. `ignores` at the parent."""
        self.assertEqual(self._v(
            "import os, helper\nfrom pathlib import Path\n"
            "def resolve_workspace(os=helper):\n"
            "    return Path(os.getenv('HOME'))\n"), "unknown")

    def test_an_except_as_rebinding_is_not_proven(self):
        """Same class, a form neither round named. `ignores` at the parent."""
        self.assertEqual(self._v(
            "import os, helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    try:\n        pass\n"
            "    except Exception as os:\n        pass\n"
            "    return Path(os.getenv('HOME'))\n"), "unknown")

    def test_a_parameter_that_shadows_nothing_stays_clean(self):
        """The negative that makes it a rule and not 'revoke on any parameter'.

        The key is a LITERAL, so the non-literal-key rule cannot fire and this
        isolates the parameter axis by itself."""
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace(base='/tmp'):\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_an_unshadowed_import_is_still_proven(self):
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.getenv('HOME'))\n"), "ignores")

    def test_a_real_read_is_still_detected_through_a_parameter(self):
        """Over-revoking would hide a genuine honours; it must not."""
        self.assertEqual(self._v(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace(base='/tmp'):\n"
            "    return Path(os.environ['SUTANDO_WORKSPACE'])\n"), "honours")

    def test_the_probe_reports_the_shadowed_copy_rather_than_ok(self):
        """Integrated: the classifier verdict must reach the probe's status."""
        ws = Path(tempfile.mkdtemp())
        d = ws / "skill-repos" / "shadowed" / "scripts"
        d.mkdir(parents=True)
        (d / "helper.py").write_text(
            "import os\ndef getenv(key):\n"
            "    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "import os, helper\nfrom pathlib import Path\n"
            "def resolve_workspace(os=helper):\n"
            "    return Path(os.getenv('HOME'))\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn")
        self.assertNotIn("none honour", r["detail"])


class Coverage(unittest.TestCase):
    def test_zero_copies_reports_coverage_rather_than_going_silent(self):
        """Sutando-Mini on #3892: a probe that finds nothing must SAY so."""
        r = hc.check_vendored_resolver_env(workspace_dir=Path(tempfile.mkdtemp()))
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "ok")
        self.assertIn("zero copies scanned", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
