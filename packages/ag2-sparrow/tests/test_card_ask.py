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
    ("switch to haiku", NONE),      # a named model: the desk's direct pick (named_in), not the menu
    ("你在用哪个账号和模型" + "，" * card_ask.ASK_MAX, NONE),
    ("", NONE),
    (None, NONE),
)


class Policy(unittest.TestCase):
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

    def task(self, said, tier="owner", kind="dm"):
        path = self.tmp / "task.txt"
        path.write_text("id: task-1\nsource: ag2space\nchannel_id: !dm:s.space\nchannel_kind: %s\n"
                        "user_id: @owner:s.space\naccess_tier: %s\ntask: %s\n" % (kind, tier, said))
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

    def test_anyone_else_another_room_or_another_account_goes_on_to_the_model(self):
        self.assertEqual(self.task("你在用哪个账号和模型", tier="team"), "")
        self.assertEqual(self.task("你在用哪个账号和模型", kind="room"), "")
        self.assertEqual(self.task("我的 GitHub 账号"), "")
        self.assertEqual(self.offers(), (0, [0, 0]))


if __name__ == "__main__":
    unittest.main()
