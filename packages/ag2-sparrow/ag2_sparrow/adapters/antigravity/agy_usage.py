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

The numbers come from the desktop app, which already reads them for its usage card (ag2space-cinny-desktop #1323,
usage_meter.rs `agent_usage_at`: agy's Google sign-in from the Keychain, Cloud Code's quota endpoints). Sparrow reads
no Keychain item and holds no token: it asks the app through a request file in this agent's state dir, the way the
account switch does (#6878 / desktop #1330), and the app answers in a result file beside it:

  request  <ROOT>/state/.ag2-usage-request.json (0600, written whole then renamed in)
           {"v": 1, "nonce": "<uuid4 hex>", "at": <unix s>, "agent": "<agent mxid>", "runtime": "antigravity"}
  result   <ROOT>/state/.ag2-usage-result.json (0600, written whole then renamed in)
           {"v": 1, "nonce": "<the request's>", "usage": <agent_usage's provider entry for this agent:
            {"provider": "antigravity", "status": "ok" | "error" | "not_logged_in" | "token_expired" | "throttled",
             "account_label": str | null, "error": str | null,
             "windows": [{"kind": "model:<group>:weekly_7d" | "model:<group>:session_5h", "used_pct": int,
                          "resets_at": "<RFC 3339>" | null}]}>}

ROOT is the agent's work root, as its antigravity-local.conf names it. `read` waits up to WAIT_S (35 s) for the answer
with the request's nonce. No answer (the app is not running, or not one that knows this request), a status that is
not "ok", or no window with a number: None, and the owner is told the usage isn't available right now.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import time
import uuid

from ... import bounded_file_read, quota_notice

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


REQUEST, RESULT = ".ag2-usage-request.json", ".ag2-usage-result.json"
# The app's bound on an answer: one look (2 s) plus agy's three quota calls in turn (10 s timeout each), and a margin.
WAIT_S, POLL_S = 35.0, 0.25
RESULT_MAX = 64 * 1024
_WINDOWS = {"weekly_7d": "weekly", "session_5h": "five-hour"}


def read(state_dir, agent_id: str, wait_s: "float | None" = None, clock=time.monotonic, sleep=time.sleep):
    """This agent's usage as the desktop app read it (`from_app`): {"rows", "account"}, or None when it did not
    answer in `wait_s` or could not read it. One request at a time; a request left unanswered is taken back."""
    state = str(state_dir)
    request, result = os.path.join(state, REQUEST), os.path.join(state, RESULT)
    nonce = uuid.uuid4().hex
    try:
        with _quiet():
            os.unlink(result)        # an answer to an earlier request is not this one's
        _write_private(request, {"v": 1, "nonce": nonce, "at": int(time.time()), "agent": str(agent_id or ""),
                                 "runtime": "antigravity"})
    except OSError:
        return None
    deadline = clock() + (WAIT_S if wait_s is None else wait_s)
    try:
        while True:
            answer = _answer(result, nonce)
            if answer is not None:
                with _quiet():
                    os.unlink(result)
                return from_app(answer.get("usage"))
            if clock() >= deadline:
                return None
            sleep(POLL_S)
    finally:
        with _quiet():
            if _nonce_of(request) == nonce:
                os.unlink(request)   # unanswered: a late app finds nothing to do


def from_app(usage):
    """agent_usage's provider entry -> {"rows", "account"}: the account is the label of the sign-in that very read
    used (never one kept from before), '' when it gave none. None when there are no rows to say."""
    rows = rows_from_app(usage)
    if not rows:
        return None
    label = usage.get("account_label")
    return {"rows": rows, "account": label.strip()[:120] if isinstance(label, str) else ""}


def rows_from_app(usage):
    """agent_usage's provider entry -> the rows `words` says; None unless its status is "ok" with a window."""
    if not isinstance(usage, dict) or usage.get("status") != "ok" or not isinstance(usage.get("windows"), list):
        return None
    rows = []
    for w in usage["windows"]:
        if not isinstance(w, dict) or not isinstance(w.get("kind"), str) or not w["kind"].startswith("model:"):
            continue
        name, _, base = w["kind"][len("model:"):].rpartition(":")
        if not name.strip():
            continue
        rows.append({"name": name.strip(), "window": _WINDOWS.get(base, ""), "used": w.get("used_pct"),
                     "resets": _when(w.get("resets_at"))})
    return rows_of(rows) or None


def _when(stamp) -> str:
    """An RFC 3339 UTC moment ("2026-12-04T00:26:13Z") in local words, '' when there is none or it is not one."""
    found = re.match(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?Z$", str(stamp or ""))
    if not found:
        return ""
    return quota_notice.reset_from_epoch(calendar.timegm(tuple(int(g) for g in found.groups()) + (0, 0, 0)))


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        return kind is not None and issubclass(kind, OSError)


def _write_private(path, record: dict) -> None:
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except BaseException:
        with _quiet():
            os.unlink(tmp)
        raise


def _load(path):
    """A private regular file of this user's, as JSON; None when it is absent, not that, or not an object."""
    data, _why = bounded_file_read.read_regular_bytes(path, RESULT_MAX, follow=False, private=True)
    if data is None:
        return None
    try:
        got = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _nonce_of(path) -> str:
    return str((_load(path) or {}).get("nonce") or "")


def _answer(path, nonce):
    got = _load(path)
    return got if got and got.get("v") == 1 and got.get("nonce") == nonce else None


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
