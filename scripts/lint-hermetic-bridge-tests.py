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

# Grandfathered: known-unisolated at the time this lint landed. Mini's shared-helper
# migration removes these; the stale-entry check below forces the list to shrink.
# Measured on origin/main (2026-07-30, post-#2428-merge) with the AST classifier. The count rose
# from 26 to 27 when detection moved off regex: two files the regex called clean were real
# bypasses (assignment-shaped comment / assignment after exec_module), which is exactly the
# P1 qingyun raised on #2429.
KNOWN_UNISOLATED = frozenset(
    """
tests/audio-transcribe-skill.test.py
tests/bridge-env-token-perms.test.py
tests/bridge-not-allowlisted-ack.test.py
tests/bridge-restart-intercept.test.py
tests/bridge-skill-path-resolution.test.py
tests/bridges-allowlist-default-readonly.test.py
tests/bridges-sending-orphan-recovery.test.py
tests/discord-bridge-access-no-clobber.test.py
tests/discord-bridge-attachment-filename-sanitize.test.py
tests/discord-bridge-codex-subprocess-argv.test.py
tests/discord-bridge-collaborator-tier.test.py
tests/discord-bridge-delivery-failure-visible.test.py
tests/discord-bridge-delivery-sentinel.test.py
tests/discord-bridge-discord-state-detection.test.py
tests/discord-bridge-dm-catchup.test.py
tests/discord-bridge-dm-fallback-source-guard.test.py
tests/discord-bridge-file-markers.test.py
tests/discord-bridge-mod-judge-actions.test.py
tests/discord-bridge-mod-judge-buffer.test.py
tests/discord-bridge-mod-judge-codex.test.py
tests/discord-bridge-mod-judge-dispatcher.test.py
tests/discord-bridge-mod-judge-integration.test.py
tests/discord-bridge-mod-judge-trackers.test.py
tests/discord-bridge-mod-judge.test.py
tests/discord-bridge-mod-server-config.test.py
tests/discord-bridge-multibot-seed-gate.test.py
tests/discord-bridge-reply-directive.test.py
tests/discord-bridge-state-prefetch.test.py
tests/discord-bridge-task-write-instrument.test.py
tests/discord-bridge-thread-seed-owner-notice.test.py
tests/discord-bridge-welcome-on-first-post.test.py
tests/discord-chunker.test.py
tests/discord-task-source-invariance.test.py
tests/discord-writeside-attachments.test.py
tests/dm-result-multipart-upload.test.py
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
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and _is_os_environ(tgt.value)
                and _const_str(tgt.slice) == "CLAUDE_CONFIG_DIR"
            ):
                return node.lineno
    return None

def _access_seed_line(tree: ast.Module) -> "int | None":
    """Earliest module-level WRITE that creates a `channels/<ch>/access.json`.

    Required in addition to the CLAUDE_CONFIG_DIR assignment, because pointing the env
    var at an EMPTY temp dir is not isolation: `channel_access_path()` falls back to the
    LEGACY real-home `~/.claude/channels/<ch>/access.json` when the canonical path is
    missing, so the operator's real allowlist is still what gets read. This is the exact
    thing my own #2428 fix had to add, and my first predicate then failed to require it —
    john caught it on tests/discord-bridge-public-notice-suppression.test.py, which sets
    the env var and seeds only `.env`.

    A WRITE, never a mention: that file names "access.json" in its module docstring, and
    a constant-substring check would have called it isolated. Same shape as
    "a comment is not isolation".

    The receiver may be an inline expression OR a variable the path was built into
    first — `p = chan / "access.json"; p.write_text(...)` seeds exactly as much as
    `(chan / "access.json").write_text(...)`. Binding a path to a name is not a
    behavioral difference, so `_access_path_names` propagates the taint through
    module-level assignments and both shapes count.
    """
    tainted = _access_path_names(tree)

    def _is_access_receiver(expr: ast.AST) -> bool:
        for c in ast.walk(expr):
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and "access.json" in c.value:
                return True
            if isinstance(c, ast.Name) and c.id in tainted:
                return True
        return False

    for node in tree.body:
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            if sub.func.attr not in {"write_text", "write_bytes"}:
                continue
            if _is_access_receiver(sub.func.value):
                return node.lineno
    return None


def _access_path_names(tree: ast.Module) -> "set[str]":
    """Module-level names whose value carries an `access.json` path.

    Straight-line propagation to a fixpoint, so a name built from another tainted
    name is tainted too (`ACCESS = "access.json"` → `p = chan / ACCESS` → `p`).
    Module level only: a name bound inside a function or an `if` body has not
    necessarily been bound by the time the bridge loads, and unbound-at-load is
    exactly the reachability failure this gate refuses to guess about.
    """
    tainted: set[str] = set()
    while True:
        grew = False
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            carries = any(
                (isinstance(c, ast.Constant) and isinstance(c.value, str) and "access.json" in c.value)
                or (isinstance(c, ast.Name) and c.id in tainted)
                for c in ast.walk(node.value)
            )
            if not carries:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in tainted:
                    tainted.add(target.id)
                    grew = True
        if not grew:
            return tainted


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

    exec_line, mod_var = _bridge_load_call(tree)
    if exec_line is None:
        return None
    iso_line = _isolation_line(tree)
    # Isolation only counts when it EXECUTES BEFORE the bridge import. Setting the env
    # afterwards leaves the module-level resolution already done against host config.
    seed_line = _access_seed_line(tree)
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


def scan(paths) -> dict[str, str]:
    out = {}
    for p in paths:
        verdict = classify(REPO / p)
        if verdict:
            out[p] = verdict
    return out


def tracked_tests() -> list[str]:
    r = subprocess.run(
        ["git", "ls-files", "--", "tests/*.py"], capture_output=True, text=True, cwd=REPO
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def changed_tests(base: str) -> list[str]:
    r = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD", "--", "tests/*.py"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def main() -> int:
    import os

    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "--diff":
        base = os.environ.get("BASE_REF", "origin/main")
        targets = changed_tests(base)
        if not targets:
            print("lint-hermetic-bridge-tests: no test files changed — nothing to scan")
            return 0
    else:
        targets = tracked_tests()

    results = scan(targets)

    if mode == "--list":
        for p, v in sorted(results.items()):
            print(f"{v:9} {p}")
        return 0

    new_violations = [p for p, v in results.items() if v == VIOLATION and p not in KNOWN_UNISOLATED]
    mitigated = [p for p, v in results.items() if v == MITIGATED]

    # The grandfather list must shrink, never rot: a listed file that now isolates
    # (or no longer imports a bridge) has to come off the list in the same PR.
    stale = []
    if mode != "--diff":
        for p in sorted(KNOWN_UNISOLATED):
            if not (REPO / p).exists() or results.get(p) != VIOLATION:
                stale.append(p)

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

    if not new_violations:
        print(
            f"lint-hermetic-bridge-tests: ok "
            f"({len(results)} bridge-importing tests scanned, "
            f"{len(KNOWN_UNISOLATED)} grandfathered, {len(mitigated)} mitigated)"
        )
        return 0

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
