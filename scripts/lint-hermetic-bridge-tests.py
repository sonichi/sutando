#!/usr/bin/env python3
"""Sutando lint: a test that imports a bridge MUST isolate CLAUDE_CONFIG_DIR first.

WHY
---
`src/discord-bridge.py` (and the slack/telegram siblings) resolve channel config at
**module level**, so the work happens during `exec_module`, before a test can intervene:

    src/discord-bridge.py:205   channels_env = claude_home_path("channels", "discord", ".env")
    src/discord-bridge.py:555   ACCESS_FILE  = channel_access_path("discord")

`channel_access_path()` reads `$CLAUDE_CONFIG_DIR` and falls back to the LEGACY real-home
`~/.claude/channels/<ch>/access.json` when the canonical path is missing. A test that does
not set `CLAUDE_CONFIG_DIR` therefore inherits whatever the developer happens to have, and
the symptom differs per machine:

  * clean box     -> legacy fallback + `[util_paths] DEPRECATION: using legacy ...`
  * operator box  -> silently imports that operator's REAL channel allowlist

Verified 2026-07-30 by re-running the import with `CLAUDE_CONFIG_DIR` popped from the env:
`ACCESS_FILE = /Users/<operator>/.claude/channels/discord/access.json`. Green everywhere,
trustworthy nowhere. Setting a bot token alone does NOT help — that only stops the `.env`
read, never the access resolution.

THE FIX a test must apply (before `exec_module`):

    os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-...")
    _cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
    _cfg.mkdir(parents=True, exist_ok=True)
    (_cfg / "access.json").write_text('{"allowFrom": []}')

DETECTION NOTES (all learned the hard way)
------------------------------------------
1. Detection is **AST-based, not regex**, and order-aware. Two earlier text-scanning drafts
   were bypassable, both demonstrated by qingyun on #2429:
     * an assignment-SHAPED comment (`# os.environ["CLAUDE_CONFIG_DIR"] = ...`) matched the
       regex — comments never reach the AST;
     * a REAL assignment placed AFTER `exec_module()` matched too — isolation that executes
       after the import is useless, because the module-level resolution already ran.
   Isolation now counts only when an executable assignment precedes the bridge import.
   This is not theoretical: `tests/bridge-env-token-perms.test.py` sets CLAUDE_CONFIG_DIR at
   line 179 but calls exec_module at line 124, and the regex draft called it clean.
   An unparseable file is treated as a VIOLATION — a file that cannot be analysed is not
   proven clean.
2. Recognize **post-import mitigation**. `tests/slack-bridge-tier-map.test.py` reassigns
   `mod.ACCESS_FILE` to a temp path after `exec_module`, deliberately, so its destructive
   write/unlink cannot touch the operator's real file. The import still resolves host config,
   so it is not clean — but it is not the same defect, and hard-failing the one author who
   thought about this is how lints get switched off. It reports as MITIGATED (non-fatal).

Usage:
  python3 scripts/lint-hermetic-bridge-tests.py           # scan whole tree (report + gate)
  python3 scripts/lint-hermetic-bridge-tests.py --diff    # scan only files added/modified vs BASE_REF
  python3 scripts/lint-hermetic-bridge-tests.py --list    # print current violators, exit 0

Exit 1 ONLY when a test outside KNOWN_UNISOLATED violates. A KNOWN_UNISOLATED entry that no
longer violates is reported as a NOTE, not a failure — hard-failing there is a footgun: the
moment a PR fixes a listed file, main goes red until someone edits this script. (Found while
testing this lint: #2428 fixes tests/bridge-audit-wiring.test.py, and a fatal stale-check
would have reddened main on its merge.)
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    or "."
)

BRIDGE_IMPORT = re.compile(r"(discord|slack|telegram)-bridge\.py")

# Grandfathered: known-unisolated at the time this lint landed.
#
# Net-flat this round, two offsetting edits:
#   - REMOVED tests/dm-result-multipart-upload.test.py — it never imported a bridge;
#     it only NAMED one in prose, and target selection used to regex the raw file
#     text. _code_strings() now selects structurally, so the entry went stale and
#     the stale-entry check (correctly) failed the run until it was dropped.
#   + ADDED tests/discord-access-backup.test.py — a REAL hole, not a false positive:
#     it builds channels/discord/access.json under a mkdtemp inside a class fixture
#     while exec_module runs at MODULE level, so the import still resolves against
#     the developer's real config. It landed in #2358, after the 2026-07-30 baseline
#     below was measured, which is why it is absent from it. Grandfathered rather
#     than fixed here: repairing another PR's test is a second concern, and
#     CONTRIBUTING forbids bundling it. Follow-up tracked separately. Mini's shared-helper
# migration removes these; the stale-entry check below forces the list to shrink.
#   + ADDED tests/dm-fallback-digest-no-dm.test.py — false positive: it only
#     reads the bridge's source text and execs an unrelated snippet, never imports it.
# Measured on origin/main (2026-07-30, post-#2428-merge) with the AST classifier. The count rose
# from 26 to 27 when detection moved off regex: two files the regex called clean were real
# bypasses (assignment-shaped comment / assignment after exec_module), which is exactly the
# P1 qingyun raised on #2429.
KNOWN_UNISOLATED = frozenset(
    """
tests/audio-transcribe-skill.test.py
tests/discord-access-backup.test.py
tests/bridge-env-token-perms.test.py
tests/bridge-not-allowlisted-ack.test.py
tests/bridge-restart-intercept.test.py
tests/bridge-skill-path-resolution.test.py
tests/bridges-allowlist-default-readonly.test.py
tests/bridges-sending-orphan-recovery.test.py
tests/discord-bridge-attachment-filename-sanitize.test.py
tests/discord-bridge-codex-subprocess-argv.test.py
tests/discord-bridge-collaborator-tier.test.py
tests/discord-bridge-delivery-failure-visible.test.py
tests/discord-bridge-delivery-sentinel.test.py
tests/discord-bridge-dm-catchup.test.py
tests/discord-bridge-dm-fallback-source-guard.test.py
tests/discord-bridge-mod-judge-codex.test.py
tests/discord-bridge-mod-judge-trackers.test.py
tests/discord-bridge-multibot-seed-gate.test.py
tests/discord-bridge-reply-directive.test.py
tests/discord-bridge-task-write-instrument.test.py
tests/discord-task-source-invariance.test.py
tests/discord-writeside-attachments.test.py
tests/dm-fallback-digest-no-dm.test.py
tests/health-check-fix-down-bridges.test.py
tests/owner-activity-channel-id.test.py
tests/slack-bridge-access-durable-backup.test.py
tests/slack-bridge-allowlist.test.py
tests/slack-bridge-channel-context.test.py
tests/slack-bridge-chunking.test.py
tests/slack-bridge-download-html-guard.test.py
tests/slack-bridge-download-stream.test.py
tests/slack-bridge-orphan-recovery.test.py
tests/slack-bridge-pending-recovery.test.py
tests/slack-bridge-task-timeout.test.py
tests/slack-bridge-tier-map.test.py
tests/slack-bridge-tofu-enroll.test.py
tests/slack-bridge-write-task.test.py
tests/slack-proactive-delivery-idempotency.test.py
tests/slack-proactive-owner-resolution.test.py
tests/slack-writeside-attachments.test.py
tests/telegram-bridge-access.test.py
tests/telegram-bridge-forward-attribution.test.py
tests/telegram-bridge-proactive-owner-resolution.test.py
tests/telegram-bridge-progress-stream.test.py
tests/telegram-bridge-tofu-enroll.test.py
tests/telegram-bridge-tofu.test.py
tests/telegram-writeside-attachments.test.py
""".split()
)

CLEAN, MITIGATED, VIOLATION = "clean", "mitigated", "violation"


# This lint's own test builds fixture strings containing `exec_module` and a bridge path,
# so a naive scan classifies the test file itself as in-scope. Exempt it, the same way
# scripts/lint-claude-home-path.sh exempts itself for quoting the pattern it forbids.
SELF_EXEMPT = {"tests/lint-hermetic-bridge-tests.test.py"}


def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_os_environ(node) -> bool:
    """True only for a literal `os.environ` receiver.

    Deliberately does NOT accept a bare `environ`, nor any attribute merely NAMED environ.
    qingyun demonstrated both bypasses on #2429: `fake.environ["CLAUDE_CONFIG_DIR"] = ...` and
    a shadowed `environ = {}` each classified clean while the real inherited CLAUDE_CONFIG_DIR
    stayed active. Proving a bare `environ` is `from os import environ` AND unshadowed is more
    analysis than this gate needs; requiring the explicit form costs a test author nothing.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


