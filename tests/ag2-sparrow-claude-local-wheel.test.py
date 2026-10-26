#!/usr/bin/env python3
"""The claude-local reuse bundle proved from an installed wheel: build the
wheel, install it into a fresh venv, and from an unrelated cwd with no repo src on sys.path exercise
hitl construction and apply_action, the claude-local accepter over its own store root, the lifecycle's
claim/authorize/confirm/stage/publish and the dead-generation recover, the guarded key send against a
scratch tmux server (a short socket under /tmp, created and killed here), and the abnormal-pattern
import; then grep the packaged scripts for any repo-only reference."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "packages" / "ag2-sparrow"

PROBE = r'''
import json, os, subprocess, sys, time, hashlib
from pathlib import Path
import ag2_sparrow
here = Path(ag2_sparrow.__file__).resolve()
assert "site-packages" in str(here), here
assert not any(p and Path(p).resolve() == Path(os.environ["REPO_SRC"]).resolve() for p in sys.path), sys.path
out = {"package": str(here.parent)}

# hitl construction and apply_action, then the claude-local accepter over its own root
from ag2_sparrow.hitl.manager import HitlManager, HitlStore
from ag2_sparrow.hitl.schema import Action, ActionReply, HumanRequirement
state = Path(sys.argv[1]) / "state"
m = HitlManager(HitlStore(state / "hitl" / "requirements"))
r = m.create(HumanRequirement(kind="permission", runtime="x", message="m", guard="g", device={"id": "d"}, room="!r",
                              actions=[Action(id="ok", kind="allow_once", label="OK")]))
m.apply_action(ActionReply(hitl_id=r.id, expected_revision=r.revision, action_id="ok", guard=r.guard, room="!r"))
out["hitl"] = m.get(r.id).status

from ag2_sparrow import claude_local_accepter as accepter, claude_local_card as card, claude_local_records as records
from ag2_sparrow import task_lifecycle as tl
work = Path(sys.argv[1]) / "work"
(work / "tasks").mkdir(parents=True)
(work / "results").mkdir()
assert tl.prepare_work_dir(str(work)) is None
tl.enable_local_format(str(work))
opaque = card.opaque_install("wheel", work)
store_root = card.store_root(state)
card.register_install(store_root, opaque, work, Path(sys.argv[1]) / "sup.lock")
stall = {"reason": "static", "since": 1.0, "frame_hash": "a", "task": "t", "checks": "c", "lines": []}
records.save_supervisor(str(work), dict(records.empty_supervisor(), stall=stall, install_id=opaque))
store, mgr = card.manager(store_root)
req = mgr.create(card.build(card.KIND_STALL, incident="i", install_id=opaque, room="!dm", stall=stall, tail=[],
                            attach_cmd="ag2-sparrow-claude-local attach", failed_check="c"))
out["accept"] = accepter.accept(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id="stop",
                                            guard=req.guard, room="!dm", sender="@o"), state_dir=state, owner_ok=True,
                                log=lambda *a: None)

# the lifecycle, local mode: claim, authorize, confirm, stage, publish; then a dead generation recovered
(work / "tasks" / "task-a.txt").write_text("id: task-a\ntask: hi\n")
dead = subprocess.Popen(["sleep", "0"]); dead.wait()
lc = tl.Lifecycle(str(work), lineage=lambda pid, start: False if pid == dead.pid else True)
gen = {"pid": os.getpid(), "start": "s"}
attempt = lc.prepare("task-a", "interrupted", mode=tl.LOCAL, generation=gen)
token = lc.authorize_call("task-a", attempt, text_hash="h")
lc.confirm_turn("task-a", "turn-1", token, "h")
out["stage"] = lc.stage_result("task-a", b"the answer\n", "turn-1")
out["publish"] = lc.publish_and_settle("task-a")
out["result"] = (work / "results" / "task-a.txt").read_text()
(work / "tasks" / "task-b.txt").write_text("id: task-b\ntask: hi\n")
b = lc.prepare("task-b", "interrupted", mode=tl.LOCAL, generation={"pid": dead.pid, "start": "gone"})
lc.authorize_call("task-b", b, text_hash="h")      # CALLING when the generation died: never resent
lc.recover(generation={"pid": dead.pid, "start": "gone"}, proof=False)
out["recovered"] = (work / "results" / "task-b.txt").read_text()

# the guarded key send on a scratch server
from ag2_sparrow import tmux_pane_keys, tmux_probe
sock = sys.argv[2]
subprocess.run(["tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d", "-s", "w", "-x", "80", "-y", "20", "cat"], check=True)
try:
    time.sleep(0.3)
    rc = subprocess.run(tmux_pane_keys.argv(sock, "w:0", "-l", "WHEEL-SENT"), capture_output=True).returncode
    time.sleep(0.3)
    out["send"] = (rc, "WHEEL-SENT" in subprocess.run(["tmux", "-S", sock, "capture-pane", "-p", "-t", "w:0"],
                                                       capture_output=True, text=True).stdout)
    out["probe"] = tmux_probe.has_session(sock, "w")
finally:
    subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)

from ag2_sparrow.cli_wedge_patterns import ABNORMAL_PATTERNS
out["patterns"] = sorted(name for name, _ in ABNORMAL_PATTERNS)
from ag2_sparrow.adapters.claude_code import cli
out["cli"] = cli.COMMAND
out["plugin"] = str(cli.PLUGIN)
print(json.dumps(out))
'''

# A logged-in Claude Code new enough for the adapter's pin: the two calls connect's preflight makes.
FAKE_CLAUDE = """#!/bin/sh
case "$1" in
  --version) echo "2.1.303 (Claude Code)" ;;
  auth) echo '{"loggedIn": true}' ;;
  *) exit 3 ;;
