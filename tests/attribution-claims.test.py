#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from attribution_claims import (  # noqa: E402
    AttributionClaimWriter, AttributionError, AttributionStoreError,
    canonical_account_id, canonical_event_id, performer_kind_policy_claim,
    retraction_claim, uses_account_claim, validate_claim,
)

AGENT = "agent:018f0f65-7b4a-7cc1-8f52-8c6ad9a60d7d"
OWNER = "human:018f0f65-7b4a-7cc1-8f52-8c6ad9a60d7e"
FAILS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}" + ("" if condition else f" — {detail}"))
    if not condition:
        FAILS.append(label)


print("── versioned fixture ──")
fixture = REPO / "tests" / "fixtures" / "attribution-claims-v1.jsonl"
rows = [validate_claim(json.loads(line)) for line in fixture.read_text().splitlines()]
check("shared fixture contains all v1 predicates used by Life", len(rows) == 3)
check("provider object identity ignores mutable taxonomy", canonical_event_id({
    "provider": "github", "account_id": "account:github:7",
    "resource_id": "owner/example", "object_type": "push", "object_id": "99",
}) == rows[2]["subject"])

print("── writer contract ──")
tmp = Path(tempfile.mkdtemp(prefix="attribution-claims-"))
shard = tmp / "claims.jsonl"
writer = AttributionClaimWriter(shard)
check("first append records", writer.append(rows[0]) == "recorded")
before = shard.read_bytes()
check("identical append is idempotent", writer.append(rows[0]) == "duplicate")
check("idempotent append does not change bytes", shard.read_bytes() == before)
collision = dict(rows[0], object="account:github:8")
try:
    writer.append(collision)
    check("same claim ID with different content fails closed", False)
except AttributionStoreError:
    check("same claim ID with different content fails closed", True)
shard.write_bytes(shard.read_bytes() + b'{"partial":')
try:
    writer.append(rows[1])
    check("partial final line fails closed", False)
except AttributionStoreError:
    check("partial final line fails closed", True)

print("── concurrent production writer ──")
concurrent = tmp / "concurrent.jsonl"
worker = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from attribution_claims import AttributionClaimWriter, canonical_account_id, uses_account_claim
i = int(sys.argv[3])
claim = uses_account_claim(
    principal_id='agent:018f0f65-7b4a-7cc1-8f52-8c6ad9a60d7d',
    account_id=canonical_account_id('github', str(i + 10)), basis='owner_asserted',
    asserted_at='2026-08-16T17:00:00Z',
    author='human:018f0f65-7b4a-7cc1-8f52-8c6ad9a60d7e',
    dedupe_key=f'concurrent:{i}',
)
AttributionClaimWriter(Path(sys.argv[2])).append(claim)
"""
processes = [subprocess.Popen([
    sys.executable, "-c", worker, str(REPO / "src"), str(concurrent), str(i),
]) for i in range(12)]
codes = [process.wait(timeout=10) for process in processes]
check("all writer processes exited cleanly", all(code == 0 for code in codes))
concurrent_rows = [validate_claim(json.loads(line)) for line in concurrent.read_text().splitlines()]
check("concurrent shard has one valid line per claim", len(concurrent_rows) == 12)
check("writer keeps private file permissions", oct(os.stat(concurrent).st_mode & 0o777) == "0o600")

print("── policy and retraction ──")
policy = performer_kind_policy_claim(
    account_id="account:github:7", performer_kind="agent",
    scope={"provider": "github", "account_ids": ["account:github:7"],
           "resource_ids": ["owner/example"], "object_types": ["push"]},
    asserted_at="2026-08-16T17:00:00Z", author=OWNER,
    dedupe_key="policy:test",
)
check("scoped owner policy validates", validate_claim(policy)["scope"]["resource_ids"] == ["owner/example"])
try:
    performer_kind_policy_claim(
        account_id="account:github:7", performer_kind="agent",
        scope={"provider": "github", "account_ids": ["account:github:8"]},
        asserted_at="2026-08-16T17:00:00Z", author=OWNER, dedupe_key="bad-policy",
    )
    check("policy cannot escape its subject account", False)
except AttributionError:
    check("policy cannot escape its subject account", True)
retraction = retraction_claim(
    target_claim_id=policy["id"], asserted_at="2026-08-16T17:01:00Z",
    author=OWNER, dedupe_key="retract:policy:test",
)
check("retraction is a new claim targeting old bytes", retraction["object"] == policy["id"])

if FAILS:
    print(f"\nFAIL — {len(FAILS)} attribution claim check(s): {', '.join(FAILS)}")
    raise SystemExit(1)
print("\nPASS — attribution claim contract")