# Recognised sources of an ISOLATED directory. Deliberately a closed list: the
# question is not "is this a path" but "is this provably NOT the operator's own
# config dir", and only an explicitly-temporary source answers that.
_TMP_FACTORIES = {"mkdtemp", "TemporaryDirectory", "mkstemp"}


def _is_isolated_value(node, isolated_names: "set[str]") -> bool:
    """True only when the value provably comes from a temporary directory.

    The fourth false-CLEAN of this predicate, found by self-audit rather than
    review: `_isolation_line` proved an ASSIGNMENT happened, never that the value
    pointed anywhere safe. A test doing

        os.environ["CLAUDE_CONFIG_DIR"] = os.path.expanduser("~/.claude")

    and then seeding `channels/discord/access.json` under it classified CLEAN — while
    writing into the OPERATOR'S REAL allowlist. That is strictly worse than the
    fallback-read this gate was built to stop: it mutates host config.

    Under-approximating CLEAN is the documented stance of this file, so anything not
    recognisably temporary is refused, including a bare name whose origin we cannot
    see. `pytest`'s `tmp_path` is not accepted here because these are plain scripts,
    not pytest tests; add it deliberately if that ever changes.
    """
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            fn = c.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
            if name in _TMP_FACTORIES:
                return True
        if isinstance(c, ast.Name) and c.id in isolated_names:
            return True
    return False


def _isolation_line(tree: ast.Module) -> int | None:
    """Earliest MODULE-LEVEL `os.environ["CLAUDE_CONFIG_DIR"] = ...`, else None.

    Deliberately narrow. Proving a test is hermetic is hard — env precedence, execution
    context and reachability all matter — so this does not try. It recognizes exactly the
    one documented fix and treats everything else as unproven. Under-approximating CLEAN is
    safe; over-approximating it makes the gate worthless, and every hole qingyun found on
    #2429 was a false CLEAN:

      * `cfg["CLAUDE_CONFIG_DIR"] = ...` — a dict that is not the environment. Receiver is
        now checked, not just the key.
      * `os.environ.setdefault("CLAUDE_CONFIG_DIR", ...)` — a NO-OP when the developer
        already has the var set, which is precisely the case the lint exists to catch.
      * `HOME` / `CLAUDE_HOME` only — lower precedence than an inherited CLAUDE_CONFIG_DIR,
        so it does not guarantee anything.
      * `with patch(...): pass` before the import — the patch has EXPIRED by the time
        exec_module runs. Statically proving a patch is active at the import is not
        something line numbers can do, so patch-based isolation is no longer accepted.

    Module level is required so the assignment is guaranteed to execute: a body nested in a
    function, branch or with-block may never run, or may run after the import.
    """
    # POINT-IN-TIME, not final-state. Two opposite errors live here:
    #   * A monotone set says `d` is isolated forever, so
    #         d = mkdtemp(); d = "/Users/<me>/.claude"
    #         os.environ["CLAUDE_CONFIG_DIR"] = d
    #     reads as "provably isolated" while it actually points the env var at the
    #     operator's REAL config dir — the test then writes into it and imports the
    #     bridge against it.
    #   * A final-state set makes the opposite mistake: in
    #         _ccd = mkdtemp(); os.environ["CLAUDE_CONFIG_DIR"] = _ccd
    #         _ccd = "/tmp/elsewhere"
    #     the env var WAS set to a temp dir and a later rebinding does not un-set
    #     it, so calling that unisolated is a false positive — and a false positive
    #     is what gets a lint switched off (#2392/#2407).
    # Both vanish if the question is asked where it is actually asked: what did this
    # name hold at the line the assignment executes?
    isolated: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and _is_os_environ(tgt.value)
                and _const_str(tgt.slice) == "CLAUDE_CONFIG_DIR"
                # ...AND the value must provably be a temporary dir. Pointing the env
                # var at the operator's real ~/.claude is an assignment, not isolation.
                and _is_isolated_value(node.value, isolated)
            ):
                return node.lineno
        # Update AFTER testing this statement, so the test above sees the state as
        # of the line it is on.
        this_isolated = _is_isolated_value(node.value, isolated)
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                if this_isolated:
                    isolated.add(tgt.id)
                else:
                    isolated.discard(tgt.id)
    return None

# A seed is a WRITE. Two call shapes exist in this repo and both really occur:
#   method style : (dir / "access.json").write_text(...)
#   helper style : write_private_text(dir / "access.json", ...)   (#2356)
# The helper takes the path as an ARGUMENT, so a receiver-only check misses it.
_WRITE_METHODS = {"write_text", "write_bytes"}
_WRITE_HELPERS = {"write_private_text"}

# A write only creates a file if its PARENT DIRECTORY already exists. `mkdtemp()` hands
# back an EMPTY directory, so `$CLAUDE_CONFIG_DIR/channels/<ch>/` does not exist until the
# test makes it, and a bare
#     (cfg / "channels" / "discord" / "access.json").write_text("{}")
# raises FileNotFoundError having created nothing. Swallow that error and the bridge
# still imports, with `channel_access_path()` still falling back to the operator's real
# allowlist — a write that was recorded as a seed while the canonical file never existed.
# The write alone was therefore never evidence; this module's own docstring has always
# shown `mkdir(parents=True, exist_ok=True)` as part of the safe shape, and the predicate
# now requires it.
#
# `parents=True` is required, not decorative: `channels/` itself is absent in a fresh
# mkdtemp, so a plain `.mkdir(exist_ok=True)` on `channels/<ch>` raises too.
# `os.makedirs` is recursive by definition and needs no keyword.
_MKDIR_METHODS = {"mkdir"}
_MAKEDIRS_FUNCS = {"makedirs"}


