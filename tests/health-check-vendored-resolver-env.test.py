#!/usr/bin/env python3
"""`check_vendored_resolver_env` must be non-executing and fail honest.

Non-executing: a subprocess is failure isolation, not a security boundary, so a
checked-out copy must never be imported. Fail-honest: an unmeasurable copy reads
`unknown`, never as one of the clean ones.
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
        """Detection must not run what it inspects."""
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
        """`t = sib.resolve_workspace(); return t` must carry the callee's
        verdict: a verdict taken only on the return path drops this shape."""
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
        """`import helper as os` at module scope, with a nested `import os`
        supplying the identity a file-wide scan would count instead."""
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
    """A call whose result is unaccounted for is `unknown`. Bare and dotted
    callees alike: `mystery()` and `config.workspace()` each return a value."""

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
    """The one-hop delegate limit is enforced, not documented: mutual
    delegates must fail closed rather than recurse."""

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
    """A non-literal key and a non-Name binding target are both dataflow. A
    reader that models only literals and only Name targets misses the read."""

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
    """Module scope binds names every function below reads, so a body-scoped
    walk cannot see them; one shared binding enumerator, not a private rule."""

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
        """A verdict that never reaches the probe's detail is a fix nobody sees."""
        ws = Path(tempfile.mkdtemp())
        _vendor(ws, "import os\nfrom pathlib import Path\n"
                    "RAW = os.getenv('SUTANDO_WORKSPACE')\n"
                    "def resolve_workspace():\n"
                    "    return Path(RAW)\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("still honour", r["detail"])


class OsPathIsNotAPureNamespace(unittest.TestCase):
    """`os.path` is not a pure namespace: it contains expandvars(), which reads
    the environment, so a wholesale exemption of the namespace is a false clean."""

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
    """Env APIs are matched by PROVEN origin, not by suffix: `helper.getenv(...)`
    and `helper.environ[...]` are arbitrary imported code, not understood reads."""

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
    """Proof of provenance is revoked by any later binding of the same name;
    a trusted name that is rebound has stopped being the thing that was proven."""

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
        """The star binds `os` unenumerably; a file-wide walk counts the nested
        import instead and calls that provenance."""
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
    """`global os; import helper as os` REPLACES the module binding, and CPython
    records it as imported+global in the child scope, never as assigned."""

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
        """A comprehension target binds the name for the comprehension, and the
        value escapes via [0] — the branch is reachable."""
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
    """Every source that binds a name revokes provenance. Two enumerators of
    the same question is the defect: one left out is one false clean per form."""

    def _v(self, body):
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\ndef getenv(key):\n"
            "    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(body)
        return hc._resolver_env_verdict(d / "workspace_default.py")[0]

    def test_a_parameter_default_shadowing_os_is_not_proven(self):
        """A parameter default can shadow a trusted module-level name."""
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


class ScopeAndOriginAreProven(unittest.TestCase):
    """A file-wide walk picks a function by NAME, and an import binds a name
    without saying what it holds. Neither is the property being certified."""

    def _v(self, body):
        d = Path(tempfile.mkdtemp())
        f = d / "workspace_default.py"
        f.write_text(body)
        return hc._resolver_env_verdict(f)[0]

    _DIRTY = ("import os\nfrom pathlib import Path\n"
              "def resolve_workspace():\n"
              "    return Path(os.environ['SUTANDO_WORKSPACE'])\n")
    _NESTED = ("def unrelated():\n"
               "    def resolve_workspace():\n"
               "        return Path('/clean')\n"
               "    return resolve_workspace\n")

    def test_a_nested_same_name_def_after_it_does_not_shadow_the_real_one(self):
        self.assertEqual(self._v(self._DIRTY + self._NESTED), "honours")

    def test_nor_does_one_declared_before_it(self):
        """Ordering pair: a dict built by walking cannot depend on source order."""
        self.assertEqual(
            self._v("import os\nfrom pathlib import Path\n"
                    + self._NESTED + self._DIRTY.split("\n", 2)[2]), "honours")

    def test_a_genuinely_clean_module_level_resolver_still_reads_ignores(self):
        """Positive control: scoping the lookup must not condemn every file."""
        self.assertEqual(
            self._v("from pathlib import Path\n"
                    "def resolve_workspace():\n"
                    "    return Path('/fixed')\n" + self._NESTED), "ignores")

    def test_a_nested_def_in_the_DELEGATE_does_not_shadow_it_either(self):
        """The sibling lookup selects by name across the whole file too."""
        d = Path(tempfile.mkdtemp())
        (d / "sutando_config.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.environ['SUTANDO_WORKSPACE'])\n"
            "def unrelated():\n"
            "    def resolve_workspace():\n"
            "        return Path('/clean')\n"
            "    return resolve_workspace\n")
        (d / "workspace_default.py").write_text(
            "import sutando_config\n"
            "def resolve_workspace():\n"
            "    return sutando_config.resolve_workspace()\n")
        self.assertEqual(
            hc._resolver_env_verdict(d / "workspace_default.py")[0], "honours")

    def test_an_imported_value_is_unresolved_dataflow_not_a_clean_name(self):
        """`WORKSPACE` holds whatever the other module put there — here, the env."""
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\nWORKSPACE = os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "from pathlib import Path\nfrom helper import WORKSPACE\n"
            "def resolve_workspace():\n"
            "    return Path(WORKSPACE)\n")
        v, why = hc._resolver_env_verdict(d / "workspace_default.py")
        self.assertEqual(v, "unknown", why)
        self.assertIn("WORKSPACE", why)

    def test_a_dotted_read_off_an_imported_module_is_unresolved_too(self):
        self.assertEqual(
            self._v("import helper\nfrom pathlib import Path\n"
                    "def resolve_workspace():\n"
                    "    return Path(helper.WORKSPACE)\n"), "unknown")

    def test_but_a_followed_module_call_is_not_an_opaque_value(self):
        """Negative control: the delegate hop must survive the origin rule."""
        d = Path(tempfile.mkdtemp())
        (d / "sutando_config.py").write_text(
            "from pathlib import Path\n"
            "def resolve_workspace():\n    return Path('/c')\n")
        (d / "workspace_default.py").write_text(
            "import sutando_config\n"
            "def resolve_workspace():\n"
            "    return sutando_config.resolve_workspace()\n")
        self.assertEqual(
            hc._resolver_env_verdict(d / "workspace_default.py")[0], "ignores")

    def test_one_local_hop_does_not_wash_out_the_import(self):
        """The backstop must run in the BINDING fixpoint, not only on the return:
        an assignment moves the imported name out of the return expression."""
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\nWORKSPACE = os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "from pathlib import Path\nfrom helper import WORKSPACE\n"
            "def resolve_workspace():\n"
            "    resolved = WORKSPACE\n"
            "    return Path(resolved)\n")
        v, why = hc._resolver_env_verdict(d / "workspace_default.py")
        self.assertEqual(v, "unknown", why)
        self.assertIn("WORKSPACE", why)

    def test_two_hops_do_not_either(self):
        """One hop is not a special case; the fixpoint carries it any distance."""
        self.assertEqual(
            self._v("import helper\nfrom pathlib import Path\n"
                    "def resolve_workspace():\n"
                    "    a = helper.WORKSPACE\n    b = a\n"
                    "    return Path(b)\n"), "unknown")

    def test_a_module_constant_defined_HERE_still_reads_ignores(self):
        """Negative control: the rule is about IMPORTED values, not module scope.
        A constant this file binds is dataflow the analysis fully resolved."""
        self.assertEqual(
            self._v("from pathlib import Path\nWS = '/fixed'\n"
                    "def resolve_workspace():\n"
                    "    r = WS\n    return Path(r)\n"), "ignores")

    def test_a_followed_call_does_not_exempt_a_VALUE_read_of_the_same_name(self):
        """One identifier, two roles: exempting the NAME exempts the value read
        as well as the followed call."""
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "WORKSPACE = os.environ['SUTANDO_WORKSPACE']\n"
            "def resolve_workspace():\n    return Path('/x')\n")
        (d / "workspace_default.py").write_text(
            "import helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(helper.WORKSPACE) if helper.resolve_workspace() "
            "else Path('/fixed')\n")
        v, why = hc._resolver_env_verdict(d / "workspace_default.py")
        self.assertEqual(v, "unknown", why)
        self.assertIn("helper", why)

    def test_a_shadowed_pure_callee_is_not_trusted_by_spelling(self):
        """`Path` is trusted for what it IS, not what it is called: an import can
        bind the spelling to arbitrary code."""
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\ndef Path(_):\n    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "from helper import Path\ndef resolve_workspace():\n"
            "    return Path('/fixed')\n")
        v, why = hc._resolver_env_verdict(d / "workspace_default.py")
        self.assertEqual(v, "unknown", why)

    def test_the_real_pathlib_Path_is_still_trusted(self):
        """Negative control: distrusting a shadowed spelling must not distrust the
        canonical one, or every clean resolver reads unknown."""
        d = Path(tempfile.mkdtemp())
        (d / "workspace_default.py").write_text(
            "from pathlib import Path\ndef resolve_workspace():\n"
            "    return Path('/fixed')\n")
        self.assertEqual(
            hc._resolver_env_verdict(d / "workspace_default.py")[0], "ignores")

    def test_a_locally_reassigned_pure_callee_is_not_trusted_either(self):
        """An assignment rebinds the spelling as surely as an import does."""
        d = Path(tempfile.mkdtemp())
        (d / "helper.py").write_text(
            "import os\ndef f(_):\n    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "import helper\nPath = helper.f\ndef resolve_workspace():\n"
            "    return Path('/fixed')\n")
        v, why = hc._resolver_env_verdict(d / "workspace_default.py")
        self.assertEqual(v, "unknown", why)

    def test_the_probe_reports_the_shadowed_callee_too(self):
        """Integrated: the classifier verdict must reach the probe's status."""
        ws = Path(tempfile.mkdtemp())
        d = ws / "skill-repos" / "shadowcall" / "scripts"
        d.mkdir(parents=True)
        (d / "helper.py").write_text(
            "import os\ndef Path(_):\n    return os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "from helper import Path\ndef resolve_workspace():\n"
            "    return Path('/fixed')\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn")
        self.assertNotIn("none honour", r["detail"])

    def test_the_probe_reports_the_mixed_shape_too(self):
        """Integrated: the classifier verdict must reach the probe's status."""
        ws = Path(tempfile.mkdtemp())
        d = ws / "skill-repos" / "mixed" / "scripts"
        d.mkdir(parents=True)
        (d / "helper.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "WORKSPACE = os.environ['SUTANDO_WORKSPACE']\n"
            "def resolve_workspace():\n    return Path('/x')\n")
        (d / "workspace_default.py").write_text(
            "import helper\nfrom pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(helper.WORKSPACE) if helper.resolve_workspace() "
            "else Path('/fixed')\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn")
        self.assertNotIn("none honour", r["detail"])

    def test_the_probe_reports_the_one_hop_import_too(self):
        """Integrated: the fixpoint verdict must reach the probe's status."""
        ws = Path(tempfile.mkdtemp())
        d = ws / "skill-repos" / "hopped" / "scripts"
        d.mkdir(parents=True)
        (d / "helper.py").write_text(
            "import os\nWORKSPACE = os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "from pathlib import Path\nfrom helper import WORKSPACE\n"
            "def resolve_workspace():\n"
            "    resolved = WORKSPACE\n"
            "    return Path(resolved)\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn")
        self.assertNotIn("none honour", r["detail"])

    def test_the_probe_reports_the_opaque_import_rather_than_ok(self):
        """Integrated: the classifier verdict must reach the probe's status."""
        ws = Path(tempfile.mkdtemp())
        d = ws / "skill-repos" / "opaque" / "scripts"
        d.mkdir(parents=True)
        (d / "helper.py").write_text(
            "import os\nWORKSPACE = os.environ['SUTANDO_WORKSPACE']\n")
        (d / "workspace_default.py").write_text(
            "from pathlib import Path\nfrom helper import WORKSPACE\n"
            "def resolve_workspace():\n"
            "    return Path(WORKSPACE)\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "warn")
        self.assertNotIn("none honour", r["detail"])


class Coverage(unittest.TestCase):
    def test_zero_copies_reports_coverage_rather_than_going_silent(self):
        """A probe that finds nothing must SAY so."""
        r = hc.check_vendored_resolver_env(workspace_dir=Path(tempfile.mkdtemp()))
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "ok")
        self.assertIn("zero copies scanned", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
