#!/usr/bin/env python3
"""Every test-looking Python file must actually be executed by CI.

CI discovers Python tests with `find tests -name '*.test.py'`. Anything outside
that root or suffix runs only if ci.yml names it explicitly. A fixed list is
maintenance the next author will not know they owe: a test added under
packages/*/tests/ is silently never run, and reads as coverage anyway.

This guard makes that gap self-detecting instead of silent. It failed to exist
when five suites -- including the transport gate and the src/-vs-package drift
guard -- had never run in CI.
"""
import glob
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from active_code import active_lines, active_text, python_args, program_python_args  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
COVGATE = REPO / "scripts" / "coverage-gate.sh"
DISCOVER = REPO / "scripts" / "discover-python-tests.sh"


def discovered_by_find():
    """What CI actually discovers — by RUNNING the one discovery owner.

    Earlier revisions parsed `find` and its root assignments out of ci.yml and
    coverage-gate.sh. Four review rounds found four ways that lied (unioned
    roots hiding a one-sided loss, comments, reset-vs-append order, assignments
    placed after the consumer). Reading a declaration is not observing the
    behaviour; executing the shared helper is."""
    out = subprocess.run(["bash", str(DISCOVER)], cwd=str(REPO),
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"{DISCOVER.name} failed rc={out.returncode}: {out.stderr.strip()}")
    # splitlines, not split: a path containing a space becomes two fake paths
    # under whitespace splitting, and both then read as undiscovered.
    return {p for p in out.stdout.splitlines() if p and "node_modules" not in p}


def _uncommented(text: str) -> str:
    """Lines with comments removed. A commented-out invocation is not a caller."""
    return active_text(text)


_YAML_DQUOTE_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v",
    "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/",
    "\\": "\\", "N": "", "_": " ", "L": " ", "P": " ",
}


def _yaml_dquote_unescape(body: str) -> str:
    """Decode YAML double-quoted scalar escapes (spec 5.7) -- `\\n` is a
    REAL newline, not two characters, so a folded-looking one-liner can
    actually be a multi-line shell script (round 29, keweichen: `"echo a
    \\npython3 x.py"` decodes to two lines, and Bash runs the second)."""
    out, i, n = [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch); i += 1; continue
        esc = body[i + 1]
        if esc in _YAML_DQUOTE_ESCAPES:
            out.append(_YAML_DQUOTE_ESCAPES[esc]); i += 2; continue
        for prefix, width in (("x", 4), ("u", 6), ("U", 10)):
            if esc == prefix and i + 1 + width <= n:
                try:
                    out.append(chr(int(body[i + 2:i + width], 16)))
                    i += width
                    break
                except ValueError:
                    pass
        else:
            out.append(ch); out.append(esc); i += 2
    return "".join(out)


def _dquote_join(parts: list[str]) -> str:
    """Join a double-quoted scalar's physical-line parts per YAML spec 5.7:
    a line ending in an ODD run of `\\` is an escaped break -- no fold space,
    and that one backslash is consumed by the break, not passed to
    `_yaml_dquote_unescape` (round 32, kewei-red-ag2space: unconditional
    `" ".join` let `\\<space>` decode the join's own inserted space back into
    a real one, silently matching real YAML by accident on this input and
    diverging on any other). A BLANK physical part folds to a newline, not a
    space -- one `\\n` per consecutive blank (round 33, kewei-red-ag2space:
    a blank line between two commands is real YAML line-break semantics;
    joining it with a space instead glued two Bash statements into one,
    crediting a script as the first command's argument when it never
    receives it, and orphaning it in the reverse direction)."""
    out, blanks = None, 0
    for part in parts:
        if part == "":
            blanks += 1
            continue
        if out is None:
            out = "\n" * blanks
        elif blanks:
            out += "\n" * blanks
        else:
            trailing = len(out) - len(out.rstrip("\\"))
            if trailing % 2 == 1:
                out = out[:-1]
            else:
                out += " "
        blanks = 0
        out += part
    return (out or "") + "\n" * blanks


def _quote_close_split(value: str, q: str) -> "tuple[bool, str]":
    """(closed, text before the matching close), honouring the quote
    style's own escape — `''` inside single quotes is a literal quote,
    `\\X` inside double quotes escapes X (a literal `"` included)."""
    i = 0
    while i < len(value):
        ch = value[i]
        if q == "'" and ch == "'":
            if i + 1 < len(value) and value[i + 1] == "'":
                i += 2
                continue
            return True, value[:i]
        if q == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                return True, value[:i]
        i += 1
    return False, value


