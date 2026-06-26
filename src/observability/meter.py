"""``record(u)`` -- the durable usage primitive (Python twin of meter.ts).

Guarantees: DURABLE synchronous append to
``<workspace>/data/usage/usage-YYYY-MM-DD.jsonl`` (O_APPEND, single write per
line; ``fsync`` when ``SUTANDO_METERING_FSYNC`` is truthy, default off);
IDEMPOTENT on ``usage_id`` (append-only, at-least-once; downstream dedups);
NEVER raises (returns the stamped record even on append failure). The durable
append happens BEFORE the advisory ``usage.recorded`` obs event. The metering
SHIPPER is intentionally NOT here.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from workspace_default import resolve_workspace  # flat src/ module (see obs/sink.py note)

from .ids import new_trace_id, new_usage_id
from .config import load_observability_config
from .obs import emit

__all__ = ["record", "ledger_path"]

_TRUEISH = {"1", "true", "yes", "on"}


def ledger_path(at_ms: float | None = None, workspace: Path | None = None) -> Path:
    if at_ms is None:
        at_ms = time.time() * 1000
    date = time.strftime("%Y-%m-%d", time.localtime(at_ms / 1000))
    ws = workspace if workspace is not None else resolve_workspace(migrate=False)
    return Path(ws) / "data" / "usage" / f"usage-{date}.jsonl"


def _fsync_enabled() -> bool:
    return os.environ.get("SUTANDO_METERING_FSYNC", "").strip().lower() in _TRUEISH


def record(usage_input: dict[str, Any]) -> dict[str, Any]:
    # tenant_id defaults from config when the caller doesn't specify (BYOK -> None).
    if "tenant_id" in usage_input:
        tenant_id = usage_input["tenant_id"]
    else:
        try:
            tenant_id = load_observability_config()["tenant"]["id"]
        except Exception:
            tenant_id = None

    ts = usage_input["ts"] if usage_input.get("ts") is not None else round(time.time(), 3)
    rec: dict[str, Any] = {
        "schema": 1,
        "usage_id": usage_input.get("usage_id") or new_usage_id(),
        "ts": ts,
        "tenant_id": tenant_id,
        "trace_id": usage_input.get("trace_id") or new_trace_id(),
        "actor": usage_input["actor"],
        "source": usage_input["source"],
        "meter": usage_input["meter"],
        "quantity": usage_input["quantity"],
        "unit": usage_input["unit"],
        "provider": usage_input["provider"],
        "provider_ref": usage_input.get("provider_ref"),
        "attrs": usage_input.get("attrs") or {},
    }
    if usage_input.get("source_file") is not None:
        rec["source_file"] = usage_input["source_file"]

    # --- durability point: synchronous append BEFORE the advisory emit ---
    try:
        path = ledger_path(rec["ts"] * 1000)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if _fsync_enabled():
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            with path.open("ab") as fh:
                fh.write(data)
    except Exception as e:  # noqa: BLE001 — never raise out of record()
        try:
            print(
                f"[meter] FAILED to record usage {rec['usage_id']} ({rec['meter']}): {e}",
                file=sys.stderr,
            )
        except Exception:
            pass

    # --- advisory obs event (the ledger bills; this just surfaces usage inline) ---
    try:
        attrs = rec["attrs"]
        emit(
            {
                "source": rec["source"],
                "source_file": rec.get("source_file"),
                "trace_id": rec["trace_id"],
                "actor": rec["actor"],
                "kind": "usage.recorded",
                "outcome": "ok",
                "usage": {
                    "provider": rec["provider"],
                    "model": attrs.get("model"),
                    "input_tokens": attrs.get("input_tokens"),
                    "output_tokens": attrs.get("output_tokens"),
                    "cache_read": attrs.get("cache_read"),
                    "cache_creation": attrs.get("cache_creation"),
                    "cost_usd": attrs.get("cost_usd"),
                },
                "data": {
                    "meter": rec["meter"],
                    "quantity": rec["quantity"],
                    "unit": rec["unit"],
                    "usage_id": rec["usage_id"],
                    "provider_ref": rec["provider_ref"],
                },
            }
        )
    except Exception:
        pass  # advisory is fire-and-forget

    return rec
