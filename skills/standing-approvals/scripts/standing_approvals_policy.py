"""Standing approvals: the pure rules — scope grammar, matching, expiry and which rows count.

No I/O and no room capability: rows arrive as `Approval` records whose cells carry the value,
the account that wrote it (`by`) and when (`updated`, epoch ms). Every answer fails closed —
a line that does not parse, a key the action does not supply, or an expiry that cannot be read
makes the row not count, never count more.

Scope: one condition per line (`;` also separates), all must hold.
  key: glob[ | glob ...]     every supplied value of `key` matches one of the globs
  key != glob[ | glob ...]   every supplied value of `key` matches none of them
  key <= 50                  also <, >=, >, ==; every supplied value is a number within it
  any                        alone: any target for that kind (an empty scope too, on a row the
                             owner created himself)
Keys and values compare case-insensitively; `*` matches any run of characters, `/` included.
"""
from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

ACTIVE, PROPOSED, REVOKED = "Active", "Proposed", "Revoked"
STATUSES = (PROPOSED, ACTIVE, REVOKED)
# The cells that define what a row approves; the owner's activation must postdate each of them.
TERMS = ("kind", "scope", "expires")

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")
BOUND_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.\-]*)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")
GLOB_RE = re.compile(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$")
NUMERIC_OPS = ("<=", ">=", "==", "<", ">")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?$")


class ScopeError(ValueError):
    """A scope that does not parse; the row it belongs to does not count."""


@dataclass(frozen=True)
class Condition:
    key: str
    op: str  # "glob", "not_glob", or one of NUMERIC_OPS
    patterns: Tuple[str, ...] = ()
    bound: Optional[float] = None

    def describe(self) -> str:
        if self.op == "glob":
            return f"{self.key}: {' | '.join(self.patterns)}"
        if self.op == "not_glob":
            return f"{self.key} != {' | '.join(self.patterns)}"
        return f"{self.key} {self.op} {self.bound:g}"


@dataclass(frozen=True)
class Scope:
    conditions: Tuple[Condition, ...]
    explicit_any: bool = False

    @property
    def any(self) -> bool:
        return not self.conditions


@dataclass
class Cell:
    value: object = None
    by: Optional[str] = None
    updated: Optional[int] = None


@dataclass
class Approval:
    id: str
    cells: Dict[str, Cell] = field(default_factory=dict)
    created_by: Optional[str] = None

    def get(self, name: str) -> Cell:
        return self.cells.get(name) or Cell()

    def text(self, name: str) -> str:
        v = self.get(name).value
        return "" if v is None else str(v)


def fold(text: object) -> str:
    return " ".join(str(text).split()).casefold()


def _number(text: str) -> Optional[float]:
    try:
        n = float(text.strip())
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _patterns(raw: str, line: str) -> Tuple[str, ...]:
    pats = tuple(p.strip() for p in raw.split("|"))
    if not pats or any(not p for p in pats):
        raise ScopeError(f"empty pattern in {line!r}")
    return tuple(p.casefold() for p in pats)


def parse_line(line: str) -> Condition:
    m = BOUND_RE.match(line)
    if m and m.group(2) != "!=":
        key, op, raw = m.groups()
        n = _number(raw)
        if n is None:
            raise ScopeError(f"{line!r}: {raw!r} is not a number")
        return Condition(key.casefold(), op, bound=n)
    if m:  # key != glob
        return Condition(m.group(1).casefold(), "not_glob", _patterns(m.group(3), line))
    m = GLOB_RE.match(line)
    if not m or not KEY_RE.match(m.group(1)):
        raise ScopeError(f"{line!r} is not `key: pattern`, `key != pattern` or `key <= number`")
    return Condition(m.group(1).casefold(), "glob", _patterns(m.group(2), line))


def parse_scope(text: Optional[str]) -> Scope:
    lines = [ln.strip() for chunk in (text or "").splitlines() for ln in chunk.split(";")]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if any(ln.casefold() == "any" for ln in lines):
        if len(lines) > 1:
            raise ScopeError("`any` cannot be combined with other conditions")
        return Scope((), explicit_any=True)
    return Scope(tuple(parse_line(ln) for ln in lines))


def parse_attrs(pairs: Iterable[str]) -> Dict[str, List[str]]:
    """`key=value` pairs into {key: [values]}; a repeated key supplies several values."""
    out: Dict[str, List[str]] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not KEY_RE.match(key):
            raise ValueError(f"--attr {pair!r} is not key=value")
        out.setdefault(key.casefold(), []).append(value.strip())
    return out


def condition_holds(c: Condition, attrs: Dict[str, List[str]]) -> Tuple[bool, str]:
    values = attrs.get(c.key) or []
    if not values:
        return False, f"the action names no {c.key!r} (scope needs {c.describe()})"
    for v in values:
        if c.op in NUMERIC_OPS:
            n = _number(v)
            ok = n is not None and {"<=": n <= c.bound, ">=": n >= c.bound, "==": n == c.bound,
                                    "<": n < c.bound, ">": n > c.bound}[c.op]
        else:
            hit = any(fnmatch.fnmatchcase(v.casefold(), p) for p in c.patterns)
            ok = hit if c.op == "glob" else not hit
        if not ok:
            return False, f"{c.key}={v!r} is outside {c.describe()}"
    return True, ""


def scope_matches(scope: Scope, attrs: Dict[str, List[str]]) -> Tuple[bool, str]:
    for c in scope.conditions:
        ok, why = condition_holds(c, attrs)
        if not ok:
            return False, why
    return True, ""


def expiry_of(value: object) -> Optional[datetime]:
    """The first moment the row no longer counts, in host-local naive time; raises on a bad value.
    A date alone runs through the end of that day."""
    if isinstance(value, dict):
        value = value.get("end") or value.get("start")
    m = DATE_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"unreadable expiry {value!r}")
    if m.group(2):
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
    return datetime.combine(date.fromisoformat(m.group(1)), datetime.min.time()) + timedelta(days=1)