def _yaml_scalar(value: str) -> str:
    """A wholly YAML-quoted `run:` value, unwrapped.

    The quotes are YAML's, not the shell's; leaving them makes the shell-quote
    blanking swallow a real invocation. A DOUBLE-quoted scalar's backslash
    escapes are decoded too -- see `_yaml_dquote_unescape`. Single-quoted
    YAML scalars have no backslash-escape mechanism at all (unchanged)."""
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return _yaml_dquote_unescape(v[1:-1])
    if len(v) >= 2 and v[0] == v[-1] == "'" and "'" not in v[1:-1]:
        return v[1:-1]
    return value


def _fold_scalar(lines: list[str]) -> list[str]:
    """YAML `>` folding (spec 8.1.3), approximated for a flat `run:` body:
    adjacent plain lines join with a single space, the way Bash would see
    one command instead of two; a blank or still-indented line keeps its
    own line break (round 28, qingyun-wu: `>-` with `echo inert` then a
    python invocation folds into ONE `echo` command, naming the invocation
    that never runs; the reverse split -- `python3` alone, then its path
    on the next line -- folds into a real invocation the line-per-line
    model named nothing)."""
    out, para = [], []

    def _emit():
        if para:
            out.append(" ".join(para))
            para.clear()

    for ln in lines:
        if not ln.strip() or ln[:1] in (" ", "\t"):
            _emit()
            out.append(ln)
            continue
        para.append(ln.strip())
    _emit()
    return out


def _run_bodies(text: str) -> list[str]:
    """Lines inside a workflow `run:` value — the only place a command executes.

    A path under `name:` or `if:` is data; scanning the whole file counts it.
    Dedented per YAML block-scalar rules, per BLOCK (round 24, keweichen):
    real CI strips each block's own common indentation before Bash ever
    sees the script, so a line-exact check (e.g. a heredoc terminator)
    must see the SAME text CI would run, not the raw source indentation.
    An EXPLICIT indentation indicator (`|2`, `>3`, ...) overrides the
    auto-detected common indent entirely (round 25, keweichen): it strips
    exactly `key_column + N` from every line, deliberately preserving any
    extra source indentation as literal content -- `min(indents)` cannot
    express that, since it always strips the block's OWN minimum. Reset
    alongside `indent`, not just at a new `run:` (round 27, keweichen): a
    stale indicator survived a dedent-ended block into the final
    unconditional `_flush()`, crashing `None + indicator` on any file
    whose last `run:` used one and was followed by an ordinary sibling key.
    The scalar TYPE (`|` literal vs `>` folded) was captured and discarded
    (round 28, qingyun-wu): every block scalar was dedented line-per-line
    regardless of indicator, so a folded `run: >-` was read as literal
    multi-line shell -- see `_fold_scalar`. Reset alongside the rest.
    A quoted FLOW scalar left open across a line break (round 30,
    keweichen) is folded the same way -- adjacent lines join with a
    single space -- rather than read line-per-line, which named neither
    physical line as the command Bash actually runs."""
    out, indent, indicator, scalar, block = [], None, None, None, []
    quote_char, quote_parts = None, []

    def _flush():
        cut = indent + indicator if indicator is not None else None
        if cut is None:
            indents = [len(ln) - len(ln.lstrip()) for ln in block if ln.strip()]
            cut = min(indents) if indents else 0
        lines = [ln[cut:] for ln in block]
        out.extend(_fold_scalar(lines) if scalar == ">" else lines)
        block.clear()

    for ln in text.splitlines():
        if quote_char is not None:
            closed, before = _quote_close_split(ln.strip(), quote_char)
            quote_parts.append(before)
            if closed:
                joined = (_dquote_join(quote_parts) if quote_char == '"'
                          else " ".join(quote_parts))
                out.append(_yaml_scalar(quote_char + joined + quote_char))
                quote_char, quote_parts = None, []
            continue
        stripped = ln.strip()
        m = re.match(r"-?\s*run:\s*([|>])?([+-]?)(\d*)([+-]?)\s*(.*)$", stripped)
        if m and re.search(r"(^|\s)run:", stripped):
            _flush()
            # The KEY's column, not the line's: a `- ` list marker sits left of
            # it, so a sibling key would otherwise read as a continuation line.
            indent = ln.index("run:")
            scalar = m.group(1)
            digits = m.group(3)
            indicator = int(digits) if digits else None
            val = m.group(5)
            if val and val[0] in "\"'":
                closed, before = _quote_close_split(val[1:], val[0])
                if closed:
                    out.append(_yaml_scalar(val[0] + before + val[0]))
                else:
                    quote_char, quote_parts = val[0], [before]
            elif val:
                out.append(_yaml_scalar(val))
            continue
        if indent is not None:
            if stripped and (len(ln) - len(ln.lstrip())) <= indent:
                _flush()
                indent, indicator, scalar = None, None, None
            else:
                block.append(ln)
    _flush()
    return out


