#!/usr/bin/env python3
"""Phase-1a compat shims: each retired root-level module name must still import
and resolve to the CANONICAL MODULE OBJECT itself (sys.modules alias), so
patching through either name reaches one implementation.
Run: python3 tests/restructure-compat-shims.test.py
"""
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))  # delivery_provider imports ag2_sparrow

SHIMS = {
    "result_ready": ("delivery.readiness", "read_ready_result"),
    "result_router": ("delivery.router", "note_empty_result"),
    "result_channel_key": ("delivery.channel_key", "phone_call_key"),
    "discord_post_gate": ("channels.discord.post_gate", "make_client"),
    "discord_rest_client": ("channels.discord.client", "DiscordRestClient"),
    "discord_http": ("channels.discord.http", "request_json"),
    "discord_reader": ("channels.discord.reader", "render_line"),
    "discord_delivery_provider": ("channels.discord.delivery_provider", "DiscordDeliveryProvider"),
    "discord_context_policy": ("policy.context.discord", "gate"),
    "send_allowlist": ("policy.egress.attachment", "is_path_sendable"),
    "team_result_guard": ("policy.egress.result", "scan_team_result"),
    "team_guardrail": ("policy.guardrail", "TEAM_GUARDRAIL"),
}

fails = []
for shim_name, (canon_name, attr) in SHIMS.items():
    try:
        shim = importlib.import_module(shim_name)
        canon = importlib.import_module(canon_name)
        # True alias: the retired name must BE the canonical module object,
        # so patching through either name reaches one implementation.
        same = shim is canon and getattr(shim, attr) is getattr(canon, attr)
        print(f"  {'ok  ' if same else 'FAIL'} {shim_name} is {canon_name} (module identity)")
        if not same:
            fails.append(shim_name)
    except Exception as e:
        print(f"  FAIL {shim_name}: {type(e).__name__}: {e}")
        fails.append(shim_name)

if fails:
    print(f"\n{len(fails)} shim(s) broken: {fails}", file=sys.stderr)
    sys.exit(1)
print("\nPASS — all 9 retired names re-export their canonical modules by identity")
