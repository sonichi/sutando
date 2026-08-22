"""Production injection seam for the Discord post-gate.

`DiscordRestClient` accepts an injected `validator`, but each sender is a
separate process (bridge CLI, dm-result, bot2bot-post, task-progress,
notify.sh); a wrapper cannot retrofit a constructor argument into them.
This module is the ONE factory those entrypoints construct through, and it
resolves the validator from launch wiring the personal layer controls:

  1. `$SUTANDO_DISCORD_POST_GATE` — path to a Python file exporting
     `validate(channel_id, payload) -> falsy | refusal-reason str`
     (env override; same pattern as `$SUTANDO_DOWN_BRIDGE_ACTION`).
  2. `bridges.discord_post_gate` in `sutando.config.json[.local]` —
     same path, per-clone.

Unconfigured -> None (ungated; the repo ships mechanism only). Configured
but unloadable -> a validator that REFUSES every send, naming the load
failure: config selects WHICH ruleset applies, never WHETHER one does, so
a named-but-broken policy must fail closed, not silently disable the gate.
The policy itself (e.g. chain-check) lives in the personal layer / skills
repo — this module never names or imports a concrete skill.
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from discord_rest_client import DiscordRestClient


def _configured_path(repo_root=None) -> str:
    env = os.environ.get("SUTANDO_DISCORD_POST_GATE", "").strip()
    if env:
        return env
    # May raise: a config layer that cannot load leaves gating state UNKNOWN,
    # and the caller must fail closed rather than treat it as unconfigured.
    from sutando_config import load_config
    bridges = load_config(repo_root).get("bridges") or {}
    return str(bridges.get("discord_post_gate") or "").strip()


def _fail_closed(reason: str):
    def _refuse(channel_id, payload):
        return reason
    return _refuse


def resolve_validator(repo_root=None):
    """The configured validator callable, None (unconfigured), or a
    fail-closed refuser when a configured policy cannot be loaded."""
    try:
        path = _configured_path(repo_root)
    except Exception as e:  # noqa: BLE001 — unreadable config fails CLOSED
        return _fail_closed(
            f"post-gate config unreadable ({type(e).__name__}: {e}); "
            "refusing unvalidated sends")
    if not path:
        return None
    file = Path(os.path.expanduser(path))
    if not file.is_absolute():
        # Anchor to the config's own repo, never the process cwd.
        from sutando_config import _find_repo_root
        root = Path(repo_root) if repo_root else _find_repo_root()
        if root:
            file = Path(root) / file
    try:
        spec = importlib.util.spec_from_file_location(
            f"sutando_post_gate_{uuid.uuid4().hex}", file)
        if spec is None or spec.loader is None:
            raise ImportError(f"not importable: {file}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        validate = getattr(mod, "validate")
    except Exception as e:  # noqa: BLE001 — a named-but-broken gate fails CLOSED
        return _fail_closed(
            f"post-gate policy {path!r} failed to load "
            f"({type(e).__name__}: {e}); refusing unvalidated sends")
    if not callable(validate):
        return _fail_closed(
            f"post-gate policy {path!r} has a non-callable `validate`; "
            "refusing unvalidated sends")
    return validate


def make_client(token: str, timeout: int = 10,
                transport=None, repo_root=None) -> DiscordRestClient:
    """The production Discord-client factory. Every delivery entrypoint
    constructs through here so the resolved post-gate covers all of them.
    A caller needing an explicit validator binds DiscordRestClient directly."""
    return DiscordRestClient(token, transport=transport, timeout=timeout,
                             validator=resolve_validator(repo_root))