def _is_ccd_ref(node) -> bool:
    """True for a literal `os.environ["CLAUDE_CONFIG_DIR"]` READ."""
    return (
        isinstance(node, ast.Subscript)
        and _is_os_environ(node.value)
        and _const_str(node.slice) == "CLAUDE_CONFIG_DIR"
    )


def _ccd_root_names(tree: ast.Module) -> "set[str]":
    """Names that hold the CONFIGURED config-dir root.

    Seeded from the value assigned to `os.environ["CLAUDE_CONFIG_DIR"]` — the
    `_ccd = mkdtemp(); os.environ["CLAUDE_CONFIG_DIR"] = _ccd` shape — so a path
    later built from `_ccd` is recognized as canonical just like one built from
    `os.environ["CLAUDE_CONFIG_DIR"]` directly.
    """
    roots: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        # REBINDING REVOKES ROOTEDNESS, in source order. Without this a name kept
        # its root status forever:
        #     _ccd = mkdtemp() ; os.environ["CLAUDE_CONFIG_DIR"] = _ccd
        #     _ccd = "/tmp/elsewhere"
        #     (Path(_ccd)/"channels"/"discord"/"access.json").write_text(...)
        # still recorded `_ccd` as the configured root, so the write classified as
        # seeding the canonical discord file while at runtime it lands in
        # /tmp/elsewhere and the real one stays absent — the same false CLEAN as
        # the segment case, reached through the ROOT instead of the SEGMENT.
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                roots.discard(tgt.id)
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and _is_os_environ(tgt.value)
                and _const_str(tgt.slice) == "CLAUDE_CONFIG_DIR"
                and isinstance(node.value, ast.Name)
            ):
                roots.add(node.value.id)
    return roots


def _rooted_segments(tree: ast.Module) -> "dict[str, list[str]]":
    """Module-level names whose value is a path ROOTED at the configured config dir,
    mapped to the literal path segments accumulated along the way.

    Fixpoint over straight-line module-level assignments, so
    `_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"` records
    `_cfg -> {"channels", "discord"}` and a later `p = _cfg / "access.json"` records
    `p -> {"channels", "discord", "access.json"}`.

    Rootedness is the point. The previous predicate tainted any name carrying the
    STRING "access.json", so a write to an unrelated `/tmp/x/access.json` counted as a
    seed while the canonical file stayed absent — a false CLEAN qingyun demonstrated on
    #2429 for both the method and helper shapes. Tracking the root instead means only a
    write under the dir the test actually configured can satisfy the gate.
    """
    roots = _ccd_root_names(tree)
    consts = _literal_segment_names(tree)
    rooted: dict[str, list[str]] = {}
    # LAST WRITE WINS, in source order. The previous rule kept a binding unless
    # the new segment list was strictly LONGER, so a same-length rebinding was
    # silently ignored:
    #     _cfg = <ccd>/"channels"/"discord"   ; _cfg.mkdir(parents=True)
    #     _cfg = <ccd>/"channels"/"slack"     ; (_cfg/"access.json").write_text()
    # kept `_cfg -> channels/discord` and classified the file CLEAN, while at
    # runtime only the SLACK allowlist is written and the Discord canonical file
    # is still absent — so importing the Discord bridge can fall back to the
    # operator's real allowlist (qingyun-wu, #2429).
    #
    # Convergence is detected by comparing the WHOLE map across a pass rather
    # than per-assignment. A per-assignment `grew` flag cannot work with
    # last-write-wins: a name assigned twice would flip on every pass and spin
    # forever. Snapshot-compare terminates as soon as a pass is a no-op, and the
    # iteration cap is a backstop against a pathological oscillation.
    for _ in range(64):
        before = {k: list(v) for k, v in rooted.items()}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            ok, segs = _expr_root_segments(node.value, roots, rooted, consts)
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if ok:
                    rooted[target.id] = segs
                else:
                    # Rebound to something NOT rooted at the configured dir: the
                    # name no longer names a canonical path, so drop it rather
                    # than leave the stale rooted binding vouching for it.
                    rooted.pop(target.id, None)
        if rooted == before:
            return rooted
    return rooted


def _literal_segment_names(tree: ast.Module) -> "dict[str, list[str]]":
    """Module-level names bound to plain string constants, mapped to those strings.

    A path segment is often factored out (`ACCESS = "access.json"; p = cfg / ACCESS`).
    Such a name contributes SEGMENTS but never ROOTEDNESS — it says nothing about which
    directory the path starts from, so it cannot rescue an unrooted write.
    """
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        segs: list[str] = []
        for c in ast.walk(node.value):
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                segs.extend(x for x in c.value.split("/") if x)
        # A bare alias (`CHANNEL = OTHER`) carries the ALIASED name's segments.
        # Only a bare Name is resolved: resolving names nested in arbitrary
        # expressions would ADD segments, and more segments means more paths
        # match a seed — that widens CLEAN, which is the unsafe direction.
        if not segs and isinstance(node.value, ast.Name):
            segs = list(out.get(node.value.id, []))
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if segs:
                out[target.id] = segs
            else:
                # REBINDING DROPS THE BINDING. Previously a rebind that yielded no
                # literal `continue`d, leaving the stale segments in place:
                #     OTHER = "slack" ; CHANNEL = "discord" ; CHANNEL = OTHER
                # kept `CHANNEL -> discord`, so a test that writes only the SLACK
                # access file classified CLEAN for a DISCORD bridge import — the
                # canonical discord file is absent at runtime, so import-time
                # resolution falls back to the operator's real allowlist
                # (john-the-dev + qingyun-wu, #2429). Dropping fails CLOSED: an
                # unprovable segment cannot satisfy the seed check.
                out.pop(target.id, None)
    return out


def _expr_root_segments(expr: ast.AST, roots: "set[str]", rooted: "dict[str, list[str]]",
                        consts: "dict[str, list[str]] | None" = None):
    """(is_rooted_at_configured_ccd, literal segments) for a path expression."""
    consts = consts or {}
    is_rooted = False
    segs: list[str] = []

    def visit(node: ast.AST) -> None:
        # ORDER MATTERS. `ast.walk` is breadth-first and loses the left-to-right
        # sequence of a `root / "channels" / "discord" / "access.json"` chain, so the
        # old set-valued result accepted any PERMUTATION — a rooted
        # `access.json/channels/discord` classified as the canonical seed while the
        # real allowlist was never created (john-the-dev, #2429). Walking the `/`
        # spine structurally is what makes the exact suffix checkable.
        nonlocal is_rooted
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            visit(node.left)
            visit(node.right)
            return
        if _is_ccd_ref(node):
            is_rooted = True
            return
        if isinstance(node, ast.Name):
            if node.id in roots:
                is_rooted = True
            elif node.id in rooted:
                is_rooted = True
                segs.extend(rooted[node.id])
            elif node.id in consts:
                segs.extend(consts[node.id])
            return
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                segs.extend(x for x in node.value.split("/") if x)
            return
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            # `.parent` DROPS the last component. Order-blind matching never had to
            # model this; an ordered suffix check does, or the repo's own
            # `p = …/access.json; p.parent.mkdir(parents=True)` idiom stops
            # resolving to the channel DIRECTORY and reads as a violation.
            visit(node.value)
            if segs:
                segs.pop()
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(expr)
    return is_rooted, segs


