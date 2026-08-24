#!/usr/bin/env python3
"""The Slack watchdog DM must resolve its token the way the bridge does.

`_slack_owner_creds()` is how health-check reaches the owner when something
breaks. It read env -> .env only, so on a vault-only host it returned None and
the alert was dropped silently — the alerting channel failing with no alert
that the alerting channel failed.

The same file's relaunch path (~:3499) already tiers env -> .env -> vault and
says so in a comment, so this was an inconsistency within one file rather than
an unknown.

Run: python3 tests/health-check-slack-watchdog-vault-tier.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

failures = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}  {extra}")


def creds_on_host(*, env_token=None, envfile_token=None, vault_token=None,
                  owner="U123OWNER"):
    """Drive the real _slack_owner_creds() against a synthetic host.

    Returns (creds, vault_vars_asked). Each tier is supplied independently so a
    case can isolate exactly one of them.
    """
    import channel_token as ct
    asked = []
    saved_vault = ct.token_from_vault
    saved_env = os.environ.get("SLACK_BOT_TOKEN")
    saved_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    cfg = tempfile.mkdtemp()
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        slack = Path(cfg, "channels", "slack")
        slack.mkdir(parents=True)
        # access.json is always valid, so the token is the ONLY variable. The
        # shape matches a live host: tofuOwner is IN allowFrom at owner tier.
        _access = {"tofuOwner": owner}
        if owner:
            _access["allowFrom"] = [owner]
            _access["tierMap"] = {owner: "owner"}
        (slack / "access.json").write_text(json.dumps(_access))
        if envfile_token:
            (slack / ".env").write_text(f"SLACK_BOT_TOKEN={envfile_token}\n")
        if env_token:
            os.environ["SLACK_BOT_TOKEN"] = env_token
        else:
            os.environ.pop("SLACK_BOT_TOKEN", None)

        def fake_vault(var, vault_get=None):
            asked.append(var)
            return vault_token or ""

        ct.token_from_vault = fake_vault
        spec = importlib.util.spec_from_file_location(
            "hc_slack", REPO / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(hc)
        except SystemExit:
            pass
        return hc._slack_owner_creds(), asked
    finally:
        ct.token_from_vault = saved_vault
        os.environ.pop("SLACK_BOT_TOKEN", None)
        if saved_env is not None:
            os.environ["SLACK_BOT_TOKEN"] = saved_env
        if saved_cfg is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = saved_cfg
        else:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)


print("vault-only host (the regression):")
creds, asked = creds_on_host(vault_token="xoxb-vault")
check("the watchdog can reach the owner", creds is not None, repr(creds))
check("the vault was actually consulted", "SLACK_BOT_TOKEN" in asked, repr(asked))
check("and it is the vault's token", bool(creds) and creds[0] == "xoxb-vault", repr(creds))

print("controls — each tier alone still works, and precedence is unchanged:")
creds, asked = creds_on_host(env_token="xoxb-env", vault_token="xoxb-vault")
check("env wins over vault", bool(creds) and creds[0] == "xoxb-env", repr(creds))
check("...and short-circuits it", asked == [], repr(asked))

creds, asked = creds_on_host(envfile_token="xoxb-file", vault_token="xoxb-vault")
check(".env wins over vault", bool(creds) and creds[0] == "xoxb-file", repr(creds))
check("...and short-circuits it", asked == [], repr(asked))

print("control — a genuinely unconfigured host must still be None:")
creds, asked = creds_on_host()
check("no token anywhere -> None", creds is None, repr(creds))
check("the vault was still asked before giving up", "SLACK_BOT_TOKEN" in asked, repr(asked))

print("control — a token with no resolvable owner is still None:")
creds, _ = creds_on_host(vault_token="xoxb-vault", owner="")
check("token without owner -> None (nobody to DM)", creds is None, repr(creds))

print("the recipient is TIER-selected, not the first allowFrom entry:")
# Activating the vault path turned "no send" into a send, so recipient policy is
# newly reachable here — a team-tier or demoted user must NOT get a watchdog DM.
def creds_with_access(access: dict, vault_token="xoxb-vault"):
    import importlib.util
    import json as _j
    import os
    import tempfile

    import channel_token as ct
    saved_v, saved_cfg = ct.token_from_vault, os.environ.get("CLAUDE_CONFIG_DIR")
    cfg = tempfile.mkdtemp()
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        slack = Path(cfg, "channels", "slack"); slack.mkdir(parents=True)
        (slack / "access.json").write_text(_j.dumps(access))
        os.environ.pop("SLACK_BOT_TOKEN", None)
        ct.token_from_vault = lambda var, vault_get=None: vault_token
        spec = importlib.util.spec_from_file_location("hc_tier", REPO / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(hc)
        except SystemExit:
            pass
        return hc._slack_owner_creds()
    finally:
        ct.token_from_vault = saved_v
        if saved_cfg is not None: os.environ["CLAUDE_CONFIG_DIR"] = saved_cfg
        else: os.environ.pop("CLAUDE_CONFIG_DIR", None)

c = creds_with_access({"allowFrom": ["team-only"], "tierMap": {"team-only": "team"}})
check("a team-tier-only host sends to NOBODY", c is None, repr(c))

c = creds_with_access({"tofuOwner": "demoted", "allowFrom": ["demoted", "realowner"],
                       "tierMap": {"demoted": "team", "realowner": "owner"}})
check("a DEMOTED tofuOwner does not win", bool(c) and c[1] == "realowner", repr(c))

c = creds_with_access({"allowFrom": ["teamguy", "realowner"],
                       "tierMap": {"teamguy": "team", "realowner": "owner"}})
check("team-first ordering does not win", bool(c) and c[1] == "realowner", repr(c))

c = creds_with_access({"tofuOwner": "o1", "allowFrom": ["o1"]})
check("control: an unmapped allowlist keeps the legacy owner default",
      bool(c) and c[1] == "o1", repr(c))

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — the Slack watchdog resolves env -> .env -> vault")
