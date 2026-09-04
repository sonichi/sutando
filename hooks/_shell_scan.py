#!/usr/bin/env python3
"""One Bash-shaped scanner for the PreToolUse guards that must read a command.

Guards decide things like "is this argument the one bash will rewrite before
`gh` sees it". Answering that from regex lookbehind or `shlex` fails in three
ways a reviewer reproduced against bash itself: an escaped quote ends a
`shlex(posix=False)` token early, an EVEN run of backslashes leaves `$(` active
while a lookbehind reads the last one as an escape, and an escaped space looks
like a word boundary. All three UNDER-deny, silently.

So state is carried, not inferred from the previous character: quoting, escape,
and whether we are at a word start. `bash` is the oracle the tests compare to.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

OPERATORS = (";;", "&&", "||", ";", "|", "&", "(", ")", "\n")
# What bash SUBSTITUTES: command substitution only. `$VAR` is an ordinary
# interpolation, not a code span, and denying it would cry wolf.
_SUBST_OPENERS = ("`", "$(")


@dataclass
class Word:
    """One argv word, plus what the raw source said about it."""

    raw: str = ""
    text: str = ""
    quoted: str = ""          # the quote style that opened the VALUE: '' " or ''
    expands: bool = False     # an ACTIVE ` or $( — bash would run it
    is_operator: bool = False

    def basename_is(self, name: str, fold: bool = False) -> bool:
        """Is this word the program `name`, however it was spelled?

        Compares the EXPANDED text, so `/usr/bin/gh` and `"gh"` both match; with
        fold=True, `GH` does too — a case-insensitive filesystem runs it.
        """
        base = os.path.basename(self.text)
        return base.lower() == name.lower() if fold else base == name


@dataclass
class _State:
    quote: str = ""       # "", "'", or '"'
    escaped: bool = False
    in_word: bool = False
    words: List[Word] = field(default_factory=list)
    cur: Word = field(default_factory=Word)


def _flush(st: _State) -> None:
    if st.in_word:
        st.words.append(st.cur)
    st.cur = Word()
    st.in_word = False


def _double_quote_escapes(ch: str) -> bool:
    """Inside double quotes bash honours a backslash only before these; before
    anything else the backslash is a literal character."""
    return ch in '$`"\\\n'


def words(command: str) -> List[Word]:
    """argv words plus operators, or [] when the line does not lex.

    An unterminated quote makes bash refuse the whole command, so returning []
    is the honest answer — a guard must not scan a fragment of something that
    will never run.
    """
    st = _State()
    i, n = 0, len(command)
    while i < n:
        ch = command[i]

        if st.escaped:
            # The backslash is consumed; the character it protected is literal
            # and — crucially — cannot start a word, end one, or open a comment.
            st.cur.raw += ch
            st.cur.text += ch
            st.escaped = False
            i += 1
            continue

        if ch == "\\" and st.quote != "'":
            if st.quote == '"' and not _double_quote_escapes(command[i + 1:i + 2] or " "):
                # Literal backslash inside double quotes. It does NOT escape the
                # next character, so `\\$(` leaves the substitution ACTIVE.
                st.cur.raw += ch
                st.cur.text += ch
                st.in_word = True
                i += 1
                continue
            st.in_word = True
            st.cur.raw += ch
            st.escaped = True
            i += 1
            continue

        if st.quote:
            st.cur.raw += ch
            if ch == st.quote:
                st.quote = ""
            else:
                st.cur.text += ch
                if st.quote == '"' and _opens_substitution(command, i):
                    st.cur.expands = True
            i += 1
            continue

        if ch in ("'", '"'):
            st.quote = ch
            st.in_word = True
            st.cur.raw += ch
            if not st.cur.quoted:
                st.cur.quoted = ch
            i += 1
            continue

        if ch == "#" and not st.in_word:
            # Only at a word start: `a#b` and `x/y#frag` are literal.
            while i < n and command[i] != "\n":
                i += 1
            continue

        if ch in " \t":
            _flush(st)
            i += 1
            continue

        op = _operator_at(command, i)
        if op:
            _flush(st)
            st.words.append(Word(raw=op, text=op, is_operator=True))
            i += len(op)
            continue

        st.in_word = True
        st.cur.raw += ch
        st.cur.text += ch
        if _opens_substitution(command, i):
            st.cur.expands = True
        i += 1

    if st.quote or st.escaped:
        return []            # bash would refuse this line; scan nothing
    _flush(st)
    return st.words


def _operator_at(command: str, i: int) -> str:
    for op in OPERATORS:
        if command.startswith(op, i):
            return op
    return ""


def _opens_substitution(command: str, i: int) -> bool:
    for opener in _SUBST_OPENERS:
        if command.startswith(opener, i):
            return True
    return False


def segments(command: str) -> List[List[Word]]:
    """Simple commands, split on unquoted operators.

    A guard that arms on seeing `gh` must disarm at the separator, or a later
    unrelated command inherits the arming.
    """
    out: List[List[Word]] = [[]]
    for w in words(command):
        if w.is_operator:
            if out[-1]:
                out.append([])
            continue
        out[-1].append(w)
    return [seg for seg in out if seg]
