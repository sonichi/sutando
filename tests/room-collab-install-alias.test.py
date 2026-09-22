#!/usr/bin/env python3
"""After `skills/install.sh`, the old skill name still reaches the old script.

The rename keeps `room-doc` alive for one release as an alias link to the
`room-collab` directory. An alias that resolves the skill but not the script
a cached instruction names — `<installed>/room-doc/scripts/room_doc.py` — is a
window that only looks open. So this runs the real installer into a scratch
CLAUDE_CONFIG_DIR and walks both paths the way a caller would.

Run: python3 tests/room-collab-install-alias.test.py  (exit 0 pass / 1 fail)
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def install_into(config_dir: Path) -> str:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    out = subprocess.run(["bash", str(REPO / "skills" / "install.sh")], env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_both_names_reach_both_scripts_after_install():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "cfg"
        out = install_into(cfg)
        skills = cfg / "skills"
        assert (skills / "room-collab" / "SKILL.md").is_file(), "the new skill is installed"
        assert (skills / "room-collab" / "scripts" / "room_collab.py").is_file()
        assert (skills / "room-doc").is_symlink(), "the old name is an alias link"
        assert "room-doc → room-collab (alias)" in out, out
        # the path a cached instruction names, through the installed alias
        old = skills / "room-doc" / "scripts" / "room_doc.py"
        assert old.is_file(), f"{old} must resolve through the alias"
        # and it forwards: the new script's usage, the note on stderr, stdout clean
        run = subprocess.run([sys.executable, str(old), "--help"], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr[-300:]
        assert "usage: room_collab" in run.stdout, run.stdout[:120]
        assert "room-collab" in run.stderr, "the note says where to go"
        assert "note:" not in run.stdout, "the note is not on stdout, where JSON consumers read"


def test_a_real_room_doc_directory_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "cfg"
        real = cfg / "skills" / "room-doc"
        real.mkdir(parents=True)
        (real / "keep").write_text("mine")
        install_into(cfg)
        assert not real.is_symlink() and (real / "keep").read_text() == "mine", \
            "a directory someone put there is not replaced by the alias"


def test_a_broken_old_link_is_replaced():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "cfg"
        (cfg / "skills").mkdir(parents=True)
        os.symlink(str(Path(tmp) / "gone"), str(cfg / "skills" / "room-doc"))
        install_into(cfg)
        assert (cfg / "skills" / "room-doc" / "scripts" / "room_doc.py").is_file(), \
            "a link to a folder that no longer exists is re-pointed at the alias"


def test_the_old_skill_dir_is_not_installed_as_a_skill():
    # skills/room-doc/ holds only the forwarder; with no SKILL.md the installer
    # must not link it, or two skills would register for one thing.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "cfg"
        out = install_into(cfg)
        assert "✓ room-doc\n" not in out and "↻ room-doc" not in out, out


for _name, _fn in sorted((k, v) for k, v in list(globals().items()) if k.startswith("test_")):
    check(_name, _fn)

if FAILS:
    print("room-collab install alias: FAIL")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("room-collab install alias: ok")
