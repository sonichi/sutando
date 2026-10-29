#!/usr/bin/env python3
"""No new DotSpace literal in behaviour-bearing code: `ag2.space` / `s.space` in any host form
(bare, `chat.`, `dev.`, `sutando.`) outside src/dotspace.py and a small, explicit allowlist.

Comments and docstrings are prose and do not count; a string a user reads (a health message, a
placeholder mxid) is allowed only by a listed fragment. Routing, linking and trust decisions
belong in src/dotspace.py (`docs/dotspace.md`), never in a caller's literal.

Run: python3 tests/dotspace-literal-guard.test.py
"""
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("src", "skills", "packages/ag2-sparrow/ag2_sparrow", "scripts")
SUFFIXES = (".py", ".ts", ".json", ".sh", ".mjs", ".plist")
# One pattern for every host form, any case, dots plain or regex-escaped: the label before
# `.space` must itself be `ag2` or `s`, so `address.space` is no hit while `dev-chat.s.space` is.
LITERAL = re.compile(r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9-]+\\?\.)*(?:ag2|s)\\?\.space(?![A-Za-z0-9-])", re.I)

# path -> None (every line allowed: the resolver and its bundled copy) or the fragments a line
# must contain. Add a fragment only for prose a person reads, never for a value code acts on.
ALLOW: dict[str, "tuple[str, ...] | None"] = {
    "src/dotspace.py": None,
    "packages/ag2-sparrow/ag2_sparrow/dotspace.py": None,
    "src/health-check.py": ("ag2.space mobile", "ag2.space gateway not configured"),
    "skills/task-progress/scripts/notify.py": ("(e.g. dev.ag2.space)",),
    "packages/ag2-sparrow/ag2_sparrow/adapters/hatch/cli.py": (
        "@my-agent:ag2.space",
        "@your-agent:ag2.space",
        "(s.space listener)",
        "the s.space Agent Portal",
        "in an s.space room",
        "no s.space room",
        "the s.space gateway",
        "agent's s.space MXID",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/hatch/ag2-sparrow-hatch.sh": (
        "@your-agent:ag2.space",
        "No s.space message reaches",
        "s.space task(s) need answers",
    ),
    # The product name s.space in prose a person or agent reads, never a host a caller acts on.
    "src/policy/guardrail.py": (
        "\"s.space\", \"GUEST tier\"",
        "the s.space broker attests",
    ),
    "packages/ag2-sparrow/ag2_sparrow/team_guardrail.py": (
        "\"s.space\", \"GUEST tier\"",
        "the s.space broker attests",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_cloud_session/session_adapter.py": (
        "standing s.space instructions",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_code/ag2space-inbox/hooks/hooks.json": (
        "s.space inbox connector",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_code/ag2space-inbox/hooks/register.ts": (
        "[s.space task ",
        "an [s.space task ...]",
        "one s.space task",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_code/cli.py": (
        "an s.space agent",
        "code from s.space",
        "the s.space backend URL",
        "when s.space told",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_code/inbox.py": (
        "[s.space] This attempt",
        "[s.space] The session",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_code/shape.py": (
        "as an s.space agent",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_routine/routine_adapter.py": (
        "owner's s.space ",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/codex/connect_cli.py": (
        "as an s.space agent",
        "as an s.space ",
        "the s.space backend URL",
        "code from s.space",
        "when s.space told",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/hatch/builtin-runtime-text.json": (
        "new s.space messages",
        "owner's s.space DM",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/hatch/hook-definition.json": (
        "new s.space messages",
    ),
    "packages/ag2-sparrow/ag2_sparrow/adapters/hatch/keepalive.py": (
        "an s.space task",
    ),
    "packages/ag2-sparrow/ag2_sparrow/connect_bridge.py": (
        "code in s.space once",
    ),
    "packages/ag2-sparrow/ag2_sparrow/connect_common.py": (
        "removed in s.space;",
        "the relay URL s.space returned",
        "copy it from s.space again",
        "\"s.space code: \"",
        "s.space is still redeeming",
        "s.space is busy",
        "new one in s.space\"",
        "s.space refused the code",
        "s.space's answer",
    ),
}

TEST_FILE = re.compile(r"(^test_.*\.py$)|(_test\.py$)|(\.test\.(py|ts)$)")


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return "tests" in parts[:-1] or "node_modules" in parts or "__pycache__" in parts \
        or bool(TEST_FILE.search(parts[-1]))


def code_lines_py(source: str) -> list[str]:
    """The source's lines with comments and docstrings blanked, so only code is scanned."""
    lines = source.splitlines()
    spans: list[tuple[int, int, int, int]] = []  # (row0, col0, row1, col1), 1-based rows
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                spans.append((*tok.start, *tok.end))
    except (tokenize.TokenError, SyntaxError):
        pass
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            d = body[0]
            spans.append((d.lineno, d.col_offset, d.end_lineno, d.end_col_offset))
    for r0, c0, r1, c1 in spans:
        for row in range(r0, r1 + 1):
            line = lines[row - 1]
            a = c0 if row == r0 else 0
            b = c1 if row == r1 else len(line)
            lines[row - 1] = line[:a] + " " * (b - a) + line[b:]
    return lines


def code_lines_ts(source: str) -> list[str]:
    out = []
    for line in source.splitlines():
        s = line.lstrip()
        out.append("" if s.startswith(("//", "/*", "*")) else line)
    return out


def code_lines_sh(source: str) -> list[str]:
    return ["" if line.lstrip().startswith("#") else line for line in source.splitlines()]


