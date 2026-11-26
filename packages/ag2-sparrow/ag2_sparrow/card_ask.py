"""The owner's own words that ask for the account card, the model card, or both: one policy for every
connector, read by the card desk (`account_switch.asks_for_card`, `model_switch.ModelSwitch.asks`).

A short message asks when every word in it is one of a small closed vocabulary (who: "you", "codex",
"你"; asking: "which", "what", "哪个", "什么"; switching: "switch", "换"; glue: "are", "using", "在用",
"的") and it names the account or the model. Chinese, English and the two mixed read the same. Any other
word ("create an account on GitHub", "我的 GitHub 账号", "which model is best for coding") leaves the
message to the model, as does a message over ASK_MAX characters.
"""
from __future__ import annotations

import re

ASK_MAX = 80
ACCOUNT, MODEL = "account", "model"

_TOPIC = {
    ACCOUNT: ("accounts", "account", "e-mail", "email", "signed in as", "logged in as",
              "账号", "账户", "帐号", "帐户"),
    MODEL: ("models", "model", "模型"),
}
_ASKING = ("which", "what's", "whats", "what", "who", "哪一个", "哪个", "哪", "什么", "啥", "吗")
_SWITCH = ("switch", "change", "切换", "更换", "换")
_MINE = ("my", "我的")      # the owner's own account only with a switch: "switch my account"
# What the owner may call the agent: product names in one string, never paths to an adapter.
_NAMES = tuple("claude code|agy|antigravity|codex|claude|gemini".split("|"))
_GLUE = _NAMES + (
    "you're", "youre", "you", "u", "your", "this agent", "the agent", "are", "r", "is", "'s", "do", "does",
    "using", "use", "on", "in", "with", "as", "to", "signed in", "logged in", "currently", "right now", "now", "current", "the", "it", "this", "that",
    "one", "can", "could", "see", "tell me", "show me", "show", "please", "pls", "hey", "hi", "ok", "okay",
    "so", "and", "&", "/",
    "你的", "您的", "自己的", "你", "您", "现在的", "目前的", "当前的", "现在", "目前", "当前", "在用的", "在用",
    "使用的", "使用", "用的", "用", "登录的", "登录", "登的", "在", "是", "的", "能不能", "可以", "能",
    "看得到", "看得见", "看到", "看见", "告诉我", "一下", "个", "请问", "请", "帮我", "我要", "我想", "我说",
    "我是说", "我问", "那么", "那", "和", "跟", "还有", "呢", "啊", "呀", "吧", "嘛",
)
# Asking what the word means ("什么是账号") is not asking which one this agent is on.
_DEFINES = ("什么是",)

_KINDS = ([(w, "topic:" + k) for k, ws in _TOPIC.items() for w in ws] + [(w, "ask") for w in _ASKING]
          + [(w, "switch") for w in _SWITCH] + [(w, "mine") for w in _MINE] + [(w, "glue") for w in _GLUE]
          + [(w, "defines") for w in _DEFINES])
_KIND_OF = {w: k for w, k in _KINDS}


def _alt(word: str) -> str:
    # Latin words stand alone ("on" is not inside "one"); Chinese words have no spaces to stand by.
    body = re.escape(word).replace(r"\ ", r"\s+")
    return r"(?<![a-z])" + body + r"(?![a-z])" if re.match(r"[a-z]", word) else body


_WORD = re.compile("|".join(_alt(w) for w in sorted(_KIND_OF, key=len, reverse=True)))
_SPACE = re.compile(r"[\s,，、:：;；.。!！?？~～]+")


def asked(text) -> frozenset:
    """Which cards a whole short message asks for: {"account"}, {"model"}, both, or none."""
    said = (text or "").replace("’", "'").strip().lower()
    if not said or len(text) > ASK_MAX:
        return frozenset()
    if said.startswith("/"):
        # A slash command is the desk's exact match ("/model"), never these words ("/models please").
        return frozenset((ACCOUNT,)) if said == "/account" else frozenset()
    kinds, at = set(), 0
    while at < len(said):
        gap = _SPACE.match(said, at)
        if gap:
            at = gap.end()
            continue
        word = _WORD.match(said, at)
        if not word:
            return frozenset()
        kinds.add(_KIND_OF[re.sub(r"\s+", " ", word.group())])
        at = word.end()
    if "defines" in kinds or ("mine" in kinds and "switch" not in kinds):
        return frozenset()
    return frozenset(k[len("topic:"):] for k in kinds if k.startswith("topic:"))


def asks_for_account(text) -> bool:
    return ACCOUNT in asked(text)


def asks_for_model(text) -> bool:
    return MODEL in asked(text)
