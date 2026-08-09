---
name: trusted-capabilities
description: Discover, evaluate, safely install, and update reusable skills from a curated set of trusted open-source repositories.
---

# Trusted capabilities

Use this skill when the owner asks to find, assess, install, or update reusable
agent skills and tools from public ecosystems.

The catalog is allowlist-only. Skill sources can be installed after inspection.
Tool sources can be discovered and statically inspected, while tool and index
sources remain install-disabled because their setup and runtime permissions are
project-specific.

```bash
C=skills/trusted-capabilities/scripts/catalog.py
python3 "$C" sources
python3 "$C" search browser
python3 "$C" inspect openai-skills skills/.curated/browser-automation
python3 "$C" inspect mcp-reference-servers src/filesystem
python3 "$C" install openai-skills skills/.curated/browser-automation
# Review the dry-run output, then copy its exact commit into the write:
python3 "$C" install openai-skills skills/.curated/browser-automation --commit <40-char-sha> --yes
python3 "$C" update browser-automation
python3 "$C" update browser-automation --commit <40-char-sha> --yes
```

`install` and `update` show the resolved commit, destination, and risk findings,
then print a write command pinned to that immutable commit. Writes require both
the full `--commit <sha>` from the reviewed dry run and `--yes`; resolving the
moving default branch again is not authorization to write. Updates also
re-check that the source remains installable in the current allowlist. The
default destination is the runtime's canonical `$CLAUDE_CONFIG_DIR/skills/`
path via `src/util_paths.py`. Use `--dest-root` for an explicit test or
alternate install root.

Every install is pinned to a Git commit, fetched file-by-file, bounded by file
count and total size, and written atomically. Provenance is recorded in
`.sutando-source.json`; `update` uses it to compare and fetch the same upstream
path.
