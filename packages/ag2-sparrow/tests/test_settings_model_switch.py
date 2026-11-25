"""The app's settings page switches a local agent's model (`<adapter> model [--set <id>] --agent <id>`):
the pick is the model card's own state, and the NEXT turn really runs on it, the owner's and a
collaborator's alike: agy gets `session/set_model`, Codex's `turn/start` carries `model`, and local Claude
Code's session starts again with `--model`. The page's `current` is what the connector saw run, never the
stored pick, which stays `pending` until a turn has used it."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_antigravity_adapter as agy_t  # noqa: E402  (its Work harness and stub agy)
import test_codex_connector as codex_t  # noqa: E402  (its stub app-server runner)

from ag2_sparrow import model_settings  # noqa: E402
from ag2_sparrow.adapters.antigravity import cli as agy_cli  # noqa: E402

from ag2_sparrow.adapters.claude_code import cli as claude_cli, hold  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector, connect_cli as codex_cli  # noqa: E402

OPUS = "claude-opus-4-6-thinking"


def page(main, root, *args):
    """What the app runs: the adapter's `model` verb for the agent, its one JSON line and exit code."""
    out, cli = io.StringIO(), sys.modules[main.__module__]
    with mock.patch.object(cli, "selected", lambda a: ("job", "")), \
            mock.patch.object(cli, "load_config", lambda local=None: ({"ROOT": str(root)}, "")), \
            contextlib.redirect_stdout(out):
        rc = main(["model", *args, "--agent", "@a:s.space"])
    return rc, json.loads(out.getvalue().strip().splitlines()[-1])


class AntigravityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = agy_t.Work(self.tmp.name)
        (self.w.home / "models").write_text("")
        self.w.task("task-1", "STUB:TEXT hi")          # a first turn: agy says which models it has
        self.w.serve()

    def tearDown(self):
        self.tmp.cleanup()

    def set_models(self):
        return [r["params"]["modelId"] for r in self.w.records() if r.get("method") == "session/set_model"]

    def test_a_page_pick_is_pending_until_the_next_owner_turn_runs_on_it(self):
        rc, shown = page(agy_cli.main, self.w.root)
        self.assertEqual((rc, shown["current"], shown["pending"], [m["id"] for m in shown["available"]]),
                         (0, "gemini-3.8-flash-medium", None, ["gemini-3.8-flash-medium", OPUS]))
        rc, shown = page(agy_cli.main, self.w.root, "--set", OPUS)
        self.assertEqual((rc, shown["current"], shown["pending"]), (0, "gemini-3.8-flash-medium", OPUS),
                         "the pick is not the model in use until a turn has used it")
        self.assertEqual(self.set_models(), [])
        self.w.task("task-2", "STUB:TEXT again")
        self.w.serve()
        self.assertEqual(self.set_models(), [OPUS], "the next turn switches agy's Session")
        self.assertEqual(self.w.metrics()[-1]["model"], OPUS)
        _, shown = page(agy_cli.main, self.w.root)
        self.assertEqual((shown["current"], shown["pending"], shown["refused"]), (OPUS, None, None))
        # A page pick names no DM: nothing is queued for any room, and nothing stays owed.
        self.assertEqual(list((Path(str(self.w.root) + ".bridge") / "state").rglob("*.json")), [])
        self.assertEqual(json.loads((self.w.root / "state" / "model.json").read_text())["untold"], [])

    def test_the_model_in_use_is_still_the_pick_once_a_later_session_opens(self):
        """A new Session opens on agy's own default before `session/set_model` moves it: that opening
        report is not the model the turn runs on, and the page must not go back to showing it."""
        page(agy_cli.main, self.w.root, "--set", OPUS)
        for n, room in ((2, "!room:s.space"), (3, "!other:s.space")):
            self.w.task("task-%d" % n, "STUB:TEXT again", channel=room)
            self.w.serve()
            self.assertEqual(self.w.metrics()[-1]["model"], OPUS)
            _, shown = page(agy_cli.main, self.w.root)
            self.assertEqual((shown["current"], shown["pending"]), (OPUS, None), "after turn %d" % n)

    def test_a_turn_the_runtime_ran_on_another_model_shows_that_model(self):
        page(agy_cli.main, self.w.root, "--set", OPUS)
        self.w.task("task-2", "STUB:TEXT again")
        self.w.serve()
        (self.w.home / "set-model-fails").write_text("")
        self.w.task("task-3", "STUB:TEXT again", channel="!other:s.space")
        self.w.serve()
        self.assertEqual(page(agy_cli.main, self.w.root)[1]["current"], "gemini-3.8-flash-medium")

    def test_a_collaborators_turn_runs_on_the_owners_page_pick(self):
        page(agy_cli.main, self.w.root, "--set", OPUS)
        (self.w.root / "tasks" / "task-2.txt").write_text(
            agy_t.task_text("task-2", "STUB:TEXT from a collaborator", tier="team", collaborator=True), encoding="utf-8")
        self.w.serve()
        self.assertEqual(self.set_models(), [OPUS])
        self.assertEqual(page(agy_cli.main, self.w.root)[1]["current"], OPUS)

    def test_a_model_agy_did_not_list_is_refused_and_nothing_changes(self):
        rc, shown = page(agy_cli.main, self.w.root, "--set", "gpt-9")
        self.assertEqual((rc, shown["pending"]), (1, None))
        self.assertIn("not one of this agent's models", shown["error"])


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name) / "work"
        (Path(str(self.work) + ".bridge") / "state").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def turn(self, tid, text):
        codex_t.write_text(self.work, tid, text)
        self.assertEqual(codex_t.run_connector(self.work, once=1), 0)
        return codex_t.result(self.work, tid)

    def test_before_codex_listed_its_models_the_page_offers_the_allowlist(self):
        rc, shown = page(codex_cli.main, self.work)
        self.assertEqual((rc, [m["id"] for m in shown["available"]], shown["current"]),
                         (0, list(codex_connector.MODEL_ALLOWLIST), None))
        self.assertEqual(page(codex_cli.main, self.work, "--set", "gpt-4o")[0], 1, "never off the allowlist")

    def test_the_next_owner_and_collaborator_turns_carry_the_page_pick(self):
        rc, shown = page(codex_cli.main, self.work, "--set", "gpt-6-sol")
        self.assertEqual((rc, shown["pending"], shown["current"]), (0, "gpt-6-sol", None))
        owner = "id: task-t1\ntimestamp: 2026-11-14T00:00:00Z\nsource: ag2space\nchannel_id: !room:s.space\n" \
                "access_tier: owner\ntask: hello\n"
        self.assertIn("model=gpt-6-sol", self.turn("task-t1", owner), "turn/start carries the pick")
        _, shown = page(codex_cli.main, self.work)
        self.assertEqual((shown["current"], shown["pending"]), ("gpt-6-sol", None))
        self.assertIn("model=gpt-6-sol", self.turn("task-t2", codex_t.gateway_mid_task("task-t2", "hi", collaborator=True)),
                      "a collaborator's turn runs on the owner's pick too")
        self.assertEqual(list((Path(str(self.work) + ".bridge") / "state").rglob("*.json")), [], "no DM card")


