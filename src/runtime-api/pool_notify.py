#!/usr/bin/env python3
"""Lead-side progress communication (lead-follower pool, slice L5).

The lead is the only party that can see a channel's conversation move
between cores (affinity yield) or a claim sit unfinished — followers each
see only their own slice, and the owner sees interleaved replies with no
explanation. This module turns those two lead-visible events into one-time,
channel-addressed notices:

- handoff: a channel's task was assigned to a different core than the one
  that last handled that channel;
- stall: a claimed task passed `stall_after_s` with no done-flag from its
  claimer.

Policy only: what to say, when, and at-most-once bookkeeping. Delivery is an
injected `send_fn(source, channel, message) -> bool` — the daemon binds the
transport (a skill script today), keeping provider mechanics at the edge.
Everything is injected (dirs, clock, sender); stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

STALL_AFTER_S = 600  # matches slack-bridge's user-visible timeout notice

# slack-bridge already posts its own timeout notice (#1428); excluded here so
# one stall never pings the owner twice. Remove when that policy centralizes.
STALL_EXCLUDED_SOURCES = frozenset({"slack"})

_HEADER_RE = re.compile(
    r"^(?:(?P<key>source|channel_id|chat_id):\s*(?P<val>\S+))", re.M)
_CLAIMED_RE = re.compile(r"^(task-[A-Za-z0-9._~-]+)\.claimed-(.+)\.txt$")
# Same stem in all three states; anchored so task-1 cannot match task-12.
_PRESENT_RE = re.compile(
    r"^(task-[A-Za-z0-9._~-]+?)(?:\.(?:assigned|claimed)-.+)?\.txt$")


def read_routing(path: Path) -> "tuple[str, str] | None":
    """(source, channel) from a task header, or None when unroutable."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    fields = {m.group("key"): m.group("val")
              for m in _HEADER_RE.finditer(text)}
    channel = fields.get("channel_id") or fields.get("chat_id")
    source = fields.get("source")
    if not channel or not source:
        return None
    return source, channel


class PoolNotifier:
    def __init__(self, tasks_dir, state_dir, send_fn,
                 now_fn=time.time, stall_after_s: int = STALL_AFTER_S):
        self.tasks_dir = Path(tasks_dir)
        self.state_dir = Path(state_dir)
        self.send = send_fn
        self.now = now_fn
        self.stall_after_s = stall_after_s

    # ── ledger (single-writer: the lead) ────────────────────────────────────
    def _ledger_path(self) -> Path:
        return self.state_dir / "pool" / "notify-ledger.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._ledger_path().read_text())
            if isinstance(data, dict):
                data.setdefault("channels", {})
                data.setdefault("tasks", {})
                return data
        except (OSError, ValueError):
            pass
        return {"channels": {}, "tasks": {}}

    def _save(self, ledger: dict) -> None:
        p = self._ledger_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(ledger))
            os.replace(tmp, p)
        except OSError:
            pass  # fail-open: notices are best-effort, never block the sweep

    # ── handoff notices ─────────────────────────────────────────────────────
    def on_assigned(self, task_name: str, instance: str) -> bool:
        """Called per sweep assignment. Notifies the channel when its
        conversation moved to a different core. Returns True if sent."""
        stem = task_name[:-len(".txt")]
        assigned = self.tasks_dir / f"{stem}.assigned-{instance}.txt"
        routing = read_routing(assigned)
        ledger = self._load()
        sent = False
        if routing is not None:
            source, channel = routing
            prev = ledger["channels"].get(channel)
            if prev is not None and prev != instance:
                msg = (f"{instance} picked this up — {prev} is busy with "
                       f"earlier tasks and will still finish what it "
                       f"already started. — pool-lead")
                sent = bool(self._try_send(source, channel, msg))
            ledger["channels"][channel] = instance
        self._save(ledger)
        return sent

    # ── stall notices ───────────────────────────────────────────────────────
    def check_stalls(self) -> "list[str]":
        """Scan claimed tasks; notify each channel at most once while its
        task is unfinished longer than `stall_after_s` with no done-flag.
        First-seen times live in the ledger (rename keeps mtime, and ctime
        is not portable) and survive a repool, so the at-most-once marker
        and the clock both span re-claims. Returns the stems notified."""
        try:
            entries = list(self.tasks_dir.iterdir())
        except OSError:
            return []
        ledger = self._load()
        present = {m.group(1) for m in
                   (_PRESENT_RE.match(f.name) for f in entries) if m}
        notified = []
        for f in entries:
            m = _CLAIMED_RE.match(f.name)
            if not m:
                continue
            stem, inst = m.group(1), m.group(2)
            row = ledger["tasks"].setdefault(
                stem, {"first_claimed": self.now(), "notified": []})
            if "stall" in row["notified"]:
                continue
            if self.now() - float(row["first_claimed"]) < self.stall_after_s:
                continue
            if self._done_flag(stem, inst):
                continue
            routing = read_routing(f)
            if routing is None or routing[0] in STALL_EXCLUDED_SOURCES:
                row["notified"].append("stall")  # unroutable: never retry
                continue
            mins = int((self.now() - float(row["first_claimed"])) // 60)
            msg = (f"{inst} is still working on this ({mins}+ min). "
                   f"It will reply here when it finishes. — pool-lead")
            if self._try_send(routing[0], routing[1], msg):
                row["notified"].append("stall")
                notified.append(stem)
        # Prune on presence, not on claimed: a repool renames the file back and
        # dropping the row here would lose the marker and re-arm the stall.
        ledger["tasks"] = {k: v for k, v in ledger["tasks"].items()
                           if k in present}
        self._save(ledger)
        return notified

    def _done_flag(self, stem: str, instance: str) -> bool:
        return (self.state_dir / "cores" / instance / "done"
                / f"{stem}.flag").exists()

    def _try_send(self, source: str, channel: str, message: str) -> bool:
        try:
            return bool(self.send(source, channel, message))
        except Exception:  # noqa: BLE001 — a broken sender must not stop the lead
            return False