def named_in_workflows():
    """Files any workflow ACTIVELY invokes, e.g. `python3 path/to/x.py`.

    Position, not presence, via python_args(): `echo python3 x.py` (no
    quotes to blank) used to name x.py as if it ran, and a real quoted
    invocation like `python3 'x.py'` used to be blanked to nothing along
    with it — a regex over unquoted text can't tell an argument from a
    caller in either direction."""
    named = set()
    for wf in (REPO / ".github" / "workflows").glob("*.yml"):
        named.update(_named_in(wf.read_text()))
    return named


def _named_in(workflow_text: str) -> set:
    """One workflow's actively-invoked scripts: run bodies scanned as ONE program,
    so a `false &&` guard survives the line break it sits on."""
    return set(program_python_args("\n".join(_run_bodies(workflow_text))))


def test_looking_files():
    """Git-TRACKED test-looking files only.

    A bare glob also sees transient files that other suites create while running
    (temp fixtures written inside the tree), which made this guard fail in one CI
    step and pass in another within the same job — order-dependent, and a flaky
    guard is worse than none. Only committed files are part of the repo's test
    surface, so ask git rather than the filesystem."""
    import subprocess
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        raise unittest.SkipTest("not a git checkout — this guard is about committed files")
    files = [f for f in out.stdout.decode().split("\0") if f]
    keep = set()
    for f in files:
        base = Path(f).name
        if "node_modules" in f:
            continue
        if base.endswith(".test.py") or base.startswith("test_") and base.endswith(".py") \
           or base.endswith("_test.py"):
            keep.add(str(Path(f)))
    return keep


def orphans_in(all_files, discovered, named):
    """The actual rule, extracted so a synthetic case can pin it.

    Inlined, this was un-pinnable: gutting it to `orphans = []` passed the whole
    suite, because the only other test exercised set arithmetic in isolation
    rather than the code path the real assertion uses."""
    return sorted(set(all_files) - set(discovered) - set(named))


class TestCICoversEveryPythonTest(unittest.TestCase):
    def test_no_python_test_is_invisible_to_ci(self):
        import os
        os.chdir(REPO)
        orphans = orphans_in(test_looking_files(), discovered_by_find(), named_in_workflows())
        self.assertEqual(
            orphans, [],
            "these test files are never executed by CI — either move them to "
            "tests/<name>.test.py (auto-discovered) or name them explicitly in "
            "a workflow:\n  " + "\n  ".join(orphans),
        )

    def test_the_guard_can_actually_fail(self):
        """A guard that cannot fire is the bug it exists to catch.

        Exercises orphans_in() — the same function the real assertion calls — so
        stubbing that computation breaks this case too. Testing the set algebra
        inline instead left the real check gutted-and-green."""
        self.assertEqual(
            orphans_in({"packages/somewhere/tests/test_invented.py"}, set(), set()),
            ["packages/somewhere/tests/test_invented.py"],
            "an out-of-tree file must register as an orphan")

    def test_a_discovered_file_is_not_an_orphan(self):
        self.assertEqual(orphans_in({"tests/x.test.py"}, {"tests/x.test.py"}, set()), [])

    def test_a_workflow_named_file_is_not_an_orphan(self):
        self.assertEqual(orphans_in({"scripts/y.py"}, set(), {"scripts/y.py"}), [])


