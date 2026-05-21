#!/usr/bin/env python3
"""proactive-notify runner — cron entry.

Walks pings.yaml, fetches candidates per source, applies match filters,
dedups against state/fired.json, picks channel via channel-policy.yaml,
and delivers via the action module.

CLI:
  --live          actually deliver (default is dry-run; logs intended deliveries)
  --once          one pass then exit (default; cron invokes with this)
  --pings PATH    override pings.yaml location
  --policy PATH   override channel-policy.yaml location
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    sys.stderr.write("ERROR: pyyaml required. Run: pip install pyyaml\n")
    sys.exit(2)

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from channel_router import Ping, route  # noqa: E402
from presence import snapshot  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

SKILL_DIR = SCRIPTS_DIR.parent
SKILL_NAME = SKILL_DIR.name
TEMPLATE_DIR = SKILL_DIR / "config"
WORKSPACE_SKILL_DIR = resolve_workspace() / "skills" / SKILL_NAME
DEFAULT_PINGS = WORKSPACE_SKILL_DIR / "pings.yaml"
DEFAULT_POLICY = WORKSPACE_SKILL_DIR / "channel-policy.yaml"
STATE_DIR = WORKSPACE_SKILL_DIR / "state"
FIRED_PATH = STATE_DIR / "fired.json"
DRY_RUN_LOG = resolve_workspace() / "logs" / "proactive-notify-dryrun.log"


def _bootstrap_workspace_config() -> None:
    """First-run: copy .example templates from repo to workspace if absent."""
    WORKSPACE_SKILL_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for live, template in (
        (DEFAULT_PINGS, TEMPLATE_DIR / "pings.yaml.example"),
        (DEFAULT_POLICY, TEMPLATE_DIR / "channel-policy.yaml.example"),
    ):
        if not live.exists() and template.exists():
            live.write_text(template.read_text())


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _load_fired() -> dict[str, str]:
    if not FIRED_PATH.exists():
        return {}
    try:
        return json.loads(FIRED_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_fired(fired: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FIRED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fired, indent=2, sort_keys=True))
    tmp.replace(FIRED_PATH)


def _matches(item: dict, criteria: dict) -> bool:
    """Apply a `match:` block to a candidate item. Returns True if it passes."""
    if not criteria:
        return True
    # Numeric range on minutes_until: starts_in_min_min / starts_in_min_max
    if (mu := item.get("minutes_until")) is not None:
        lo = criteria.get("starts_in_min_min")
        hi = criteria.get("starts_in_min_max")
        if lo is not None and mu < int(lo):
            return False
        if hi is not None and mu > int(hi):
            return False
    if (pat := criteria.get("exclude_titles_matching")):
        if re.search(pat, item.get("title", "")):
            return False
    if (tag := criteria.get("exclude_description_tag")):
        if tag in (item.get("description") or ""):
            return False
    return True


def _render(template: str, item: dict) -> str:
    try:
        return template.format(**item)
    except KeyError as e:
        return f"{template} [render-error: missing {e}]"


def _process_ping(spec: dict, pings_fired: dict[str, str], presence_snap, policy: dict, live: bool) -> list[dict]:
    """Process one ping spec end-to-end. Returns list of dispatch records."""
    name = spec.get("name")
    source_name = spec.get("source")
    if not name or not source_name:
        return []
    try:
        source_mod = importlib.import_module(f"sources.{source_name}")
    except ModuleNotFoundError:
        return [{"ping": name, "status": "error", "reason": f"unknown source: {source_name}"}]
    candidates = source_mod.fetch(spec.get("source_config") or {})

    results = []
    for item in candidates:
        if not _matches(item, spec.get("match") or {}):
            continue
        dedup_key = f"{name}:{item.get('dedup_key_suffix', '')}"
        if dedup_key in pings_fired:
            continue
        body = _render(spec.get("body_template", ""), item)
        ping = Ping(
            name=name,
            urgency=spec.get("urgency", "fyi"),
            voice_natural=bool(spec.get("voice_natural", False)),
            body=body,
            dedup_key=dedup_key,
            prefer_channel=spec.get("prefer_channel"),
            quiet_hours_override=bool(spec.get("quiet_hours_override", False)),
        )
        channel = route(ping, presence_snap, policy)
        record = {
            "ping": name,
            "dedup_key": dedup_key,
            "channel": channel,
            "body": body,
            "urgency": ping.urgency,
        }
        if not live:
            record["status"] = "dry-run"
        else:
            try:
                action_mod = importlib.import_module(f"actions.{channel}")
            except ModuleNotFoundError:
                record["status"] = "error"
                record["reason"] = f"no action module for channel: {channel}"
            else:
                res = action_mod.send(ping, body)
                if res.get("ok"):
                    record["status"] = "sent"
                    record["id"] = res.get("id", "")
                    pings_fired[dedup_key] = datetime.now(timezone.utc).isoformat()
                else:
                    record["status"] = "error"
                    record["reason"] = res.get("error", "unknown")
        results.append(record)
    return results


def _log_dry_run(records: list[dict]) -> None:
    if not records:
        return
    DRY_RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DRY_RUN_LOG.open("a") as f:
        for r in records:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **r}) + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Actually deliver (default: dry-run)")
    parser.add_argument("--once", action="store_true", help="One pass then exit")
    parser.add_argument("--pings", type=Path, default=DEFAULT_PINGS)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)

    _bootstrap_workspace_config()
    pings_doc = _load_yaml(args.pings)
    policy = _load_yaml(args.policy)
    presence_snap = snapshot(policy)
    fired = _load_fired()

    all_records: list[dict] = []
    for spec in pings_doc.get("pings", []):
        all_records.extend(_process_ping(spec, fired, presence_snap, policy, args.live))

    if args.live:
        _save_fired(fired)
    else:
        _log_dry_run(all_records)

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if args.live else "dry-run",
        "candidates": len(all_records),
        "sent": sum(1 for r in all_records if r.get("status") == "sent"),
        "dry_run": sum(1 for r in all_records if r.get("status") == "dry-run"),
        "errors": sum(1 for r in all_records if r.get("status") == "error"),
    }
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
