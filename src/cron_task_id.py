"""Canonical naming contract for a cron job's task id and result filename.

The writer (`cron-runner.py`) slugifies a configured job name before building
`task-cron-<slug>-<epoch_ms>`; any reader that globs the RAW name silently
finds nothing for every name needing slugification. Both sides bind here.
"""
from __future__ import annotations

import re

_NON_SLUG = re.compile(r"[^A-Za-z0-9_-]+")
_RUNS = re.compile(r"-{2,}")

#: Every result filename the writer produces starts with this.
TASK_PREFIX = "task-cron-"

#: Safe literal glob for discovery: never interpolate a job name into a
#: pattern — `a/b` becomes a path separator and `*`/`?` become wildcards.
DISCOVERY_GLOB = TASK_PREFIX + "*"


# Measured over 2056 live+archived records: `.txt`, `-late-duplicate.txt`, and
# `.no-task.<stamp>.txt`. An unknown suffix fails closed (record not counted).
RECORD_SUFFIX = r"(?:-late-duplicate)?(?:\.no-task\.\d+)?\.txt"

# The archiver appends its OWN stamp, so an archived record carries two: the
# emit stamp (ms) then the archive stamp (s). Both halves demand >=10 digits so
# a job named `<name>-2` still cannot pass its `2` off as this job's stamp.
STAMP = r"(?:\d{10,}-\d{10,}|\d+)"


def sanitize_name(name: str) -> str:
    """Slugify a cron name for use in a task id and filename.

    Replaces any character that is not alphanumeric, '-', or '_' with '-',
    then collapses consecutive '-' and strips leading/trailing '-'.
    """
    slug = _NON_SLUG.sub("-", name)
    slug = _RUNS.sub("-", slug).strip("-")
    return slug or "unnamed"


def task_id(name: str, stamp: int) -> str:
    """The writer's task id: the one place this string is spelled."""
    return f"{TASK_PREFIX}{sanitize_name(name)}-{stamp}"


def record_matcher(name: str) -> "re.Pattern[str]":
    """Match exactly this job's result files, rejecting prefix neighbours.

    Anchored on both ends: a bare `<slug>-` prefix test also accepts
    `<slug>-extra-<stamp>`, so a neighbouring job would vouch for this one.

    The post-stamp grammar is the three suffixes records actually carry, so a
    neighbour like `<slug>-2-late` cannot pass its `2` off as this job's stamp.

    An ARCHIVED record carries a second stamp (`<emit-ms>-<archive-s>`); both
    halves must then be >=10 digits, which is what keeps `<slug>-2` out.
    """
    slug = re.escape(sanitize_name(name))
    return re.compile(
        rf"^{re.escape(TASK_PREFIX)}{slug}-{STAMP}{RECORD_SUFFIX}$"
    )
