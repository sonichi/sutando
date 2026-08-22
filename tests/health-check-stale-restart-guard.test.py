#!/usr/bin/env python3
"""The canonical-checkout guard must cover the STALE auto-restart path.

`fix_down_bridges()` has always guarded the DOWN path, but the stale path is
the one that actually kills and relaunches — and it booted whatever was checked
out. This pins the decision unit behaviourally (the restart glue in main() is
un-importable, so the decision is extracted rather than regex-asserted).
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def main() -> int:
    hc = _load()
    failures = []

    def check(cond, label):
        print(("PASS: " if cond else "FAIL: ") + label)
        if not cond:
            failures.append(label)

    # A non-canonical checkout must refuse the restart and carry the reason.
    ok, why = hc.stale_restart_allowed(
        REPO, guard=lambda _d: (False, "checkout on 'feat/x', not main"))
    check(ok is False, "non-canonical checkout refuses a stale auto-restart")
    check("feat/x" in why, "the refusal carries the guard's reason, not a generic string")

    # A canonical checkout must still allow it — a guard that never permits is
    # as useless as one that never refuses.
    ok2, why2 = hc.stale_restart_allowed(REPO, guard=lambda _d: (True, ""))
    check(ok2 is True, "canonical checkout still permits the stale restart")
    check(why2 == "", "no reason is reported when the restart is permitted")

    # A git PROBE FAILURE refuses: .git exists, so the checkout may be a
    # feature branch — a transient failure must not re-authorize the restart.
    ok_u, why_u = hc.stale_restart_allowed(
        REPO, guard=lambda _d: (False, f"{hc.CHECKOUT_UNREADABLE} (git exit 128)"))
    check(ok_u is False, "an UNREADABLE git state refuses (probe failure != bundle)")
    check(hc.CHECKOUT_UNREADABLE in why_u,
          "and it reports why the checkout could not be determined")

    # A NONGIT install (shipped bundle — no .git at all) keeps recovery: it has
    # no branch to be wrong on, and the pre-guard stale path always restarted it.
    ok_b, why_b = hc.stale_restart_allowed(
        REPO, guard=lambda _d: (False, f"{hc.CHECKOUT_NONGIT} (no .git in /x)"))
    check(ok_b is True, "a NONGIT install (bundle) still permits the stale restart")
    check(hc.CHECKOUT_NONGIT in why_b, "and the bundle reason is carried")

    # ...but a DETERMINED wrong branch must still refuse, or the guard is inert.
    ok_d, _ = hc.stale_restart_allowed(
        REPO, guard=lambda _d: (False, "checkout has uncommitted changes"))
    check(ok_d is False, "a determined non-canonical checkout still refuses")

    # The default guard must BE the down path's guard, not a private copy that
    # can drift. Compare against the real symbol rather than asserting on prose.
    import inspect
    src = inspect.getsource(hc.stale_restart_allowed)
    check("_checkout_is_canonical" in src,
          "defaults to the same _checkout_is_canonical the down path uses")
    sentinel = []
    hc.stale_restart_allowed(REPO, guard=lambda d: (sentinel.append(d), (True, ""))[1])
    check(sentinel == [REPO],
          "the guard is called with the repo dir it was handed, not a hardcoded path")

    # REAL guard against REAL checkouts — the branch comparison must honor
    # core.expected_branch, or pinned hosts never auto-apply stale updates.
    import os
    import subprocess
    import tempfile
    env_no_override = {k: v for k, v in os.environ.items() if k != "SUTANDO_EXPECTED_BRANCH"}
    try:
        hc.git_argv("--version")
    except FileNotFoundError:
        print("SKIP: no runnable git (resolver) — real-checkout cases skipped")
        print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
        return 1 if failures else 0
    with tempfile.TemporaryDirectory() as td:
        pinned = Path(td) / "pinned"
        pinned.mkdir()
        # gitignored per-clone in the real repo too — an ignored config file
        # must not read as "uncommitted changes" and mask the branch verdict.
        (pinned / ".gitignore").write_text("sutando.config.local.json\n")
        for argv in (hc.git_argv("init", "-q"),
                     hc.git_argv("checkout", "-q", "-b", "pinned-branch"),
                     hc.git_argv("add", ".gitignore"),
                     hc.git_argv("-c", "user.email=t@t", "-c", "user.name=t",
                                 "commit", "-q", "--allow-empty", "-m", "seed")):
            subprocess.run(argv, cwd=pinned, check=True, capture_output=True)
        from unittest import mock
        with mock.patch.dict(os.environ, env_no_override, clear=True):
            ok_wrong, why_wrong = hc._checkout_is_canonical(pinned)
            check(ok_wrong is False and "not main" in why_wrong,
                  "unconfigured: a pinned-branch checkout is non-canonical vs main")
            (pinned / "sutando.config.local.json").write_text(
                '{"core": {"expected_branch": "pinned-branch"}}')
            # load_config memoizes per (process, repo_root); without a reset the
            # second read returns the pre-config cache and the pin is invisible.
            sc = sys.modules.get("sutando_config")
            if sc is None:
                sys.path.insert(0, str(REPO / "src"))
                import sutando_config as sc
            sc._CACHE = None
            ok_pin, why_pin = hc._checkout_is_canonical(pinned)
            check(ok_pin is True,
                  f"core.expected_branch=pinned-branch makes the same checkout canonical ({why_pin})")
            allowed_pin, _ = hc.stale_restart_allowed(pinned)
            check(allowed_pin is True,
                  "and the stale path permits the restart on the configured pin")
        # Bundle needs POSITIVE provenance: the engine-manifest layout the
        # build ships (manifest in the PARENT dir, .git stripped).
        engine = Path(td) / "engine"
        (engine / "sutando" / "src").mkdir(parents=True)
        bare = engine / "sutando"
        ok_bare, why_bare = hc._checkout_is_canonical(bare)
        check(ok_bare is False and why_bare.startswith(hc.CHECKOUT_UNREADABLE),
              f"no .git AND no manifest is UNREADABLE, not a bundle ({why_bare})")
        allowed_bare, _ = hc.stale_restart_allowed(bare)
        check(allowed_bare is False, "and the stale path refuses it (fail-closed)")
        # Existence is not provenance: dir / corrupt JSON / sha-less all refuse.
        (engine / "ENGINE_MANIFEST.json").mkdir()
        ok_d, why_d = hc._checkout_is_canonical(bare)
        check(ok_d is False and why_d.startswith(hc.CHECKOUT_UNREADABLE),
              f"a DIRECTORY at the manifest path is not provenance ({why_d})")
        check(hc.stale_restart_allowed(bare)[0] is False,
              "and the stale path refuses the directory-manifest checkout")
        (engine / "ENGINE_MANIFEST.json").rmdir()
        (engine / "ENGINE_MANIFEST.json").write_text('{bad')
        check(hc.stale_restart_allowed(bare)[0] is False,
              "corrupt-JSON manifest refuses")
        (engine / "ENGINE_MANIFEST.json").write_text('{}')
        check(hc.stale_restart_allowed(bare)[0] is False,
              "sha-less manifest refuses")
        (engine / "ENGINE_MANIFEST.json").write_text('{"sha": "abc"}')
        ok_ng, why_ng = hc._checkout_is_canonical(bare)
        check(ok_ng is False and why_ng.startswith(hc.CHECKOUT_NONGIT),
              f"manifest in the parent makes it a verified bundle ({why_ng})")
        allowed_ng, _ = hc.stale_restart_allowed(bare)
        check(allowed_ng is True, "and the stale path permits bundle recovery")
        # A .git that resolves nowhere is damaged metadata, never a bundle.
        broken = Path(td) / "broken"
        broken.mkdir()
        (broken / ".git").symlink_to(Path(td) / "gone")
        (broken / "ENGINE_MANIFEST.json").parent.mkdir(exist_ok=True)
        ok_bl, why_bl = hc._checkout_is_canonical(broken)
        check(ok_bl is False and why_bl.startswith(hc.CHECKOUT_UNREADABLE),
              f"a broken .git symlink is UNREADABLE, not a bundle ({why_bl})")
        allowed_bl, _ = hc.stale_restart_allowed(broken)
        check(allowed_bl is False, "and the stale path refuses the broken-link checkout")

    print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
