#!/usr/bin/env python3
"""The owner's bare "switch account / model / core" request answered with its card, in code.

The core was told in CLAUDE.md to answer these with a card, and it did not always do so: it
answered in words, said "the card is above", or could not answer at all while its plan was at
its limit. So the core's task watcher (watch-tasks-stream.sh) asks this module first, before it
announces a task: a request it recognises gets its card posted here and a `[no-send]` result, and
the core never sees it. Anything else, or any failure, goes to the core as before.

Recognised only when the whole message is the request: an owner-tier DM whose words, after
greetings and "please" / "can you" are dropped, are one of the phrases below, or a short question
in the owner's own words about which account / model / core is in use ("你在用什么模型", "现在用的什么
账号和什么模型", "which model are you using?", mixed Chinese and English), which gets one card per thing
asked, in the order account, model, core. "switch to sonnet" names its own choice, and a question
that asks why or about a failure needs an answer, so both stay with the core (and the limit hold). The card scripts check that the
room is the core's checked owner DM, so a card can never be posted anywhere else.

CLI: switch_card_intercept.py <task_file> --workspace W --results-dir R [--repo REPO]
exit 0 = the card was posted and the task answered; 1 = not ours, or not done (the core takes it).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent
sys.path.insert(0, str(_SRC))

POST_TIMEOUT_S = 45

_ACCOUNT = ("switch account", "switch accounts", "switch my account", "change account", "change my account",
            "switch the account", "change the account", "another account", "use another account",
            "use a different account", "account", "accounts", "/account",
            "换账号", "换个账号", "换一个账号", "切换账号", "切账号", "换账户", "切换账户", "账号", "换号")
_MODEL = ("/model", "switch model", "switch models", "change model", "change the model", "switch the model",
          "switch my model", "change my model", "model", "models", "pick a model", "which model",
          "换模型", "换个模型", "换一个模型", "切换模型", "切模型", "模型")
_RUNTIME = ("switch core", "switch the core", "change core", "change the core", "switch runtime",
            "change runtime", "switch runtimes", "switch engine", "/core", "/runtime",
            "换 core", "换core", "切换 core", "切换core", "切 core", "切core", "换运行时", "切换运行时")

KINDS = {"account": _ACCOUNT, "model": _MODEL, "runtime": _RUNTIME}
SCRIPTS = {"account": ("account-switch", "account_card.py"),
           "model": ("model-switch", "model_card.py"),
           "runtime": ("runtime-switch", "runtime_card.py")}

_LEAD = re.compile(r"^(?:(?:hi|hey|hello|ok|okay|please|pls|plz|can you|could you|would you|can i|"
                   r"i want to|i'd like to|i want|let me|let's|help me|you|sutando|"
                   r"请|帮我|麻烦|你|能不能|可以|我要|我想)[\s,，]*)+", re.I)
_TAIL = re.compile(r"(?:[\s,，]*(?:please|pls|now|for me|thanks|thank you|吧|一下|呢|吗|啊))+$", re.I)
_MENTION = re.compile(r"@\S+")
_PUNCT = re.compile(r"[\s?？!！.。,，~～]+$")


def normalize(words: str) -> str:
    text = _MENTION.sub(" ", words or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    for _ in range(2):
        text = _PUNCT.sub("", text)
        text = _LEAD.sub("", text).strip()
        text = _TAIL.sub("", text).strip()
    return _PUNCT.sub("", text)


def kind_of(words: str) -> Optional[str]:
    """Which card the owner's whole message asks for, or None."""
    text = normalize(words)
    if not text or len(text) > 40:
        return None
    for kind, phrases in KINDS.items():
        if text in phrases:
            return kind
    return None


# A short which/what question naming the account, model or core; why/failure questions and named
# choices ("to sonnet", "换成 opus") stay with the core.
_ASKS = re.compile(r"\b(?:what|which|who)\b|\?|？|什么|哪个|哪一个|哪|吗|是不是|还是|在用|现在用|using|current|are you on", re.I)
_NOT_A_LOOKUP = re.compile(r"\b(?:why|how come|fail(?:ed|s|ing)?|error|broken|switch(?:ed)? to|change(?:d)? to|"
                           r"to|into|and then)\b|为什么|怎么回事|怎么没|失败|报错|没切|切不|不能|换成|换到|切到|改成|"
                           r"sonnet|opus|haiku|gpt|gemini|luna|terra|\bsol\b", re.I)
