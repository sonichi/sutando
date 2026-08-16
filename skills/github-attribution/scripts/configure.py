#!/usr/bin/env python3
"""Configure GitHub account and performer-kind attribution claims."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from attribution_claims import (  # noqa: E402
    AttributionClaimWriter, canonical_account_id, performer_kind_policy_claim,
    uses_account_claim,
)
from sutando_config import resolve_workspace  # noqa: E402
from util_paths import _host_label  # noqa: E402


def _identity() -> dict:
    process = subprocess.run(
        ["gh", "api", "user", "--jq", "{id:.id,login:.login}"],
        check=True, capture_output=True, text=True, timeout=20,
    )
    value = json.loads(process.stdout)
    if not isinstance(value, dict) or not value.get("id") or not value.get("login"):
        raise RuntimeError("GitHub returned an invalid authenticated identity")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--repository", action="append", default=[])
    parser.add_argument("--all-repositories", action="store_true")
    parser.add_argument("--exclude-repository", action="append", default=[])
    parser.add_argument("--object-type", action="append", default=[])
    parser.add_argument("--all-object-types", action="store_true")
    parser.add_argument("--exclude-object-type", action="append", default=[])
    parser.add_argument("--not-before")
    parser.add_argument("--not-after")
    args = parser.parse_args()
    if not args.repository and not args.all_repositories:
        parser.error("pass --repository at least once or explicitly use --all-repositories")
    if not args.object_type and not args.all_object_types:
        parser.error("pass --object-type at least once or explicitly use --all-object-types")
    identity = _identity()
    account = canonical_account_id("github", str(identity["id"]))
    asserted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    scope = {
        "provider": "github",
        "account_ids": [account],
        "resource_ids": sorted(set(args.repository)),
        "object_types": sorted(set(args.object_type)),
        "exclude_resource_ids": sorted(set(args.exclude_repository)),
        "exclude_object_types": sorted(set(args.exclude_object_type)),
    }
    if args.not_before:
        scope["not_before"] = args.not_before
    if args.not_after:
        scope["not_after"] = args.not_after
    workspace = Path(resolve_workspace())
    writer = AttributionClaimWriter(
        workspace / "hosts" / _host_label() / "attribution" / "claims.jsonl")
    connection = uses_account_claim(
        principal_id=args.agent_id, account_id=account,
        basis="provider_auth_observed", asserted_at=asserted_at,
        author=args.owner_id,
        dedupe_key=f"github-connection:{args.agent_id}:{account}:{asserted_at}",
    )
    scope_key = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    policy = performer_kind_policy_claim(
        account_id=account, performer_kind="agent", scope=scope,
        asserted_at=asserted_at, author=args.owner_id,
        dedupe_key=f"github-agent-policy:{scope_key}:{asserted_at}",
    )
    statuses = [writer.append(connection), writer.append(policy)]
    print(json.dumps({
        "account_id": account, "login": identity["login"],
        "connection_claim": connection["id"], "policy_claim": policy["id"],
        "statuses": statuses, "shard": str(writer.path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
