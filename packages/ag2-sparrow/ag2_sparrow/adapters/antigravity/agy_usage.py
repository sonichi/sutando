"""The owner asks Antigravity how much of its own usage is left: answered here, from code, never by the model.

`asks(text)` reads a short message the way card_ask reads a card ask. Every word in it must come from a small
closed vocabulary, it must name usage ("usage", "quota", "limits", "额度", "用量", "用了多少"), and it must name the
agent ("you", "your", "agy", "你") or what is left ("left", "剩"). Chinese, English and the two mixed read the same:
"你还剩多少额度", "how much usage do you have left?", "what's your usage", "我的 agy 用了多少". Any other word
("show CLI usage", "usage of this function", "what's the usage of X") leaves the message to the model, as does
a message over ASK_MAX characters.

The answer comes in one of three states, and the connector picks it (antigravity_connector.usage_task):
- the account is out of usage (quota_notice says so): only the quota card, as for any card ask;
- the usage can be read: what is used of each window, as agy's own /usage panel lists them (`words`);
- it cannot be read: UNAVAILABLE, never a 0.

Sparrow has no reader yet. The only one is the desktop app's (ag2space-cinny-desktop #1323, usage_meter.rs). It
reads agy's Google sign-in from the Keychain, with the owner's approval for the app only, and asks Cloud Code's
internal quota endpoints. `read` is where a reader goes: it returns the rows, or None when it cannot say. Until
one exists here, every read says None, and the owner is told the usage isn't available right now.
"""
from __future__ import annotations

import re

ASK_MAX = 80
UNAVAILABLE = "Antigravity's usage isn't available right now, so I can't say how much is left."
ON_API_KEY = "This agent uses its own Gemini API key, so its usage isn't shown here."

# Not "limit" alone: "how do you limit" asks about something else.
_TOPIC = ("usage", "quota", "quotas", "limits", "rate limits", "rate limit", "usage limit", "usage limits", "credits",
          "额度", "用量", "使用量", "配额", "限额", "用了多少", "用掉多少", "用完", "用完了", "用光", "用光了")
_SELF = ("you", "your", "you're", "youre", "u", "agy", "antigravity", "this agent",
         "你", "您", "你的", "您的", "你自己的", "自己的")
_LEFT = ("left", "remaining", "remain", "still have", "剩", "还剩", "剩下", "剩余", "还有", "剩多少")
_GLUE = (
    "what", "what's", "whats", "how much", "how many", "how", "is", "are", "do", "does", "have", "has", "got",
    "the", "my", "any", "still", "now", "right now", "currently", "current", "today", "this week",
    "check", "show", "show me", "tell me", "see", "please", "pls", "hey", "hi", "ok", "okay", "so",
    "多少", "几", "吗", "呢", "啊", "呀", "吧", "了", "的", "是", "有", "还", "现在", "目前", "当前", "今天",
    "查", "查一下", "查查", "看看", "看一下", "告诉我", "请", "请问", "帮我", "一下", "我的", "怎么样", "如何",
)
_KIND_OF = dict([(w, "glue") for w in _GLUE] + [(w, "left") for w in _LEFT] + [(w, "self") for w in _SELF]
                + [(w, "topic") for w in _TOPIC])


def _alt(word: str) -> str:
    # Latin words stand alone ("u" is not inside "usage"); Chinese words have no spaces to stand by.
    body = re.escape(word).replace(r"\ ", r"\s+")
    return r"(?<![a-z'])" + body + r"(?![a-z'])" if re.match(r"[a-z]", word) else body


_WORD = re.compile("|".join(_alt(w) for w in sorted(_KIND_OF, key=len, reverse=True)))
_SPACE = re.compile(r"[\s,，、:：;；.。!！?？~～]+")


def asks(text) -> bool:
    """Whether a whole short message asks how much of this agent's own usage is used or left."""
    said = (text or "").replace("’", "'").strip().lower()
    if not said or len(said) > ASK_MAX or said.startswith("/"):
        return False
    kinds, at = set(), 0
    while at < len(said):
        gap = _SPACE.match(said, at)
        if gap:
            at = gap.end()
            continue
        word = _WORD.match(said, at)
        if not word:
            return False
        kinds.add(_KIND_OF[re.sub(r"\s+", " ", word.group())])
        at = word.end()
    return "topic" in kinds and bool(kinds & {"self", "left"})


def read(agent_id: str):
    """This agent's usage windows, as agy's /usage panel lists them: [{"name", "window", "used" (percent),
    "resets"}], or None when they cannot be read. Sparrow has no reader yet (see the module's note)."""
    return None


def rows_of(got) -> list:
    """The rows a reader gave that can be said, [] when there are none: a row without a number is never shown as 0."""
    if not isinstance(got, list):
        return []
    rows = []
    for row in got:
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            return []
        used = row.get("used")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
            return []
        rows.append(row)
    return rows


def words(rows: list, using: str = "") -> str:
    """The usage in words: one line per window, led by the account in use when `using` names it."""
    lines = ["Antigravity's usage%s:" % (" (%s)" % using if using else "")]
    for row in rows:
        window = str(row.get("window") or "").strip()
        resets = str(row.get("resets") or "").strip()
        lines.append("- %s%s: %d%% used%s" % (str(row["name"]).strip(), ", " + window if window else "",
                                              round(row["used"]), " (resets %s)" % resets if resets else ""))
    return "\n".join(lines)
