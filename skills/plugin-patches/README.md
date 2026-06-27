# plugin-patches — keep a useful plugin-cache edit, fleet-wide, without silent drift

Plugin caches under `$CLAUDE_CONFIG_DIR/plugins/cache/` are managed like
`node_modules`: a hand-edit there is **clobbered on the next plugin update** and
is **invisible to git + sync-memory**. So a genuinely-useful local tweak to a
plugin file silently *works on the host that made it and breaks on every other*.
That actually happened once: a `group append` subcommand was hand-added to the
`discord:access` skill on one host; the bot then recommended `group append` to
the owner, and it failed on every stock install — and nobody noticed for six
weeks, because nothing flagged the divergence.

This directory is the fix: turn a kept plugin-cache edit into something
**tracked, re-applied, and loud** instead of a silent in-place edit.

## How it works

- **`*.patch`** — a unified diff (`diff -u` of the *pristine* plugin file vs the
  edited one). Generate the pristine from the git-tracked marketplace clone:
  `~/.claude/plugins/marketplaces/<marketplace>/external_plugins/<plugin>/...`.
- **`plugin-patches.json`** — manifest: each patch + the `target_glob` it applies
  to (relative to `$CLAUDE_CONFIG_DIR`) + an `applied_marker` (a string that's
  present once the patch is in) + a description.
- **`apply-plugin-patches.py`** — run at startup (wired into `src/startup.sh`).
  For each manifest entry, on each host:
  - **idempotent** — marker already present ⇒ skip (already patched).
  - **fail-loud, never force** — apply only if `patch --dry-run` succeeds. If a
    plugin update shifted the file so the patch no longer applies, emit a `WARN`
    and move on. It **never** `--fuzz`/forces a mismatched patch, and a stale
    patch **never fails startup**.

Net: the edit is **kept** (re-applied after a plugin clobber), **synced** (every
host pulls this dir and applies it), and **visible** (it's in git, and a stale
apply is loud — also surfaceable by a drift-audit).

## The condition you must know

A `diff` patch is **version-pinned** — it carries context lines from the exact
plugin version it was cut from. On a plugin bump that rewrites the target file,
the patch won't apply; the applier WARNs ("re-base the patch") rather than
silently dropping the edit. Re-cut the patch against the new pristine when that
happens.

## The real endgame: upstream it

A patch here is a **bridge**, not a home. If the change is broadly good — like
`group append` / `group rm-allow`, which add-without-replacing and avoid the
"lock yourself out" trap of stock `group add` — **upstream it** to the plugin's
repo. Then every host gets it on update, there's no version-pinned patch to
re-base, and you delete the entry here.

## Adding a patch

1. `diff -u --label a/<path> --label b/<path> <pristine> <edited> > skills/plugin-patches/<name>.patch`
   (use generic `a/`,`b/` labels — never commit absolute `/Users/...` paths).
2. Add an entry to `plugin-patches.json` (`target_glob` relative to `$CLAUDE_CONFIG_DIR`, an `applied_marker`).
3. Done — it applies on the next startup, idempotently, on every host.
