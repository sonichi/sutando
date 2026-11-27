#!/usr/bin/env python3
"""The owner's bare "switch account / model / core" request answered with its card, in code.

The core was told in CLAUDE.md to answer these with a card, and it did not always do so: it
answered in words, said "the card is above", or could not answer at all while its plan was at
its limit. So the core's task watcher (watch-tasks-stream.sh) asks this module first, before it
announces a task: a request it recognises gets its card posted here and a `[no-send]` result, and
the core never sees it. Anything else, or any failure, goes to the core as before.

Recognised only when the whole message is the request: an owner-tier DM whose words, after
greetings and "please" / "can you" are dropped, are one of the phrases below. "switch to sonnet"
names its own choice and stays with the core (and the limit hold). The card scripts check that the
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


def request(task_text: str, workspace: Optional[Path] = None) -> Optional[dict]:
    """{kind, room, reply_to} when the task is the owner's bare switch request in a DM, else None."""
    import local_task_protocol as ltp
    import task_envelope
    parsed = task_envelope.attested_task_headers(task_text, workspace)
    h = parsed.headers
    if (h.get("access_tier") or "").strip() != "owner" or (h.get("channel_kind") or "").strip() != "dm":
        return None
    if (h.get("interaction_type") or "message").strip() != "message" or not (h.get("channel_id") or "").strip():
        return None
    if (h.get("content_modalities") or "text").strip() not in ("", "text"):
        return None
    _reply, words = ltp.split_reply_context(ltp.sender_words(parsed))
    kind = kind_of(words)
    if kind is None:
        return None
    return {"kind": kind, "room": h["channel_id"].strip(),
            "reply_to": (h.get("source_message_id") or "").strip()}


def limit_lead(workspace: Path, now=time.time) -> str:
    """The core's usage limit in force, in the quota card's own words; '' when none is (or it cannot be read).
    Out of usage, the owner's ask for any card is answered by the limit's card first."""
    try:
        import core_quota_notice
        rec = json.loads(Path(workspace).joinpath(*core_quota_notice.FILE).read_text(encoding="utf-8"))
        if not core_quota_notice.in_force(rec, now()):
            return ""
        return core_quota_notice.words(str(rec.get("name") or "The core"), str(rec.get("reset") or ""),
                                       credits=bool(rec.get("credits")))
    except (OSError, ValueError, TypeError, ImportError):
        return ""


def post(req: dict, workspace: Path, repo: Path = _REPO, run=subprocess.run) -> bool:
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
    lead = limit_lead(workspace)
    if lead:
        req = dict(req, lead=lead, kind="quota")
    if not post(req, workspace, repo, run):
        log(f"{Path(task_file).name}: the {req['kind']} card was not posted; the core takes it")
        return False
    if not answer(results_dir, Path(task_file).name, req["kind"]):
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
