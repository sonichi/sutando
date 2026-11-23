#!/usr/bin/env python3
"""Which workers a pool roster counts as members: each key of its `workers` mapping whose row is not
`state: "retired"` (a row that is not a mapping is a member). The one statement of that rule.

Dependency-free, so core code and the worker-pool skill share it without core importing the skill.
Reading the roster, and what to do when it is unreadable or mis-shaped, stays with each caller:
`members` takes a mapping and raises like any mapping access when handed something else.
"""
from __future__ import annotations

from typing import Mapping

RETIRED = "retired"


def is_member(row) -> bool:
    """A roster row counts unless it is a mapping whose `state` is retired."""
    return not (isinstance(row, Mapping) and row.get("state") == RETIRED)


def members(workers: Mapping) -> set:
    """The worker ids of `workers` (a roster's `workers` mapping) that are members."""
    return {wid for wid, row in workers.items() if is_member(row)}