def _code_strings(tree: ast.Module) -> str:
    """String constants that are CODE, excluding docstrings and bare string exprs.

    Target selection used to regex the raw file text, so a file that merely
    MENTIONED `discord-bridge.py` in prose was treated as a bridge-importing test
    and required to be hermetic. That fired for real: tests/discord-read-forwarded
    .test.py (#2458) names the bridge in its module docstring while importing only
    `src/discord-read.py`, which resolves no channel config at import — a false
    positive, and a false positive is what gets a lint disabled (#2392/#2407).

    Comments are absent from the AST already; this drops docstrings too, so only
    strings the module actually evaluates — a `spec_from_file_location(...)`
    argument, a `REPO / "src" / "discord-bridge.py"` spine — can select a target.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
        # A bare string statement anywhere is prose, not code.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            docstrings.add(id(node.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.append(node.value)
    return "\n".join(out)


def _imported_bridge_channels(text: str) -> "set[str]":
    """Which bridge channels this test loads — {'discord'}, {'telegram'}, ...

    `channel_access_path()` resolves per CHANNEL, so seeding
    `channels/discord/access.json` does nothing for a test that imports
    `telegram-bridge.py`: the canonical telegram file is still absent and the
    import falls back to the operator's real Telegram allowlist. qingyun
    demonstrated exactly that on #2429 — and my own positive fixture had
    accidentally locked the shape in by seeding `slack` while the fixture loaded
    the Discord bridge.
    """
    return set(BRIDGE_IMPORT.findall(text))


# Statements whose bodies do NOT run just because the module was imported. A seed
# written inside one of these is not a seed: the canonical access.json is never
# created before the bridge loads, so the import still falls back to the operator's
# real allowlist. qingyun's repro on #2429 put a fully-inline canonical write inside
# a `def never_called():` and it classified CLEAN, because ast.walk() descends into
# function bodies.
_DEFERRED_BODIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


# Statements after one of these never run in the same block.
_TERMINATORS = (ast.Raise, ast.Return, ast.Continue, ast.Break)


def _until_terminator(body):
    """Yield statements up to and including the first definite terminator.

    `raise` inside a `try` body is the live case: everything after it is dead, but
    the handler swallows the exception so the module import continues — meaning a
    seed written after the raise never happens while the bridge still loads.
    """
    for st in body:
        yield st
        if isinstance(st, _TERMINATORS):
            return


def _reachable_nodes(stmt: ast.AST):
    """Walk `stmt`, descending ONLY into bodies that run unconditionally on import.

    Round 7 (qingyun): skipping def/class bodies was not enough. Descending into every
    child of `if` and `try` admitted a seed under `if False:` or inside an `except`
    handler that never fires — recorded as if it had executed before the bridge import.

    What runs unconditionally when a module is imported:
      * top-level statements
      * a `with` body (the context manager is entered)
      * a `try` BODY and its `finally`

    What does NOT:
      * either branch of an `if`  — `if False:` is the degenerate case, but no `if` branch
        is guaranteed
      * an `except` handler       — only on exception
      * a `try`/`for`/`while` `else` — conditional on how the block exits
      * `for`/`while` bodies      — an empty iterable runs the body zero times

    Under-approximating CLEAN is this file's documented stance, so anything not
    guaranteed is refused. The cost is stated plainly rather than hidden: a legitimate
    `if not (cfg / "access.json").exists(): seed()` now reads as a violation. That is a
    real false positive, and it is the same class as the one #2357 exposed — see the
    open design question about isolation-via-helper and scoped isolation.
    """
    if isinstance(stmt, _DEFERRED_BODIES):
        return
    yield stmt
    if isinstance(stmt, ast.If):
        return                      # neither branch is guaranteed
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        return                      # zero iterations is legal
    if isinstance(stmt, ast.Try):
        # A `try` BODY runs, but only up to the first statement that definitely
        # terminates it. A seed placed AFTER an unconditional `raise` never executes,
        # yet the exception is swallowed by the handler and the import proceeds
        # without the canonical file — so the lint would report clean on a test that
        # never seeded anything (qingyun, round 8).
        for child in _until_terminator(stmt.body):
            yield from _reachable_nodes(child)
        for child in _until_terminator(stmt.finalbody):
            yield from _reachable_nodes(child)
        return                      # handlers and `else` are conditional
    for child in ast.iter_child_nodes(stmt):
        yield from _reachable_nodes(child)


def _access_seed_line(tree: ast.Module, channels: "set[str]") -> "int | None":
    """Earliest module-level WRITE that creates the CANONICAL
    `$CLAUDE_CONFIG_DIR/channels/<ch>/access.json`.

    Required in addition to the CLAUDE_CONFIG_DIR assignment, because pointing the env
    var at an EMPTY temp dir is not isolation: `channel_access_path()` falls back to the
    LEGACY real-home `~/.claude/channels/<ch>/access.json` when the canonical path is
    missing, so the operator's real allowlist is still what gets read.

    Three conditions, all necessary — a write that satisfies only the last one is the
    false CLEAN this predicate was rebuilt to reject:
      1. it is a WRITE, never a mention (that file names "access.json" in its own module
         docstring, and a substring check would have called it isolated);
      2. the path is ROOTED at the configured config dir (`os.environ["CLAUDE_CONFIG_DIR"]`
         or the name assigned into it) — an unrelated `/tmp/x/access.json` is NOT a seed;
      3. the path carries both a `channels` segment and an `access.json` segment, so it is
         the file `channel_access_path()` actually reads.

    The path may be an inline expression or a variable built up first; binding a path to a
    name is not a behavioral difference, so `_rooted_segments` propagates through
    module-level assignments and both shapes count.
    """
    roots = _ccd_root_names(tree)
    rooted = _rooted_segments(tree)
    consts = _literal_segment_names(tree)

    # Names that actually resolve to the `os` module / `os.makedirs` IN THIS FILE.
    # Recognising a call by its attribute name alone let `Fake().makedirs(parent)` —
    # a no-op — vouch for a parent directory that was never created; the canonical
    # write then raised, was swallowed, and the file still classified `clean`
    # (qingyun-wu, #2429).
    os_aliases: "set[str]" = set()
    makedirs_names: "set[str]" = set()
    for _n in ast.walk(tree):
        if isinstance(_n, ast.Import):
            for _a in _n.names:
                if _a.name == "os":
                    os_aliases.add(_a.asname or "os")
        elif isinstance(_n, ast.ImportFrom) and _n.module == "os":
            for _a in _n.names:
                if _a.name in _MAKEDIRS_FUNCS:
                    makedirs_names.add(_a.asname or _a.name)

    # An IMPORTED name is not necessarily still that module at the call site.
    # `import os` … `os = Fake()` … `os.makedirs(p)` passed the receiver check
    # while creating nothing (qingyun-wu, #2429). Any name that is REBOUND
    # anywhere — assignment, for-target, with-as, def/class, parameter — stops
    # vouching for anything. Conservative on purpose: a shadowed alias reads as
    # a violation, and this gate's documented stance is to under-approximate
    # CLEAN, so a false positive costs a grandfather entry while a false CLEAN
    # costs the operator's real allowlist.
    _shadowed: "set[str]" = set()
    for _n in ast.walk(tree):
        _targets = []
        if isinstance(_n, ast.Assign):
            _targets = list(_n.targets)
        elif isinstance(_n, (ast.AugAssign, ast.AnnAssign)):
            _targets = [_n.target]
        elif isinstance(_n, ast.For):
            _targets = [_n.target]
        elif isinstance(_n, ast.withitem):
            _targets = [_n.optional_vars] if _n.optional_vars else []
        elif isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _shadowed.add(_n.name)
        # ONLY a bare Name target rebinds the name. `os.environ[k] = v` has a
        # Subscript target whose base happens to be the Name `os` — walking into
        # it would declare `os` shadowed and turn every correctly-isolated test
        # into a violation, since setting CLAUDE_CONFIG_DIR is the first thing
        # they all do. Tuple/List/Starred are unpacked one level for `a, b = ...`.
        _pending = [t for t in _targets if t is not None]
        while _pending:
            _t = _pending.pop()
            if isinstance(_t, ast.Name):
                _shadowed.add(_t.id)
            elif isinstance(_t, (ast.Tuple, ast.List)):
                _pending.extend(_t.elts)
            elif isinstance(_t, ast.Starred):
                _pending.append(_t.value)
    os_aliases -= _shadowed
    makedirs_names -= _shadowed

    def _path_channel(expr: ast.AST, *, want_access_json: bool) -> "str | None":
        """The channel this path expression belongs to under the CONFIGURED config dir.

        `want_access_json=True` asks for the access FILE (a write target);
        `False` asks for the containing DIRECTORY (an mkdir target).
        """
        is_rooted, segs = _expr_root_segments(expr, roots, rooted, consts)
        if not is_rooted:
            return None
        # EXACT components in the EXACT canonical ORDER. Two false CLEANs came from
        # weakening this (both qingyun-wu / john-the-dev, #2429):
        #   substring membership -> a rooted `notchannels/discord/access.json.bak`
        #                           matched on "channels" and "access.json"
        #   unordered set        -> any PERMUTATION matched, so a rooted
        #                           `access.json/channels/discord` classified clean
        #                           while the real allowlist was never created
        # `_expr_root_segments` now preserves left-to-right order, so the canonical
        # run `channels/<bridge>/access.json` is checkable as a contiguous sequence.
        comps = list(segs)
        for i in range(len(comps) - 1):
            if comps[i] != "channels":
                continue
            ch = comps[i + 1]
            if ch not in ("discord", "slack", "telegram"):
                continue
            rest = comps[i + 2:]
            if want_access_json:
                # access.json must be the FINAL component, so `.bak`/`.tmp`
                # siblings of the real file are never mistaken for it.
                if rest == ["access.json"]:
                    return ch
            elif not rest:
                return ch
        return None

    def _seeded_channel(expr: ast.AST) -> "str | None":
        """The channel this write seeds, or None if it is not a canonical seed."""
        return _path_channel(expr, want_access_json=True)

    def _mkdir_target(call: ast.Call) -> "ast.AST | None":
        """The directory a call creates RECURSIVELY, or None if it creates nothing.

        Only recursive creation counts. `channels/` does not exist inside a fresh
        `mkdtemp()`, so `(cfg / "channels" / "discord").mkdir(exist_ok=True)` raises
        FileNotFoundError exactly like the unguarded write it was supposed to make safe.
        """
        if isinstance(call.func, ast.Attribute) and call.func.attr in _MKDIR_METHODS:
            for kw in call.keywords:
                if kw.arg == "parents" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True:
                    return call.func.value
            return None
        # `makedirs` takes the path as an ARGUMENT, so a fake receiver still hands us
        # the canonical path and every path check passes — only the receiver is a lie.
        # (`.mkdir(parents=True)` above needs no such guard: it returns the RECEIVER as
        # the created path, so a fake receiver fails path resolution on its own.)
        # Hence: the receiver must be a name that really imports the `os` module here,
        # or a bare name really imported via `from os import makedirs`.
        if isinstance(call.func, ast.Attribute):
            recv = call.func.value
            name = (call.func.attr
                    if isinstance(recv, ast.Name) and recv.id in os_aliases
                    else None)
        elif isinstance(call.func, ast.Name):
            # `makedirs_names` holds only names proven to BE os.makedirs, so an alias
            # (`from os import makedirs as md`) normalises to the canonical spelling.
            name = "makedirs" if call.func.id in makedirs_names else None
        else:
            name = None
        if name in _MAKEDIRS_FUNCS:
            for kw in call.keywords:
                if kw.arg == "name":
                    return kw.value
            return call.args[0] if call.args else None
        return None

    # channel -> earliest REACHABLE line that recursively creates channels/<ch>/.
    # Computed from the same `_reachable_nodes` walk as the seeds, so an mkdir parked
    # under `if False:` or inside a never-called `def` cannot vouch for a write.
    created: dict[str, int] = {}
    for node in tree.body:
        for sub in _reachable_nodes(node):
            if not isinstance(sub, ast.Call):
                continue
            target = _mkdir_target(sub)
            if target is None:
                continue
            ch = _path_channel(target, want_access_json=False)
            if ch and sub.lineno < created.get(ch, 1 << 30):
                created[ch] = sub.lineno

    # channel -> earliest module-level line that seeds it
    seeded: dict[str, int] = {}

    def _record(ch: "str | None", lineno: int, at: int) -> None:
        """Record a seed — but only if `channels/<ch>/` was created BEFORE line `at`.

        The parent-directory precondition is what makes the write evidence rather than
        an intention. Ordering is checked on the CALL's own line, not the enclosing
        top-level statement's, so an mkdir and a write sharing a `with` block are still
        ordered correctly relative to each other.
        """
        if ch and ch not in seeded and created.get(ch, 1 << 30) < at:
            seeded[ch] = lineno

    for node in tree.body:
        for sub in _reachable_nodes(node):
            if not isinstance(sub, ast.Call):
                continue
            # Method style: (dir / "access.json").write_text(...) — path is the RECEIVER.
            if isinstance(sub.func, ast.Attribute):
                if sub.func.attr in _WRITE_METHODS:
                    _record(_seeded_channel(sub.func.value), node.lineno, sub.lineno)
            # Helper style: write_private_text(dir / "access.json", data) — path is an
            # ARGUMENT. #2356 makes this the canonical way access files are written, so
            # a receiver-only check would start false-flagging correctly-seeded tests
            # the moment it lands. Accept it called bare or via a module attribute.
            name = (
                sub.func.attr if isinstance(sub.func, ast.Attribute)
                else sub.func.id if isinstance(sub.func, ast.Name)
                else None
            )
            if name in _WRITE_HELPERS:
                # ARGUMENT ROLE MATTERS. `write_private_text(path, data)` writes to
                # arg 0; every later argument is CONTENT. Scanning all arguments
                # accepted a canonical path passed as the DATA while the write went
                # somewhere else entirely — qingyun's repro classified clean without
                # ever creating $CLAUDE_CONFIG_DIR/channels/<ch>/access.json. Only the
                # path position counts; `path=` covers the keyword form.
                path_arg = sub.args[0] if sub.args else None
                for kw in sub.keywords:
                    if kw.arg == "path":
                        path_arg = kw.value
                if path_arg is not None:
                    # The helper is held to the SAME parent-directory precondition. It
                    # does not exist in this tree yet (#2356 introduces it), so its
                    # parent-creating semantics cannot be verified here — and an
                    # unverifiable exemption is exactly the kind of assumption this
                    # predicate keeps getting caught making. If #2356 lands with a
                    # helper that creates parents itself, add it to a
                    # _PARENT_CREATING_HELPERS set then, against the real source.
                    _record(_seeded_channel(path_arg), node.lineno, sub.lineno)
    # EVERY imported bridge must be seeded. Return the LATEST such line so the
    # caller's `seed_line < exec_line` ordering check covers all of them.
    if not channels or any(ch not in seeded for ch in channels):
        return None
    return max(seeded[ch] for ch in channels)


def _bridge_load_call(tree: ast.AST):
    """(lineno, namespace_var) of the earliest call that EXECUTES the bridge source.

    Two mechanisms are recognized, because both really occur in this repo:
      * `spec.loader.exec_module(mod)` — the importlib path;
      * `exec(src, ns.__dict__)` / `exec(src, ns)` — reading the bridge with
        read_text() and exec'ing it into a namespace.

    Recognizing only exec_module was a scope hole, not a strictness choice: a file
    loading the bridge via exec() returned None ("out of scope") and was never checked
    at all. qingyun and john both hit it on #2429 with
    tests/discord-bridge-public-notice-suppression.test.py, which execs the bridge and
    seeds only `.env` — so channel_access_path()'s legacy fallback still resolved the
    operator's real access.json at import. Out-of-scope is a SILENT PASS, which is the
    worst verdict a gate can give.
    """
    best, ns = None, None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        line, cand = None, None
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "exec_module":
            line = node.lineno
            a = node.args[0] if node.args else None
            cand = a.id if isinstance(a, ast.Name) else None
        elif isinstance(fn, ast.Name) and fn.id == "exec" and len(node.args) >= 2:
            line = node.lineno
            g = node.args[1]
            if isinstance(g, ast.Attribute) and g.attr == "__dict__" and isinstance(g.value, ast.Name):
                cand = g.value.id          # exec(src, bridge.__dict__)
            elif isinstance(g, ast.Name):
                cand = g.id                # exec(src, ns)
        if line is not None and (best is None or line < best):
            best, ns = line, cand
    return best, ns




def _mitigation_line(tree: ast.Module, exec_line: int, mod_var: "str | None") -> "int | None":
    """Module-level `<mod>.ACCESS_FILE = ...` that runs AFTER the bridge import, else None.

    Both extra conditions are qingyun's (#2429): without the receiver check, an unrelated
    `cfg.ACCESS_FILE = ...` counted; without the ordering check, a rebind BEFORE exec_module
    counted even though the import then re-resolves against host config. MITIGATED is
    non-fatal, so a false mitigation silently downgrades a real violation.
    """
    if mod_var is None:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or node.lineno <= exec_line:
            continue
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Attribute)
                and tgt.attr in {"ACCESS_FILE", "channels_env"}
                and isinstance(tgt.value, ast.Name)
                and tgt.value.id == mod_var
            ):
                return node.lineno
    return None


def classify(path: Path) -> str | None:
    """Return a verdict, or None when the file is out of scope."""
    try:
        rel = path.resolve().relative_to(REPO.resolve()).as_posix()
    except (ValueError, OSError):
        rel = path.as_posix()
    if rel in SELF_EXEMPT:
        return None
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    # Cheap PRE-FILTER over raw text: a superset. Kept first so an unparseable file
    # that never mentions a bridge still exits here rather than reaching the
    # conservative VIOLATION below — that verdict is for files we had reason to scan.
    if not BRIDGE_IMPORT.search(text):
        return None
    if "exec_module" not in text and "exec(" not in text:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Unparseable test file: fall back to the conservative verdict rather than
        # silently passing it. A file that cannot be analysed is not proven clean.
        return VIOLATION

    # STRUCTURAL confirmation: only strings the module actually evaluates may select
    # a target. A docstring or comment naming the bridge is prose, not an import.
    code = _code_strings(tree)
    if not BRIDGE_IMPORT.search(code):
        return None

    exec_line, mod_var = _bridge_load_call(tree)
    if exec_line is None:
        return None
    iso_line = _isolation_line(tree)
    # Isolation only counts when it EXECUTES BEFORE the bridge import. Setting the env
    # afterwards leaves the module-level resolution already done against host config.
    seed_line = _access_seed_line(tree, _imported_bridge_channels(code))
    # CLEAN needs BOTH, both before the load: the env override AND a seeded canonical
    # access.json. Env-var-only leaves channel_access_path() on its legacy real-home
    # fallback, which is the very read this gate exists to prevent.
    if (
        iso_line is not None
        and iso_line < exec_line
        and seed_line is not None
        and seed_line < exec_line
    ):
        return CLEAN
    return MITIGATED if _mitigation_line(tree, exec_line, mod_var) is not None else VIOLATION



# --- workspace-resolver stub arity ----------------------------------

# The CCD checks prove a test isolates CLAUDE_CONFIG_DIR, not the WORKSPACE.
# A CCD-hermetic test can still write to the real `<workspace>/state/`.

RESOLVER_NAMES = frozenset({"resolve_workspace", "_resolve_workspace"})

#: Resolver stubs predating this check; same contract as KNOWN_UNISOLATED.
#: The list must SHRINK, never rot; each entry was verified latent when added.
KNOWN_RESOLVER_STUBS = {
    "tests/telegram-bridge-access.test.py",
}


def _lambda_absorbs_args(fn: ast.Lambda) -> bool:
    """True when the lambda can take an argument: `*args`/`**kwargs` or any declared
    parameter. A bare `lambda:` is the failure mode this exists to catch."""
    a = fn.args
    if a.vararg or a.kwarg:
        return True
    return bool(a.args or a.posonlyargs or a.kwonlyargs)


def _assign_target_name(t: ast.expr) -> str | None:
    """The bare name a single assignment target binds (`x` or `mod.x`)."""
    if isinstance(t, ast.Attribute):
        return t.attr
    if isinstance(t, ast.Name):
        return t.id
    return None


def _binding_state(value: ast.expr, env: "dict[str, bool]") -> "bool | None":
    """Is `value` a non-absorbing stub? None when it says nothing about arity."""
    if isinstance(value, ast.Lambda):
        return not _lambda_absorbs_args(value)
    if isinstance(value, ast.Name):
        return env.get(value.id)
    return None


def _assign_targets(node: "ast.stmt") -> "list[ast.expr]":
    """Targets an assignment binds. `ast.Assign` has `.targets`; `ast.AnnAssign` has a
    single `.target` and binds NOTHING when `.value` is None (a bare annotation)."""
    if isinstance(node, ast.AnnAssign):
        return [node.target] if node.value is not None else []
    return list(getattr(node, "targets", []))


def _unsafe_names_in_scope(body: "list[ast.stmt]") -> "set[str]":
    """Names this scope ever binds to a zero-arg lambda, at any depth, not descending
    into nested def/class. Order-free: a nested function reads the global when it RUNS."""
    found: "set[str]" = set()
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # An annotated assignment binds like a plain one, so `_fake: object = lambda: tmp`
        # must reach the gate; a bare `_fake: object` has value None and binds nothing.
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Lambda):
            if not _lambda_absorbs_args(n.value):
                for t in _assign_targets(n):
                    if isinstance(t, ast.Name):
                        found.add(t.id)
        for f in ("body", "orelse", "finalbody", "handlers"):
            sub = getattr(n, f, None)
            if isinstance(sub, list):
                stack.extend(x for x in sub if isinstance(x, ast.stmt) or hasattr(x, "body"))
    return found


class _ScopeWalk:
    """Reaching-binding walk over ONE lexical scope in statement order; nested scopes take
    a snapshot, so a sibling cannot leak. Branches merge with OR (unsafe if any path is)."""

    def __init__(self, env: "dict[str, bool]", out: "list[tuple[int, str]]",
                 inherited: "set[str] | None" = None, *, class_body: bool = False) -> None:
        self.env = dict(env)
        self.out = out
        # Names an ENCLOSING scope ever binds unsafely, carried down so a method still
        # gets late-binding treatment even though the class body is not its scope.
        self.ever_unsafe: "set[str]" = set(inherited or ())
        # A class namespace is NOT a lexical scope for its methods: an unqualified
        # name in a method skips the class body and resolves module or enclosing.
        self.class_body = class_body
        self.func_env = dict(env)
        self.func_unsafe = set(inherited or ())

    def run(self, body: "list[ast.stmt]") -> None:
        own = _unsafe_names_in_scope(body)
        self.ever_unsafe |= own
        if not self.class_body:
            # A class body's own names must not reach its methods (see __init__).
            self.func_unsafe |= own
        for stmt in body:
            self.visit(stmt)

    def visit(self, node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # ONE rule for every nested scope rather than a branch per surface: a class
            # namespace lexically encloses nothing nested inside it.
            base_env = self.func_env if self.class_body else self.env
            base_unsafe = self.func_unsafe if self.class_body else self.ever_unsafe
            if isinstance(node, ast.ClassDef):
                # A class body executes IMMEDIATELY at its own statement, so
                # definition-point state is exact — no late-binding widening.
                _ScopeWalk(base_env, self.out, base_unsafe, class_body=True).run(node.body)
                return
            # A function body runs LATER: a global resolves at CALL time, so a binding
            # written after the `def` still reaches it.
            inner = dict(base_env)
            for nm in base_unsafe:
                inner[nm] = True
            _ScopeWalk(inner, self.out, base_unsafe).run(node.body)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            self._assign(node)
            return
        # `finalbody` is NOT an alternative branch: it runs on EVERY path, after
        # whichever of body/handlers/orelse ran, so OR-merging it keeps the prior state.
        finalbody = getattr(node, "finalbody", None)
        # On a `try`, `else` is NOT an alternative to the body: Python runs it
        # SEQUENTIALLY after the body succeeds, so both are ONE success path.
        _try_types = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
        if isinstance(node, _try_types):
            success = list(getattr(node, "body", None) or []) + \
                list(getattr(node, "orelse", None) or [])
            branches = [success] if success else []
        else:
            branches = [b for b in (getattr(node, "body", None),
                                    getattr(node, "orelse", None)) if b]
        branches += [h.body for h in getattr(node, "handlers", [])]
        if not branches and not finalbody:
            return
        merged: "dict[str, bool]" = {}
        # A statement whose body may not run leaves the PRE-STATE live: `if` with no
        # `else`, a loop over an empty iterable, a `try` that raised immediately.
        covers_all = (isinstance(node, ast.If) and node.orelse) or isinstance(
            node, (ast.With, ast.AsyncWith)
        )
        if not covers_all:
            merged.update(self.env)
        for body in branches:
            # A control-flow block is the SAME scope, not a nested one, so the sub-walk
            # must carry EVERYTHING forward, including `ever_unsafe`.
            w = _ScopeWalk(self.env, self.out, self.ever_unsafe,
                           class_body=self.class_body)
            w.func_env = dict(self.func_env)
            w.func_unsafe = set(self.func_unsafe)
            w.run(body)
            for k, v in w.env.items():
                merged[k] = merged.get(k, False) or v
        self.env.update(merged)
        # `finally` runs last over the merged state and its result WINS: it executes
        # whether the body completed, raised or returned, so its rebinding is final.
        if finalbody:
            w = _ScopeWalk(self.env, self.out, self.ever_unsafe,
                           class_body=self.class_body)
            w.func_env = dict(self.func_env)
            w.func_unsafe = set(self.func_unsafe)
            w.run(finalbody)
            self.env.update(w.env)

    def _assign(self, node: "ast.Assign | ast.AnnAssign") -> None:
        targets = _assign_targets(node)
        if not targets:            # bare annotation: no value, so no binding happens
            return
        state = _binding_state(node.value, self.env)
        for t in targets:                            # flag BEFORE rebinding
            nm = _assign_target_name(t)
            if nm in RESOLVER_NAMES and state:
                self.out.append((node.lineno, nm))
        for t in targets:
            if isinstance(t, ast.Name) and t.id not in RESOLVER_NAMES:
                self.env[t.id] = bool(state)


def resolver_stub_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, name) for every zero-arg lambda that reaches a resolver name. Tracked per
    scope in statement order - a file-global alias set false-flags cross-function cases."""
    out: list[tuple[int, str]] = []
    _ScopeWalk({}, out).run(getattr(tree, "body", []))
    return sorted(set(out))

