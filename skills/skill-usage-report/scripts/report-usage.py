#!/usr/bin/env python3
"""Drain the local skill-usage log and report it to AG2 Cloud (AU#93).

Reads <workspace>/state/skill-usage-log.jsonl (written by hooks/log-usage.py),
aggregates per slug, and POSTs one batched report to
POST {cloud}/api/skills/usage as {agentId, events:[{slug, count, lastUsedAt}]}.

Graceful degrade (exit 0, log left in place) when: log empty/missing, no
AG2_CLOUD_TOKEN in the vault, no agent identity, network/HTTP failure. The
log is renamed to a .reporting file BEFORE the POST so events arriving during
the report are never lost; on failure the .reporting content is folded back.

The endpoint only accepts usage for skills currently equipped to this agent
(skill_assignments rows) — everything else comes back in `skipped`. That is
expected until the owner equips skills to this agent's mxid.

Identity: agentId = $AGENT_MXID (the ag2space identity, same key the
EquipPanel / assignments API use).

Cloud origin is declared in this skill's manifest.json `config` block (the
config-only manifest convention in skills/MANIFEST.md) and resolved as
`$AG2_CLOUD_ORIGIN override > manifest default > CLOUD_FALLBACK`.
"""

import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CLOUD_ENV_NAME = "AG2_CLOUD_ORIGIN"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifest.json"
# Last-resort only. The manifest's `config` block is the declared source of
# truth for this default (skills/MANIFEST.md); this constant exists so an
# unreadable/corrupt manifest degrades to the historical value instead of
# crashing a cron-invoked reporter. If the two ever disagree, the manifest wins.
CLOUD_FALLBACK = "https://sutando.ag2.ai"
MAX_EVENTS = 100  # server cap per report


