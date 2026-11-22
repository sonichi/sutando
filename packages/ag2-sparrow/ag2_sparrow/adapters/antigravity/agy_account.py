"""Antigravity's account card, in the owner's DM with the agent (Commons "切换账号 / API key 需求", part A).

The owner says 换账号 / switch account / 用 API key (or /account, or the turn answers with the account card's
mark) and gets a card with 「切换账号」 and 「用 API key」 (plus 「停用 API key」 while a key is saved). It is a card
of card_desk's `Desk`, asked for and tapped as the Codex account card is (account_switch): only the owner's
tap in their DM acts.

No secret is ever taken in chat: every card reply and card input in the web client is sent as a room
message, so neither a sign-in code nor a key may travel that way.

- 切换账号 points to the agent's settings, Connections, Antigravity row, "Switch account": the desktop runs
  `agy acp --login` there in its sign-in terminal, whose paste box takes the code. agy keeps one Google
  sign-in per Mac, so this moves every Antigravity agent here; the card says so. The next owner turn reads
  which account agy authenticated (`applyAuthResult` in agy's own log, `signed_in_as`) and says when it
  changed.
- 用 API key points to that row's API key box (the Keychain item gemini_key reads).
- 停用 API key deletes this agent's own Keychain item (gemini_key.clear_key).
- A sign-in code, a landing address with one, a Gemini key, or the bridge's redaction marker for one, sent
  in chat is never passed to agy (`carries_secret`): the owner is asked to delete the message.
- When a turn fails on agy's quota (RESOURCE_EXHAUSTED, "Individual quota reached"), `quota_hit` posts the
  card on its own, at most once per QUOTA_EVERY_S.
"""
from __future__ import annotations

import json
import os
import re
import time

from ... import account_switch, card_desk, connector_cards
from . import gemini_key

SWITCH, KEY, UNKEY = "switch", "key", "unkey"
LABELS = {SWITCH: "切换账号", KEY: "用 API key", UNKEY: "停用 API key"}
SHARED = "注意：这台 Mac 上所有 Antigravity agent 共用一个 Google 登录，换了以后它们都会用新账号。"
STATE_NAME = "agy-account.json"
QUOTA_EVERY_S = 1800.0
# Words that ask for the card, on their own (short messages only, so a long request is the model's).
ASK_RE = re.compile(r"^\s*(?:/account|(?:请|帮我|我要|我想)?(?:切换|换|更换)(?:一下|个)?账号|switch(?: the)? accounts?|"
                    r"change(?: the)? accounts?|(?:改)?用\s*api\s*key|use (?:an |a |the )?(?:gemini )?api key)"
                    r"\s*[。.!！?？]?\s*$", re.I)
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
REDACTED_KEY = "[REDACTED-Google API Key]"
KEY_IN_CHAT = ("这条消息里像是有授权码或 API key，我没有转给 Antigravity，也没有存。请把它从对话里删掉；授权码请贴在 "
               "设置 → Connections → 这个 agent 的 Antigravity 一行「Switch account」打开的登录终端里，key 请填在那一行的 "
               "API key 框里。")
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|Individual quota reached|quota (?:exceeded|exhausted|reached)", re.I)
_SIGN_IN_CODE = re.compile(r"(?<![A-Za-z0-9])4/[0-9A-Za-z_\-]{20,}")
_CODE_URL = re.compile(r"https?://\S*[?&]code=[^&\s]{8,}")
_AUTH_LINE = re.compile(r"applyAuthResult: email=([^,\s]+@[^,\s]+)")


def asks_for_card(text) -> bool:
    return bool(ASK_RE.match(text or "")) and len(text or "") <= 60


def carries_key(text) -> bool:
    """A Gemini API key, or the bridge's marker for one it redacted."""
    return bool(KEY_RE.search(text or "")) or REDACTED_KEY in (text or "")


def carries_secret(text) -> bool:
    """A Gemini API key or its redaction marker, a Google sign-in code (4/0A...), or an address carrying one."""
    said = text or ""
    return carries_key(said) or bool(_SIGN_IN_CODE.search(said)) or bool(_CODE_URL.search(said))


