"""Antigravity's account card, all in the owner's DM with the agent (Commons "切换账号 / API key 需求", part A).

The owner says 换账号 / switch account / 用 API key (or /account, or the turn answers with the account card's
mark) and gets a card with 「切换账号」 and 「用 API key」 (plus 「停用 API key」 while a key is saved). It is a card
of card_desk's `Desk`, asked for and tapped as the Codex account card is (account_switch): only the owner's
tap in their DM acts.

- 切换账号 runs `agy acp --login` ("Log in with Google interactively, then exit") in the background under a
  pseudo-terminal, no window. The sign-in link it prints is posted on the card (and opened on this computer
  when it can be). The owner signs in in the browser and sends the authorization code, or the page's whole
  address, back in this DM: `Connector.handle` hands that one message to `take_code` before anything else,
  which passes the code to the waiting process and never to the model, a log, metrics or state. When the
  process exits 0 the card says 已切到 <account>. agy keeps one Google sign-in for the Mac, so this moves every
  Antigravity agent here; the card says so. The platform offers a connector no way to delete the message,
  so the card asks the owner to delete it.
- 用 API key: a key sent in chat is redacted by the bridge's chat secret filter before the task file is
  written (Google API keys are on its list), so this connector never receives one. The card points to the
  agent's Antigravity settings (Connections), which save the key in the Keychain item gemini_key reads. A
  key that still arrives, or its redaction marker, is answered here and never passed to agy.
- 停用 API key deletes this agent's own Keychain item (gemini_key.clear_key).
- When a turn fails on agy's quota (RESOURCE_EXHAUSTED, "Individual quota reached"), `quota_hit` posts the
  card on its own, at most once per QUOTA_EVERY_S.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

from ... import account_switch, card_desk, connector_cards
from . import gemini_key

SWITCH, KEY, UNKEY = "switch", "key", "unkey"
LABELS = {SWITCH: "切换账号", KEY: "用 API key", UNKEY: "停用 API key"}
SHARED = "注意：这台 Mac 上所有 Antigravity agent 共用一个 Google 登录，换了以后它们都会用新账号。"
STATE_NAME = "agy-account.json"
WAIT_S = 600.0            # a sign-in not finished by then is given up
LINK_WAIT_S = 20.0        # how long agy has to print its sign-in link
EXIT_WAIT_S = 90.0        # how long agy has to finish once it has the code
QUOTA_EVERY_S = 1800.0
OUTPUT_CAP = 64 * 1024
# Words that ask for the card, on their own (short messages only, so a long request is the model's).
ASK_RE = re.compile(r"^\s*(?:/account|(?:请|帮我|我要|我想)?(?:切换|换|更换)(?:一下|个)?账号|switch(?: the)? accounts?|"
                    r"change(?: the)? accounts?|(?:改)?用\s*api\s*key|use (?:an |a |the )?(?:gemini )?api key)"
                    r"\s*[。.!！?？]?\s*$", re.I)
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
REDACTED_KEY = "[REDACTED-Google API Key]"
KEY_IN_CHAT = ("这条消息里有 API key（或被平台遮掉的 key），我没有转给 Antigravity，也没有存。请把它从对话里删掉；"
               "key 请填在 设置 → Connections → 这个 agent 的 Antigravity 里。")
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|Individual quota reached|quota (?:exceeded|exhausted|reached)", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_URL = re.compile(r"https://[^\s\"'<>\x1b]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CODE = re.compile(r"[A-Za-z0-9/_\-.~+=]{8,2048}")


def asks_for_card(text) -> bool:
    return bool(ASK_RE.match(text or "")) and len(text or "") <= 60


def carries_key(text) -> bool:
    """A Gemini API key, or the bridge's marker for one it redacted."""
    return bool(KEY_RE.search(text or "")) or REDACTED_KEY in (text or "")


def code_in(text) -> str:
    """The authorization code in what the owner sent: the `code` of a pasted address, else the message
    itself when it is one code-shaped token; '' for anything else (ordinary words go to the model)."""
    said = (text or "").strip()
    if said.startswith(("http://", "https://")) and not any(c.isspace() for c in said):
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(said).query).get("code") or []
        return values[0] if values and _CODE.fullmatch(values[0]) else ""
    return said if _CODE.fullmatch(said) and any(c.isdigit() for c in said) else ""


class Login:
    """One `agy acp --login` under a pseudo-terminal: the sign-in link it prints, the code fed to it, its
    exit. Its output stays in memory (OUTPUT_CAP), never logged; nothing of it is written anywhere."""

    def __init__(self, argv, env=None, cwd=None, popen=subprocess.Popen):
        import pty
        master, slave = pty.openpty()
        try:
            self.p = popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, close_fds=True,
                           start_new_session=True)
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master, self.out, self.lock = master, bytearray(), threading.Lock()
        self.started = time.time()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            if not chunk:
                return
            with self.lock:
                self.out += chunk
                del self.out[:-OUTPUT_CAP]

    def text(self) -> str:
        with self.lock:
            return _ANSI.sub("", self.out.decode("utf-8", "replace"))

    def link(self, timeout=LINK_WAIT_S) -> str:
        end = time.time() + timeout
        while time.time() < end:
            m = _URL.search(self.text())
            if m:
                return m.group(0).rstrip(".,;)")
            if self.p.poll() is not None:
                return ""
            time.sleep(0.05)
        return ""

    def feed(self, code: str) -> None:
        os.write(self.master, code.encode("utf-8") + b"\r")

    def wait(self, timeout=EXIT_WAIT_S):
        try:
            return self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def account(self) -> str:
        """The account the sign-in names, shortened (account_switch.masked); '' when it names none."""
        found = _EMAIL.findall(self.text())
        return account_switch.masked(found[-1]) if found else ""

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        try:
            os.close(self.master)
        except OSError:
            pass


