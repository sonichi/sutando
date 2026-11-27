"""The owner's own short words that ask for the account card, the model card, or both: one policy
(card_ask.asked), read through the card desk by every connector's account and model card.

Run:
    python3 packages/ag2-sparrow/tests/test_card_ask.py
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import account_switch, card_ask, card_desk, model_switch  # noqa: E402
from ag2_sparrow.adapters.antigravity import agy_account  # noqa: E402

from ag2_sparrow.adapters.claude_code import account_card, model_card  # noqa: E402

A, M = frozenset({"account"}), frozenset({"model"})
AM = A | M
NONE = frozenset()
# (words, the cards they ask for). The first block was measured as missed by the 0.12.4 matcher.
TABLE = (
    ("你在用哪个账号和模型", AM),
    ("什么 account", A),
    ("我说 agy account", A),
    ("你在用什么模型", M),
    ("what model are you using", M),
    ("What model are you using?", M),
    ("which account and model are you on?", AM),
    ("你在用什么账号和什么模型？", AM),
    ("which model", M),
    ("换模型", M),
    ("帮我切换模型", M),
    ("change your model", M),
    ("codex 用的什么 model", M),
    ("agy 在用哪个账号", A),
    # hits the 0.12.4 matcher already had
    ("你在用什么账号", A),
    ("which account", A),
    ("切换账号", A),
    ("switch account", A),
    ("/account", A),
    ("/models please", NONE),      # a slash command is the desk's exact match, never these words
    ("who are you signed in as?", A),
    ("能看到你的账号吗", A),
    ("switch my account", A),
    # another account, another question, or more than the question: the model's
    ("create an account on GitHub", NONE),
    ("我的 GitHub 账号", NONE),
    ("我的账号", NONE),
    ("which model is best for coding?", NONE),
    ("what model should I use for this essay", NONE),
    ("什么是模型", NONE),
    ("what is an account", NONE),
    ("what's your account of what happened?", NONE),
    ("帮我注册一个账号", NONE),
    ("data model for the users table", NONE),
    ("switch to haiku", NONE),
    # "model" or "account" as an ordinary word in a request: the agent's work, not a card
    ("Can you model this?", NONE),
    ("model it", NONE),
    ("what can you model?", NONE),
    ("could you model that for me", NONE),
    ("use model", NONE),
    ("用模型", NONE),
    ("你能帮我建个模型吗", NONE),
    ("账号和密码在哪", NONE),
    ("account for this", NONE),      # a named model: the desk's direct pick (named_in), not the menu
    ("你在用哪个账号和模型" + "，" * card_ask.ASK_MAX, NONE),
    ("", NONE),
    (None, NONE),
)


class Policy(unittest.TestCase):
    def test_a_switch_is_told_from_a_question(self):
        for said, switch in (("switch account", True), ("换模型", True), ("/account", True), ("change your model", True),
                             ("which model", False), ("你在用哪个账号和模型", False), ("hi", False)):
            self.assertEqual(card_ask.switches(said), switch, said)

    def test_a_pick_named_in_words(self):
        for said in ("switch to opus", "Switch to Claude Sonnet 4.5", "change the model to gemini 2.5 pro",
                     "use opus please", "换成 sonnet", "切换到 gpt-5.1 吧", "use o3"):
            self.assertTrue(card_ask.names_a_pick(said), said)
        for said in ("switch to dark mode", "use claude to write this", "go to the store", "", None,
                     "switch to opus " + "x" * card_ask.ASK_MAX):
            self.assertFalse(card_ask.names_a_pick(said), said)

    def test_the_table(self):
        for said, cards in TABLE:
            with self.subTest(said=said):
                self.assertEqual(card_ask.asked(said), cards)
                self.assertEqual(account_switch.asks_for_card(said), "account" in cards)
                self.assertEqual(model_switch.ModelSwitch.asks(said), "model" in cards)

    def test_every_card_reads_the_one_policy(self):
        self.assertIs(account_switch.AccountSwitch.asks, card_ask.asks_for_account)
        self.assertIs(account_card.AccountCard.asks, card_ask.asks_for_account)
        # Codex and Antigravity use ModelSwitch itself; claude-local its subclass.
        self.assertIs(model_switch.ModelSwitch.asks, card_ask.asks_for_model)
        self.assertIs(model_card.ClaudeModels.asks, card_ask.asks_for_model)
        for said, cards in TABLE:
            self.assertEqual(agy_account.AgyAccount.asks(said), "account" in cards, said)


class Card:
    ANSWER = "[no-send] answered\n"

    def __init__(self, kind, asks, publish, posted=True):
        self.KIND, self.ASK, self.CARD_ID = kind, "/" + kind, kind + "-switch"
        self.MARK, self.HINT, self.IN_DM = "[%s-card]" % kind, "h", "dm"
        self.asks, self.publish, self.posted, self.offers = asks, publish, posted, 0

    def usable(self):
        return True

    def named_in(self, words):
        return ""

    def offer(self, sent, result_file):
        self.offers += 1
        self.publish(result_file, self.ANSWER if self.posted else "no %s card\n" % self.KIND)
        return "%s-card" % self.KIND


class Desk(unittest.TestCase):
    """Through the desk, as every connector reads a task: the owner's ask in their DM shows each card it
    names, once, and the task's result is written once."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="card-ask-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.written, self.by_card = [], []
        self.own = own = lambda path, body: self.by_card.append(body)  # noqa: E731 - each card's own publish
        self.model = Card("model", model_switch.ModelSwitch.asks, own)
        # claude-local keeps one account card per way of asking (/account, /login): shown once.
        self.accounts = [Card("account", account_switch.asks_for_card, own) for _ in range(2)]
        self.desk = card_desk.Desk(str(self.tmp), lambda path, body: self.written.append(body), lambda s: s,
                                   [self.model] + self.accounts)

    def task(self, said, tier="owner", kind="dm", extra=""):
        path = self.tmp / "task.txt"
        path.write_text("id: task-1\nsource: ag2space\nchannel_id: !dm:s.space\nchannel_kind: %s\n"
                        "user_id: @owner:s.space\n%saccess_tier: %s\ntask: %s\n" % (kind, extra, tier, said))
        return self.desk.task(str(path), str(self.tmp / "result.txt"))

    def offers(self):
        return self.model.offers, [c.offers for c in self.accounts]

    def test_account_and_model_together_show_both_cards_and_one_answer(self):
        self.assertEqual(self.task("你在用哪个账号和模型"), "model-card+account-card")
        self.assertEqual(self.offers(), (1, [1, 0]))
        self.assertEqual((self.written, self.by_card), ([Card.ANSWER], []))
        for card in [self.model] + self.accounts:
            self.assertIs(card.publish, self.own, "each card keeps its own publish")

    def test_a_card_that_could_not_be_shown_is_said_in_words(self):
        self.accounts[0].posted = False
        self.assertEqual(self.task("which account and model are you on?"), "model-card+account-card")
        self.assertEqual(self.written, ["no account card\n"])

    def test_one_of_them_shows_that_card_alone(self):
        self.assertEqual(self.task("你在用什么模型"), "model-card")
        self.assertEqual(self.offers(), (1, [0, 0]))
        self.assertEqual(self.task("我说 agy account"), "account-card")
        self.assertEqual(self.offers(), (1, [1, 0]))

    def test_model_or_account_as_an_ordinary_word_reaches_the_agent(self):
        for said in ("Can you model this?", "what can you model?", "model it", "你能帮我建个模型吗",
                     "账号和密码在哪", "account for this"):
            with self.subTest(said=said):
                self.assertEqual(self.task(said), "")
        self.assertEqual((self.offers(), self.written), ((0, [0, 0]), []))

    def test_another_account_goes_on_to_the_model(self):
        self.assertEqual(self.task("我的 GitHub 账号"), "")
        self.assertEqual(self.task("我的 GitHub 账号", tier="team"), "")
        self.assertEqual(self.offers(), (0, [0, 0]))
        self.assertEqual(self.written, [])

    def test_in_a_room_the_owner_asking_gets_the_one_line_and_no_card(self):
        # A room is no place for a switch: the owner gets the line, never a card or the model.
        for said, line in (("你在用哪个账号和模型", card_desk.IN_ROOM_ZH), ("switch account", card_desk.IN_ROOM),
                           ("/model", card_desk.IN_ROOM), ("/account opus", card_desk.IN_ROOM),
                           ("what model are you using?", card_desk.IN_ROOM)):
            with self.subTest(said=said):
                self.written.clear()
                self.assertEqual(self.task(said, kind="room"), "card-in-room")
                self.assertEqual(self.written, [line + "\n"])
        self.assertEqual(self.offers(), (0, [0, 0]))
        self.assertEqual(self.by_card, [])

    def answered(self, said, tier, kind="room", extra=""):
        self.written.clear()
        self.assertEqual(self.task(said, tier=tier, kind=kind, extra=extra), "card-not-owner", said)
        line, = self.written
        return line.rstrip("\n")

    def test_team_asking_which_model_is_told_its_name_only(self):
        # Owner 2026-11-26: a team member (a collaborator too) asking which model is told its name, in their
        # language, by the code, never the model; nothing about the account.
        self.model.current_name = lambda: "Claude Sonnet 4.5"
        for said, extra, line in (("what model are you using?", "", "I'm running on Claude Sonnet 4.5."),
                                  ("你在用什么模型", "collaborator: true\n", "我现在用的模型是 Claude Sonnet 4.5。"),
                                  ("which account and model are you on?", "", "I'm running on Claude Sonnet 4.5."),
                                  ("你在用哪个账号和模型", "", "我现在用的模型是 Claude Sonnet 4.5。")):
            with self.subTest(said=said):
                self.assertEqual(self.answered(said, "team", extra=extra), line)
        self.assertEqual(self.answered("what model are you using?", "team", kind="dm"),
                         "I'm running on Claude Sonnet 4.5.", "in a DM too")
        self.assertEqual((self.offers(), self.by_card), ((0, [0, 0]), []))

    def test_team_with_no_model_known_is_told_so_without_a_guess(self):
        self.model.current_name = lambda: ""
        self.assertEqual(self.answered("what model are you using?", "team"), card_desk.MODEL_UNKNOWN)
        self.assertEqual(self.answered("你在用什么模型", "team"), card_desk.MODEL_UNKNOWN_ZH)

        def broken():
            raise RuntimeError("unreadable")
        self.model.current_name = broken
        self.assertEqual(self.answered("which model", "team"), card_desk.MODEL_UNKNOWN)
        del self.model.current_name     # a card that cannot say at all
        self.assertEqual(self.answered("which model", "team"), card_desk.MODEL_UNKNOWN)

    def test_team_asking_about_the_account_is_told_it_is_only_for_the_owner(self):
        self.model.current_name = lambda: "Opus"
        for said, line in (("which account are you on?", card_desk.ACCOUNT_OWNER_ONLY),
                           ("who are you signed in as?", card_desk.ACCOUNT_OWNER_ONLY),
                           ("你在用什么账号", card_desk.ACCOUNT_OWNER_ONLY_ZH)):
            for kind in ("room", "dm"):
                with self.subTest(said=said, kind=kind):
                    self.assertEqual(self.answered(said, "team", kind=kind), line)

    def test_a_switch_by_anyone_but_the_owner_switches_nothing(self):
        self.model.current_name = lambda: "Opus"
        picked = []
        self.model.named_in = lambda words: "haiku" if "haiku" in words else ""
        self.model.asked_for = lambda *a: picked.append(a) or "model-picked"
        for said, team, guest in (("/model", card_desk.SWITCH_OWNER_ONLY, card_desk.ASK_OWNER),
                                  ("/account opus", card_desk.SWITCH_OWNER_ONLY, card_desk.ASK_OWNER),
                                  ("switch account", card_desk.SWITCH_OWNER_ONLY, card_desk.ASK_OWNER),
                                  ("switch to haiku", card_desk.SWITCH_OWNER_ONLY, card_desk.ASK_OWNER),
                                  ("switch to opus", card_desk.SWITCH_OWNER_ONLY, card_desk.ASK_OWNER),
                                  ("换成 sonnet", card_desk.SWITCH_OWNER_ONLY_ZH, card_desk.ASK_OWNER_ZH),
                                  ("换模型", card_desk.SWITCH_OWNER_ONLY_ZH, card_desk.ASK_OWNER_ZH)):
            with self.subTest(said=said):
                self.assertEqual(self.answered(said, "team"), team)
                self.assertEqual(self.answered(said, "guest"), guest)
        self.assertEqual((picked, self.offers(), self.by_card), ([], (0, [0, 0]), []))

    def test_a_guest_is_told_to_ask_the_owner_and_nothing_else(self):
        self.model.current_name = lambda: "Claude Sonnet 4.5"
        for said, line in (("what model are you using?", card_desk.ASK_OWNER),
                           ("which account are you on?", card_desk.ASK_OWNER),
                           ("你在用哪个账号和模型", card_desk.ASK_OWNER_ZH)):
            for kind in ("room", "dm"):
                with self.subTest(said=said, kind=kind):
                    self.assertEqual(self.answered(said, "guest", kind=kind), line)
                    self.assertNotIn("Sonnet", line)

    def test_no_reply_to_anyone_but_the_owner_names_the_account(self):
        # Whatever the account card would say (who is signed in, the email, the plan): never in these lines.
        self.model.current_name = lambda: "Opus"
        for card in self.accounts:
            card.using = lambda: "susan@example.com (Max plan)"
        for tier in ("team", "guest"):
            for said in ("which account are you on?", "你在用哪个账号和模型", "/account", "switch account",
                         "who are you signed in as?", "what's your email?"):
                self.written.clear()
                self.task(said, tier=tier, kind="room")
                for line in self.written:
                    self.assertNotIn("@", line, (tier, said))
                    self.assertNotIn("Max", line, (tier, said))

    def test_anyone_else_s_other_words_still_reach_the_model(self):
        for tier in ("team", "guest"):
            for said in ("hi there", "Can you model this?", "which model is best for coding?", "use claude to write this",
                         "switch to dark mode", "我的 GitHub 账号", "create an account on GitHub"):
                for kind in ("room", "dm"):
                    with self.subTest(tier=tier, said=said, kind=kind):
                        self.assertEqual(self.task(said, tier=tier, kind=kind), "")
        self.assertEqual((self.offers(), self.written, self.by_card), ((0, [0, 0]), [], []))

    def test_an_ambient_or_unknown_tier_is_left_as_it_was(self):
        for tier in ("ambient", "", "nobody"):
            with self.subTest(tier=tier):
                self.assertEqual(self.task("what model are you using?", tier=tier, kind="room"), "")
        self.assertEqual(self.written, [])

    def test_a_pick_named_in_a_room_gets_the_line_and_picks_nothing(self):
        self.model.named_in = lambda words: "haiku" if "haiku" in words else ""
        picked = []
        self.model.asked_for = lambda *a: picked.append(a) or "model-picked"
        self.assertEqual(self.task("switch to haiku", kind="room"), "card-in-room")
        self.assertEqual((picked, self.written), ([], [card_desk.IN_ROOM + "\n"]))
        self.assertEqual(self.task("switch to haiku"), "model-picked", "in the DM the pick is made as before")

    def test_ordinary_words_in_a_room_still_reach_the_model(self):
        for said in ("Can you model this?", "我的 GitHub 账号", "hi there", "which model is best for coding?"):
            with self.subTest(said=said):
                self.assertEqual(self.task(said, kind="room"), "")
        self.assertEqual((self.offers(), self.written), ((0, [0, 0]), []))

    def test_the_model_s_name_is_read_as_the_card_names_it(self):
        state = self.tmp / "state"
        state.mkdir()
        log = lambda *a: None  # noqa: E731
        codex = model_switch.ModelSwitch(str(state), str(self.tmp), None, "Codex", None, lambda s: s, log)
        self.assertEqual(codex.current_name(), "", "no model known yet: not guessed")
        codex.seen([("gpt-5.1", "GPT-5.1"), ("o3", "o3")], "gpt-5.1")
        self.assertEqual(codex.current_name(), "GPT-5.1")
        claude = model_card.ClaudeModels(str(state), str(self.tmp), None, "Claude Code", None, lambda s: s, log,
                                         allowed=model_card.ALLOWED)
        claude.seen(model_card.CHOICES, "sonnet[1m]")
        self.assertEqual(claude.current_name(), "Sonnet (1M context)")

    def test_a_task_naming_no_channel_kind_is_not_a_room(self):
        self.assertFalse(card_desk.in_room({"channel_kind": ""}))
        self.assertFalse(card_desk.in_room({"channel_kind": "dm"}))
        self.assertTrue(card_desk.in_room({"channel_kind": "room"}))
        self.assertTrue(card_desk.in_room({"channel_kind": "place"}))


if __name__ == "__main__":
    unittest.main()
