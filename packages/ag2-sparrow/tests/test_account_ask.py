"""The owner's own words that ask for the account card: one policy (account_switch.asks_for_card), read by
the card desk for every connector's account card, never a per-adapter copy.

Run:
    python3 packages/ag2-sparrow/tests/test_account_ask.py
"""
import ast
import sys
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import account_switch, card_desk  # noqa: E402
from ag2_sparrow.adapters.antigravity import agy_account  # noqa: E402

SWITCH = ("/account", "switch account", "Switch account.", "change the account", "please switch accounts",
          "换账号", "帮我切换账号", "我想换个账号")
WHICH = ("agy account, can you see it?", "which account are you using?", "Which account are you on",
         "what account are you using", "what's your account?", "which account", "what account is this?",
         "who are you signed in as?", "can you see your account?", "can you see which account you're on?",
         "what account are you signed in with", "which account is codex using?", "codex account, which one?",
         "hey, which account are you on?", "what email are you signed in as?",
         "你用的哪个账号", "你用的是哪个账号？", "哪个账号", "账号是什么", "你的账号是什么？", "你现在用的是什么账号",
         "能看到你的账号吗", "你登录的是哪个账号")
# About some other account, or more than the question: these are the model's.
NOT_THE_CARD = ("create an account on github", "which account should I use for AWS billing?",
                "what account does Bob use?", "switch account settings page is broken", "帮我注册一个账号",
                "哪个账号欠费了", "write a script that can switch account for my users",
                "can you see my account balance", "what is an account", "账号被封了怎么办",
                "which accounts payable entries are open?", "delete my account",
                "what's your account of what happened?", "", None,
                "which account are you using? also summarise the attached report and send it to the team tonight")


class Policy(unittest.TestCase):
    def test_asking_to_switch_or_which_account_in_english_or_chinese_is_the_cards(self):
        for said in SWITCH + WHICH:
            with self.subTest(said=said):
                self.assertTrue(account_switch.asks_for_card(said))
                self.assertTrue(account_switch.asks_for_card("  " + said.upper() + " "), "case and edges do not matter")

    def test_another_account_or_a_longer_request_is_not(self):
        for said in NOT_THE_CARD:
            with self.subTest(said=said):
                self.assertFalse(account_switch.asks_for_card(said))
        self.assertFalse(account_switch.asks_for_card("which account" + " " * account_switch.ASK_MAX + "?"), "too long")

    def test_every_account_card_reads_the_one_policy(self):
        self.assertIs(account_switch.AccountSwitch.asks, account_switch.asks_for_card)
        for said in SWITCH + WHICH:
            self.assertTrue(agy_account.AgyAccount.asks(said), said)
        for said in NOT_THE_CARD:
            self.assertFalse(agy_account.AgyAccount.asks(said), said)
        self.assertTrue(agy_account.asks_for_card("use an API key"), "Antigravity's own extra words still ask")
        self.assertFalse(account_switch.asks_for_card("use an API key"))
        # No adapter keeps words of its own for "which account" or "switch account".
        for path in sorted((PKG_ROOT / "ag2_sparrow" / "adapters").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 400:
                    self.assertNotRegex(node.value, r"哪个账|which\|what|switch\(\?: the\)\? account",
                                        "%s keeps its own copy of the ask" % path.name)


class Card:
    KIND, CARD_ID, ASK, MARK, HINT, IN_DM = "account", "account-switch", "/account", "[account-card]", "h", "dm"
    asks = staticmethod(account_switch.asks_for_card)

    def __init__(self, usable=True):
        self.up, self.seen = usable, []

    def usable(self):
        return self.up

    def offer(self, sent, result_file):
        self.seen.append(("offer", sent.get("channel_id")))
        return "account-card"

    def asked_for(self, words, sent, result_file):
        self.seen.append(("slash", words))
        return "account-card"


class NoWords(Card):
    KIND, ASK = "model", "/model"
    asks = None


class Desk(unittest.TestCase):
    """Through the desk, as every connector reads a task: only the owner's ask in their DM shows the card."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="account-ask-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.account, self.model, self.said = Card(), NoWords(), []
        self.desk = card_desk.Desk(str(self.tmp), lambda path, body: self.said.append(body), lambda s: s,
                                   [self.model, self.account])

    def task(self, said, tier="owner", kind="dm"):
        path = self.tmp / "task.txt"
        path.write_text("id: task-1\nsource: ag2space\nchannel_id: !dm:s.space\nchannel_kind: %s\n"
                        "user_id: @owner:s.space\naccess_tier: %s\ntask: %s\n" % (kind, tier, said))
        return self.desk.task(str(path), str(self.tmp / "result.txt"))

    def test_the_owners_question_in_their_dm_shows_the_account_card(self):
        for said in ("which account are you using?", "agy account, can you see it?", "你用的哪个账号", "switch account"):
            with self.subTest(said=said):
                self.account.seen.clear()
                self.assertEqual(self.task(said), "account-card")
                self.assertEqual(self.account.seen, [("offer", "!dm:s.space")])
        self.assertEqual(self.model.seen, [], "a card with no words of its own is never asked")

    def test_a_near_miss_anyone_else_or_another_room_goes_on_to_the_model(self):
        self.assertEqual(self.task("create an account on github"), "")
        self.assertEqual(self.task("which account are you using?", tier="team"), "")
        self.assertEqual(self.task("which account are you using?", kind="room"), "")
        self.account.up = False
        self.assertEqual(self.task("which account are you using?"), "", "no card can be shown: the model answers")
        self.assertEqual(self.account.seen, [])

    def test_the_slash_command_still_reaches_asked_for(self):
        self.assertEqual(self.task("/account now"), "account-card")
        self.assertEqual(self.account.seen, [("slash", "now")])


if __name__ == "__main__":
    unittest.main()
