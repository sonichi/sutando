"""Migration fencing: single-protocol-per-epoch (seam doc §4).

One logical item is interpreted by exactly ONE claim protocol within an
epoch. Migration = lock out drainers -> one-shot convert (each item
individually atomic) -> write the version fence -> start only the new
drainer. The fence is written LAST: a crash anywhere mid-conversion leaves
the fence at the old epoch, so the old protocol stays authoritative and no
item is ever interpreted by both protocols.

Legacy delivered-sentinels map conservatively: a sentinel written after the
provider call returned is evidence, not proof (the crash window between
API-return and sentinel-write means its absence proves nothing and its
presence only witnesses the call returned). Only a durable provider receipt
reference upgrades to CONFIRMED; anything else converts to OUTCOME_UNKNOWN
for park/reconcile — never CONFIRMED (seam doc §4, Discord's sentinel).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Optional

from .contract import DeliveryOutcome

EPOCH_FILE = "protocol-epoch"
DEFAULT_EPOCH = "A"


def read_epoch(root: Path) -> str:
    try:
        return (Path(root) / EPOCH_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return DEFAULT_EPOCH


def write_fence(root: Path, epoch: str) -> None:
    p = Path(root) / EPOCH_FILE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(epoch, encoding="utf-8")
    os.replace(tmp, p)


def _stage(c, path: Path, data: bytes) -> None:
    """Publish `path` by rename, never by writing the final name in place.
    A crash mid-write otherwise leaves a truncated file that every
    `if not path.exists()` idempotence check reads as already-written.
    Staged inside C's tmp/ under a unique name: a leftover beside the
    published names would both block the retry and satisfy verification."""
    import time as _time
    from .backend_c import TMP as _TMP
    tmp = c._d(_TMP) / f"import{os.getpid()}.{_time.time_ns()}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        off = 0
        while off < len(data):
            off += os.write(fd, data[off:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _same_bytes(path: Path, payload: bytes) -> bool:
    try:
        return path.read_bytes() == payload
    except OSError:
        return False


def _retire_budget(ap: Path) -> None:
    """A terminal ends the cycle, so its attempt budget dies with it — C does
    this on completion, and an imported terminal must not leave one live."""
    ap.unlink(missing_ok=True)


def _reconcile_marker(c, marker: Path, payload: bytes, key: str,
                      report: dict, conflicts: list, counter: str) -> None:
    """Stage `payload` at `marker`, or reconcile with what is already there.
    A marker holding DIFFERENT bytes is an earlier cycle's body, so skipping it
    would fence C on the stale payload; that is a conflict, not a completed import."""
    if not marker.exists():
        _stage(c, marker, payload)
        report[counter] += 1
    elif _same_bytes(marker, payload):
        report["skipped"] += 1
    else:
        conflicts.append(key)


def _receipt_matches(records, rec) -> bool:
    """A terminal record proves THIS A record migrated only if it carries
    that record's receipt. Otherwise C is holding an earlier cycle's."""
    want = (rec.get("provider"), rec.get("destination"))
    for r in records:
        got = r.get("receipt") or {}
        if (got.get("provider"), got.get("destination")) == want:
            return True
    return False


def _attempts_value(ap: Path) -> Optional[int]:
    """The count an attempts file holds, or None when absent or unreadable.
    A file that merely PARSES can still hold a previous cycle's number."""
    try:
        return int(ap.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def classify_legacy_sentinel(
        receipt_ref: Optional[str]) -> DeliveryOutcome:
    """Sentinel-mapping table (normative). receipt_ref is the provider's
    durable receipt reference when the legacy record carries one; a bare
    sentinel (written after the call, no receipt) is OUTCOME_UNKNOWN."""
    if receipt_ref:
        return DeliveryOutcome.CONFIRMED
    return DeliveryOutcome.OUTCOME_UNKNOWN


def convert_item_atomic(path: Path, render: Callable[[bytes], bytes],
                        is_converted: Callable[[bytes], bool],
                        fault: Optional[Callable[[str], None]] = None) -> bool:
    """The per-item conversion primitive: read -> write temp -> ONE
    os.replace over the SAME path. There is no unlink step and no second
    visible name, so no crash point can leave old and new both visible.
    Idempotent: an already-converted item is left untouched (False), which
    is what lets a restarted migrator resume mid-pass. `fault` is the
    fault-injection hook — called before each internal mutation with a
    step label; tests raise from it to crash INSIDE the item."""
    data = path.read_bytes()
    if is_converted(data):
        return False
    tmp = path.with_name(path.name + ".tmp")
    if fault:
        fault("pre-write-tmp")
    tmp.write_bytes(render(data))
    if fault:
        fault("pre-replace")
    os.replace(tmp, path)
    if fault:
        fault("post-replace")
    return True


def convert_epoch(root: Path, items: Iterable[str],
                  convert_one: Callable[[str], None], target_epoch: str,
                  crash_after: Optional[int] = None) -> int:
    """One-shot conversion pass. convert_one(item) must be per-item atomic
    over a single path (use convert_item_atomic — one os.replace, no
    unlink, no second visible name) and idempotent, so a restarted pass
    resumes over the same item list and completes BEFORE any drainer
    starts: the fence is written only after ALL items converted, and until
    then read_epoch still names the old protocol. crash_after=N simulates
    a crash before the (N+1)th item; intra-item crash points are the
    convert_item_atomic fault hook."""
    done = 0
    for item in items:
        if crash_after is not None and done >= crash_after:
            raise RuntimeError("simulated crash mid-conversion")
        convert_one(item)
        done += 1
    write_fence(root, target_epoch)
    return done


def import_a_state(root: Path) -> dict:
    """Idempotent A->C import of one outbox root, run in a QUIESCE window
    (no drainer of either protocol may be running).

    Mapping (ruling's table): READY/QUEUED unclaimed -> C ready/; PARKED ->
    C undelivered/ with A's reason; DELIVERED -> a C terminal record carrying
    A's persisted receipt (imported=True, with a 2-part pseudo-incarnation —
    there is no live claim to bind); any CLAIMED non-terminal item -> C undelivered/ as
    import-outcome-unknown (reconcile territory: the claim's fate is exactly
    as unknowable as a mid-delivery crash). Attempt counts carry over.

    Originals are PRESERVED: after every item converts and the per-state
    counts verify, .items is renamed to .items-migrated (rollback = rename
    back) and the epoch fence is written LAST — a crash anywhere earlier
    leaves .items and the A fence intact, and a re-run resumes.
    Returns per-state counts plus verified/fence flags.
    """
    import json as _json
    import time as _time

    from .backend_c import (SEP, DesignCClaimBackend, _safe_component,
                            _safe_key, read_terminal_records)

    root = Path(root)
    items_dir = root / ".items"
    migrated_dir = root / ".items-migrated"
    conflicts: list = []
    report = {"ready": 0, "parked": 0, "delivered": 0, "unknown": 0,
              "skipped": 0, "verified": False, "fenced": False}
    # Import is defined ONLY for epoch A. Already-C is an explicit no-op;
    # any other or unreadable epoch is another protocol's state — fail closed.
    try:
        epoch = read_epoch(root)
    except (OSError, ValueError):
        report["unmigratable"] = "epoch fence unreadable"
        return report
    if epoch == "C":
        report["noop"] = "root is already C"
        report["verified"] = True
        report["fenced"] = True
        return report
    if epoch != "A":
        report["unmigratable"] = f"epoch {epoch!r} is not A"
        return report
    if items_dir.is_symlink():
        # A symlink-to-directory passes is_dir(): migrating THROUGH it would
        # fence this root against state that lives somewhere else. Fail closed.
        report["unmigratable"] = ".items is a symlink"
        return report
    if not items_dir.is_dir():
        if os.path.lexists(items_dir):
            # A FILE or dangling symlink at .items is ambiguous A-side state,
            # not proof of absence. Fail closed; nothing is fenced.
            report["unmigratable"] = ".items exists but is not a directory"
            return report
        if os.path.lexists(migrated_dir) and (
                migrated_dir.is_symlink() or not migrated_dir.is_dir()):
            # A symlinked or non-directory rollback namespace is not evidence
            # the rename completed — ambiguous state fails closed, unfenced.
            report["unmigratable"] = ".items-migrated exists but is not a real directory"
            return report
        if migrated_dir.is_dir() and read_epoch(root) != "C":
            # Rename-to-fence crash window: re-verify against the preserved
            # copies, then finish the fence.
            items_dir = migrated_dir
            resume_rename_done = True
        else:
            # Genuinely clean root (nothing to import, nothing half-moved):
            # complete the activation INTENTIONALLY so the epoch is decided.
            from .backend_c import DesignCClaimBackend
            DesignCClaimBackend(root, activate=True)
            write_fence(root, "C")
            report["verified"] = True
            report["fenced"] = True
            return report
    else:
        resume_rename_done = False
    c = DesignCClaimBackend(root, activate=True)
    # A's key fn, distinct from C's _safe_key in scope: A-era filenames must
    # be checked with A's encoding; kept local to avoid a legacy module dep.
    from ag2_sparrow.outbox import _safe_key as _a_key
    for f in sorted(items_dir.glob("*.json")):
        # Every A record must be a real local regular file whose name binds
        # to its body's item_id — a symlink or renamed record is not imported.
        if f.is_symlink() or not f.is_file():
            key = _safe_key(f.stem)
            marker = c._d("undelivered") / f"{key}{SEP}import-invalid-file{SEP}import"
            # Presence-only ON PURPOSE: the payload is a constant, so an existing
            # marker cannot be holding a different cycle's body.
            if not marker.exists():
                _stage(c, marker, b"")
                report["unknown"] += 1
            else:
                report["skipped"] += 1
            continue
        try:
            rec = _json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        if (not isinstance(rec, dict) or "item_id" not in rec
                or not isinstance(rec["item_id"], str)):
            key = _safe_key(f.stem)
            marker = c._d("undelivered") / f"{key}{SEP}import-unreadable{SEP}import"
            _reconcile_marker(c, marker,
                              f.read_bytes() if f.exists() else b"",
                              key, report, conflicts, "unknown")
            continue
        if f.stem != _a_key(rec["item_id"]):
            key = _safe_key(f.stem)
            marker = c._d("undelivered") / f"{key}{SEP}import-name-mismatch{SEP}import"
            _reconcile_marker(c, marker, f.read_bytes(),
                              key, report, conflicts, "unknown")
            continue
        item_id = rec["item_id"]
        key = _safe_key(item_id)
        status = rec.get("status", "QUEUED")
        payload = rec.get("payload", "").encode("utf-8")
        n = int(rec.get("attempts", 0) or 0)
        ap = c._attempts_path(key)
        # Bind to A's CURRENT count, not to the file parsing: a stale or
        # truncated file otherwise becomes this cycle's budget. Absent reads 0.
        if not n:
            ap.unlink(missing_ok=True)
        elif _attempts_value(ap) != n:
            _stage(c, ap, str(n).encode())
        from . import contract as _contract  # noqa: F401 — outcome names below
        import ag2_sparrow.outbox as _outbox
        claim = _outbox.read_delivery_claim(root, item_id)
        if status == "DELIVERED":
            dst = c._terminal_path(key)
            if dst.exists():
                # A colliding NAME is not membership: only a record passing
                # C's total validator for THIS id counts; else fail closed.
                _recs = read_terminal_records(root, item_id)
                if _recs:
                    # Presence proves SOME cycle imported this key, not
                    # THIS A record; a republish would serve the old receipt.
                    if _receipt_matches(_recs, rec):
                        _retire_budget(ap)
                        report["skipped"] += 1
                    else:
                        conflicts.append(key)
                    continue
                report["unmigratable"] = (
                    f"terminal collision for {key} is not valid proof")
                return report
            # Normative: a bare sentinel (no durable receipt) is
            # OUTCOME_UNKNOWN, never upgraded to a confirmed terminal.
            _ref = (rec.get("destination")
                    if rec.get("provider") and rec.get("destination") else None)
            if classify_legacy_sentinel(_ref) is not DeliveryOutcome.CONFIRMED:
                marker = c._d("undelivered") / f"{key}{SEP}outcome-unknown{SEP}import"
                _reconcile_marker(c, marker, payload,
                                  key, report, conflicts, "unknown")
                continue
            c._write_terminal(key, {
                "schema": 1, "item_id": item_id, "outcome": "confirmed",
                "receipt": {"provider": rec.get("provider"),
                             "destination": rec.get("destination")},
                "completed_ns": _time.time_ns(), "worker": "a-import",
                # 2-part pseudo-incarnation: binds id+worker for the total
                # validator, can never equal a real 5-part claim filename.
                "attempts": n, "imported": True,
                "incarnation": f"{key}{SEP}{_safe_component('a-import')}",
            }, f"a-import{SEP}{key}")
            _retire_budget(ap)
            report["delivered"] += 1
        elif status == "PARKED":
            reason = _safe_component(str(rec.get("reason") or "parked")[:40])
            marker = c._d("undelivered") / f"{key}{SEP}{reason}{SEP}import"
            _reconcile_marker(c, marker, payload,
                              key, report, conflicts, "parked")
        elif claim is not None:
            marker = c._d("undelivered") / f"{key}{SEP}import-outcome-unknown{SEP}import"
            _reconcile_marker(c, marker, payload,
                              key, report, conflicts, "unknown")
        else:
            rp = c._d("ready") / key
            if rp.exists():
                if _same_bytes(rp, payload):
                    report["skipped"] += 1
                else:
                    conflicts.append(key)
            elif c.publish(item_id, payload):
                report["ready"] += 1
            else:
                report["skipped"] += 1       # tokens present: C already owns it
    # Verify by MEMBERSHIP: every A item is represented somewhere in C.
    missing = []
    for f in sorted(items_dir.glob("*.json")):
        # Same classification as the conversion pass: any record it would
        # quarantine verifies under the quarantine key, never its raw value.
        try:
            rec = _json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        if (isinstance(rec, dict) and isinstance(rec.get("item_id"), str)
                and f.stem == _a_key(rec["item_id"])):
            k = _safe_key(rec["item_id"])
            # Terminal membership is SEMANTIC — the total validator, not a
            # name at the terminal path (a corrupt collision is not proof).
            has_terminal = bool(read_terminal_records(root, rec["item_id"]))
        else:
            k = _safe_key(f.stem)
            has_terminal = False    # quarantined records never map to terminals
        from .backend_c import is_producer_token

        def _valid_marker(e):
            parts = e.name.split(SEP)
            return parts[0] == k and len(parts) == 3
        # Tokens count as C ownership ONLY in the full claim grammar — a
        # prefix-matching junk name must fail verification, not satisfy it.
        raw_tokens = c._tokens(k)
        valid_tokens = [t for t in raw_tokens
                        if is_producer_token(t.name)]
        present = ((c._d("ready") / k).exists()
                   or has_terminal
                   or any(_valid_marker(e)
                          for e in c._d("undelivered").iterdir())
                   or any(_valid_marker(e)
                          for e in c._d("archive").iterdir())
                   or valid_tokens)
        if raw_tokens and not valid_tokens:
            report.setdefault("malformed_tokens", []).extend(
                t.name for t in raw_tokens[:3])
            missing.append(k)
            continue
        if not present:
            missing.append(k)
    if conflicts:
        # C holds a DIFFERENT representation for these keys: skipping serves
        # the stale one, and overwriting a receipt is not the importer's call.
        report["conflicts"] = sorted(set(conflicts))[:5]
        return report
    if missing:
        report["missing"] = missing[:5]
        return report                        # fence NOT written; re-run resumes
    report["verified"] = True
    if not resume_rename_done:
        items_dir.rename(migrated_dir)
    write_fence(root, "C")
    report["fenced"] = True
    return report


FALLBACK_COUNTER = "a-fallback-hits.json"


def resolve_delivery(root: Path, item_id: str) -> dict:
    """Audit/re-drive resolver — the ONLY consumers of the migration-window
    fallback (design_a_retirement_dual_read.md). The claim/deliver hot path
    never calls this: a live item C cannot see does not exist.

    C is authoritative: a C terminal record answers outright. Only a C-miss
    on a C-FENCED root consults the preserved A state via dual_read (read-
    only, counted); a root still on epoch A keeps its live A store and gets
    no fallback. Returns {"source": "c"|"a-fallback"|None, "delivered",
    "receipt", "record"}.
    """
    from .backend_c import read_terminal_records

    root = Path(root)
    recs = read_terminal_records(root, item_id)
    if recs:
        r = recs[-1]
        return {"source": "c", "delivered": r.get("outcome") == "confirmed",
                "receipt": r.get("receipt"), "record": r}
    if read_epoch(root) == "C":
        rec = dual_read(root, item_id)
        if rec is not None:
            # Same normative rule as the importer: DELIVERED without a
            # durable receipt is OUTCOME_UNKNOWN, not a delivered answer.
            _ref = (rec.get("destination")
                    if rec.get("provider") and rec.get("destination") else None)
            delivered = (rec.get("status") == "DELIVERED"
                         and classify_legacy_sentinel(_ref)
                         is DeliveryOutcome.CONFIRMED)
            receipt = ({"provider": rec.get("provider"),
                        "destination": rec.get("destination")}
                       if delivered else None)
            return {"source": "a-fallback", "delivered": delivered,
                    "receipt": receipt, "record": rec}
    return {"source": None, "delivered": False, "receipt": None,
            "record": None}


def dual_read(root: Path, item_id: str) -> "dict | None":
    """C-miss fallback during the migration window: read-only lookup of the
    PRESERVED A record under .items-migrated/. Returns the parsed record or
    None. The first consult on a root initializes the counter at count 0 (a
    liveness marker: the release gate treats an ABSENT counter as "dual_read
    never ran here", not as a clean zero), and every hit bumps it — A's
    deletion gate is that counter staying at a measured zero across a full
    release, so a hit is a FINDING (an id the importer should have covered),
    not a silent rescue. Never writes to either item store."""
    import json as _json
    import time as _time

    from ag2_sparrow.outbox import _safe_key as _a_safe_key

    root = Path(root)
    migrated_dir = root / ".items-migrated"
    if migrated_dir.is_symlink() or not migrated_dir.is_dir():
        return None
    counter = root / FALLBACK_COUNTER

    def _counter_rmw(update):
        # One exclusive flock around the whole read-modify-write, and a
        # per-writer tmp name: concurrent dual_read() calls lose no increments.
        import fcntl
        with open(counter.with_suffix(".lock"), "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                prior = _json.loads(counter.read_text(encoding="utf-8"))
                raw = prior.get("count") if isinstance(prior, dict) else None
                # Release-gate integer: fail closed to 0 on any malformed
                # shape (null/bool/str/list/non-finite/negative/oversized).
                count = (raw if isinstance(raw, int) and not isinstance(raw, bool)
                         and 0 <= raw <= 10**12 else 0)
                existed = True
            except FileNotFoundError:
                count, existed = 0, False
            except (OSError, ValueError):
                count, existed = 0, True
            payload = update(count, existed)
            if payload is None:
                return
            tmp = counter.with_suffix(f".{os.getpid()}.{_time.time_ns()}.tmp")
            tmp.write_text(_json.dumps(payload), encoding="utf-8")
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)                 # data durable before it is named
            finally:
                os.close(fd)
            os.replace(tmp, counter)
            dfd = os.open(counter.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)                # the rename itself is durable
            finally:
                os.close(dfd)

    # Liveness marker: count 0 turns "file absent" from an assumed-benign
    # state into "dual_read never ran here", which the release gate warns on.
    _counter_rmw(lambda count, existed: None if existed else
                 {"count": 0, "initialized_ts": _time.time()})
    p = migrated_dir / f"{_a_safe_key(item_id)}.json"
    if p.is_symlink() or not p.is_file():
        return None
    try:
        rec = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("item_id") != item_id:
        return None
    _counter_rmw(lambda count, existed:
                 {"count": count + 1, "last_hit_ts": _time.time(),
                  "last_item": item_id})
    return rec
