#!/usr/bin/env python3
"""Re-apply version-controlled plugin-cache patches at startup.

Why this exists
---------------
Plugin caches under ``$CLAUDE_CONFIG_DIR/plugins/cache/`` are managed like
``node_modules`` — a hand-edit there is clobbered on the next plugin update and
is invisible to git + sync-memory. That "works here / breaks elsewhere" drift is
exactly what bit the fleet once (a local ``group append`` edit on one host that
silently failed on every stock install). The honest fix is to make a kept local
edit **tracked + re-applied + loud**, not a silent cache edit.

Contract
--------
- **Idempotent**: if the patch's ``applied_marker`` is already present in the
  target, skip (the patch is in place — e.g. the host that originally made the
  edit, or a prior run).
- **Fail-loud, never force**: apply only if a ``--dry-run`` succeeds. If the
  dry-run fails (a plugin update shifted the file → the patch no longer applies),
  emit a WARN and move on. We never ``--fuzz``/force a mismatched patch, and a
  stale patch never fails startup — it surfaces as a "re-base me" signal (and the
  drift-audit covers the same case).
- **The real fix is upstream**: a patch here is a *bridge*. When the change is
  broadly good (e.g. these safer subcommands), upstream it to the plugin repo so
  every host gets it on update and the patch retires.

Usage: ``python3 apply-plugin-patches.py``  (called from src/startup.sh)
Exit code is always 0 unless the manifest itself is malformed — a stale/missing
patch is a WARN, not a startup failure.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST = SCRIPT_DIR / "plugin-patches.json"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
TAG = "[plugin-patches]"


def log(level: str, msg: str) -> None:
    stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
    print(f"{TAG} {level:5} {msg}", file=stream, flush=True)


def have_patch_tool() -> bool:
    try:
        subprocess.run(["patch", "--version"], capture_output=True, check=False)
        return True
    except FileNotFoundError:
        return False


def apply_one(entry: dict) -> None:
    patch_file = SCRIPT_DIR / entry["patch"]
    marker = entry["applied_marker"]
    target_glob = entry["target_glob"]

    if not patch_file.is_file():
        log("WARN", f"patch file missing: {patch_file.name} — skipping")
        return

    targets = glob.glob(str(CLAUDE_HOME / target_glob))
    if not targets:
        log("INFO", f"{patch_file.name}: target not installed ({target_glob}) — skipping")
        return

    for tgt in targets:
        try:
            body = Path(tgt).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log("WARN", f"{patch_file.name}: cannot read {tgt}: {e}")
            continue

        if marker in body:
            log("OK", f"{patch_file.name}: already applied -> {tgt}")
            continue

        # Dry-run first; never force a mismatched patch.
        dry = subprocess.run(
            ["patch", "--dry-run", "--forward", str(tgt), "-i", str(patch_file)],
            capture_output=True, text=True,
        )
        if dry.returncode != 0:
            log("WARN", (
                f"{patch_file.name}: does NOT apply cleanly to {tgt} — the plugin "
                f"likely updated. NOT forcing. Re-base the patch (or upstream the "
                f"change and delete it). patch said: "
                f"{(dry.stdout or dry.stderr).strip().splitlines()[-1] if (dry.stdout or dry.stderr).strip() else 'context mismatch'}"
            ))
            continue

        real = subprocess.run(
            ["patch", "--forward", str(tgt), "-i", str(patch_file)],
            capture_output=True, text=True,
        )
        if real.returncode == 0:
            log("OK", f"{patch_file.name}: applied -> {tgt}  ({entry.get('desc', '')[:60]})")
        else:
            log("WARN", f"{patch_file.name}: dry-run passed but apply failed on {tgt} "
                        f"(skipped, not forced): {(real.stdout or real.stderr).strip()[:120]}")


def main() -> int:
    if not MANIFEST.is_file():
        log("INFO", "no manifest — nothing to apply")
        return 0
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log("ERROR", f"manifest is not valid JSON: {e}")
        return 1  # the only hard failure — a broken manifest is a real bug
    if not have_patch_tool():
        log("WARN", "`patch` tool not found on PATH — skipping all plugin patches")
        return 0
    for entry in data.get("patches", []):
        try:
            apply_one(entry)
        except Exception as e:  # one bad entry never breaks the rest / startup
            log("WARN", f"entry {entry.get('patch', '?')} errored (skipped): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