def scan_resolver_stubs(paths) -> dict[str, list[tuple[int, str]]]:
    """Resolver-stub violations per path, over EVERY tracked test rather than the
    bridge-loading subset - a resolver stub is hazardous wherever it appears."""
    out: dict[str, list[tuple[int, str]]] = {}
    for rel in paths:
        try:
            tree = ast.parse((REPO / rel).read_text(errors="ignore"))
        except (OSError, SyntaxError):
            continue
        hits = resolver_stub_violations(tree)
        if hits:
            out[rel] = hits
    return out


def scan(paths) -> dict[str, str]:
    out = {}
    for p in paths:
        verdict = classify(REPO / p)
        if verdict:
            out[p] = verdict
    return out


#: git's wordings for "this comparison cannot be made in this checkout" -- as
#: distinct from a genuine git failure. Two different situations, both routine
#: under `actions/checkout@v4` depth 1 and both requiring the same response:
#:   * the ref is ABSENT           -> "bad revision" / "unknown revision" / ...
#:   * the ref exists but shares NO HISTORY with HEAD in the grafted shallow
#:     graph                       -> "no merge base"
#: The first cut of this fallback listed only the absent-ref spellings, so the
#: shallow case it was written for still hard-failed. Verified against real git:
#: `git diff --name-only A...HEAD` across unrelated histories exits 128 with
#: `fatal: A...HEAD: no merge base`.
_UNRESOLVABLE_REF = re.compile(
    r"bad revision|unknown revision|ambiguous argument|not a valid object name"
    r"|no merge base",
    re.I,
)

