#!/usr/bin/env python3
"""Named mutants of src/delivery/disposal.py, src/undelivered_quarantine.py
(the no-replace transition's owner), src/outbox_cli.py and the bridge's orphan
arms, so a reviewer can reproduce
"this test kills that mutant" without hand-editing the module.

    python3 tests/_helpers/disposal_mutants.py list
    python3 tests/_helpers/disposal_mutants.py apply <name>     # edits src + vendored copy
    python3 tests/_helpers/disposal_mutants.py revert           # undo the applied mutant

Each mutant is one exact-string substitution; `apply` refuses when the text
is not found exactly once, so a stale mutant is a loud failure, not a no-op.
`revert` undoes the substitution in place (never a git checkout, which would
also discard unrelated edits to the module).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DISPOSAL = [REPO / "src" / "delivery" / "disposal.py",
            REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_disposal.py"]
QUARANTINE = [REPO / "src" / "undelivered_quarantine.py",
              REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "undelivered_quarantine.py"]
# Mutants whose site lives in the quarantine module; every other one edits disposal.
IN_QUARANTINE = {"fallback-links-then-unlinks", "place-replaces-a-taken-name",
                 "place-does-not-retry", "restore-links-then-unlinks", "restore-aside-unlinks",
                 "restore-trusts-the-link"}
CLI = [REPO / "src" / "outbox_cli.py", REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "outbox_cli.py"]
BRIDGE = [REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"]
OUTBOX = [REPO / "src" / "outbox.py", REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "outbox.py"]
DEDUP = [REPO / "src" / "dedup_recovery.py", REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "dedup_recovery.py"]
IN_DEDUP = {"reask-republished", "reask-cleanup-fails-the-publication", "reask-claims-a-colliding-id",
            "reask-claims-a-colliding-id-after-a-race", "reask-id-from-the-clock-alone"}
IN_OUTBOX = {"proof-ignores-its-stamp", "binding-ignores-the-publication", "reader-trusts-the-earlier-digest", "delivered-rule-compares-the-composed-body", "delivered-body-never-differs"}
IN_CLI = {"cli-exits-0-on-no-safe-move", "cli-reads-epoch-after-lock", "cli-parks-on-no-safe-move"}
BACKEND = [REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "delivery_core" / "backend_a.py"]
IN_BACKEND = {"publish-drops-the-source"}
GUARD = [REPO / "src" / "policy" / "egress" / "result.py",
         REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "team_result_guard.py"]
IN_GUARD = {"archive-clobbers-a-decision", "conflicting-record-stays-actionable", "update-ignores-id-ownership", "issue-reissues-a-reserved-id", "record-of-ignores-the-body", "migration-seeds-from-the-live-copy", "archive-outside-the-ledger-lock", "ledger-ignores-prior-records", "artifact-claims-another-body"}
IN_BRIDGE = {"unsent-brings-back-a-send-instruction", "unsent-drops-the-delivered-body-reference", "unsent-ignores-a-changed-generation", "verdict-cache-keyed-by-task", "late-duplicate-hashes-the-raw-bytes", "unsent-skips-the-guard", "unsent-skips-owner-mention",
             "unsent-ignores-suppression", "unsent-hides-its-markers",              "confirmed-archives-another-body", "terminal-delivered-archives-another-body",
             "late-duplicate-archives-another-body", "orphan-links-then-unlinks", "orphan-trusts-any-retirement", "orphan-decodes-privately",
             "archive-by-pathname", "archive-replaced-drops-bookkeeping", "archive-error-retires",
             "archive-fallback-retires",
             "reask-id-per-pass", "reask-published-before-tracked",
             "archive-warning-is-not-placed", "unsent-strands-the-task", "settle-under-a-live-reply",
             "settle-an-undelivered-id", "reask-plan-unlocked",
             "settle-forgets-a-live-task", "settle-never-retried",
             "archive-result-ignores-the-task", "alias-never-retried", "alias-settles-on-its-primary",
             "orphan-moves-before-tracking", "orphan-recovery-sends-before-tracking",
             "track-keeps-a-non-durable-id", "track-exposes-before-durable", "settle-intent-not-restored",
             "archive-moves-before-the-settle-intent", "settle-save-forgets-a-previous-process",
             "ledger-marked-read-before-the-read", "unreadable-ledger-written-over"}
STATE = Path(__file__).with_name(".disposal_mutant_applied")

MUTANTS: dict[str, tuple[str, str, str]] = {
    "live-owner-ages-out": (
        "a live owner's claim is reclaimed on age alone",
        "        return not (c.start and owner.start_usec and c.start != owner.start_usec)\n",
        "        return (not (c.start and owner.start_usec and c.start != owner.start_usec)) "
        "and (time.time() - c.acquired <= CLAIM_MAX_S)\n"),
    "liveness-after-age": (
        "the age bound is checked before liveness",
        "    owner = process_identity(c.pid)\n    if owner.state is OwnerState.DEAD:\n",
        "    owner = process_identity(c.pid)\n    if time.time() - c.acquired > CLAIM_MAX_S:\n"
        "        return False\n    if owner.state is OwnerState.DEAD:\n"),
    "identity-ignores-write-time": (
        "a reused inode with new bytes' write time reads as the same publication",
        "    if (st.st_dev, st.st_ino, st.st_mtime_ns) != (generation.dev, generation.ino, generation.mtime_ns):\n        return False\n    return identity_of(path)[1] == generation\n",
        "    if (st.st_dev, st.st_ino) != (generation.dev, generation.ino):\n        return False\n"
        "    return identity_of(path)[1].digest == generation.digest\n"),
    "restore-registered-after-rename": (
        "the .restore path becomes visible before this process holds it",
        "        ACTIVE_CLAIMS.add(str(restoring))             # registered before it can be seen\n        try:\n            os.rename(claim, restoring)",
        "        try:\n            os.rename(claim, restoring)\n            ACTIVE_CLAIMS.add(str(restoring))"),
    "lock-never-taken": (
        "every transition runs without the directory lock",
        "    path = results_dir / LOCK_NAME\n    deadline = time.monotonic() + LOCK_WAIT_S\n",
        "    held[key] = 1\n    try:\n        yield\n    finally:\n        held[key] = 0\n    return\n"
        "    path = results_dir / LOCK_NAME\n    deadline = time.monotonic() + LOCK_WAIT_S\n"),
    "recovery-ignores-claim-identity": (
        "an abandoned claim is quarantined even when it holds a reply the owner never verified",
        "            verified, _ = _verify_fd(fd, named)         # the body, not a stat of a name\n",
        "            verified = True\n"),
    "recovery-unisolated": (
        "one claim's recovery failure aborts the pass",
        "                except Exception as e:  # noqa: BLE001 - isolation is the point\n",
        "                except DisposalBusy as e:  # noqa: BLE001 - isolation is the point\n"),
    "lock-error-escapes": (
        "a lock the filesystem refuses raises into the drain",
        "    except OSError as e:                                # DisposalBusy, ENOLCK, EACCES, a dir that cannot be read\n",
        "    except DisposalBusy as e:                                # DisposalBusy, ENOLCK, EACCES, a dir that cannot be read\n"),
    "recovery-digest-only": (
        "recovery calls a claim verified when only its bytes match the name",
        "            named = ResultIdentity(os.fstat(fd).st_dev, c.ino, c.mtime_ns, c.digest)\n",
        "            _st = os.fstat(fd)\n            named = ResultIdentity(_st.st_dev, _st.st_ino, _st.st_mtime_ns, c.digest)\n"),
    "put-back-replaces": (
        "the put-back uses a replacing rename, so a retaken canonical name is overwritten",
        "        rename_noreplace(Path(claim), Path(rfile), log)\n    except FileExistsError:\n        return False\n",
        "        os.rename(Path(claim), Path(rfile))\n    except FileExistsError:\n        return False\n"),
    "verify-skips-rewrite-check": (
        "an in-place rewrite of the claimed inode during hashing goes unnoticed",
        "    same = (h.hexdigest() == generation.digest\n            and (again.st_mtime_ns, again.st_size) == (st.st_mtime_ns, st.st_size))\n",
        "    same = h.hexdigest() == generation.digest\n"),
    "missing-dir-not-skipped": (
        "recovery runs against an absent results directory",
        "        if not results_dir.is_dir():\n            return\n        for odd in find_malformed",
        "        for odd in find_malformed"),
    "fallback-links-then-unlinks": (
        "without a kernel primitive the put-back links then unlinks the claim after a stat",
        "    raise FileExistsError(errno.ENOTSUP, \"no no-replace rename on this platform; nothing moved\", str(dst))\n",
        "    os.link(src, dst)\n    sa, sb = os.stat(src), os.stat(dst)\n"
        "    if (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino):\n        os.unlink(src)\n        return\n"
        "    raise FileExistsError(errno.EEXIST, \"retaken\", str(dst))\n"),
    "duplicate-unlinked-after-stat": (
        "a second name of a reply is unlinked after a link-count check",
        "    try:\n        kept = _place(claim, results_dir, stem, log)\n",
        "    try:\n        if os.stat(claim).st_nlink > 1:\n            os.unlink(claim)\n            return\n"
        "        kept = _place(claim, results_dir, stem, log)\n"),
    "quarantine-skips-the-post-move-verify": (
        "the quarantined file is not read again after the move, so a rewrite after verification stays quarantined",
        "                    if _still_is(fd, generation, log, rfile.stem, target):\n",
        "                    if True:\n"),
    "recovery-skips-the-post-move-verify": (
        "recovery does not read the body again after its own move",
        "        if not (c.restore or not verified) and not _still_is(fd, named, log, c.stem, target):\n",
        "        if False:\n"),
    "precheck-outside-the-catch": (
        "an error from the results-dir precheck escapes recovery into the drain",
        "    results_dir = Path(results_dir)\n    try:\n        if not results_dir.is_dir():\n            return\n",
        "    results_dir = Path(results_dir)\n    if not results_dir.is_dir():\n        return\n    try:\n"),
    "place-replaces-a-taken-name": (
        "a quarantine move replaces whatever already holds the chosen name",
        "            move(Path(src), target)\n            return target\n",
        "            os.replace(Path(src), target)\n            return target\n"),
    "place-does-not-retry": (
        "a taken quarantine name is not skipped for a fresh one",
        "            if e.errno not in (None, errno.EEXIST):\n                raise\n    raise FileExistsError(errno.EEXIST, \"no free name\"",
        "            raise\n    raise FileExistsError(errno.EEXIST, \"no free name\""),
    "restore-links-then-unlinks": (
        "restore links the quarantined body to the live name, then unlinks the quarantine copy",
        "        rename_noreplace(found[-1], target)\n    except FileExistsError as e:\n",
        "        os.link(found[-1], target)\n        os.unlink(found[-1])\n    except FileExistsError as e:\n"),
    "restore-aside-unlinks": (
        "without a primitive, restore unlinks the quarantined name instead of renaming it aside",
        "        os.rename(quarantined, aside)\n",
        "        os.unlink(quarantined)\n"),
    "restore-trusts-the-link": (
        "restore reports RESTORED although a producer replaced the live name after the link",
        "    if not _held_by_another(target, aside):\n        return RestoreOutcome.RESTORED, target\n",
        "    if True:\n        return RestoreOutcome.RESTORED, target\n"),
    "retire-recaptures-the-generation": (
        "an orphan retire binds to whatever is at the name when it takes the lock",
        "        with locked(results_dir):\n            done = _retire(",
        "        with locked(results_dir):\n            _, generation = identity_of(rfile)\n"
        "            done = _retire("),
    "missing-destination-is-source-gone": (
        "a destination that is missing reads as nothing left to move",
        "    except _SourceGone:\n        return _unless_replaced(",
        "    except FileNotFoundError:\n        return _unless_replaced("),
    "retire-ignores-a-post-move-replacement": (
        "a reply published at the name after the move is not reported",
        "    if os.path.lexists(rfile):\n        return done._replace(outcome=Retirement.REPLACEMENT_LIVE)\n",
        "    if False:\n        return done._replace(outcome=Retirement.REPLACEMENT_LIVE)\n"),
    "busy-lock-raises": (
        "a busy disposal lock escapes the retirement as an exception",
        "    except DisposalBusy as e:\n        return Retired(",
        "    except ZeroDivisionError as e:\n        return Retired("),
    "retire-lock-error-escapes": (
        "a lock or directory error other than busy escapes the typed retirement",
        "    except OSError as e:\n        if done is None:",
        "    except DisposalBusy as e:\n        if done is None:"),
    "retire-unlock-error-erases-the-outcome": (
        "an unlock failure after a committed move is reported as nothing moved",
        "        if done is None:                              # the lock or the directory itself failed\n",
        "        if True:                              # the lock or the directory itself failed\n"),
    "retire-fallback-reads-as-placed": (
        "a body kept outside the requested directory is reported as placed",
        "    if ended.parent != directory:\n",
        "    if False:\n"),
    "delivered-body-never-differs": (
        "the owner rules every live reply at a delivered id as the one that was sent",
        "    return not isinstance(stored, dict) or stored.get(\"body\") != ready_body\n",
        "    return False and stored.get(\"body\") != ready_body\n"),
    "delivered-rule-compares-the-composed-body": (
        "the delivered-id rule compares composed wire bodies even when the source digest is known",
        "    proof = source_proof(d)\n    if proof:\n",
        "    proof = source_proof(d)\n    if False:\n"),
    "reader-trusts-the-earlier-digest": (
        "the delivered-id rule reinterprets an earlier source_sha256 as a ready-body digest",
        "    proof = source_proof(d)\n    if proof:\n",
        "    proof = source_proof(d) or d.get(\"source_sha256\")\n    if proof:\n"),
    "publish-drops-the-source": (
        "the delivery backend's publish never persists the source digest",
        "                **outbox.source_proof_fields(source_ready_sha256, text, outbox.new_publication_id()),\n",
        "                **outbox.source_proof_fields(None, text, outbox.new_publication_id()),\n"),
    "cli-parks-on-no-safe-move": (
        "requeue parks the record again when the body could not be restored",
        "            _emit(payload, args.json)\n            return 4\n",
        "            outbox.park_item(args.root, args.item_id, \"requeue undone\")\n"
        "            _emit(payload, args.json)\n            return 4\n"),
    "cli-exits-0-on-no-safe-move": (
        "requeue reports success when the body could not be restored",
        "            return 4\n",
        "            return 0\n"),
    "cli-reads-epoch-after-lock": (
        "requeue reads its rollback epoch after the transition's lock is released",
        "            payload[\"resend_epoch\"] = epoch_written\n",
        "            payload[\"resend_epoch\"] = outbox.resend_epoch_for(args.root, args.item_id)\n"),
    "orphan-trusts-any-retirement": (
        "an orphan arm counts any retirement outcome as its requested disposition",
        "left in place\")\n    return done.retired\n",
        "left in place\")\n    return True\n"),
    "orphan-decodes-privately": (
        "the orphan sweep decides readiness of the bytes it read by itself",
        "        raw = ready_body_of(data)\n        if raw is None:\n",
        "        try:\n            raw = data.decode(\"utf-8\").strip()\n"
        "        except UnicodeDecodeError:\n            continue\n        if not raw:\n"),
    "confirmed-archives-another-body": (
        "a confirmed send of the stored body lets a different live reply be archived as sent",
        "        if root is not None and delivered_body_differs(root, item_id, ruled):\n",
        "        if False and delivered_body_differs(root, item_id, ruled):\n"),
    "terminal-delivered-archives-another-body": (
        "a later pass at a delivered id archives a different live reply as sent",
        "            if delivered_body_differs(core.backend.root, item_id, ruled):\n",
        "            if False and delivered_body_differs(core.backend.root, item_id, ruled):\n"),
    "late-duplicate-archives-another-body": (
        "the sweep archives a different reply at a delivered id as a late duplicate",
        "            if _root is not None and delivered_body_differs(_root, _item, raw):\n",
        "            if False and delivered_body_differs(_root, _item, raw):\n"),
    "late-duplicate-hashes-the-raw-bytes": (
        "the late-duplicate arm rules on the raw file bytes, not the ready body",
        "            if _root is not None and delivered_body_differs(_root, _item, raw):\n",
        "            if _root is not None and delivered_body_differs(_root, _item, data.decode(\"utf-8\", \"replace\")):\n"),
    "unsent-skips-the-guard": (
        "a reply ruled unsent at a delivered id skips the result guard",
        "    body, withheld = _guarded_result_body(tid, raw)\n",
        "    body, withheld = raw, None\n"),
    "unsent-skips-owner-mention": (
        "a reply ruled unsent at a delivered id skips owner-mention routing",
        "    mention = _owner_mention_disposition(tid, raw)\n    if mention is None:\n        _log(f\"result {tid}: {why}; its owner",
        "    mention = False\n    if mention is None:\n        _log(f\"result {tid}: {why}; its owner"),
    "unsent-ignores-a-changed-generation": (
        "a reply that replaced the one ruled on is disposed of on that ruling",
        "    if generation is not None and ready.identity != generation:\n",
        "    if False:\n"),
    "unsent-ignores-suppression": (
        "a suppressed reply at a delivered id is quarantined and handed over for sending",
        "    if skip is not None:\n        done = disposal.retire_generation(",
        "    if False:\n        done = disposal.retire_generation("),
    "unsent-hides-its-markers": (
        "the operator line omits a destination or attachment marker the reply carries",
        "               + (f\"; marked {' '.join(markers)}\" if markers else \"\")\n",
        "               + \"\"\n"),
    "verdict-cache-keyed-by-task": (
        "the result guard reuses a task's withheld verdict for a different body",
        "    cached = (tid, source_digest(body))\n",
        "    cached = (tid, \"\")\n"),
    "artifact-claims-another-body": (
        "an existing review or suppression record of another body counts as written",
        "    if path.is_file():\n        return _holds(path, field, payload[field])\n",
        "    if path.is_file():\n        return True\n"),
    "proof-ignores-its-stamp": (
        "a source proof is trusted without this writer's version stamp",
        "    if (record.get(\"proof_version\") != PROOF_VERSION or not proof or not isinstance(payload, str)\n",
        "    if (not proof or not isinstance(payload, str)\n"),
    "binding-ignores-the-publication": (
        "a proof stays trusted after another publication rewrote the record",
        "    return hashlib.sha256(json.dumps([PROOF_VERSION, payload, publication_id]).encode(\"utf-8\")).hexdigest()\n",
        "    return hashlib.sha256(json.dumps([PROOF_VERSION, payload]).encode(\"utf-8\")).hexdigest()\n"),
    "ledger-ignores-prior-records": (
        "records from before the reservation ledger are not seeded as issued ids",
        "            _reserve(path, _record_digest(existing, field))\n",
        "            return None\n"),
    "issue-reissues-a-reserved-id": (
        "an id reserved for this body but whose record moved is issued again",
        "                found = _record_of(path, digest, field)\n                if found is not None:\n                    return found, True\n",
        "                found = None\n                if found is not None:\n                    return found, True\n"),
    "record-of-ignores-the-body": (
        "an id's record is returned whatever body it holds",
        "        if existing.exists() and _record_digest(existing, field) == digest:\n",
        "        if existing.exists() and digest:\n"),
    "migration-seeds-from-the-live-copy": (
        "a pre-ledger id is seeded from its live copy before its archived decision",
        "    for existing in (_archived(path), path):           # a decision owns the id it decided\n",
        "    for existing in (path, _archived(path)):           # a decision owns the id it decided\n"),
    "archive-outside-the-ledger-lock": (
        "archiving a record does not wait for an issuance in progress",
        "    with _ledger_lock(_record_directory(path)):\n        if not path.is_file() or not _owns_its_id(path, field):\n            return False\n        archive",
        "    with contextlib.nullcontext():\n        if not path.is_file() or not _owns_its_id(path, field):\n            return False\n        archive"),
    "unsent-brings-back-a-send-instruction": (
        "a reply at a delivered id is handed over as one to send by hand",
        "REVIEW_UNSENT = \"review it; it may already have been sent\"\n",
        "REVIEW_UNSENT = \"its outbox id is already delivered: send it by hand\"\n"),
    "unsent-drops-the-delivered-body-reference": (
        "the operator line omits the delivered wire body it must be compared with",
        "               + f\"; delivered wire body {_body_ref(_delivered_wire_body(item_id))}\"\n",
        "               + \"\"\n"),
    "archive-clobbers-a-decision": (
        "archiving a record writes over an archived decision of the same id",
        "            os.link(path, archive / path.name)           # no-clobber: an existing decision stays\n            os.unlink(path)\n",
        "            path.replace(archive / path.name)\n"),
    "conflicting-record-stays-actionable": (
        "a live record whose id belongs to another body or decision stays actionable",
        "            if isinstance(record, dict) and _owns_its_id(path, field):\n",
        "            if isinstance(record, dict):\n"),
    "update-ignores-id-ownership": (
        "an update writes a record that no longer owns its id",
        "        if not path.is_file() or not _owns_its_id(path, field) or record.get(field) != json.loads(\n",
        "        if not path.is_file() or record.get(field) != json.loads(\n"),
    "orphan-links-then-unlinks": (
        "an orphan arm moves the canonical result itself: link, then unlink its name",
        "        done = disposal.retire_generation(RESULTS_DIR, rfile, generation, _log, directory, _names(base))\n",
        "        Path(directory).mkdir(parents=True, exist_ok=True)\n"
        "        os.link(str(rfile), str(Path(directory) / next(_names(base))))\n"
        "        Path(rfile).unlink()\n"
        "        done = disposal.Retired(disposal.Retirement.PLACED)\n"),
    "archive-by-pathname": (
        "a sent result is archived by renaming whatever holds its name",
        "    done = disposal.retire_generation(RESULTS_DIR, path, generation, _log, ARCHIVE_RESULTS_DIR,\n"
        "                                      _names(f\"{tid}-{int(time.time())}\"))\n",
        "    ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)\n"
        "    path.rename(ARCHIVE_RESULTS_DIR / f\"{tid}-{int(time.time())}.txt\")\n"
        "    done = disposal.Retired(disposal.Retirement.PLACED)\n"),
    "archive-rereads-generation": (
        "the archive retires the generation it finds at the name, not the one that was sent",
        "            done = _retire(Path(results_dir), Path(rfile), generation,",
        "            done = _retire(Path(results_dir), Path(rfile), identity_of(rfile)[1],"),
    "archive-replaced-drops-bookkeeping": (
        "a replacement found at archive time is treated as retired",
        "             \"archived; the newer one stays live\")\n        return False\n",
        "             \"archived; the newer one stays live\")\n        return True\n"),
    "archive-error-retires": (
        "an archive the filesystem refuses still forgets the sent result",
        "f\"result {tid}: sent, but could not be archived ({done.cause})\")\n        return False\n",
        "f\"result {tid}: sent, but could not be archived ({done.cause})\")\n        return True\n"),
    "archive-fallback-retires": (
        "a sent result kept outside the archive still forgets the id",
        "f\"result {tid}: sent, but kept outside the archive: {done.cause}\")\n        return False\n",
        "f\"result {tid}: sent, but kept outside the archive: {done.cause}\")\n        return True\n"),
    "no-primitive-calls-the-replacement-live": (
        "without a no-replace rename, a replacement kept aside is reported as still live",
        "            if _cannot_put_back():\n                log(",
        "            if False:\n                log("),
    "reask-id-per-pass": (
        "every retried dedup decision mints a fresh re-ask id",
        "        _reask_id(tid), commit_identity=_commit,",
        "        f\"task-{uuid.uuid4().hex[:18]}\", commit_identity=_commit,"),
    "reask-published-before-tracked": (
        "a re-ask is published although its in-flight entry did not commit",
        "            if not _save_inflight(inflight):\n                inflight.discard(new_id)\n                return False\n",
        "            bool(_save_inflight(inflight))\n"),

    "archive-warning-is-not-placed": (
        "a placed archive with an unlock warning keeps the id looked for",
        "    if not done.retired:\n        disposal.report_once(f\"archive:{tid}\"",
        "    if not done.retired or done.warning:\n        disposal.report_once(f\"archive:{tid}\""),

    "reask-cleanup-fails-the-publication": (
        "a temp-file cleanup error after the re-ask was linked reads as a failed publication",
        "        try:\n            tmp.unlink()\n        except OSError as e:",
        "        try:\n            tmp.unlink()\n        except ZeroDivisionError as e:"),

    "unsent-strands-the-task": (
        "a reply kept for a person at a delivered id leaves its task pending and its id in flight",
        "    if root is None or os.path.lexists(rfile):\n        return False",
        "    if True:\n        return False"),
    "settle-under-a-live-reply": (
        "a task is retired although a newer reply is live at its name",
        "    if root is None or os.path.lexists(rfile):\n        return False",
        "    if root is None:\n        return False"),
    "reask-plan-unlocked": (
        "two planners may check and link the same re-ask at once",
        "        with disposal.locked(RESULTS_DIR):\n            action, payload = plan_dedup_recovery(",
        "        if True:\n            action, payload = plan_dedup_recovery("),


    "settle-an-undelivered-id": (
        "a task is retired although its id was never delivered",
        "    if (read_item(root, item_id) or {}).get(\"status\") != \"DELIVERED\":\n        return False\n    return _settle_task(tid, inflight)\n",
        "    if False:\n        return False\n    return _settle_task(tid, inflight)\n"),
    "settle-never-retried": (
        "a disposed-of id whose task could not be archived is never settled again",
        "                    changed |= _settle_task(tid, inflight)\n",
        "                    changed |= bool(0 and rfile)\n"),
    "archive-result-ignores-the-task": (
        "a sent result whose task could not be archived still retires every guard",
        "    if not _archive_task_file(tid):\n        _task_left_executable(tid)\n        return False\n    _settled(tid)\n    return True\n",
        "    _archive_task_file(tid)\n    _settled(tid)\n    return bool(tid)\n"),
    "alias-never-retried": (
        "only an id that is its own delivery is retried",
        "                if tid in _SETTLE_PENDING or (tid not in _SETTLE_SCANNED and _result_disposed(tid)):\n",
        "                if _delivery_tid(tid) == tid and (tid in _SETTLE_PENDING or (tid not in _SETTLE_SCANNED and _result_disposed(tid))):\n"),
    "orphan-moves-before-tracking": (
        "the late-duplicate arm moves the only live reply before the id is durably tracked",
        "            if find_task_file(TASKS_DIR, tid) is not None and not _track_durably(tid, inflight):\n",
        "            if find_task_file(TASKS_DIR, tid) is not None and not (_track_durably(tid, inflight) or True):\n"),
    "orphan-recovery-sends-before-tracking": (
        "orphan recovery delivers and moves the result before the id is durably tracked",
        "        if not _track_durably(tid, inflight):\n            _log(f\"orphan sweep: {tid} could not be saved as in flight; its result stays \"\n                 \"live for a later sweep\")\n            continue\n",
        "        if not (_track_durably(tid, inflight) or True):\n            _log(f\"orphan sweep: {tid} could not be saved as in flight; its result stays \"\n                 \"live for a later sweep\")\n            continue\n"),

    "settle-forgets-a-live-task": (
        "bookkeeping is forgotten although the task file is still in the executable queue",
        "    if not _archive_task_file(tid):\n        _task_left_executable(tid)              # every caller",
        "    if False:\n        _task_left_executable(tid)              # every caller"),
    "alias-settles-on-its-primary": (
        "an alias counts its primary's delivered record as its own",
        "    return (root is not None and delivery == tid\n",
        "    return (root is not None and delivery is not None\n"),


    "reask-republished": (
        "a retried decision writes its re-ask task again",
        "        if mine:\n            return \"requeue\", new_task_id\n",
        "        if False:\n            return \"requeue\", new_task_id\n"),
    "reask-claims-a-colliding-id": (
        "an id that holds another original's re-ask is handed to this caller",
        "        if mine is False:\n            return \"defer\", None                         # the id is someone else's\n",
        "        if mine is False and not mine is False:\n            return \"defer\", None                         # the id is someone else's\n"),
    "reask-claims-a-colliding-id-after-a-race": (
        "an id another pass took first is handed to this caller whatever it holds",
        "            if _published_as(Path(tasks_dir), Path(results_dir), new_task_id, body):\n                return \"requeue\", new_task_id\n            return \"defer\", None\n",
        "            if _published_as(Path(tasks_dir), Path(results_dir), new_task_id, body) is not None:\n                return \"requeue\", new_task_id\n            return \"defer\", None\n"),
    "track-keeps-a-non-durable-id": (
        "an id whose in-flight save failed is left in memory",
        "        if not _save_inflight(set(inflight) | {tid}):\n            return False\n        inflight.add(tid)\n        return True\n",
        "        inflight.add(tid)\n        if not _save_inflight(set(inflight) | {tid}):\n            return False\n        return True\n"),
    "track-exposes-before-durable": (
        "the id is visible to other threads before the write that holds it commits",
        "        if not _save_inflight(set(inflight) | {tid}):\n            return False\n        inflight.add(tid)\n        return True\n",
        "        inflight.add(tid)\n        if not _save_inflight(set(inflight) | {tid}):\n            inflight.discard(tid)\n            return False\n        return True\n"),
    "settle-intent-not-restored": (
        "a restart forgets the ids a previous process could not settle",
        "        if _SETTLE_LOADED:\n            return True\n",
        "        if True:\n            return True\n"),

    "reask-id-from-the-clock-alone": (
        "two originals asked in one millisecond are handed one re-ask id",
        "    return f\"task-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}\"\n",
        "    return f\"task-{int(time.time() * 1000)}\"\n"),
    "archive-moves-before-the-settle-intent": (
        "the sent result is moved although the intent proving it was served could not be saved",
        "    if not _intend_settle(tid):\n",
        "    if not (_intend_settle(tid) or True):\n"),

    "settle-save-forgets-a-previous-process": (
        "a save before the first drain overwrites the ids a previous process left",
        "        if not _load_settle_pending():\n            return False\n        return _durable_write(",
        "        if False:\n            return False\n        return _durable_write("),
    "ledger-marked-read-before-the-read": (
        "the ledger is marked read before its read resolves, so a failed or racing read loses its ids",
        "        try:\n            text = _read_settle_ledger(_settle_pending_file())\n",
        "        _SETTLE_LOADED = True\n        try:\n            text = _read_settle_ledger(_settle_pending_file())\n"),
    "unreadable-ledger-written-over": (
        "a ledger that could not be read is written over with this process's ids",
        "        if not _load_settle_pending():\n            return False\n        return _durable_write(",
        "        if not (_load_settle_pending() or True):\n            return False\n        return _durable_write("),

}


def _files(name: str) -> "list[Path]":
    if name in IN_CLI:
        return CLI
    if name in IN_BRIDGE:
        return BRIDGE
    if name in IN_OUTBOX:
        return OUTBOX
    if name in IN_BACKEND:
        return BACKEND
    if name in IN_GUARD:
        return GUARD
    if name in IN_DEDUP:
        return DEDUP
    return QUARANTINE if name in IN_QUARANTINE else DISPOSAL


def _edit(old: str, new: str, files: "list[Path]") -> None:
    for f in files:
        text = f.read_text()
        if text.count(old) != 1:
            sys.exit(f"{f.name}: expected the mutation site exactly once, found {text.count(old)}")
        f.write_text(text.replace(old, new))


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("list", "apply", "revert"):
        print(__doc__)
        return 2
    if argv[1] == "list":
        for name, (what, _o, _n) in MUTANTS.items():
            print(f"{name:34s} {what}")
        return 0
    if argv[1] == "revert":
        if not STATE.exists():
            print("nothing applied")
            return 0
        name = STATE.read_text().strip()
        _what, old, new = MUTANTS[name]
        _edit(new, old, _files(name))
        STATE.unlink()
        print(f"reverted {name}")
        return 0
    name = argv[2] if len(argv) > 2 else ""
    if name not in MUTANTS:
        sys.exit(f"unknown mutant {name!r}; see `list`")
    if STATE.exists():
        sys.exit(f"{STATE.read_text().strip()} is still applied; revert first")
    what, old, new = MUTANTS[name]
    _edit(old, new, _files(name))
    STATE.write_text(name)
    print(f"applied {name}: {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
