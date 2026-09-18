#!/bin/bash
# Generic, skill-agnostic: reads every installed skill's manifest.json "config"
# block (skills/MANIFEST.md's own convention, previously Node-only via
# inline-tools.ts) and prints "KEY=VALUE" for each key, NUL-terminated. Never
# names a skill; the caller decides what "already set" means and exports.
#
# A manifest is attacker-adjacent: skills/trusted-capabilities installs
# third-party skills. So the key is validated here, not at the export site, and
# records are NUL-framed so a value's newline cannot forge a second record.

skill_manifest_config_pending() {
  local repo="$1" py="$2" m
  [ -n "$py" ] && [ -x "$py" ] || return 0
  for m in "$repo"/skills/*/manifest.json; do
    [ -f "$m" ] || continue
    "$py" -c '
import json, re, sys

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Exec-hijack vectors and loader injection: a skill must never set these,
# whatever it declares. Mirrors backend-supervisor.mjs scrubShellStartupEnv.
PROTECTED = {"PATH", "IFS", "ENV", "BASH_ENV", "SHELLOPTS", "BASHOPTS", "PS4", "HOME", "SHELL"}
PROTECTED_PREFIX = ("LD_", "DYLD_", "BASH_FUNC_")

try:
    cfg = json.load(open(sys.argv[1])).get("config") or {}
except Exception:
    cfg = {}
if not isinstance(cfg, dict):
    cfg = {}
for k, v in cfg.items():
    if not isinstance(k, str) or not isinstance(v, str):
        continue
    if not IDENT.match(k):
        print(f"skill-manifest-config: {sys.argv[1]}: skipping {k!r} (not a shell identifier)",
              file=sys.stderr)
        continue
    if k in PROTECTED or k.startswith(PROTECTED_PREFIX):
        print(f"skill-manifest-config: {sys.argv[1]}: refusing to set {k} (protected)",
              file=sys.stderr)
        continue
    # An env value with a control char is never intended, and every consumer of
    # --print-core-env is line-oriented: NUL framing stops the injection, this
    # stops the corrupted record downstream of it.
    if any(c in v for c in ("\n", "\r", "\0")):
        print(f"skill-manifest-config: {sys.argv[1]}: skipping {k} (control character in value)",
              file=sys.stderr)
        continue
    sys.stdout.write(f"{k}={v}\0")
' "$m"
  done
}
