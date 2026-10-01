#!/usr/bin/env python3
"""Structural pin: ci.yml's python step delegates both load-sensitive branches to the selector.

The behavioural partition test cannot tell the selector from an inline copy that agrees
with it today, and a copy would keep passing after a fix lands only in the selector. This
parses the step's run block as the shell sees it (comments and heredoc bodies dropped,
continuations joined) and requires the leg-6 branch to run
`bash scripts/select-load-sensitive-suites.sh only "$LIST"` exactly once, the other
branch the same with `without`, and no other executed line in either branch to read "$LIST".

Run: python3 tests/python-ci-selector-delegation.test.py
"""
import re
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
SELECTOR = "scripts/select-load-sensitive-suites.sh"


def step_body(text: str) -> str:
    start = text.find("      - name: Run Python standalone tests\n")
    run = text.find("        run: |\n", start)
    if start < 0 or run < 0:
        raise AssertionError("could not find the 'Run Python standalone tests' run: block in ci.yml")
    m = re.match(r"(.*?)(?=\n {6}- name:|\n {2}\w|\Z)", text[run + len("        run: |\n"):], re.S)
    return m.group(1)


def executed_commands(body: str):
    """Each executed simple command as a token list: comments, heredoc bodies dropped."""
    lines, heredoc_end, pending = [], None, ""
    for raw in body.splitlines():
        if heredoc_end is not None:
            if raw.strip() == heredoc_end:
                heredoc_end = None
            continue
        line = pending + raw.strip()
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        pending = ""
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        lex.commenters = "#"
        toks = list(lex)
        if not toks:
            continue
        for i, t in enumerate(toks[:-1]):
            if t in ("<<", "<<-"):
                heredoc_end = toks[i + 1].strip("'\"")
        cmd = []
        for t in toks:
            if t in (";", "&&", "||", "|", "&"):
                if cmd:
                    lines.append(cmd)
                cmd = []
            else:
                cmd.append(t)
        if cmd:
            lines.append(cmd)
    return lines


def branches(cmds):
    """The commands of the `SHARD = 6` branch and of its else branch, by if/fi depth."""
    six, other, where, depth = [], [], None, 0
    for c in cmds:
        if c[0] == "if":
            depth += 1
            if where is None and "${SHARD:-1}" in c and "6" in c:
                where, top = six, depth
                continue
        elif c[0] == "fi":
            if where is not None and depth == top:
                return six, other
            depth -= 1
        elif c == ["else"] and where is six and depth == top:
            where = other
            continue
        if where is not None:
            where.append(c)
    raise AssertionError("ci.yml: no `if [ \"${SHARD:-1}\" = 6 ] ... else ... fi` in the python step")


def check(name, cmds, mode):
    fails = []
    calls = [c for c in cmds if c[:3] == ["bash", SELECTOR, mode]]
    if len(calls) != 1:
        fails.append(f"{name}: {len(calls)} executed `bash {SELECTOR} {mode}` call(s), not exactly 1")
    elif calls[0][3:4] != ["$LIST"]:
        fails.append(f"{name}: the selector call does not read \"$LIST\": {calls[0]}")
    readers = [c for c in cmds if "$LIST" in " ".join(c) and c[:2] != ["bash", SELECTOR]]
    if readers:
        fails.append(f"{name}: {len(readers)} executed line(s) besides the selector read \"$LIST\": {readers[0]}")
    return fails


def main() -> int:
    six, other = branches(executed_commands(step_body(CI.read_text())))
    fails = check("leg 6 branch", six, "only") + check("legs 1-5 branch", other, "without")
    for f in fails:
        print("  FAIL", f)
    if fails:
        return 1
    print(f"PASS: leg 6 and legs 1-5 each run `{SELECTOR} only|without \"$LIST\"` exactly once, "
          "and nothing else in either branch reads the list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