def _manifest_default(
    key: str = CLOUD_ENV_NAME, manifest_path: Path = MANIFEST_PATH
) -> "str | None":
    """Read a declared config default out of this skill's manifest.

    Mirrors skills/proactive-loop/scripts/self-development-enabled.py — the
    in-repo precedent for a pipeline script consuming a config-only manifest.
    Any read/parse problem returns None so the caller can fall back rather than
    raise: this runs from cron.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config = manifest.get("config", {})
        if not isinstance(config, dict):
            return None
        value = config.get(key)
    except (OSError, ValueError, TypeError):
        return None
    return str(value) if value is not None else None


def resolve_cloud_origin(
    environ: "dict[str, str] | None" = None, manifest_path: Path = MANIFEST_PATH
) -> str:
    """Resolve the cloud origin per the skill-config precedence.

    env override > manifest `config` default > CLOUD_FALLBACK.

    An env var set to the empty string is treated as UNSET rather than as an
    override to "" — an empty origin would build a garbage URL, and the shell
    idiom `AG2_CLOUD_ORIGIN= cmd` reads as "leave it alone", not "use nothing".
    """
    env = os.environ if environ is None else environ
    override = env.get(CLOUD_ENV_NAME)
    if override:
        return override
    declared = _manifest_default(CLOUD_ENV_NAME, manifest_path)
    if declared:
        return declared
    return CLOUD_FALLBACK


CLOUD = resolve_cloud_origin()


def _skill_dir() -> Path:
    return Path(os.path.realpath(__file__)).parents[1]


sys.path.insert(0, str(_skill_dir()))
from usage_lock import claim_lock as _claim_lock  # noqa: E402
from usage_lock import reporter_run_lock  # noqa: E402


def repo_root() -> Path:
    return Path(os.path.realpath(__file__)).parents[3]


def _renderable(ts: int) -> bool:
    """Can datetime actually format this timestamp?

    Called INSIDE the per-record guard, and that placement is the whole point.
    `int(rec["ts"])` happily accepts an arbitrarily large integer, but the
    `datetime.fromtimestamp()` that renders it runs AFTER the claim is taken
    below. An out-of-range value therefore raised past the guard and exited
    nonzero with the claim stranded — so every later run re-folded the same
    poison record and re-stranded it, and valid usage never drained again. One
    corrupt byte disabled the reporter permanently.

    (Deliberately no literal call syntax for the claim step in this docstring:
    the claim-lock suite locates that step by scanning this file's source, and
    prose shaped like code is indistinguishable from code to a substring scan.)

    Repro (#2180 review, reproduced before this fix):
        {"slug": "corrupt-ts", "ts": 999999999999999999999999999}
        -> OverflowError: timestamp out of range for platform time_t
        -> ACTIVE_EXISTS=False, PENDING_EXISTS=True, exit nonzero

    Validating here demotes it to an ordinary malformed record: skipped, like a
    bad slug or a bad count, with the claim released normally.
    """
    try:
        datetime.fromtimestamp(ts, tz=timezone.utc)
        return True
    except (OverflowError, OSError, ValueError):
        return False


def _report(ws: Path, log: Path, pending: Path) -> int:
    """The whole report, run while holding the reporter-run lock."""

    # Recover a previously claimed log (failed or crashed mid-report) by
    # folding it back INTO the active log — the same append direction as
    # fold_back below, and the direction that is safe against concurrent
    # async hook appends. The previous sequence (read active → unlink active
    # → rename pending over it) destroyed any hook append that landed between
    # the read and the unlink, and clobbered a fresh log created after the
    # unlink (review race — real once the hook went async). Here the ACTIVE
    # log is never unlinked or renamed-over: hook appends and this fold both
    # use O_APPEND line writes, so they interleave without loss. open("a")
    # creates the log if absent (the pending-with-no-active crash case). A
    # crash between the append and the unlink re-folds the same events next
    # run — at-least-once; duplicate usage counts are preferred to loss.
    if pending.exists():
        with log.open("a", encoding="utf-8") as out, pending.open("r", encoding="utf-8") as old:
            out.write(old.read())
        pending.unlink()
    if not log.exists() or log.stat().st_size == 0:
        print("usage-report: nothing to report")
        return 0

    agent = os.environ.get("AGENT_MXID", "").strip()
    if not agent:
        print("usage-report: AGENT_MXID not set — skipping (log kept)")
        return 0
    try:
        from vault_intercept import get_vault_key  # type: ignore

        token = get_vault_key("AG2_CLOUD_TOKEN")
    except Exception:
        print("usage-report: no AG2_CLOUD_TOKEN in vault — skipping (log kept)")
        return 0

    # Claim under the shared lock. The lock is held ONLY across the rename, not
    # across the POST below: the hook takes the same lock around its open+write,
    # so holding it through a 20s HTTP round trip would put that latency on a
    # PostToolUse hook. Serialising the rename is enough — a hook either
    # completes its append before the claim, or starts after it and opens the
    # fresh active log. Neither can write into the claimed inode. (#2180 review:
    # a hook fd opened pre-rename could otherwise write after our read-to-EOF and
    # before the unlink, and the record was destroyed.)
    with _claim_lock(log, blocking=True) as locked:
        if not locked:
            # Someone is mid-append. Nothing is lost by reporting on the next
            # run, so leave the active log exactly where it is.
            print("usage-report: log busy — skipping this run (log kept)")
            return 0
        log.rename(pending)
    counts: dict[str, int] = defaultdict(int)
    last: dict[str, int] = defaultdict(int)
    with pending.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                slug, ts = rec["slug"], int(rec["ts"])
                # slug becomes a dict key and a report payload — anything but a
                # non-empty string is malformed (a list slug raised unhashable
                # at aggregation time, stranding the claim — review round 4).
                if not isinstance(slug, str) or not slug:
                    continue
                # Range-check ts HERE, not at render time — see _renderable().
                # Every ts that reaches `last` is validated, so the max() below
                # is renderable too and the events comprehension cannot raise.
                if not _renderable(ts):
                    continue
                # count is reporter-authored (fold_back) but must stay inside
                # the malformed-record guard: a bad value would otherwise raise
                # after log.rename(pending) and strand the claim. Malformed →
                # default 1 (keep the event) rather than drop the record.
                try:
                    count = max(1, int(rec.get("count", 1)))
                except Exception:
                    count = 1
                # Aggregation lives INSIDE the guard so no malformed shape can
                # raise after log.rename(pending) — the whole per-record unit
                # either lands or is skipped.
                counts[slug] += count
                last[slug] = max(last[slug], ts)
            except Exception:
                continue

    events = [
        {
            "slug": slug,
            "count": counts[slug],
            "lastUsedAt": datetime.fromtimestamp(last[slug], tz=timezone.utc).isoformat(),
        }
        for slug in counts
    ]
    if not events:
        pending.unlink()
        print("usage-report: log had no parseable events")
        return 0

    def fold_back(remaining: list) -> None:
        # Persist unsent slugs back into the ACTIVE log as count-carrying
        # records (the aggregator honors "count"), then release the claim.
        # Appending keeps any events that arrived mid-report; order is
        # irrelevant — aggregation is commutative.
        with log.open("a", encoding="utf-8") as f:
            for ev in remaining:
                f.write(
                    json.dumps(
                        {
                            "slug": ev["slug"],
                            "ts": int(datetime.fromisoformat(ev["lastUsedAt"]).timestamp()),
                            "count": ev["count"],
                        }
                    )
                    + "\n"
                )
        pending.unlink()

    accepted = skipped = sent = 0
    for i in range(0, len(events), MAX_EVENTS):
        chunk = events[i : i + MAX_EVENTS]
        req = urllib.request.Request(
            f"{CLOUD}/api/skills/usage",
            data=json.dumps({"agentId": agent, "events": chunk}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.load(resp)
        except Exception as e:
            fold_back(events[i:])
            print(
                f"usage-report: POST failed ({e}) after {sent} slug(s) — "
                f"{len(events) - i} unsent slug(s) folded back for retry"
            )
            return 0
        accepted += int(body.get("accepted") or 0)
        skipped += int(body.get("skipped") or 0)
        sent += len(chunk)

    pending.unlink()
    print(f"usage-report: {sent} slug(s) reported — accepted={accepted} skipped={skipped}")
    return 0


def main() -> int:
    root = repo_root()
    sys.path.insert(0, str(root / "src"))
    try:
        from workspace_default import resolve_workspace  # type: ignore

        ws = Path(resolve_workspace())
    except Exception as exc:
        # No ad-hoc fallback: `root / "workspace"` would defeat configured
        # resolution and read/create a second telemetry store inside the
        # checkout (#2180 review). resolve_workspace() already defaults to
        # <repo>/workspace/ when nothing is configured, so a local fallback can
        # only DISAGREE with it, never add capability.
        #
        # Exit 0 with state untouched: this runs from a cron/hook context where a
        # non-zero exit is noise, and there is nothing to report if we cannot
        # find the log. Print to stderr so the reason is visible rather than
        # silent.
        print(f"report-usage: workspace unresolved ({exc}) — nothing reported",
              file=sys.stderr)
        return 0

    log = ws / "state" / "skill-usage-log.jsonl"
    pending = log.with_suffix(".jsonl.reporting")

    # Exclude a SECOND reporter for the WHOLE run, POST included. Distinct file
    # from the hook's claim lock, so holding it this long never makes a hook
    # wait (#2180 review, second [P1]): without it, reporter B mistook reporter
    # A's live `.reporting` for a crashed run, re-posted the same events, and
    # unlinked A's claim — leaving A to fail its own `pending.unlink()` and exit
    # 1. Holding it also makes the crash-recovery branch below sound: while we
    # hold this, a `.reporting` on disk genuinely IS orphaned.
    with reporter_run_lock(log) as owned:
        if not owned:
            # Another report is in flight. Exit 0 and let the next scheduled run
            # do the work — queueing behind a network POST is worse than waiting
            # for the next tick, and nothing is lost by reporting later.
            print("usage-report: another report is in flight — skipping this run")
            return 0
        return _report(ws, log, pending)


if __name__ == "__main__":
    sys.exit(main())
