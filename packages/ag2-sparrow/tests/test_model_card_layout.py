"""The shared model card's layout (model_switch.ModelSwitch.card), the same for agy, local Codex and local
Claude Code: the model in use on top, every model one tap away. Up to six models are buttons; more are a
row per family ("Gemini 4 Neon") with its levels ("XHigh", "High", "Medium", "Low") as that row's taps,
and nothing is left to be typed."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
from ag2_sparrow import connector_cards, model_switch  # noqa: E402

AGY = [("gemini-4-neon-high", "Gemini 4 Neon (High)"), ("gemini-4-neon-xhigh", "Gemini 4 Neon (XHigh)"),
       ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)"), ("gemini-4-neon-low", "Gemini 4 Neon (Low)"),
       ("gemini-4-carbon-high", "Gemini 4 Carbon (High)"), ("gemini-4-carbon-medium", "Gemini 4 Carbon (Medium)"),
       ("gemini-4-carbon-low", "Gemini 4 Carbon (Low)"), ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
       ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"), ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)"),
       ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"), ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
       ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"), ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
       ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"), ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
       ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"), ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
       ("gemini-3.7-flash-medium", "Gemini 3.7 Flash (Medium)"), ("gemini-3.7-flash-low", "Gemini 3.7 Flash (Low)"),
       ("gemini-3.6-flash-high", "Gemini 3.6 Flash (High)"), ("gemini-3.6-flash-medium", "Gemini 3.6 Flash (Medium)"),
       ("gemini-3.6-flash-low", "Gemini 3.6 Flash (Low)"), ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
       ("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)")]


class Layout(unittest.TestCase):
    def switch(self, offered, current, **state):
        d = tempfile.mkdtemp()
        ms = model_switch.ModelSwitch(d, d, d, "agy", lambda *a: None, str, lambda *a: None)
        ms.seen(offered, current)
        if state:
            ms.save(**state)
        return ms

    def built(self, ms):
        agent_card, _ = connector_cards._core()
        body, card = ms.card(ms.state())
        self.assertIsNone(agent_card.problem(card))
        return card

    def test_agys_models_are_a_row_per_family_with_its_levels_on_the_row(self):
        card = self.built(self.switch(AGY, "gemini-3.8-flash-high"))
        self.assertNotIn("buttons", card)
        self.assertEqual(card["text"], "I'm using Gemini 3.8 Flash (High). Pick a model to switch to.")
        self.assertNotIn("/model", card["text"], "nothing is left to be typed")
        rows = card["groups"][0]["rows"]
        self.assertEqual([r["title"] for r in rows],
                         ["Gemini 4 Neon", "Gemini 4 Carbon", "Gemini 4 Hydrogen", "Gemini 4 Argon", "Gemini 3.8 Flash",
                          "Gemini 3.7 Flash", "Gemini 3.6 Flash", "Gemini 3.1 Pro"])
        self.assertEqual([a["label"] for a in rows[0]["actions"]], ["High", "XHigh", "Medium", "Low"])
        flash = rows[4]
        self.assertEqual((flash.get("tag"), [a["style"] for a in flash["actions"]]),
                         ("In use", ["primary", "secondary", "secondary"]))
        self.assertEqual(sum(len(r["actions"]) for r in rows), len(AGY), "every model is one tap")
        self.assertEqual(flash["actions"][1]["reply"], "Use Gemini 3.8 Flash (Medium)")
        self.assertEqual(flash["actions"][1]["command"], "card:model:agent:gemini-3.8-flash-medium")

    def test_a_pending_pick_is_said_where_the_core_says_it(self):
        card = self.built(self.switch(AGY, "gemini-3.8-flash-high", wanted="gemini-4-neon-low"))
        self.assertEqual(card["text"], "I'm using Gemini 3.8 Flash (High). Pick a model to switch to. Saved, not "
                                       "switched yet: Gemini 4 Neon (Low), from your next message.")
        self.assertEqual(card["note"], model_switch.NOTE_TEXT)

    def test_a_family_with_more_levels_than_a_row_holds_wraps(self):
        many = [("m-%d" % i, "Big (L%d)" % i) for i in range(6)] + [("solo", "Solo"), ("x", "X (High)")]
        card = self.built(self.switch(many, "solo"))
        rows = card["groups"][0]["rows"]
        self.assertEqual([(r["title"], len(r["actions"])) for r in rows], [("Big", 4), ("Big", 2), ("Solo", 1), ("X", 1)])
        self.assertEqual((rows[2]["actions"][0]["label"], rows[2].get("tag")), ("Use", "In use"))

    def test_six_models_or_fewer_stay_buttons_with_the_one_in_use_marked(self):
        card = self.built(self.switch(AGY[:3], "gemini-4-neon-xhigh"))
        self.assertEqual([(b["label"], b["style"]) for b in card["buttons"]],
                         [("Gemini 4 Neon (High)", "secondary"), ("Gemini 4 Neon (XHigh)", "primary"),
                          ("Gemini 4 Neon (Medium)", "secondary")])

    def test_after_a_pick_the_card_says_saved_with_the_status_the_core_uses(self):
        ms = self.switch(AGY, "gemini-3.8-flash-high")
        _, card = ms.card(ms.state(), picked="gemini-4-neon-low")
        self.assertEqual((card["state"], card["header"]["status"]["label"]), ("pending", "Saved"))
        self.assertTrue(card["text"].startswith("Saved, not switched yet: I'll use Gemini 4 Neon (Low)"))


if __name__ == "__main__":
    unittest.main()
