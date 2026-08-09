#!/usr/bin/env python3
"""Cross-loader config contract: the tracked default config must be readable by BOTH
loaders and valid against its own schema.

WHY THIS EXISTS: the same drift has now blocked two PRs independently.
  #2316 — `health_check` landed in sutando.config.json + the Python loader, but not in
          src/sutando_config.ts's KNOWN_TOP_LEVEL_KEYS, so every TS service warned
          "has top-level keys the loader does not read: 'health_check'".
  #2308 — `bridges` landed the same way, AND docs/sutando-config.schema.json declares
          `additionalProperties: false` without a `bridges` property, so the repo's own
          checked-in default config did not validate against the repo's own schema.

Twice is not coincidence, it's a missing guardrail. `src/sutando_config.ts` documents
itself as the Python twin but nothing enforced that, and the schema had no consistency
check at all. This test makes the contract structural instead of relying on a reviewer
catching it a third time.

Run: python3 tests/sutando-config-twin-contract.test.py
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "sutando.config.json"
SCHEMA = REPO / "docs" / "sutando-config.schema.json"
TS_TWIN = REPO / "src" / "sutando_config.ts"

failures = []


def check(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗'} {name}")
    if not ok:
        failures.append(f"{name}: {detail}")


def ts_known_top_level_keys(text):
    """Parse KNOWN_TOP_LEVEL_KEYS out of the TS twin without executing it."""
    m = re.search(r"KNOWN_TOP_LEVEL_KEYS\s*=\s*new Set\(\[(.*?)\]\)", text, re.S)
    if not m:
        return None
    return set(re.findall(r"'([^']+)'", m.group(1)))


print("cross-loader config contract")

config = json.loads(CONFIG.read_text())
schema = json.loads(SCHEMA.read_text())
ts_keys = ts_known_top_level_keys(TS_TWIN.read_text())

check("TS twin exposes a parseable KNOWN_TOP_LEVEL_KEYS", ts_keys is not None,
      "regex found no Set literal — did the declaration shape change?")
ts_keys = ts_keys or set()

cfg_keys = set(config)
schema_props = set(schema.get("properties", {}))

# 1. Every key in the tracked default config must be known to the TS loader, or every
#    TS consumer prints an unknown-key warning on its first config load (#2316).
missing_ts = sorted(cfg_keys - ts_keys)
check("every tracked config key is in the TS twin's KNOWN_TOP_LEVEL_KEYS",
      not missing_ts,
      f"missing from src/sutando_config.ts: {missing_ts}")

# 2. When the schema is closed (additionalProperties: false), every key in the tracked
#    default config must be declared or the repo's own config fails its own schema (#2308).
closed = schema.get("additionalProperties") is False
missing_schema = sorted(cfg_keys - schema_props)
check("schema is closed (additionalProperties: false)", closed,
      "schema is open; the drift this test guards would go undetected")
check("every tracked config key is declared in the JSON schema",
      not missing_schema,
      f"missing from docs/sutando-config.schema.json: {missing_schema}")

# 3. Latent direction: a key the TS loader accepts but the schema rejects means any user
#    who actually sets it gets a config that fails validation. Caught 2026-07-30: `migrate`
#    was in the TS twin but absent from the schema.
ts_not_in_schema = sorted(ts_keys - schema_props)
check("every TS-known key is declared in the JSON schema",
      not ts_not_in_schema,
      f"TS accepts but schema rejects (a user setting these gets an invalid config): "
      f"{ts_not_in_schema}")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("config contract holds across sutando.config.json, the JSON schema, and the TS twin.")
