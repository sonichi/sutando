"""Upgrade path for the claude-gemini -> agy rename on an existing install.

A pre-rename install has symlinks claude-gemini, claude-codex, claude-router and claude-pi
in its skills dir and no agy link; `skills/refresh-skill.sh --all` can refresh those links
but never creates agy. Afterwards:
  1. the old direct script paths <skills-dir>/claude-gemini/scripts/{gemini-run,agy-browser}.sh
     run (they forward to skills/agy through the real tree the link points into);
  2. the /claude-gemini stub's own command line runs the same way;
  3. route-ai.sh resolves the gemini wrapper to an existing skills/agy script;
  4. the shims name the shipped tree: the path they exec normalises to <skills>/agy/scripts/
     (their `pwd -P`; with `pwd` it would normalise to <skills-dir>/agy/scripts/, which the kernel
     happens to resolve the same way, so only the string pins it).
  5. The same four, with every path containing a space (the deployed shape): the router prints
     %q-escaped words and bash xtrace quotes, so the checks parse with shlex, never \\S+.
  6. The installed-skills-dir fallback: installed/claude-gemini -> physical/claude-gemini and
     installed/claude-router -> physical/claude-router with no physical/agy and a valid installed/agy:
     both shims and the router resolve to installed/agy (the installed root is taken with a logical
     cd ../.., since the kernel would follow the link before `..`).
  7. A copied (non-symlinked) claude-gemini next to an agy link falls back to that sibling, and
     with no agy anywhere it fails with a message instead of exec'ing a missing file.

Run: python3 tests/agy-rename-upgrade-path.test.py
"""
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"
OLD_NAMES = ["claude-gemini", "claude-codex", "claude-router", "claude-pi"]