_ABOUT = (("account", re.compile(r"\baccounts?\b|账号|账户|帐号", re.I)),
          ("model", re.compile(r"\bmodels?\b|模型", re.I)),
          ("runtime", re.compile(r"\bcore\b|\bruntime\b|\bengine\b|运行时|内核", re.I)))
# About the agent itself: "you"/"你" with a using verb ("are you using", "你在用"), or a Chinese using phrase with
# no subject at all ("现在用的什么账号"). "you" alone ("what do you think…") or "current" alone ("my current account balance") is not.
_YOU = re.compile(r"\b(?:you|your|you're|ur|u)\b|你|您", re.I)
_USING = re.compile(r"\b(?:using|use|on|running)\b|在用|用的|用什么|用哪|用着", re.I)
_ZH_USING = re.compile(r"在用|用的|用什么|用哪|用着")
_MINE = re.compile(r"\b(?:i|i'm|my|me|mine)\b|我", re.I)


def _about_self(text: str) -> bool:
    if _YOU.search(text):
        return bool(_USING.search(text))
    return bool(_ZH_USING.search(text)) and not _MINE.search(text)
_OTHER_THING = re.compile(r"\b(?:of|from|for|on (?:my|the|this))\b|\b(?:invoice|iphone|phone|car|bank|email|should|best|better)\b|我该|我应该|应该用|哪个好|写代码", re.I)
QUESTION_MAX = 60


def kinds_of(words: str) -> list:
    """The cards the owner's whole message asks for, in order: one for a bare request (kind_of), or
    one per thing a short "which account / model / core are you using" question names. Empty: not ours."""
    bare = kind_of(words)
    if bare:
        return [bare]
    text = normalize(words)
    asked = _ASKS.search(text) or re.search(r"[?？]\s*$", words or "")
    if not text or len(text) > QUESTION_MAX or not asked or _NOT_A_LOOKUP.search(text):
        return []
    if not _about_self(text) or _OTHER_THING.search(text):
        return []
    return [kind for kind, about in _ABOUT if about.search(text)]


def request(task_text: str, workspace: Optional[Path] = None) -> Optional[dict]:
    """{kind, room, reply_to} when the task is the owner's bare switch request in a DM, else None."""
    import local_task_protocol as ltp
    import task_envelope
    parsed = task_envelope.attested_task_headers(task_text, workspace)
    h = parsed.headers
    # The cards' own owner check (agent_card.owner_may_act), so the two never drift apart. A room (any
    # channel kind but the DM) is taken too: there it gets IN_ROOM, never a card (in_room).
    import agent_card
    if agent_card.owner_may_act(h, dm_only=False) is not None or not (h.get("channel_kind") or "").strip():
        return None
    if (h.get("interaction_type") or "message").strip() != "message" or not (h.get("channel_id") or "").strip():
        return None
    if (h.get("content_modalities") or "text").strip() not in ("", "text"):
        return None
    _reply, words = ltp.split_reply_context(ltp.sender_words(parsed))
    kinds = kinds_of(words)
    if not kinds:
        return None
    return {"kind": kinds[0], "kinds": kinds, "room": h["channel_id"].strip(),
            "reply_to": (h.get("source_message_id") or "").strip()}


def _limit(workspace: Path, now=time.time) -> dict:
    """The core's usage limit in force, as recorded; {} when none is (or it cannot be read)."""
    try:
        import core_quota_notice
        rec = json.loads(Path(workspace).joinpath(*core_quota_notice.FILE).read_text(encoding="utf-8"))
        return rec if core_quota_notice.in_force(rec, now()) else {}
    except (OSError, ValueError, TypeError, ImportError):
        return {}


def limit_lead(workspace: Path, now=time.time) -> str:
    """The limit in force, in the quota card's own words; '' when none is. Out of usage, the owner's ask
    for any card is answered by the limit's card first."""
    import core_quota_notice
    rec = _limit(workspace, now)
    return core_quota_notice.words(str(rec.get("name") or "The core"), str(rec.get("reset") or ""),
                                   credits=bool(rec.get("credits"))) if rec else ""


def limit_in_room(workspace: Path, now=time.time) -> str:
    """The limit in force, as said in a shared room (no account, nothing that switches one); '' when none is."""
    import core_quota_notice
    rec = _limit(workspace, now)
    return core_quota_notice.room_words(str(rec.get("name") or "The core"), str(rec.get("reset") or "")) if rec else ""


# No switch is made in a room with other people in it, and no card that switches is posted there: the
# owner asking there is told so in one line, in their language, and the core never sees it.
IN_ROOM = "Switching the account or model isn't available in a room. Please ask me in our DM."
IN_ROOM_ZH = "房间里不能切换账号或模型，请到私聊里找我。"
_CJK = re.compile(r"[\u3400-\u9fff]")