#: Floor for full-tree discovery. The tree held 310 tracked test files when this
#: was added; 50 is far below any plausible legitimate shrink and far above the
#: zero a broken `git ls-files` returns. A tripwire, not a census.
MIN_TRACKED_TESTS = 50


def _git_lines(cmd: list[str], what: str) -> list[str]:
    """Run a git query whose EMPTY result would otherwise read as "nothing to do".

    Both discovery paths fed `scan()` straight from `r.stdout` with no returncode
    check, so a git failure returned `[]` and the lint printed
    `ok (0 bridge-importing tests scanned)` and exited 0. Reproduced: with git
    forced to exit 128, `tracked_tests()` went 310 -> 0 and `main()` still exited
    0. A gate that reports clean because it could not look is worse than no gate
    -- it occupies the slot where a real check would go and answers with the same
    word. That is the shape this lint exists to catch, one level up.
    """
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        # An UNRESOLVABLE BASE REF is not a broken discovery -- it means this
        # comparison is inapplicable here. `actions/checkout@v4` fetches depth 1,
        # so `origin/main` is frequently absent and `origin/main...HEAD` exits 128
        # with "bad revision". Hard-failing on that breaks every shallow checkout,
        # including the repo's own test that exercises --diff -- which is exactly
        # the "a guard becomes a thing people disable" outcome this PR's own body
        # warns about, committed one layer up. Signal it and let the caller widen
        # to the full tree: more coverage than the diff, never less, never silent.
        if _UNRESOLVABLE_REF.search(err):
            print(
                f"lint-hermetic-bridge-tests: cannot resolve the base ref for {what} "
                f"({err.splitlines()[0][:120] if err else 'no stderr'}).\n"
                "  Falling back to the full tracked-test scan — wider, not quieter."
            )
            return None
        raise SystemExit(
            f"lint-hermetic-bridge-tests: FAILED to discover {what} -- "
            f"`{' '.join(cmd)}` exited {r.returncode}.\n"
            f"  {err[:300]}\n"
            "  Refusing to report a verdict on a discovery that did not run."
        )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def tracked_tests() -> list[str]:
    files = _git_lines(["git", "ls-files", "--", "tests/*.py"], "tracked tests")
    if len(files) < MIN_TRACKED_TESTS:
        raise SystemExit(
            f"lint-hermetic-bridge-tests: discovered {len(files)} tracked test files, "
            f"floor is {MIN_TRACKED_TESTS}.\n"
            "  Discovery collapsed, or the suite genuinely shrank -- if the latter, lower\n"
            "  MIN_TRACKED_TESTS deliberately in the same PR so the shrink is reviewable."
        )
    return files