def run(*cmd: str, env: dict | None = None) -> tuple[int, str]:
    e = os.environ.copy()
    e.update(env or {})
    p = subprocess.run(cmd, capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def pre_rename_install(dst: Path, skills: Path) -> None:
    for name in OLD_NAMES:
        (dst / name).symlink_to(skills / name)
    rc, out = run("bash", str(SKILLS / "refresh-skill.sh"), "--all",
                  env={"SKILLS_DST": str(dst), "REFRESH_SKILL_SETTLE_S": "0"})
    assert rc == 0, out
    for name in OLD_NAMES:
        assert (dst / name).is_symlink(), f"{name} link lost by refresh: {out}"
    assert not (dst / "agy").exists(), "refresh must not be what creates agy; the test would prove nothing"


def exec_target(script: Path, *args: str) -> Path:
    """The path a shim execs, read from bash xtrace and parsed as shell words (paths may hold spaces)."""
    rc, out = run("bash", "-x", str(script), *args)
    assert rc == 0, out
    lines = [l for l in out.splitlines() if l.startswith("+ exec bash ")]
    assert lines, out
    return Path(os.path.normpath(shlex.split(lines[-1][2:])[2]))


def router_wrapper(dst: Path) -> Path:
    rc, out = run("bash", str(dst / "claude-router" / "scripts" / "route-ai.sh"),
                  "--dry-run", "--engine", "gemini", "--", "x")
    assert rc == 0, out
    m = re.search(r"^command: (.*)$", out, re.M)
    assert m, out
    words = shlex.split(m.group(1))
    assert words[:2] == ["bash", words[1]] and words[1].endswith("/agy/scripts/gemini-run.sh"), out
    return Path(words[1])


def check_symlink_layout(dst: Path, skills: Path) -> None:
    shims = dst / "claude-gemini" / "scripts"
    rc, out = run("bash", str(shims / "gemini-run.sh"), "--help")
    assert rc == 0 and out.startswith("Usage: gemini-run.sh"), out
    # agy-browser.sh reads its first word as the action, so --help needs one in front.
    rc, out = run("bash", str(shims / "agy-browser.sh"), "status", "--help")
    assert rc == 0 and out.startswith("Usage: agy-browser.sh"), out
    body = (skills / "claude-gemini" / "SKILL.md").read_text()
    m = re.search(r'^bash "\$SKILL_DIR/([^"]+)" -- "\$ARGUMENTS"$', body, re.M)
    assert m, "stub SKILL.md has no slash-command run line"
    rc, out = run("bash", str(dst / "claude-gemini" / m.group(1)), "--help")
    assert rc == 0 and out.startswith("Usage: gemini-run.sh"), out
    wrapper = router_wrapper(dst)
    assert wrapper.is_file(), f"router names a wrapper that does not exist: {wrapper}"
    # The shim's `pwd -P` yields the physical tree (/private/var on macOS), so resolve the expectation too.
    agy_scripts = (skills / "agy" / "scripts").resolve()
    assert exec_target(shims / "gemini-run.sh", "--help") == agy_scripts / "gemini-run.sh"
    assert exec_target(shims / "agy-browser.sh", "status", "--help") == agy_scripts / "agy-browser.sh"


def test_pre_rename_install(dst: Path) -> None:
    pre_rename_install(dst, SKILLS)
    check_symlink_layout(dst, SKILLS)


def test_pre_rename_install_with_spaces_in_every_path(root: Path) -> None:
    skills = root / "check out" / "skills"
    for name in OLD_NAMES + ["agy"]:
        shutil.copytree(SKILLS / name, skills / name, symlinks=True)
    dst = root / "sk ills"
    dst.mkdir()
    pre_rename_install(dst, skills)
    check_symlink_layout(dst, skills)


def installed_sibling_layout(root: Path) -> Path:
    """installed/<skill> -> physical/<skill> with no physical/agy; installed/agy is the only agy."""
    physical, installed = root / "physical", root / "installed"
    installed.mkdir()
    for name in ("claude-gemini", "claude-router"):
        shutil.copytree(SKILLS / name, physical / name)
        (installed / name).symlink_to(physical / name)
    for name in ("agy", "claude-codex", "claude-pi"):
        (installed / name).symlink_to(SKILLS / name)
    assert not (physical / "agy").exists()
    return installed


def test_installed_sibling_fallback_gemini_run(root: Path) -> None:
    installed = installed_sibling_layout(root)
    shim = installed / "claude-gemini" / "scripts" / "gemini-run.sh"
    rc, out = run("bash", str(shim), "--help")
    assert rc == 0 and out.startswith("Usage: gemini-run.sh"), out
    assert exec_target(shim, "--help") == installed / "agy" / "scripts" / "gemini-run.sh"


def test_installed_sibling_fallback_agy_browser(root: Path) -> None:
    installed = installed_sibling_layout(root)
    shim = installed / "claude-gemini" / "scripts" / "agy-browser.sh"
    rc, out = run("bash", str(shim), "status", "--help")
    assert rc == 0 and out.startswith("Usage: agy-browser.sh"), out
    assert exec_target(shim, "status", "--help") == installed / "agy" / "scripts" / "agy-browser.sh"


def test_installed_sibling_fallback_router(root: Path) -> None:
    installed = installed_sibling_layout(root)
    wrapper = router_wrapper(installed)
    assert wrapper == installed / "agy" / "scripts" / "gemini-run.sh", wrapper
    assert wrapper.is_file(), wrapper


def test_copied_skill_without_agy_fails_loudly(dst: Path) -> None:
    shutil.copytree(SKILLS / "claude-gemini", dst / "claude-gemini")
    rc, out = run("bash", str(dst / "claude-gemini" / "scripts" / "gemini-run.sh"), "--help")
    assert rc != 0 and "skills/agy not found" in out, (rc, out)


def test_copied_skill_falls_back_to_installed_sibling(dst: Path) -> None:
    shutil.copytree(SKILLS / "claude-gemini", dst / "claude-gemini")
    (dst / "agy").symlink_to(SKILLS / "agy")
    rc, out = run("bash", str(dst / "claude-gemini" / "scripts" / "gemini-run.sh"), "--help")
    assert rc == 0 and out.startswith("Usage: gemini-run.sh"), out


def main() -> None:
    cases = [
        (test_pre_rename_install, "agy-upgrade."),
        (test_pre_rename_install_with_spaces_in_every_path, "agy up grade."),
        (test_installed_sibling_fallback_gemini_run, "agy-fallback."),
        (test_installed_sibling_fallback_agy_browser, "agy-fallback."),
        (test_installed_sibling_fallback_router, "agy-fallback."),
        (test_copied_skill_without_agy_fails_loudly, "agy-copied."),
        (test_copied_skill_falls_back_to_installed_sibling, "agy-copied."),
    ]
    failed = []
    for t, prefix in cases:
        with tempfile.TemporaryDirectory(prefix=prefix) as d:
            try:
                t(Path(d))
            except AssertionError as e:
                failed.append(t.__name__)
                print(f"FAIL {t.__name__}: {str(e).strip().splitlines()[-1][:200] if str(e).strip() else 'AssertionError'}")
                continue
            print(f"PASS {t.__name__}")
    if failed:
        raise SystemExit(f"FAILED ({len(failed)}): {', '.join(failed)}")
    print("OK")


if __name__ == "__main__":
    main()