class ClaudeLocalTest(unittest.TestCase):
    def test_the_switched_model_is_shown_and_a_restarted_session_starts_on_it(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"ROOT": d, "CLAUDE": "/bin/claude", "PLUGIN_DIR": d, "CLAUDE_CONFIG_DIR": d}
            with mock.patch.object(hold, "resolve_plugin_dir", lambda p: (Path(p), "")):
                self.assertNotIn("--model", hold.claude_argv(cfg, "s1"))
                (Path(d) / "state").mkdir()
                (Path(d) / "state" / "model-switch.json").write_text(json.dumps({"model": "opus", "at": 1}))
                self.assertEqual(hold.claude_argv(cfg, "s1")[-2:], ["--model", "opus"])
                (Path(d) / "state" / "model-switch.json").write_text(json.dumps({"model": "opus; rm -rf /"}))
                self.assertNotIn("--model", hold.claude_argv(cfg, "s1"), "only a name /model takes")
            (Path(d) / "state" / "model-switch.json").write_text(json.dumps({"model": "sonnet"}))
            out = io.StringIO()
            with mock.patch.object(claude_cli, "selected", lambda a: "job"), \
                    mock.patch.object(claude_cli, "load_config", lambda local: (cfg, "")), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(claude_cli.main(["model", "--agent", "@a:s.space"]), 0)
            shown = json.loads(out.getvalue())
            self.assertEqual((shown["current"], shown["model"], shown["pending"]), ("sonnet", "sonnet", None))
            self.assertIn("opus", [m["id"] for m in shown["available"]])


class StateTest(unittest.TestCase):
    def test_picking_the_model_in_use_or_the_pending_one_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "model.json").write_text(json.dumps({"current": "a", "available": [{"id": "a"}, {"id": "b"}]}))
            self.assertEqual(model_settings.pick(d, "a")[0]["pending"], None)
            self.assertEqual(model_settings.pick(d, "b")[0]["pending"], "b")
            before = Path(d, "model.json").read_text()
            model_settings.pick(d, "b")
            self.assertEqual(Path(d, "model.json").read_text(), before)
            # The connector refused it: the pick is dropped and named, the model in use unchanged.
            state = json.loads(before)
            state.update(wanted="", told="b")
            Path(d, "model.json").write_text(json.dumps(state))
            self.assertEqual({k: model_settings.status(d)[k] for k in ("current", "pending", "refused")},
                             {"current": "a", "pending": None, "refused": "b"})


if __name__ == "__main__":
    unittest.main()
