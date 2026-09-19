#!/usr/bin/env python3
"""D1 identity/state census across the three live chains (strangler Slice 1).

Read-only inventory — 7 identities + 7 structural facts per chain, each cell
anchored to code evidence (path + pattern). `--verify` fails when an anchor
rots, so the census cannot silently drift from the tree it describes.
`--write-doc` regenerates docs/census/d1-identity-census.md from this data.

Run: python3 scripts/census-d1.py --verify
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root

DISCORD = "src/discord-bridge.py"
AG2 = "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"
SLACK = "src/slack-bridge.py"

# Each cell: (claim, [(path, pattern), ...]) — the anchors are what --verify
# checks; the claim is what --write-doc renders beside them.
ROWS: "list[tuple[str, dict[str, tuple[str, list[tuple[str, str]]]]]]" = [
    ("provider event identity", {
        "discord": ("`message.id` from the gateway event; deduped only "
                    "in-process (`seen_message_ids`, cleared at 10k) — not "
                    "durable across restart",
                    [(DISCORD, r"seen_message_ids = set\(\)"),
                     (DISCORD, r"if len\(seen_message_ids\) > 10000")]),
        "ag2space": ("broker task id from the gateway lease; Matrix "
                     "`source_message_id` carried in the task header",
                     [(AG2, r"_local_tid\(broker_tid\)")]),
        "slack": ("Bolt Socket Mode event (SDK acks the envelope); the "
                  "bridge records no event id of its own",
                  [(SLACK, r"from slack_bolt.adapter.socket_mode import "
                           r"SocketModeHandler")]),
    }),
    ("normalized ingress identity", {
        "discord": ("`task-dc<inst>~<message.id>` via provider_task_id — "
                    "injective from the provider event id, so a replay after "
                    "restart maps to the same file (already_admitted dedups)",
                    [(DISCORD, r'task_id = provider_task_id\(f"dc\{_inst\}", str\(message\.id\)\)'),
                     (DISCORD, r"from ingress_identity import provider_task_id, already_admitted")]),
        "ag2space": ("`task-<inst>~<broker_id>` — injective mapping from the "
                     "provider id, so a replayed lease maps to the same file",
                     [(AG2, r'return f"task-\{GATEWAY_INSTANCE\}~\{broker_tid\}"')]),
        "slack": ("`task-sl<team>~<channel>-<ts>` via provider_task_id — "
                  "injective from the provider event, replay-deduped by "
                  "already_admitted",
                  [(SLACK, r'task_id = provider_task_id\(f"sl'),
                   (SLACK, r"from ingress_identity import provider_task_id, already_admitted")]),
    }),
    ("task_id", {
        "discord": ("`task-<epoch-ms>` minted by the bridge at write time "
                    "(wall clock, not event-derived)",
                    [(DISCORD, r"ts = int\(time.time\(\) \* 1000\)")]),
        "ag2space": ("`task-<inst>~<broker_id>` (provider-derived, "
                     "per-instance collision-proof)",
                     [(AG2, r"_LOCAL_TID_RE = re.compile")]),
        "slack": ("`task-<epoch-ms>` minted by the bridge",
                  [(SLACK, r'task_id = f"task-\{ts\}"')]),
    }),
    ("result publication identity", {
        "discord": ("`results/task-<id>.txt` mirrors the task id; bridge "
                    "polls it back",
                    [(DISCORD, r"pending_replies = \{\}")]),
        "ag2space": ("`results/task-<id>.txt`; body POSTed to the broker "
                     "complete endpoint keyed by the broker id",
                     [(AG2, r"_post_ready_results\(inflight\)")]),
        "slack": ("`results/task-<id>.txt` polled by the watcher loop",
                  [(SLACK, r"app\.client\.chat_postMessage\(")]),
    }),
    ("delivery_id", {
        "discord": ("reply leg keyed by task_id in `pending_replies` "
                    "(durable JSON); proactive leg is an outbox item in "
                    "`.outbox-discord-proactive`",
                    [(DISCORD, r"def _atomic_write_pending_replies"),
                     (DISCORD, r"\.outbox-discord-proactive")]),
        "ag2space": ("broker task id on ack/complete; proactive posts carry "
                     "an explicit `dedupe_key`",
                     [(AG2, r'"dedupe_key": f"')]),
        "slack": ("none stable — reply send is fire-and-forget; proactive "
                  "keyed by result filename in `slack_proactive_receipts`",
                  [(SLACK, r"from slack_proactive_receipts import"),
                   (SLACK, r"reply_receipt|reply_delivered", "absent")]),
    }),
    ("attempt_id", {
        "discord": ("proactive attempts durable in the outbox record; "
                    "reply/marker leg counts attempts in-memory only",
                    [(DISCORD, r"_transient_send_attempts: dict")]),
        "ag2space": ("no distinct attempt id — re-POST until 200 "
                     "(at-least-once), in-flight set tracked in "
                     "`remote-task-inflight*.json`",
                     [(AG2, r"INFLIGHT_FILE = _STATE /"),
                      (AG2, r"attempt_id", "absent")]),
        "slack": ("none — a failed send is retried by the next watcher poll "
                  "pass",
                  [(SLACK, r"watcher loop retry"),
                   (SLACK, r"attempt_id", "absent")]),
    }),
    ("provider receipt", {
        "discord": ("`DeliveryReceipt` (CONFIRMED carries the provider "
                    "message id) + append-only `outbox_log`",
                    [(DISCORD, r"import outbox_log")]),
        "ag2space": ("gateway `event_id` when present; bare `ok` trusted as "
                     "delivered; `record_delivered` via the vendored outbox",
                     [(AG2, r"from .outbox import DeliveryOutcome, "
                            r"record_delivered")]),
        "slack": ("none persisted for replies (failures printed); proactive "
                  "marked delivered in `slack_proactive_receipts`",
                  [(SLACK, r"mark_delivered as mark_proactive_delivered"),
                   (SLACK, r"reply_receipt|persist_reply", "absent")]),
    }),
    ("process / entrypoint", {
        "discord": ("long-running discord.py gateway client, launched by "
                    "startup.sh",
                    [(DISCORD, r"")]),
        "ag2space": ("`src/remote-gateway-bridge.py` loader executing the "
                     "vendored `ag2_sparrow.remote_gateway_bridge` long-poll "
                     "loop",
                     [("src/remote-gateway-bridge.py", r"")]),
        "slack": ("`slack_bolt` SocketModeHandler process",
                  [(SLACK, r"SocketModeHandler\(app, APP_TOKEN\)")]),
    }),
    ("durable state", {
        "discord": ("pending-replies JSON, `.outbox-discord-proactive`, "
                    "`.sending` sentinels, tasks/ results/ processed/",
                    [(DISCORD, r"def save_pending_replies")]),
        "ag2space": ("`remote-task-inflight*.json`, `remote-task-rooms*.json`,"
                     " `remote-dedup-alias*.json`, `results/undelivered/`",
                     [(AG2, r"TASK_ROOMS_FILE = _STATE /"), (AG2, r"DEDUP_ALIAS_FILE = _STATE /")]),
        "slack": ("`slack_proactive_receipts` store + tasks/ results/",
                  [(SLACK, r"proactive_was_delivered\(")]),
    }),
    ("direct network calls", {
        "discord": ("provider I/O behind `channels/discord`; plus a "
                    "localhost agent-api `/task-done` POST",
                    [(DISCORD, r"localhost:7843/task-done")]),
        "ag2space": ("REMOTE_TASK_URL REST in-module — that surface IS the "
                     "provider edge",
                     [(AG2, r"\{REMOTE_TASK_URL\}")]),
        "slack": ("`app.client.chat_postMessage` called directly from bridge "
                  "code at multiple sites (the direct outbound Slice 5 "
                  "strangles)",
                  [(SLACK, r"app\.client\.chat_postMessage\(")]),
    }),
    ("private retry / receipt", {
        "discord": ("in-memory attempt counter on the marker leg; the "
                    "proactive leg already rides the outbox-backed fence",
                    [(DISCORD, r"from proactive_claim_fence import ProactiveClaimFence")]),
        "ag2space": ("closest to canon: vendored outbox `classify_response` "
                     "three-state + `record_delivered`",
                     [(AG2, r"from .outbox_adapter import classify_response")]),
        "slack": ("private `slack_proactive_receipts` + bare poll-loop retry",
                  [(SLACK, r"from slack_proactive_receipts import")]),
    }),
    ("completion decision", {
        "discord": ("result file + marker parse -> deliver -> archive; also "
                    "POSTs `/task-done` so the web UI flips",
                    [(DISCORD, r"parse_markers\(")]),
        "ag2space": ("result POST to `/v1/results` answered 200 -> archive "
                     "(delivery completion and task completion conflated at "
                     "the 200)",
                     [(AG2, r'"POST", "/v1/results"')]),
        "slack": ("result file consumed -> post -> archive",
                  [(SLACK, r"parse_markers\(")]),
    }),
    ("restart recovery", {
        "discord": ("orphan `.sending` sweep before the poll + "
                    "pending_replies reload from JSON",
                    [(DISCORD, r"sweep orphan `.sending` files")]),
        "ag2space": ("in-flight JSON reload + gateway re-enroll claim",
                     [(AG2, r"_reenroll_state\[")]),
        "slack": ("re-poll of results/; proactive receipts consulted before "
                  "re-send (`was_delivered`)",
                  [(SLACK, r"if proactive_was_delivered\(")]),
    }),
    ("credentials owner", {
        "discord": ("`resolve_channel_token(\"DISCORD_BOT_TOKEN\")` over "
                    "channels/discord/.env",
                    [(DISCORD, r'resolve_channel_token\("DISCORD_BOT_TOKEN"')]),
        "ag2space": ("REMOTE_TASK_TOKEN via the ag2space channel env",
                     [(AG2, r'vals.get\("REMOTE_TASK_TOKEN"\)')]),
        "slack": ("SLACK_BOT_TOKEN / SLACK_APP_TOKEN: env -> channel .env -> "
                  "vault",
                  [(SLACK, r'token_from_vault\("SLACK_BOT_TOKEN"\)')]),
    }),
]

CHAINS = ("discord", "ag2space", "slack")

HEADER = """# D1 identity/state census — Discord / AG2 Space / Slack