class TestRunBodiesAreScannedAsAProgram(unittest.TestCase):
    """keweichen's [P2] on #4202 (third round): per-line scanning of run bodies,
    and the two YAML repairs that no test pinned -- each case below is one
    in-memory mutation he ran that stayed green."""

    def test_a_guard_at_the_end_of_a_line_still_guards_the_next_line(self):
        wf = "steps:\n  - run: |\n      false &&\n        python3 packages/x/test_dead.py || true\n"
        self.assertEqual(_named_in(wf), set())

    def test_a_wholly_quoted_run_scalar_names_its_real_call(self):
        # Drop the _yaml_scalar() unwrap and the shell-quote blanking swallows this.
        wf = "steps:\n  - run: \"python3 packages/x/test_real.py\"\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})

    def test_a_sibling_env_block_is_not_a_run_body(self):
        # Key-column indent: `env:` sits at the key's column, and its block-scalar
        # value is a bare invocation -- the line a dash-based indent would swallow.
        wf = ("steps:\n  - run: |\n      python3 packages/x/test_real.py\n"
              "    env:\n      CMD: |\n        python3 packages/x/test_inert.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})

    def test_an_attached_dash_c_does_not_name_its_argument(self):
        wf = "steps:\n  - run: python3 -cpass packages/x/test_dead.py\n"
        self.assertEqual(_named_in(wf), set())

    def test_backslash_continued_dquote_scalar_joins_with_no_space(self):
        """kewei-red-ag2space round 32: a YAML double-quoted scalar's own
        escaped line break (spec 5.7) joins its two physical lines directly,
        with no fold space -- confirmed against PyYAML, which decodes this
        to the single glued token `python3packages/...`, not a real `python3
        <script>` invocation. The unconditional `\" \".join()` this used to be
        let the join's own inserted space get re-consumed by `\\<space>`
        decoding, accidentally manufacturing a plausible-looking command
        that never actually runs in real CI (a false green: this test would
        read as covered when the workflow step is in fact malformed)."""
        bs = chr(92)  # exactly one backslash char, unambiguous vs source escaping
        wf = f'steps:\n  - run: "python3{bs}\n          packages/x/test_glued.py"\n'
        self.assertEqual(_named_in(wf), set())

    def test_plain_dquote_fold_still_inserts_a_space(self):
        """Control for the case above: no trailing backslash means an
        ORDINARY fold, which still must insert a space (unchanged)."""
        wf = ('steps:\n  - run: "python3\n'
              '          packages/x/test_real.py"\n')
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})

    def test_blank_continuation_line_folds_to_a_newline_not_a_space(self):
        """kewei-red-ag2space round 33: a BLANK physical line inside a
        double-quoted scalar is real YAML line-break semantics (confirmed
        against PyYAML: decodes to 'python3\\npackages/...'), so Bash runs
        `python3` with no args on one line and the bare path fails on the
        next. Joining it with a space instead glues them into one command,
        crediting the path as python3's argument though it never receives
        it -- the false green kewei's exact repro demonstrates."""
        wf = 'steps:\n  - run: "python3\n\n          packages/x/test_dead.py"\n'
        self.assertEqual(_named_in(wf), set())

    def test_blank_continuation_line_the_reverse_direction_still_names_it(self):
        """The mirror false-NEGATIVE: a python3 invocation that DOES run,
        separated from a preceding inert command by a blank line, must
        still be named -- the old space-join buried it as an argument."""
        wf = 'steps:\n  - run: "echo inert\n\n          python3 packages/x/test_real.py"\n'
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})