def signed_in_as(log_dir, pid) -> str:
    """The account agy process `pid` authenticated as, from its own log (`acp-server-<time>-<pid>.log`,
    `applyAuthResult: email=...`), shortened (account_switch.masked); '' when it says none. Only that line
    is read."""
    try:
        names = [n for n in os.listdir(log_dir) if n.startswith("acp-server-") and n.endswith("-%d.log" % int(pid))]
    except (OSError, TypeError, ValueError):
        return ""
    for name in sorted(names, reverse=True):
        try:
            with open(os.path.join(log_dir, name), encoding="utf-8", errors="replace") as f:
                found = [m.group(1) for m in (_AUTH_LINE.search(line) for line in f) if m]
        except OSError:
            continue
        if found:
            return account_switch.masked(found[-1])
    return ""


def agy_log_dir(env) -> str:
    """Where agy writes its logs by default: <HOME>/.gemini/antigravity-cli/log."""
    home = (env or {}).get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".gemini", "antigravity-cli", "log")


class AgyAccount:
    KIND, ASK, MARK, HINT, IN_DM = "account", "/account", account_switch.MARK, account_switch.HINT, account_switch.IN_DM
    CARD_ID = account_switch.CARD

    def __init__(self, state_dir, bridge_state, name, agent_id, publish, quoted, log, saved=None, clear=None):
        self.state_dir, self.bridge_state, self.name, self.agent_id = state_dir, bridge_state, name, agent_id or ""
        self.publish, self.quoted, self.log = publish, quoted, log
        self.saved = saved or (lambda: gemini_key.key_saved(self.agent_id))
        self.clear = clear or (lambda: gemini_key.clear_key(self.agent_id))

    # ── state (no secret: the account's shortened name and when the quota card was last shown) ──
    def record(self) -> dict:
        try:
            with open(os.path.join(self.state_dir, STATE_NAME), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def keep(self, **changes) -> None:
        state = dict(self.record(), **changes)
        path = os.path.join(self.state_dir, STATE_NAME)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(path + ".tmp", path)

    def usable(self) -> bool:
        return card_desk.usable(self.bridge_state)

    def keyed(self) -> bool:
        try:
            return bool(self.saved())
        except Exception as e:  # noqa: BLE001 - unknown reads as the sign-in, which agy falls back to
            self.log("Gemini API key not checked:", type(e).__name__)
            return False

    def using(self, keyed=None) -> str:
        keyed = self.keyed() if keyed is None else keyed
        if keyed:
            return "现在用的是 Gemini API key（这个 agent 自己的）。"
        who = str(self.record().get("account") or "")
        return "现在用的是这台电脑上的 Google 登录%s。" % ("（%s）" % who if who else "")

    def card(self, state, text, menu=False, keyed=False, url=""):
        agent_card, _ = connector_cards._core()
        buttons = []
        if menu:
            verbs = (SWITCH, KEY, UNKEY) if keyed else (SWITCH, KEY)
            buttons = [agent_card.button(LABELS[v], "reply", reply=LABELS[v],
                                         command=agent_card.reply("account", "agent", v)) for v in verbs]
        if url:
            buttons.append(agent_card.button("打开登录页", "url", url=url))
        return agent_card.build(account_switch.CARD if menu else account_switch.NOTE, state, title="Antigravity 账号",
                                body="Account of %s" % self.name, text=text, **({"buttons": buttons} if buttons else {}))

    def menu(self, lead=""):
        keyed = self.keyed()
        return self.card("pending", (lead + " " if lead else "") + self.using(keyed), menu=True, keyed=keyed)

    def post(self, room, built) -> bool:
        return card_desk.post(self.bridge_state, room, built, self.log)

    def say(self, room, built, result_file) -> None:
        posted = self.post(room, built)
        self.publish(result_file, account_switch.ANSWERED if posted else self.quoted(built[1]["text"]) + "\n")

    # ── card_desk's card ──
    def offer(self, sent, result_file) -> str:
        self.say(sent.get("channel_id"), self.menu(), result_file)
        return "account-card"

    def asked_for(self, words, sent, result_file) -> str:
        return self.offer(sent, result_file)

    def expired(self, sent, result_file) -> str:
        fresh = self.post(sent.get("channel_id"), self.menu())
        self.publish(result_file, self.quoted(account_switch.EXPIRED if fresh else account_switch.EXPIRED_NO_CARD) + "\n")
        return "account-card-expired"

    def tapped(self, verb, sent, result_file) -> str:
        room = str(sent.get("channel_id") or "")
        if verb == SWITCH:
            self.say(room, self.card("info", "请打开 设置 → Connections → %s 的 Antigravity 一行，点「Switch account」：会打开登录"
                                             "终端，用 Google 登录后把网页给出的授权码贴在那个终端的输入框里。请不要把授权码发在这个对话里。"
                                             "%s换好以后，我下一条回复会告诉你新账号。" % (self.name, SHARED)), result_file)
            return "account-switch-where"
        if verb == KEY:
            self.say(room, self.card("info", "Gemini API key 请填在 设置 → Connections → %s 的 Antigravity 一行的 API key 框里，它会存进这台 Mac "
                                             "的钥匙串，下一条消息起就用 key。为了安全，发在对话里的 key 会被平台自动遮掉，我收不到，也"
                                             "不会存。" % self.name), result_file)
            return "account-key-where"
        if verb == UNKEY:
            try:
                outcome = self.clear()
            except Exception as e:  # noqa: BLE001 - said as a failure, never raised into the task
                self.log("Gemini API key not removed:", type(e).__name__)
                outcome = gemini_key.FAILED
            if outcome == gemini_key.REMOVED:
                self.say(room, self.card("done", "已停用 API key，已从钥匙串删除。下一条消息起用 Google 登录（新开一个会话）。"),
                         result_file)
                return "account-key-cleared"
            if outcome == gemini_key.ABSENT:
                # A tap on an older card, after the key was already removed: nothing to do, and said so.
                self.say(room, self.card("info", "这个 agent 已经没有保存 API key 了，没有改动。" + self.using(False)), result_file)
                return "account-key-absent"
            self.log("Gemini API key not removed (security failed)")
            self.say(room, self.card("error", "没能删掉 API key（钥匙串操作失败），key 还在用。请在 设置 → Connections 里删除。"),
                     result_file)
            return "account-key-not-cleared"
        self.publish(result_file, self.quoted("I couldn't match that button. Send /account for a new card.") + "\n")
        return "account-unknown"

    # ── which account agy used ──
    def saw(self, who) -> str:
        """A turn's agy authenticated as `who` (masked, '' unknown): the words to lead the answer with when
        that is another account than the last one seen, else ''. The first account seen is only kept."""
        if not who:
            return ""
        before = str(self.record().get("account") or "")
        if before == who:
            return ""
        self.keep(account=who)
        return "（已切到 %s 账号。）" % who if before else ""

    # ── 额度用完 ──
    def quota_hit(self, sent, status, text, detail, now=None) -> str:
        """The turn failed on agy's quota: post the card in the owner's DM (at most once per QUOTA_EVERY_S).
        A line for the answer when the turn was elsewhere, '' otherwise."""
        if not QUOTA_RE.search("%s %s" % (text or "", detail or "")):
            return ""
        agent_card, _ = connector_cards._core()
        if agent_card.owner_may_act(sent) is not None:
            return "Antigravity 这个账号的额度用完了。在和我的私信里说「换账号」可以切换账号或改用 API key。" \
                if str(sent.get("access_tier") or "") == "owner" else ""
        now = time.time() if now is None else now
        if now - float(self.record().get("quota_card_at") or 0) < QUOTA_EVERY_S:
            return ""
        if self.post(sent.get("channel_id"), self.menu("Antigravity 这个账号的额度用完了。可以切换账号，或改用 API key。")):
            self.keep(quota_card_at=now)
        return ""
