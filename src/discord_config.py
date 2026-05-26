"""
Workspace-local Sutando-specific Discord configuration (closes #1147).

The official discord plugin owns `~/.claude/channels/discord/access.json` with
its documented schema (`dmPolicy`, `allowFrom`, `groups`, `pending`). Sutando
has been squatting on that file for its own fields (`tierMap` since #846,
proposed `owner` from the now-closed #1118). #1147 moves those Sutando
extensions to `$SUTANDO_WORKSPACE/state/discord-config.json` — Sutando's
owned territory, no plugin-schema conflict, no Claude-Code edit-policy prompt.

Two distinct consumers MUST resolve owner identity the same way:
  - `src/discord-bridge.py:_poll_proactive` (live bridge)
  - `src/dm-result.py:_resolve_owner_id` (fallback DM delivery)

This module is the single source of truth they both call into. The drift
class that bit us with #846's tierMap (one site got the read, the other
didn't) is fixed by funneling both through `resolve_owner_id` here.

Resolution order (config-driven; returns None if exhausted):
  1. SUTANDO_DM_OWNER_ID env var (operator override)
  2. discord-config.json["owner"]                       — Sutando, this file
  3. discord-config.json["tierMap"][uid] == "owner"     — Sutando, this file
  4. access.json["owner"]                                — legacy compat (#1118 WIP)
  5. access.json["tierMap"][uid] == "owner"              — legacy compat (#846)

On None, callers walk `allowFrom` with their own bot-filter (I/O-bearing
in both call sites — discord.py async vs sync REST in dm-result.py — so
the helper deliberately stays pure-Python).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

# Re-exported by callers so they don't need a separate workspace import.
from workspace_default import resolve_workspace  # type: ignore  # noqa: E402

CONFIG_FILENAME = "discord-config.json"

logger = logging.getLogger(__name__)


def config_path() -> Path:
    """Return the path to `<workspace>/state/discord-config.json`."""
    return resolve_workspace() / "state" / CONFIG_FILENAME


def load_config() -> dict:
    """Return parsed `discord-config.json`, or {} if missing/unreadable.

    A read error returns {} (treated as "no Sutando-side override") so the
    legacy `access.json` fallback chain remains usable — the bridge stays
    operational even if the workspace file is corrupted.
    """
    path = config_path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — explicit narrow handling below
        logger.warning("discord-config: failed to read %s: %s", path, exc)
        return {}


def save_config(data: dict) -> None:
    """Write `discord-config.json` atomically (tmp + rename).

    Creates the parent `state/` directory if missing. Callers should hold
    a lock if concurrent writers are possible; today the only writer is
    `auto_seed_if_missing` at bridge boot, so single-writer is fine.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def resolve_owner_id(
    access_data: dict,
    *,
    config: Optional[dict] = None,
) -> Optional[str]:
    """Return the canonical owner user-id (as a string) or None if no
    candidate is available.

    `access_data` is the parsed plugin `access.json` (callers already read
    it). `config` lets tests inject a fixture; production callers pass None
    and we read `discord-config.json` ourselves.

    See module docstring for the 6-step resolution chain.
    """
    # 1. Env-var override — operator escape hatch, wins over everything.
    env_owner = os.environ.get("SUTANDO_DM_OWNER_ID", "").strip()
    if env_owner:
        return env_owner

    cfg = config if config is not None else load_config()

    # 2. Workspace owner field — Sutando's canonical singleton identity.
    ws_owner = (cfg.get("owner") or "").strip() or None
    if ws_owner:
        return ws_owner

    allow_list = list(access_data.get("allowFrom") or [])

    # 3. Workspace tierMap tagged owner.
    ws_tier_map = cfg.get("tierMap") or {}
    ws_tier_owner = next(
        (uid for uid in allow_list if ws_tier_map.get(uid) == "owner"),
        None,
    )
    if ws_tier_owner:
        return str(ws_tier_owner)

    # 4. Legacy plugin-territory owner field (compat for anyone who set it
    # while #1118 was open). #1147 supersedes this path; left in for one
    # release so existing manual edits don't get silently ignored.
    legacy_owner = (access_data.get("owner") or "").strip() or None
    if legacy_owner:
        return legacy_owner

    # 5. Legacy plugin-territory tierMap (the #846 path).
    legacy_tier_map = access_data.get("tierMap") or {}
    legacy_tier_owner = next(
        (uid for uid in allow_list if legacy_tier_map.get(uid) == "owner"),
        None,
    )
    if legacy_tier_owner:
        return str(legacy_tier_owner)

    # Helper deliberately does NOT fall through to `allowFrom[0]`. That step
    # (the bug-prone "Susan Mac Studio 2026-05-25" path — `allowFrom[0]` was
    # a non-owner human because `tierMap` was null) belongs in the caller,
    # which can pair it with a bot-filter REST walk. Returning None here is
    # the signal "no config-driven answer; you do the I/O-bearing fallback".
    return None


def auto_seed_if_missing(access_data: dict, *, logger_=None) -> dict:
    """If `discord-config.json` is missing, write an initial config seeded
    from the legacy access.json heuristic and return it.

    Per Lucy's #1147 watch-point #2: when the seed falls through to
    `allowFrom[0]` (the original-bug path), emit a prominent WARNING so
    the operator catches a mis-seed rather than silently recreating the
    #1147 bug under a new filename.

    Idempotent: if the file already exists, return its current contents
    untouched.
    """
    log = logger_ or logger
    path = config_path()
    if path.exists():
        return load_config()

    seed: dict = {}

    legacy_owner = (access_data.get("owner") or "").strip() or None
    legacy_tier_map = access_data.get("tierMap") or {}
    allow_list = list(access_data.get("allowFrom") or [])

    if legacy_owner:
        seed["owner"] = legacy_owner
        log.info(
            "discord-config: auto-seeded owner=%s from access.json[\"owner\"] -> %s",
            legacy_owner,
            path,
        )
    else:
        tier_owner = next(
            (uid for uid in allow_list if legacy_tier_map.get(uid) == "owner"),
            None,
        )
        if tier_owner:
            seed["owner"] = str(tier_owner)
            log.info(
                "discord-config: auto-seeded owner=%s from access.json tierMap -> %s",
                tier_owner,
                path,
            )
        elif allow_list:
            seed["owner"] = str(allow_list[0])
            log.warning(
                "discord-config: auto-seeded owner=%s from allowFrom[0] fallback. "
                "VERIFY this is correct and edit %s if wrong — this fallback path "
                "is the same one that produced the original #1147 bug (Susan Mac "
                "Studio 2026-05-25).",
                seed["owner"],
                path,
            )
        else:
            log.warning(
                "discord-config: no owner candidates in access.json; %s left empty. "
                "Set the owner field manually before proactive DMs will deliver.",
                path,
            )

    if legacy_tier_map:
        # Mirror the existing tierMap so #846's behavior is preserved without
        # forcing the operator to re-tag every user.
        seed["tierMap"] = dict(legacy_tier_map)

    save_config(seed)
    return seed