Strangler Slice 1 (read-only): the 7 delivery identities and 7 structural
facts of each chain's DM round trip, side by side, anchored to code. Generated
by `scripts/census-d1.py --write-doc`; `--verify` (run in CI) fails when an
anchor no longer matches the tree, so every cell stays checkable.

Slack caveat: the chain is configured-not-running on the reference host, so
its rows describe code, not observed behavior — it cannot serve as live
architecture evidence until its E2E environment is restored (Slice 5).

| dimension | discord | ag2space | slack |
|---|---|---|---|
"""


def verify() -> int:
    bad = 0
    for name, cells in ROWS:
        for chain in CHAINS:
            _claim, anchors = cells[chain]
            for anchor in anchors:
                path, pattern = anchor[0], anchor[1]
                absent = len(anchor) > 2 and anchor[2] == "absent"
                text = ""
                target = REPO / path
                if target.is_file():
                    text = target.read_text(errors="replace")
                hit = bool(pattern and re.search(pattern, text))
                if absent:
                    # an absence claim rots by the tree GAINING the thing
                    if target.is_file() and hit:
                        print(f"ROTTED [{name} / {chain}] {path} now matches "
                              f"/{pattern}/ — the 'none' claim is stale")
                        bad += 1
                elif not target.is_file() or (pattern and not hit):
                    print(f"ROTTED [{name} / {chain}] {path} !~ /{pattern}/")
                    bad += 1
    print(f"census-d1: {'FAILED' if bad else 'OK'} "
          f"({len(ROWS)} rows x {len(CHAINS)} chains, {bad} rotted anchor(s))")
    return 1 if bad else 0


def write_doc() -> int:
    out = [HEADER]
    for name, cells in ROWS:
        line = [f"| **{name}**"]
        for chain in CHAINS:
            claim, anchors = cells[chain]
            refs = "; ".join(sorted({a[0] for a in anchors}))
            line.append(f" {claim} _({refs})_ ")
        out.append("|".join(line) + "|\n")
    doc = REPO / "docs" / "census" / "d1-identity-census.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("".join(out))
    print(f"wrote {doc.relative_to(REPO)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--write-doc", action="store_true")
    a = ap.parse_args()
    if a.write_doc:
        rc = write_doc()
        return rc or (verify() if a.verify else 0)
    return verify()


if __name__ == "__main__":
    sys.exit(main())
