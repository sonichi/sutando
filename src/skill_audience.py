#!/usr/bin/env python3
"""Who a skill is for, and whether this install may link it: the one decision every installer calls.

A skill's `manifest.json` `audience` is "user" (the default, also when the field or the manifest is
absent) or "developer". Any other value, or a manifest that cannot be read, counts as developer:
a typo must keep a developer skill away from users, never ship it to them. Developer skills are
linked only where sutando config sets `skills.developer` to the JSON boolean `true`.

Installers never delete an already-installed developer skill; they leave it and say so.

CLI (for shell installers): `skill_audience.py excluded <skill_dir>...` prints `<name>\t<reason>`
for each directory this install must not link, honouring the config opt-in. Exit 0 always on a
successful decision; non-zero only when the decision itself could not be made.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

USER = "user"
DEVELOPER = "developer"
AUDIENCES = (USER, DEVELOPER)


class Audience(NamedTuple):
    value: str
    reason: Optional[str]


def audience(skill_dir) -> Audience:
    """The skill's audience; `reason` explains a developer verdict that was not spelled out."""
    mf = Path(skill_dir) / "manifest.json"
    if not mf.exists():
        return Audience(USER, None)
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Audience(DEVELOPER, f"manifest.json unreadable ({type(exc).__name__}); treated as developer-only")
    if not isinstance(data, dict):
        return Audience(DEVELOPER, "manifest.json is not an object; treated as developer-only")
    if "audience" not in data:
        return Audience(USER, None)
    val = data["audience"]
    if val == USER:
        return Audience(USER, None)
    if val == DEVELOPER:
        return Audience(DEVELOPER, "developer-only")
    return Audience(DEVELOPER, f"unknown audience {val!r}; treated as developer-only")


def developer_opt_in(repo_root: Optional[Path] = None) -> bool:
    """`skills.developer` from merged sutando config (`resolve_skills_developer`)."""
    src = str(Path(__file__).resolve().parent)
    if src not in sys.path:
        sys.path.insert(0, src)
    from sutando_config import resolve_skills_developer  # noqa: PLC0415

    return resolve_skills_developer(repo_root)


def installable(skill_dir, developer: bool) -> Audience:
    """Audience(value, reason); the skill may be linked here iff `reason` is None."""
    a = audience(skill_dir)
    if a.value == USER or developer:
        return Audience(a.value, None)
    return a


def excluded(skill_dirs: Iterable, developer: bool) -> list:
    """[(name, reason)] for each directory this install must not link."""
    out = []
    for d in skill_dirs:
        a = installable(d, developer)
        if a.reason is not None:
            out.append((Path(d).name, a.reason))
    return out


def main(argv: list, repo_root: Optional[Path] = None) -> int:
    if not argv or argv[0] != "excluded":
        print("usage: skill_audience.py excluded <skill_dir>...", file=sys.stderr)
        return 2
    for name, reason in excluded(argv[1:], developer_opt_in(repo_root)):
        print(f"{name}\t{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