def in_room(task_text: str, workspace: Optional[Path] = None) -> str:
    """The line a request in a room is answered with ('' in the owner's DM): IN_ROOM, or IN_ROOM_ZH when
    the owner wrote Chinese."""
    import local_task_protocol as ltp
    import task_envelope
    parsed = task_envelope.attested_task_headers(task_text, workspace)
    if (parsed.headers.get("channel_kind") or "").strip() == "dm":
        return ""
    _reply, words = ltp.split_reply_context(ltp.sender_words(parsed))
    return IN_ROOM_ZH if _CJK.search(words or "") else IN_ROOM


def say_in_room(results_dir: Path, filename: str, line: str) -> bool:
    """The answer to a request in a room: the one line, as the task's reply in that room."""
    import result_publish
    try:
        result_publish.publish_text(Path(results_dir) / filename, line + "\n")
    except result_publish.PublishedUnconfirmed:
        return True
    except OSError:
        return False
    return True


def post(req: dict, workspace: Path, repo: Path = _REPO, run=subprocess.run) -> bool:
    """Every card the request asks for; True once any was posted, so the core never answers it twice."""
    kinds = req.get("kinds") or [req["kind"]]
    posted = [kind for kind in kinds if post_one(dict(req, kind=kind), workspace, repo, run)]
    return bool(posted)


def post_one(req: dict, workspace: Path, repo: Path = _REPO, run=subprocess.run) -> bool:
    skill, script = SCRIPTS["account" if req.get("lead") else req["kind"]]
    argv = [sys.executable, str(repo / "skills" / skill / "scripts" / script), "--workspace", str(workspace),
            "post", "--room", req["room"]]
    if req.get("lead"):
        argv += ["--retry", "--lead", req["lead"]]      # the quota card: the limit first, then the account
    if req.get("reply_to"):
        argv += ["--reply-to", req["reply_to"]]
    try:
        done = run(argv, capture_output=True, text=True, timeout=POST_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        return False
    try:
        out = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    return isinstance(out, dict) and out.get("status") == "posted"


def answer(results_dir: Path, filename: str, kind: str) -> bool:
    import result_publish
    try:
        result_publish.publish_text(Path(results_dir) / filename, f"[no-send] posted the {kind} card\n")
    except result_publish.PublishedUnconfirmed:
        return True
    except OSError:
        return False
    return True


def intercept(task_file: Path, workspace: Path, results_dir: Path, repo: Path = _REPO,
              run=subprocess.run, log=None) -> bool:
    log = log or (lambda msg: print(f"switch-card: {msg}", file=sys.stderr, flush=True))
    try:
        text = Path(task_file).read_text(encoding="utf-8", errors="replace")
        req = request(text, workspace)
    except Exception as exc:  # noqa: BLE001 - never stand between a task and the core
        log(f"{Path(task_file).name}: not read ({type(exc).__name__}); the core takes it")
        return False
    if req is None:
        return False
    line = in_room(text, workspace)
    if line:
        line = limit_in_room(workspace) or line     # out of usage: the limit is the answer in a room too
        if not say_in_room(results_dir, Path(task_file).name, line):
            log(f"{Path(task_file).name}: the in-room line was not written; the core takes it")
            return False
        log(f"{Path(task_file).name}: a {req['kind']} request in a room; answered in one line, no card")
        return True
    lead = limit_lead(workspace)
    if lead:
        req = dict(req, lead=lead, kind="quota", kinds=["quota"])   # one limit card, whatever was asked
    if not post(req, workspace, repo, run):
        log(f"{Path(task_file).name}: the {req['kind']} card was not posted; the core takes it")
        return False
    what = " and ".join(req.get("kinds") or [req["kind"]])
    if not answer(results_dir, Path(task_file).name, what):
        log(f"{Path(task_file).name}: the {req['kind']} card was posted but the result was not written")
        return True  # the card is out; a core answer now would be a second reply
    log(f"{Path(task_file).name}: posted the {req['kind']} card; answered without the core")
    return True


ANSWERED = "switch-card-answered"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_file")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--repo", default=str(_REPO))
    a = ap.parse_args(argv)
    if not intercept(Path(a.task_file), Path(a.workspace), Path(a.results_dir), Path(a.repo)):
        return 1
    print(ANSWERED, flush=True)  # the watcher needs this word, so a stub interpreter never swallows a task
    return 0


if __name__ == "__main__":
    sys.exit(main())