def validity(row: Approval, owner: str, now: datetime) -> Tuple[bool, str]:
    """(counts, why not). Counts only when Active, set Active by the owner after the last change
    to its terms, not expired, and its scope parses."""
    if not owner:
        return False, "the owner's identity is unknown"
    status = row.get("status")
    if fold(status.value or "") != fold(ACTIVE):
        return False, f"status is {status.value or 'empty'}, not {ACTIVE}"
    if status.by != owner:
        return False, "Status was not set Active by the owner"
    for name in TERMS:
        c = row.get(name)
        if c.value in (None, "") or c.by == owner:
            continue
        if c.updated is None or status.updated is None or c.updated > status.updated:
            return False, f"{name} was changed by someone else after the owner activated it"
    if not fold(row.text("kind")):
        return False, "no Kind"
    expires = row.get("expires").value
    if expires not in (None, ""):
        try:
            if now >= expiry_of(expires):
                return False, f"expired ({expires if not isinstance(expires, dict) else expires.get('start')})"
        except ValueError as e:
            return False, str(e)
    try:
        scope = parse_scope(row.text("scope"))
    except ScopeError as e:
        return False, f"scope does not parse: {e}"
    if scope.any and not scope.explicit_any and row.created_by != owner:
        # A cleared cell leaves no author behind, so only the owner's own row may mean "any" by omission.
        return False, "empty scope on a row the owner did not create (write `any` to approve any target)"
    return True, ""


def find_cover(rows: Iterable[Approval], kind: str, attrs: Dict[str, List[str]], owner: str,
               now: datetime) -> Tuple[Optional[Approval], List[str]]:
    """(the first row that covers this action, why each same-kind row did not)."""
    misses: List[str] = []
    for row in rows:
        if fold(row.text("kind")) != fold(kind):
            continue
        ok, why = validity(row, owner, now)
        if ok:
            ok, why = scope_matches(parse_scope(row.text("scope")), attrs)
        if ok:
            return row, misses
        misses.append(f"{row.id}: {why}")
    return None, misses
