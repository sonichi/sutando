#!/usr/bin/env python3
"""`data/generated/` is sendable; the rest of `data/` must stay refused.

`data/` holds `conversation.sqlite` and `memory-snapshots/*.tar.gz` on a live
host, so allowlisting it wholesale would make the conversation store and the
memory corpus attachable. The root is scoped one level down and the negative
half is asserted, so a widening to `data/` fails rather than shipping quietly.

Hermetic: builds a fixture repo whose `sutando.config.local.json` points at a
temp workspace and imports `send_allowlist` from there, so nothing is written
under the operator's workspace. `$SUTANDO_WORKSPACE` cannot do this — v0.8
stopped honoring it for resolution.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

COPY_SRC = ("send_allowlist.py", "workspace_default.py", "util_paths.py",
            "sutando_config.py")
COPY_SCRIPTS = ("sutando-config.sh", "python-binary.sh")


def build_fixture(root: Path) -> tuple[Path, Path]:
    """Fixture repo + its temp workspace. Returns (repo, workspace)."""
    # Workspace at <repo>/workspace — the post-v0.8 default — so resolution lands
    # here whether or not the local config is consulted.
    repo = root / "repo"
    ws = repo / "workspace"
    (repo / "src").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    ws.mkdir()
    for name in COPY_SRC:
        shutil.copy(REPO / "src" / name, repo / "src" / name)
    for name in COPY_SCRIPTS:
        src = REPO / "scripts" / name
        if src.is_file():
            shutil.copy(src, repo / "scripts" / name)
    shutil.copy(REPO / "sutando.config.json", repo / "sutando.config.json")
    (repo / "sutando.config.local.json").write_text(
        json.dumps({"workspace": {"path": str(ws)}}))
    (repo / "CLAUDE.md").touch()
    subprocess.run(["git", "init", "-q", str(repo)], check=False)
    return repo, ws


def load_allowlist(repo: Path):
    sys.path.insert(0, str(repo / "src"))
    spec = importlib.util.spec_from_file_location(
        "sa_fixture", repo / "src" / "send_allowlist.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sa_fixture"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    failures = []

    def check(desc, cond):
        print(f"  {'OK  ' if cond else 'FAIL'}: {desc}")
        if not cond:
            failures.append(desc)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, ws = build_fixture(root)
        sa = load_allowlist(repo)

        # The fixture only means anything if the workspace actually moved.
        # Compare RESOLVED paths: on macOS mktemp yields /var/folders, which is a
        # symlink to /private/var/folders, so a raw string compare fails spuriously.
        check("fixture redirected the workspace (not the operator's)",
              Path(ws).resolve() == Path(sa._REPO).resolve())

        # Precondition that silently inverted a reviewer's run: SEND_ALLOWED_PREFIXES
        # allows /private/tmp/sutando-*, so a fixture placed there is sendable by
        # prefix and every negative check flips. Fail loudly instead.
        real = str(Path(ws).resolve())
        prefixed = [p for p in sa.SEND_ALLOWED_PREFIXES if real.startswith(p)]
        check(f"fixture path is not covered by SEND_ALLOWED_PREFIXES {prefixed or ''}",
              not prefixed)

        roots = sa.SEND_ALLOWED_ROOTS
        check("data/generated is an allowed root",
              any(r.endswith("/data/generated") for r in roots))
        check("bare data/ is NOT an allowed root (would expose the conversation store)",
              not any(r.rstrip("/").endswith("/data") for r in roots))

        # behaviour, on real files inside the FIXTURE workspace
        gen = ws / "data" / "generated" / "ep999-bundle"
        gen.mkdir(parents=True)
        deliverable = gen / "ep999.mp4"
        deliverable.write_bytes(b"\x00")
        check("a derived deliverable under data/generated/ IS sendable",
              sa.is_path_sendable(str(deliverable)))

        for name in ("conversation.sqlite", "usage/usage-probe.jsonl",
                     "memory-snapshots/memory-live-probe.tar.gz"):
            p = ws / "data" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00")
            check(f"data/{name} stays REFUSED", not sa.is_path_sendable(str(p)))

        link = gen / "sneaky.sqlite"
        link.symlink_to(ws / "data" / "conversation.sqlite")
        check("a symlink from data/generated/ to data/ is still REFUSED "
              "(realpath collapse)", not sa.is_path_sendable(str(link)))

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("All derived-output scope tests passed.")


if __name__ == "__main__":
    main()