class AgyAccount:
    KIND, ASK, MARK, HINT, IN_DM = "account", "/account", account_switch.MARK, account_switch.HINT, account_switch.IN_DM
    CARD_ID = account_switch.CARD

    def __init__(self, state_dir, bridge_state, name, agent_id, publish, quoted, log, login_argv, env=None,
                 opener=None, login_factory=Login, saved=None, clear=None):
        self.state_dir, self.bridge_state, self.name, self.agent_id = state_dir, bridge_state, name, agent_id or ""
        self.publish, self.quoted, self.log = publish, quoted, log
        self.login_argv, self.env, self.opener, self.login_factory = list(login_argv), env, opener, login_factory
        self.saved = saved or (lambda: gemini_key.key_saved(self.agent_id))
        self.clear = clear or (lambda: gemini_key.clear_key(self.agent_id))
        self.login, self.room = None, ""

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
            return self.start(room, result_file)
        if verb == KEY:
            self.say(room, self.card("info", "Gemini API key 请填在 设置 → Connections → %s 的 Antigravity 里，它会存进这台 Mac "
                                             "的钥匙串，下一条消息起就用 key。为了安全，发在对话里的 key 会被平台自动遮掉，我收不到，也"
                                             "不会存。" % self.name), result_file)
            return "account-key-where"
        if verb == UNKEY:
            try:
                gone = bool(self.clear())
            except Exception as e:  # noqa: BLE001 - said, never raised into the task
                self.log("Gemini API key not removed:", type(e).__name__)
                gone = False
            if gone:
                self.say(room, self.card("done", "已停用 API key，已从钥匙串删除。下一条消息起用 Google 登录（新开一个会话）。"),
                         result_file)
                return "account-key-cleared"
            self.say(room, self.card("error", "没能删掉 API key，请在 设置 → Connections 里删除。" + self.using()), result_file)
            return "account-key-not-cleared"
        self.publish(result_file, self.quoted("I couldn't match that button. Send /account for a new card.") + "\n")
        return "account-unknown"

    # ── 切换账号 ──
    def waiting(self) -> bool:
        return self.login is not None and self.login.p.poll() is None

    def give_up(self) -> None:
        if self.login is not None:
            self.login.stop()
        self.login, self.room = None, ""

    def start(self, room, result_file) -> str:
        self.give_up()
        try:
            self.login = self.login_factory(self.login_argv, env=self.env)
            url = self.login.link()
        except Exception as e:  # noqa: BLE001 - whatever stopped it, the owner is told
            self.log("sign-in not started:", type(e).__name__)
            url = ""
        if not account_switch.page_ok(url):
            self.give_up()
            self.say(room, self.card("error", "没能开始登录（agy 没给出登录链接）。可以再点一次「切换账号」，或在 设置 → "
                                              "Connections 里切换。" + self.using()), result_file)
            return "account-not-started"
        self.room = room
        opened = False
        if self.opener is not None:
            try:
                opened = bool(self.opener(url))
            except Exception:  # noqa: BLE001 - the link on the card is enough
                opened = False
        text = ("%s点「打开登录页」用 Google 登录。网页给出授权码后，把授权码（或登录完成后浏览器地址栏的整个网址）直接发在这个"
                "对话里：我只交给登录程序，不给模型看，也不写进日志；发完可以把那条消息删掉。%s"
                % ("已在这台电脑的浏览器里打开登录页。" if opened else "", SHARED))
        self.say(room, self.card("info", text, url=url), result_file)
        return "account-started"

    def take_code(self, message, sent, result_file) -> str:
        """The owner's reply while a sign-in waits: when it is a code (or the page's address with one),
        hand it to agy and say how it ended; '' when it is not one, so it is an ordinary message. The
        code is never returned, logged or kept."""
        if not self.waiting() or str(sent.get("channel_id") or "") != self.room:
            return ""
        code = code_in(message)
        if not code:
            return ""
        login, room = self.login, self.room
        try:
            login.feed(code)
        except OSError:
            pass
        code = None
        rc = login.wait()
        who = login.account()
        self.give_up()
        if rc == 0:
            self.keep(account=who)
            still = "（不过这个 agent 存了 API key，会先用 key；点「停用 API key」才用这个账号。）" if self.keyed() else ""
            self.say(room, self.card("done", "已切到 %s 账号。%s%s" % (who or "新的 Google", SHARED, still)), result_file)
            self.log("sign-in finished (agy acp --login exited 0)")
            return "account-switched"
        self.log("sign-in not finished (agy acp --login %s)" % ("timed out" if rc is None else "exited %s" % rc))
        self.post(room, self.menu("登录没有完成（授权码不对、过期，或超时）。可以再点「切换账号」重试。"))
        self.publish(result_file, account_switch.ANSWERED)
        return "account-not-switched"

    def expire(self, now=None) -> str:
        if self.login is None:
            return ""
        if (now if now is not None else time.time()) - self.login.started < WAIT_S and self.waiting():
            return ""
        room = self.room
        self.give_up()
        self.post(room, self.menu("登录等了 %d 分钟没有收到授权码，已经停了。" % (WAIT_S // 60)))
        return "account-timed-out"

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