def changed_tests(base: str) -> list[str]:
    # An empty result is LEGITIMATE here (a PR may touch no test files), so this
    # path gets the returncode check but NO floor. "zero changed" is a real
    # answer; "zero tracked" never is. A floor here would fail every PR that
    # doesn't touch tests, which is how a guard becomes a thing people disable.
    return _git_lines(
        ["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD", "--", "tests/*.py"],
        f"tests changed vs {base}",
    )


def main() -> int:
    import os

    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "--diff":
        base = os.environ.get("BASE_REF", "origin/main")
        targets = changed_tests(base)
        if targets is None:            # base ref unresolvable -> widen, don't skip
            targets = tracked_tests()
        elif not targets:
            print("lint-hermetic-bridge-tests: no test files changed — nothing to scan")
            return 0
    else:
        targets = tracked_tests()

    results = scan(targets)
    stub_hits = scan_resolver_stubs(targets)

    if mode == "--list":
        for p, v in sorted(results.items()):
            print(f"{v:9} {p}")
        for p, hits in sorted(stub_hits.items()):
            for ln, nm in hits:
                print(f"{'STUB':9} {p}:{ln} ({nm})")
        return 0

    new_violations = [p for p, v in results.items() if v == VIOLATION and p not in KNOWN_UNISOLATED]
    new_stubs = {p: h for p, h in stub_hits.items() if p not in KNOWN_RESOLVER_STUBS}
    mitigated = [p for p, v in results.items() if v == MITIGATED]

    # The grandfather list must shrink, never rot: a listed file that now isolates
    # (or no longer imports a bridge) has to come off the list in the same PR.
    stale = []
    if mode != "--diff":
        for p in sorted(KNOWN_UNISOLATED):
            if not (REPO / p).exists() or results.get(p) != VIOLATION:
                stale.append(p)

    for p, hits in sorted(new_stubs.items()):
        for ln, nm in hits:
            print(
                f"error: {p}:{ln} — `{nm}` stubbed with a ZERO-ARG lambda. "
                f"25 production call sites pass `migrate=False` or `repo`; a zero-arg "
                f"stub raises TypeError at each, and behind a broad `except` that "
                f"DISABLES the write path instead of redirecting it. "
                f"Use `lambda *a, **kw: <ws>`."
            )

    for p in mitigated:
        print(f"note: {p} — import still resolves host config; destructive path rebound post-import")

    if stale:
        # WARN, never fail. Hard-failing here is a footgun: the moment a PR fixes a listed
        # file, main goes red until someone edits this script. Caught while testing this
        # lint — #2428 fixes tests/bridge-audit-wiring.test.py, and a fatal stale-check
        # would have reddened main on its merge.
        print("\nlint-hermetic-bridge-tests: NOTE — KNOWN_UNISOLATED entries no longer violating")
        print("(remove them so the list keeps shrinking):\n")
        for p_ in stale:
            print(f"  {p_}")

    if not new_violations and not new_stubs:
        print(
            f"lint-hermetic-bridge-tests: ok "
            f"({len(results)} bridge-importing tests scanned, "
            f"{len(KNOWN_UNISOLATED)} grandfathered, {len(mitigated)} mitigated; "
            f"{len(targets)} scanned for resolver stubs, "
            f"{len(KNOWN_RESOLVER_STUBS)} grandfathered)"
        )
        return 0

    if new_stubs:
        print("\nlint-hermetic-bridge-tests: FAIL — zero-arg workspace-resolver stub\n")

    if new_violations:
        print("\nlint-hermetic-bridge-tests: FAIL — test imports a bridge without isolating CLAUDE_CONFIG_DIR\n")
        for p in sorted(new_violations):
            print(f"  {p}")
        print(
            "\nThe bridge resolves channel config at import, so this reads the developer's real\n"
            "per-user channel allowlist. Set CLAUDE_CONFIG_DIR to a temp dir and seed\n"
            "channels/<ch>/access.json BEFORE exec_module. A token env var is not enough,\n"
            "and a comment saying 'hermetic' is not isolation."
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