def code_lines_xml(source: str) -> list[str]:
    """Lines with `<!-- ... -->` spans blanked, across lines."""
    out, in_comment = [], False
    for line in source.splitlines():
        kept, i = [], 0
        while i < len(line):
            if in_comment:
                j = line.find("-->", i)
                if j == -1:
                    break
                i, in_comment = j + 3, False
            else:
                j = line.find("<!--", i)
                if j == -1:
                    kept.append(line[i:])
                    break
                kept.append(line[i:j])
                i, in_comment = j + 4, True
        out.append("".join(kept))
    return out


def code_lines(rel: str, source: str) -> list[str]:
    if rel.endswith(".py"):
        return code_lines_py(source)
    if rel.endswith((".ts", ".mjs")):
        return code_lines_ts(source)
    if rel.endswith(".sh"):
        return code_lines_sh(source)
    if rel.endswith(".plist"):
        return code_lines_xml(source)
    return source.splitlines()


def scan_lines(lines) -> list[tuple[int, str]]:
    """Every literal, as (1-based line, matched host form); a line with two yields two."""
    found = []
    for i, line in enumerate(lines, 1):
        for m in LITERAL.finditer(line):
            found.append((i, m.group(0)))
    return found


def allowed(rel: str, line: str) -> bool:
    if rel not in ALLOW:
        return False
    fragments = ALLOW[rel]
    return fragments is None or any(f in line for f in fragments)


def scan_repo(repo: Path = REPO):
    """-> (offenders [(rel, line, form)], total literal count, files scanned)."""
    offenders, total, files = [], 0, 0
    for root in ROOTS:
        for path in sorted((repo / root).rglob("*")):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            rel = path.relative_to(repo).as_posix()
            if is_test_path(rel):
                continue
            files += 1
            try:
                source = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            raw = source.splitlines()
            for lineno, form in scan_lines(code_lines(rel, source)):
                total += 1
                if not allowed(rel, raw[lineno - 1]):
                    offenders.append((rel, lineno, form))
    return offenders, total, files


class TestScannerKnownAnswer(unittest.TestCase):
    # Nine hits across every form plus five near-misses; EXPECTED below is the answer key.
    FIXTURE = [
        "origin = 'https://sutando.ag2.space'",
        "gw = 'chat.ag2.space'; dev = 'dev.ag2.space'",
        "via = 'ag2.space'",
        "room = '!r:ag2.space' or '@u:dev.ag2.space'",
        "cloud = 'https://dev-sutando.s.space/'",
        "servers = ['s.space', 'dev.s.space']",
        "not_hits = ['ag2space', 'address.space', 'ag2.spaces', 'xs.space', 'ag2-space']",
        "",
        "upper = 'https://Sutando.AG2.Space'",
        r"pattern = re.compile(r'@[a-z]+:ag2\.space$')",
    ]
    EXPECTED = [
        (1, "sutando.ag2.space"), (2, "chat.ag2.space"), (2, "dev.ag2.space"), (3, "ag2.space"),
        (4, "ag2.space"), (4, "dev.ag2.space"), (5, "dev-sutando.s.space"), (6, "s.space"),
        (6, "dev.s.space"), (9, "Sutando.AG2.Space"), (10, r"ag2\.space"),
    ]

    def test_every_form_counted_exactly_once(self):
        self.assertEqual(scan_lines(self.FIXTURE), self.EXPECTED)
        self.assertEqual(len(scan_lines(self.FIXTURE)), 11)

    def test_comments_and_docstrings_do_not_count_in_python(self):
        src = ('"""Module doc names ag2.space and chat.s.space."""\n'
                'X = 1  # a comment naming dev.ag2.space\n'
                'def f():\n    """s.space in a docstring."""\n    return "sutando.ag2.space"\n'
                'Y = "ag2.space"  # trailing dev.s.space\n')
        self.assertEqual(scan_lines(code_lines_py(src)), [(5, "sutando.ag2.space"), (6, "ag2.space")])

    def test_ts_comment_lines_do_not_count(self):
        src = "// ag2.space\nconst a = 'https://sutando.ag2.space';\n * s.space\n"
        self.assertEqual(scan_lines(code_lines_ts(src)), [(2, "sutando.ag2.space")])

    def test_shell_and_plist_comments_do_not_count(self):
        sh = "# the ag2.space bridge\nVIA=ag2.space  # dev.ag2.space\n"
        self.assertEqual(scan_lines(code_lines_sh(sh)), [(2, "ag2.space"), (2, "dev.ag2.space")])
        plist = "<!-- ag2.space job\n chat.ag2.space -->\n<string>s.space</string><!-- x.s.space -->\n"
        self.assertEqual(scan_lines(code_lines_xml(plist)), [(3, "s.space")])

    def test_test_paths_are_skipped(self):
        for rel in ("src/tests/x.py", "skills/a/test_room_ops.py", "src/foo_test.py", "src/a.test.ts",
                    "skills/a/node_modules/b.json"):
            self.assertTrue(is_test_path(rel), rel)
        for rel in ("src/dotspace.py", "skills/a/scripts/testing_tools.py", "src/contest.py"):
            self.assertFalse(is_test_path(rel), rel)

    def test_allowlist_names_only_existing_files(self):
        for rel in ALLOW:
            self.assertTrue((REPO / rel).is_file(), f"{rel} is listed but missing: drop the entry")


class TestRepo(unittest.TestCase):
    def test_no_literal_outside_the_allowlist(self):
        offenders, total, files = scan_repo()
        print(f"\n  dotspace literals: {total} in {files} files scanned; allowlist: {len(ALLOW)} files")
        msg = "\n".join(f"  {rel}:{line}: {form}" for rel, line, form in offenders)
        self.assertEqual(offenders, [], "new DotSpace literal(s) in behaviour-bearing code; route them "
                                        "through src/dotspace.py:\n" + msg)
        self.assertGreater(total, 0, "the scanner found nothing at all: its roots or pattern broke")


if __name__ == "__main__":
    unittest.main()