class OptionContractThroughTheConsumerPath(unittest.TestCase):
    """keweichen's round-6 ask: pin the option contract through
    _named_in() -> orphans_in(), not only python_args() directly -- a
    workflow that credits a terminal flag's argument would silently drop
    a real orphan from CI-coverage scrutiny (false-green); one that gives
    up at `--` or a separate-value long option would flag a genuinely
    covered test as an orphan (false-orphan, the safe direction, but
    still a false alarm this suite would otherwise manufacture)."""

    def test_a_terminal_flag_does_not_false_green_an_orphan(self):
        """If -V's argument were credited, this test would vanish from
        the orphan set though python3 never opens it -- the dangerous
        direction, checked through the real consumer, not just python_args()."""
        wf = "steps:\n  - run: python3 -V packages/x/test_dead.py\n"
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_bare_double_dash_does_not_false_orphan_a_real_test(self):
        """A workflow line most of this repo's own CI could plausibly write
        -- `--` before a path defends against an accidental leading-dash
        filename. Giving up at `--` would flag this real, executed test
        as an orphan."""
        wf = "steps:\n  - run: python3 -- packages/x/test_real.py\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_a_separate_value_long_option_does_not_false_orphan_either(self):
        wf = ("steps:\n  - run: python3 --check-hash-based-pycs always "
              "packages/x/test_real.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_hidden_compat_flags_do_not_false_orphan(self):
        """keweichen's round-8 ask: -R/-t are real (missing from --help,
        confirmed by execution) and must not cost a covered test its
        credit through the actual consumer path."""
        for flag in ("-R", "-t"):
            wf = f"steps:\n  - run: python3 {flag} packages/x/test_real.py\n"
            self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
            self.assertEqual(
                orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_an_invalid_hash_policy_value_does_not_false_green_an_orphan(self):
        wf = ("steps:\n  - run: python3 --check-hash-based-pycs sometimes "
              "packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_q_and_x_do_not_false_orphan_through_the_consumer_path(self):
        """keweichen's round-9 ask: pin q/x as unchanged-neighbor controls
        so a future table edit that drops them again fails here too, not
        just in the direct python_args() tests."""
        for flag in ("-q", "-x"):
            wf = f"steps:\n  - run: python3 {flag} packages/x/test_real.py\n"
            self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
            self.assertEqual(
                orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_true_and_and_does_not_false_orphan_through_the_consumer_path(self):
        """The literal `&&`/`||` gap, pinned through the actual
        _named_in()/orphans_in() path, not just program_python_args()."""
        wf = "steps:\n  - run: true && python3 packages/x/test_real.py\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_false_or_or_does_not_false_orphan_through_the_consumer_path(self):
        wf = "steps:\n  - run: false || python3 packages/x/test_real.py\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_real.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_real.py"}, set(), _named_in(wf)), [])

    def test_false_and_and_still_does_not_false_green_through_the_consumer_path(self):
        """The original defect this file exists to prevent, re-checked
        through the consumer path now that true/false LHS is decidable."""
        wf = "steps:\n  - run: false && python3 packages/x/test_dead.py\n"
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_gated_pipe_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen's round-11 finding, pinned through _named_in()/orphans_in():
        a lone `|` used to reset reachability, so a guard's dead pipe still
        credited its script through this exact path."""
        wf = ("steps:\n  - run: false && printf x | "
              "python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_pipefail_does_not_false_orphan_through_the_consumer_path(self):
        """Same round's child regression: `set -o pipefail` makes a later
        `&&` see the pipe's real failure, pinned through the consumer path."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_leading_o_cluster_form_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 12: `set -oe pipefail` (o first, not last)."""
        wf = ("steps:\n  - run: |\n"
              "      set -oe pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_negated_pipeline_does_not_false_orphan_through_the_consumer_path(self):
        """Round 12's child false-orphan regression: a leading `!` negates
        the pipe's pipefail-adjusted result, so `&&` DOES run this one."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      ! false | true && python3 packages/x/test_live.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_amp_pipe_does_not_false_orphan_through_the_consumer_path(self):
        """`|&` was parsed as `|` then a hard-reset `&`, undoing the outer
        guard -- confirmed on Bash 5.2.32 (keweichen), pinned here."""
        wf = ("steps:\n  - run: false && printf x |& "
              "python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_set_dash_dash_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 13: `--` ends `set` option scanning."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set -- +o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_negated_direct_invocation_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 13: `!` doesn't hide the invocation from the
        consumer path either."""
        wf = "steps:\n  - run: '! python3 packages/x/test_live.py'\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_a_quoted_bang_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 15: a SHELL-quoted `'!'` is a literal command
        name (Bash: 127), not the negation reserved word."""
        wf = "steps:\n  - run: \"'!' python3 packages/x/test_dead.py\"\n"
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_set_scanning_stop_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 15: `set` ends its own option scanning at the
        first non-option word, pinned through the consumer path."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set -o pipefail positional +o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_quoted_dollar_value_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 16: shlex erases the single-quoting, but Bash
        rejects the literal option name (pipefail stays at its OFF default)
        and dead.py really runs."""
        wf = ("steps:\n  - run: |\n"
              "      set -o '$OPT'\n"
              "      false | true && python3 packages/x/test_live.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_a_lone_dash_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 16: a lone `-` ends `set`'s own option
        scanning the same as `--`."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set - +o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_per_word_quote_provenance_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 17: two occurrences of the identical resolved
        text, only the first (expandable) is the value `-o` consumes."""
        wf = ("steps:\n  - run: |\n"
              "      OPT=pipefail\n"
              "      set -o \"$OPT\" '$OPT'\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_unrecognized_option_name_abort_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 17: an unrecognized -o NAME aborts the whole
        set invocation before a later toggle in the same command is ever
        reached."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set -o invalid +o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_brace_expansion_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 17: bare brace expansion of the -o value."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipe{fail,foo}\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_an_empty_quoted_word_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 18: an empty quoted word (`""`) is a real
        positional argument -- it ends set's own option scanning before
        a later toggle in the same command is ever reached, same as any
        non-empty one would."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              '      set "" +o pipefail\n'
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_brace_expanded_word_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 18: brace expansion produces separate argv
        words, not one word with several candidate readings -- `+o`
        consumes only the first."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set +o {errexit,pipefail}\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_redirection_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 18: a redirection is stripped by the shell
        before set ever sees its argv."""
        wf = ("steps:\n  - run: |\n"
              "      set -o >/dev/null pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_an_empty_redirect_target_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 19: a redirect to an empty target aborts the
        whole command before it runs -- it is not a successfully-stripped
        no-op."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              '      set +o >"" pipefail\n'
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_an_escaped_fd_digit_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 19: an escaped digit before a redirect is a
        real argv word, not a bare fd prefix."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set \\2>/dev/null +o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_process_substitution_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 19: process substitution is a literal argv
        word, never a redirect, so it must not be silently discarded --
        here it makes `-o` abort on an unrecognized name, leaving
        pipefail off and the later script genuinely reachable."""
        wf = ("steps:\n  - run: |\n"
              "      set +o pipefail\n"
              "      set -o <(true) pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_empty_heredoc_delimiter_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 20: an empty here-doc delimiter is valid
        syntax, not a failed redirect -- the `-o pipefail` on the same
        line still takes effect. Starts pipefail OFF so a wrong
        "unchanged" verdict is observably distinct from the right one."""
        wf = ("steps:\n  - run: |\n"
              "      set +o pipefail\n"
              '      set -o <<"" pipefail\n'
              "\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_glued_process_substitution_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 20: process substitution glued to a flag
        cluster must not leak an option-shaped character into the
        cluster scan."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set +u<(echo o) pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_expandable_redirect_target_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 20: a redirect target carrying a variable
        decides success at runtime, not from its source text."""
        wf = ("steps:\n  - run: |\n"
              "      EMPTY=\n"
              "      set -o pipefail\n"
              '      set +o >"$EMPTY" pipefail\n'
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_delimiter_less_heredoc_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 21: `<<` with no delimiter word at all is a
        Bash syntax error, distinct from the valid `<<""`."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set +o pipefail <<\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_non_set_expandable_redirect_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 21: an uncertain redirect on a non-`set`
        command must not poison the pipefail model."""
        wf = ("steps:\n  - run: |\n"
              "      OUT=/dev/null\n"
              "      set +o pipefail\n"
              '      printf x >"$OUT"\n'
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_leading_redirect_before_the_command_word_does_not_false_green_through_the_consumer_path(self):
        """keweichen round 22: a redirect can precede the command word,
        so its uncertainty must survive until the full word list --
        including the command name -- is known."""
        wf = ("steps:\n  - run: |\n"
              "      EMPTY=\n"
              "      set -o pipefail\n"
              '      >"$EMPTY" set +o pipefail\n'
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_a_delimiter_less_heredoc_halts_the_program_through_the_consumer_path(self):
        """keweichen round 22: a delimiter-less heredoc is a PARSE error,
        not a no-op line -- everything later in the program is
        unreachable. Starts pipefail OFF so "no state change" and
        "parsing terminates" diverge observably."""
        wf = ("steps:\n  - run: |\n"
              "      set +o pipefail\n"
              "      set -o pipefail <<\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_ordinary_prefix_glued_to_process_substitution_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 22: a non-flag-shaped prefix (`x`) already
        guarantees a positional word on its own and needs no expandable-
        marking -- round 20's over-broad `if val:` survived round 21
        unfixed too."""
        wf = ("steps:\n  - run: |\n"
              "      set +o pipefail\n"
              "      set x<(true) -o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_brace_grouped_process_substitution_does_not_false_green_through_the_consumer_path(self):
        """keweichen round 23: a brace group already supported by the
        parser can expand its process-substitution word into a genuinely
        flag-shaped one, which must poison the same way a bare `+u<(..)`
        would."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set {+u<(echo o),pipefail}\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_heredoc_body_line_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 23/24: a heredoc BODY line that merely looks like
        a delimiter-less-heredoc command must not trigger the whole-
        program fatal halt -- it is literal data, never executed. Bare
        `EOF` (round 24: real YAML dedents `run: |` before Bash ever sees
        it, so `_run_bodies` must too, not carry an artificial indented
        delimiter just to survive its own non-dedenting bug)."""
        wf = ("steps:\n  - run: |\n"
              "      : <<'EOF'\n"
              "      set -o pipefail <<\n"
              "      EOF\n"
              "      python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_a_compound_if_condition_does_not_false_green_through_the_consumer_path(self):
        """qingyun-wu round 24: `if true && false` is false overall, the
        else runs -- confirmed by direct execution."""
        wf = ("steps:\n  - run: |\n"
              "      if true && false; then\n"
              "        python3 packages/x/test_then.py\n"
              "      else\n"
              "        python3 packages/x/test_else.py\n"
              "      fi\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_else.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_then.py", "packages/x/test_else.py"},
                       set(), _named_in(wf)),
            ["packages/x/test_then.py"])

    def test_chain_exclusivity_does_not_false_green_through_the_consumer_path(self):
        """qingyun-wu round 24: a taken `if` arm drops every later `elif`,
        regardless of its own condition -- confirmed by direct execution."""
        wf = ("steps:\n  - run: |\n"
              "      if true; then\n"
              "        python3 packages/x/test_if.py\n"
              "      elif true; then\n"
              "        python3 packages/x/test_elif.py\n"
              "      fi\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_if.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_if.py", "packages/x/test_elif.py"},
                       set(), _named_in(wf)),
            ["packages/x/test_elif.py"])

    def test_backgrounded_set_does_not_false_green_through_the_consumer_path(self):
        """qingyun-wu round 24: `set +o pipefail &` runs in a subshell and
        must not mutate the parent's modeled pipefail state -- confirmed
        by direct execution."""
        wf = ("steps:\n  - run: |\n"
              "      set -o pipefail\n"
              "      set +o pipefail &\n"
              "      wait\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_arithmetic_left_shift_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 24: `<<` inside `$((...))` is a left-shift, not
        a heredoc -- confirmed by direct execution."""
        wf = ("steps:\n  - run: |\n"
              "      x=$((1 << 2))\n"
              "      python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_blank_heredoc_terminator_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 25: a blank terminator (empty quoted delimiter)
        must survive comment/blank-line filtering, since it IS the
        heredoc's own data, not incidental whitespace to drop."""
        wf = ("steps:\n  - run: |\n"
              "      : <<''\n"
              "\n"
              "      python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_hash_heredoc_terminator_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 25: a terminator line that's literally `#` is
        heredoc data, not a real Bash comment to strip."""
        wf = ("steps:\n  - run: |\n"
              "      : <<'#'\n"
              "      #\n"
              "      python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_non_splitting_brace_process_substitution_does_not_false_green_through_the_consumer_path(self):
        """keweichen round 25: `{+u}` has no comma, stays one literal word
        -- must not be treated as if it will split."""
        wf = ("steps:\n  - run: |\n"
              "      set +o pipefail\n"
              "      set {+u}<(echo o) -o pipefail\n"
              "      false | true && python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_explicit_yaml_indentation_indicator_is_honored_not_auto_dedented(self):
        """keweichen round 25: `|2` deliberately keeps 2 leading spaces on
        every content line -- `min(indents)` cannot express that, since it
        always strips a block's own minimum. A bare `EOF` terminator then
        never matches the still-indented delimiter, so the heredoc never
        closes and the trailing command never runs -- confirmed by direct
        execution against the real YAML-decoded script."""
        wf = ("steps:\n  - run: |2\n"
              "        : <<'EOF'\n"
              "        ignored\n"
              "        EOF\n"
              "        python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_explicit_indicator_state_does_not_leak_into_a_later_flush(self):
        """keweichen round 27: a sibling key ends the `run:` block by dedent
        (not by a new `run:`), which reset `indent` but left `indicator`
        stale -- the unconditional `_flush()` at end-of-input then computed
        `None + indicator` and crashed on this ordinary, valid YAML."""
        wf = ("steps:\n  - run: |2-\n"
              "      python3 packages/x/test_live.py\n"
              "    env:\n"
              "      X: y\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_folded_scalar_joins_lines_so_a_dead_argument_names_nothing(self):
        """qingyun-wu round 28: `run: >-` FOLDS into one line, so `echo inert`
        followed by a python invocation is one `echo` command -- the
        invocation never runs, confirmed against real PyYAML + Bash."""
        wf = ("steps:\n  - run: >-\n"
              "      echo inert\n"
              "      python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), set())
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)),
            ["packages/x/test_dead.py"])

    def test_folded_scalar_joins_lines_so_a_split_invocation_still_names_its_test(self):
        """qingyun-wu round 28, the inverse split: `python3` alone on one
        line, its script path on the next -- folding joins them into a real
        invocation the line-per-line model named nothing for."""
        wf = ("steps:\n  - run: >-\n"
              "      python3\n"
              "      packages/x/test_live.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_literal_scalar_is_not_folded_by_the_new_scalar_type_tracking(self):
        """Control for the folded-scalar fix: an ordinary `run: |` block
        must still keep each line separate -- only `>` folds."""
        wf = ("steps:\n  - run: |\n"
              "      echo inert\n"
              "      python3 packages/x/test_live.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})

    def test_double_quoted_scalar_newline_escape_is_a_real_line_break(self):
        """keweichen round 29: a double-quoted YAML scalar's `\\n` decodes
        to a REAL newline (confirmed against real PyYAML), so this is a
        two-line shell script -- confirmed by direct execution that Bash
        runs the second line. `_yaml_scalar()` used to only strip the outer
        quotes, leaving a literal backslash-n that names nothing."""
        wf = 'steps:\n  - run: "echo setup\\npython3 packages/x/test_live.py"\n'
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_double_quoted_scalar_other_escapes_decode_too(self):
        """A tab escape and an escaped inner quote both decode per the
        real YAML double-quote spec -- confirmed against PyYAML."""
        wf = 'steps:\n  - run: "python3\\tpackages/x/test_live.py"\n'
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})

    def test_single_quoted_scalar_has_no_backslash_escapes(self):
        """Control: single-quoted YAML has no backslash-escape mechanism at
        all -- a literal backslash-n stays two characters, naming nothing
        on this one (unfolded, unescaped) line."""
        wf = "steps:\n  - run: 'echo setup\\npython3 packages/x/test_live.py'\n"
        self.assertEqual(_named_in(wf), set())

    def test_a_multiline_quoted_flow_scalar_folds_and_decodes(self):
        """keweichen round 30 [P2 blocker]: a double-quoted `run:` value left
        open across a line break folds like real YAML, not two separate
        physical-line reads -- neither of which named the real command."""
        wf = 'steps:\n  - run: "python3\n      packages/x/test_live.py"\n'
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_a_single_quoted_multiline_flow_scalar_also_folds(self):
        wf = "steps:\n  - run: 'python3\n      packages/x/test_live.py'\n"
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})

    def test_a_multiline_flow_scalar_still_decodes_its_own_escape(self):
        wf = 'steps:\n  - run: "python3\\tpackages/x/test_live.py\n      "\n'
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})

    def test_heredoc_nested_in_arithmetic_command_substitution_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 25: a real heredoc inside a `$(...)` nested
        within `$((...))` arithmetic must still be recognized as one."""
        wf = ("steps:\n  - run: |\n"
              "      x=$(( $(cat <<EOF >/dev/null\n"
              "      python3 packages/x/test_body.py\n"
              "      EOF\n"
              "      echo 1\n"
              "      ) ))\n"
              "      python3 packages/x/test_live.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_body.py", "packages/x/test_live.py"},
                       set(), _named_in(wf)),
            ["packages/x/test_body.py"])

    def test_if_as_plain_argument_does_not_false_orphan_through_the_consumer_path(self):
        """keweichen round 25: `printf if` is not a branch opener -- the
        `||` after it must not be masked."""
        wf = ("steps:\n  - run: |\n"
              "      false && printf if || python3 packages/x/test_dead.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_dead.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_dead.py"}, set(), _named_in(wf)), [])

    def test_printf_if_as_cmd1_still_credits_the_or_side_through_the_consumer_path(self):
        """keweichen round 25/26/27: the actually-named case has `printf if`
        as the FIRST command, not guarded by a preceding `false &&` -- its
        own exit status is undecidable, but `X && false` is false either
        way, so `|| test_live.py` still runs (confirmed on real Bash)."""
        wf = ("steps:\n  - run: |\n"
              "      printf if && false || python3 packages/x/test_live.py\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_live.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_live.py"}, set(), _named_in(wf)), [])

    def test_unknown_then_guaranteed_true_elif_proves_else_dead_through_the_consumer_path(self):
        """keweichen round 25: a trailing `else` after an undecidable `if`
        and a guaranteed-true `elif` is provably dead either way."""
        wf = ("steps:\n  - run: |\n"
              '      if [ "$X" = y ]; then\n'
              "        python3 packages/x/test_if.py\n"
              "      elif true; then\n"
              "        python3 packages/x/test_elif.py\n"
              "      else\n"
              "        python3 packages/x/test_else.py\n"
              "      fi\n")
        self.assertEqual(_named_in(wf), {"packages/x/test_if.py", "packages/x/test_elif.py"})
        self.assertEqual(
            orphans_in({"packages/x/test_if.py", "packages/x/test_elif.py",
                        "packages/x/test_else.py"}, set(), _named_in(wf)),
            ["packages/x/test_else.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