esac
"""

# A user manager with no claude-local job: connect refuses while the job runs or its state is unreadable.
FAKE_SYSTEMCTL = """#!/bin/sh
case " $* " in
  *" show "*) printf 'not-found\\ninactive\\ndisabled\\n' ;;
esac
"""


class InstalledWheel(unittest.TestCase):
    def setUp(self):
        if not shutil.which("tmux"):
            self.skipTest("tmux is not installed")
        self.tmp = Path(tempfile.mkdtemp(prefix="clw", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        if not (PKG / "build").exists():  # pip wheel leaves build/lib in the source tree
            self.addCleanup(shutil.rmtree, PKG / "build", True)

    def run_ok(self, argv, **kw):
        r = subprocess.run(argv, capture_output=True, text=True, **kw)
        self.assertEqual(r.returncode, 0, "%s\n%s\n%s" % (argv, r.stdout[-2000:], r.stderr[-2000:]))
        return r

    def test_the_bundle_works_from_an_installed_wheel_alone(self):
        dist = self.tmp / "dist"
        self.run_ok([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(dist),
                     str(PKG)], cwd=str(self.tmp))
        wheel = next(dist.glob("ag2_sparrow-*.whl"))
        venv = self.tmp / "venv"
        self.run_ok([sys.executable, "-m", "venv", str(venv)])
        py = venv / "bin" / "python"
        self.run_ok([str(py), "-m", "pip", "install", "-q", "--no-deps", "--no-index", str(wheel)])
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        env = {"PATH": "%s:%s:/usr/bin:/bin" % (venv / "bin", os.path.dirname(shutil.which("tmux"))),
               "HOME": str(self.tmp / "home"), "REPO_SRC": str(REPO / "src")}
        r = self.run_ok([str(py), "-I", "-c", PROBE, str(self.tmp / "run"), str(self.tmp / "s")], cwd=str(elsewhere),
                        env=env)
        import json
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["hitl"], "in_progress")
        self.assertEqual(out["accept"], "applied", "the bridge-owned accepter over its own root, from the wheel")
        self.assertEqual((out["stage"], out["publish"], out["result"]), ("staged", "settled", "the answer\n"))
        self.assertIn("interrupted", out["recovered"], "a dead generation's claim is answered as interrupted")
        self.assertEqual(out["send"], [0, True])
        self.assertIs(out["probe"], True)
        self.assertIn("needs-login", out["patterns"])
        self.assertEqual(out["cli"], "ag2-sparrow-claude-local")
        package = Path(out["package"])
        self.assertTrue(str(package).startswith(str(venv.resolve())), package)
        for script in sorted(package.rglob("*.sh")):
            text = script.read_text(encoding="utf-8", errors="replace")
            for forbidden in ("../scripts", "sutando-config"):
                self.assertNotIn(forbidden, text, "%s names a repo-only helper" % script.name)
        self.assertTrue((package / "tmux-pane-keys.sh").is_file() and (package / "bounded-wait.sh").is_file())
        self.assertTrue((venv / "bin" / "ag2-sparrow-claude-local").is_file(), "the console script is installed")
        plugin = package / "adapters" / "claude_code" / "ag2space-inbox"
        for rel in (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.ts"):
            self.assertTrue((plugin / rel).is_file(), "the wheel ships the plugin's %s" % rel)
        self.assertEqual(Path(out["plugin"]), plugin, "the default --plugin-dir is the shipped plugin")
        # connect with no --plugin-dir passes its preflight on the shipped plugin and, with no job running,
        # reaches the redemption, which a closed loopback port refuses with the code unspent.
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "claude").write_text(FAKE_CLAUDE)
        (fake / "claude").chmod(0o755)
        (fake / "systemctl").write_text(FAKE_SYSTEMCTL)
        (fake / "systemctl").chmod(0o755)
        cenv = dict(env, PATH="%s:%s" % (fake, env["PATH"]), HOME=str(self.tmp / "chome"))
        r = subprocess.run([str(venv / "bin" / "ag2-sparrow-claude-local"), "connect", "--base", "http://127.0.0.1:9",
                            "--code", "-"], input="acp.wheel-code\n", capture_output=True, text=True,
                           cwd=str(elsewhere), env=cenv, timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("not a Claude Code plugin", r.stderr)
        self.assertIn("could not reach http://127.0.0.1:9", r.stderr, "preflight passed without --plugin-dir")
        self.assertIn("the code is still valid", r.stderr)


if __name__ == "__main__":
    unittest.main()
