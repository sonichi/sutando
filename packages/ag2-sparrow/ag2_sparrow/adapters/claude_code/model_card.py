"""claude-local's model card: the owner switches the session's model from their DM, by the same card as
Antigravity's and local Codex's (`model_switch.ModelSwitch`).

The Mod hands every task to `inbox claim` before the session sees it, so a card matter is answered there
and never reaches the model (`card_matter`): the owner's tap on a model card this agent posted, or "/model"
in their DM (the menu), or "/model <name>". A pick is saved as the owner's wanted model, a "switching"
card is posted, and `apply` is started detached: it runs the launcher's live switch (#6679,
`Launcher.switch_model --confirm`: `/model <id>` typed into the session's pane, no restart) and then tells
the owner in the DM whether it took. The session's model is every later turn's, whoever sends it; the
isolated turn for a restricted sender runs on the same pick (`turn_model`).

    python3 -m ag2_sparrow.adapters.claude_code.model_card apply --root ROOT   # the detached step
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ... import card_desk, connect_bridge as cb, connect_common as cm, connector_cards, model_switch
from ... import claude_local_records as records
from ...acp_host import quoted
from .shape import LOCAL

NAME = "Claude Code"
# The core's own menu (skills/model-switch MODEL_SWITCH_CHOICES), in its order: one card for every Claude.
CHOICES = (("default", "Default"), ("opus", "Opus"), ("sonnet[1m]", "Sonnet (1M context)"),
           ("haiku", "Haiku"), ("fable[1m]", "Fable (1M context)"))
ALLOWED = frozenset(mid for mid, _ in CHOICES)
APPLY_DELAY_S, APPLY_TRIES = 2.0, 3
# The session may be in a long turn: the switch waits for it to go idle, polling, for up to this long in
# all (every try included), and only then says it could not switch, and why.
IDLE_WAIT_S, IDLE_POLL_S = 300.0, 5.0
# The live switch's answers worth another try, and the reason told when the last try still gives one.
RETRYABLE = {"unsent text": "your message box in the session held unsent text",
             "does not show its whole input box": "the session's screen could not be read",
             "unconfirmed": "Claude Code did not confirm the switch"}
BUSY = "the session stayed busy with a turn for 5 minutes"
NOT_APPLIED = "the switch was never applied"
# The detached apply's pid and start, kept in the card's own record (model.json): a pick with none alive is dead.
APPLY_KEY = "apply"
# The owner's own words naming a model: "switch to haiku", "use opus", "换成 haiku" (short messages only).
_NAMED_RE = re.compile(r"^\s*(?:please\s+|can you\s+)?(?:(?:switch|change|move|go)(?:\s+the)?(?:\s+models?)?\s+to"
                       r"|use|try|(?:请|帮我)?(?:切换|换|改|切)(?:到|成)|用)\s*(?:the\s+)?(.+?)"
                       r"(?:\s+models?|\s*模型)?\s*(?:please)?\s*[.!?。！？]*\s*$", re.I)
_CONTEXT = re.compile(r"\s*(?:\[1m\]|\(1m context\))\s*$", re.I)
_FAMILIES = ("opus", "sonnet", "haiku", "fable")


def _base(text: str) -> str:
    """A model id or name without its context size, lowercased: "fable[1m]" and "Fable (1M context)" are "fable"."""
    return _CONTEXT.sub("", str(text or "")).strip().lower()


class ClaudeModels(model_switch.ModelSwitch):
    """The model card for Claude: switched live (no "next message"), its models named as Claude's menu names
    them however the session reports the id ("fable" is "Fable", never the raw id), and a model named in the
    owner's own words picked directly."""

    SWITCHING = "Switching to %s… I'll confirm here once it has switched."

    def display(self, names: dict, mid: str, fallback: str = "") -> str:
        if not mid:
            return fallback
        if names.get(mid):
            return names[mid]
        exact = dict(CHOICES).get(mid)
        if exact:
            return exact
        # Another id of a menu model ("fable" beside "fable[1m]", "claude-opus-4-7"): its family's name, with no
        # context size, since this id may not have it.
        base = _base(mid)
        family = next((f for f in _FAMILIES if f in base), "")
        if family:
            return family.capitalize()
        return mid[:1].upper() + mid[1:]

    def named_in(self, words: str) -> str:
        if not words or len(words) > 60 or words.startswith("/"):
            return ""
        m = _NAMED_RE.match(words)
        if not m:
            return ""
        said = _base(m.group(1))
        ids = [cid for cid, name in CHOICES if said in (cid.lower(), _base(cid), name.lower(), _base(name))]
        return ids[0] if len(ids) == 1 else ""


def log(*parts):
    print("model-card:", *parts, file=sys.stderr)


def current_model(root) -> str:
    """The model the session is on as far as this agent knows: the last live switch's, else the default."""
    from .launcher import switched_model
    return switched_model(root) or "default"


def switcher(root, cfg: dict, publish=None, settle=True) -> model_switch.ModelSwitch:
    """The card's state and rules for this agent, its offer set to Claude's menu and its model in use. A pick
    no apply is working on any more (it stopped, or never started) is settled as not switched first, and the
    owner told, so the card never shows a pending model that will not come (`settle`; apply itself does not)."""
    ms = ClaudeModels(str(records.state_dir(root)), str(root), str(cb.bridge_state(cfg)), NAME,
                      publish or (lambda path, body: None), quoted, log, allowed=ALLOWED)
    ms.seen(CHOICES, str(ms.state().get("confirmed") or "") or current_model(root))
    if settle:
        settle_dead(root, ms)
    return ms


def apply_alive(root) -> bool:
    """Whether the detached apply recorded by `start_apply` is still running (same pid, same start)."""
    try:
        with open(records.state_dir(root) / model_switch.FILE, encoding="utf-8") as f:
            rec = json.load(f).get(APPLY_KEY) or {}
        pid = int(rec.get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    from ... import task_io
    try:
        return rec.get("start") is None or task_io.start_time(pid) == rec.get("start")
    except Exception:  # noqa: BLE001 - a start that cannot be read is taken as the same process
        return True


def settle_dead(root, ms) -> str:
    """A pending pick with no apply alive to finish it: settled as not switched, the owner told why."""
    state = ms.state()
    wanted = ms.pending(state)
    if not wanted or apply_alive(root):
        return ""
    before = str(state.get("current") or "")
    log("pending", wanted, "has no apply running: settled as not switched")
    return ms.tell(True, before if before != wanted else "", why=NOT_APPLIED)


def turn_model(root, cfg: dict) -> str:
    """The owner's model for a turn outside the session (the isolated turn): their last pick that took, ''
    for none (the login's own default)."""
    try:
        kept = str(switcher(root, cfg).state().get("confirmed") or "")
    except Exception:  # noqa: BLE001 - the isolated turn runs on the default rather than not at all
        return ""
    return kept if kept in ALLOWED and kept != "default" else ""


def card_matter(root, task_id: str, cfg: dict, popen=subprocess.Popen, environ=None) -> "tuple[str, str]":
    """(outcome, answer) when the owner's task is a card matter, answered here; ('', '') for any other task,
    which the session takes. A pick starts the live switch detached (`apply`)."""
    path = os.path.join(root, "tasks", task_id + ".txt")
    bridge = cb.bridge_state(cfg)
    try:
        connector_cards.absorb(bridge, root)      # the cards the bridge posted since, so their taps can act
    except Exception as e:  # noqa: BLE001 - a ledger that cannot take them leaves their taps refused, not a crash
        log("absorb failed:", "%s: %s" % (type(e).__name__, e))
    said: dict = {}
    outcome = desk(root, cfg, lambda result_file, body: said.update(body=body)).task(
        path, os.path.join(root, "results", task_id + ".txt"))
    if not outcome:
        return "", ""
    if outcome == "model-picked":
        start_apply(root, popen, environ)
    return outcome, said.get("body") or model_switch.ANSWERED


def desk(root, cfg: dict, publish) -> card_desk.Desk:
    """This agent's cards behind #6685's desk: the model card here, the account card (account_card). The desk
    routes a tap, a stale tap, the owner's /model or /account (/login), or the session's mark."""
    from . import account_card
    return card_desk.Desk(root, publish, quoted, [switcher(root, cfg, publish=publish)]
                          + account_card.cards(root, cfg, publish, log))


MARKED_DIR = "card-marks"
MARKED_KEEP_S = 24 * 3600.0     # a staging retry comes within minutes; a kept answer older than this is of no use


def marked_answer(root, task_id: str, body: bytes, cfg: dict) -> "bytes | None":
    """The answer to stage instead of `body` when the session answered its owner with a card's mark alone
    ("[model-card]", "[account-card]"): the card is shown in their DM and the answer is its no-send line (or
    where to ask, outside the DM). None for any other answer. The first answer is kept, so a staging retry
    stages the same bytes and never shows the card twice."""
    text = body.decode("utf-8", "replace").strip()
    if text not in (model_switch.MARK, "[account-card]"):
        return None
    kept = records.state_dir(root) / MARKED_DIR / (task_id + ".txt")
    if kept.is_file():
        return kept.read_bytes()
    path = os.path.join(root, "tasks", task_id + ".txt")
    from ... import task_io
    headers, _, _ = task_io.parse_task(path)
    said: dict = {}
    shown = desk(root, cfg, lambda result_file, out: said.update(body=out)).marked(
        headers.get("access_tier"), path, "completed", text, os.path.join(root, "results", task_id + ".txt"))
    if not shown:
        return None
    answer = (said.get("body") or model_switch.ANSWERED).encode("utf-8")
    kept.parent.mkdir(parents=True, exist_ok=True)
    prune_marks(kept.parent)
    kept.with_name(kept.name + ".tmp").write_bytes(answer)
    os.replace(kept.with_name(kept.name + ".tmp"), kept)
    return answer


def prune_marks(folder: Path, now=time.time) -> int:
    """Remove the kept card-mark answers older than MARKED_KEEP_S, so one small file per card ask does not
    pile up for the life of the work root; how many went. One that cannot be removed is left."""
    gone = 0
    try:
        names = list(folder.iterdir())
    except OSError:
        return 0
    for path in names:
        try:
            if now() - path.stat().st_mtime > MARKED_KEEP_S:
                path.unlink()
                gone += 1
        except OSError:
            continue
    return gone


def start_apply(root, popen=subprocess.Popen, environ=None) -> None:
    """`apply` in a session of its own: the claim that started it returns at once, and the switch waits for
    the session to take /model."""
    env = dict(os.environ if environ is None else environ)
    log_path = records.state_dir(root) / "model-apply.log"
    try:
        with open(log_path, "ab") as fh:
            proc = popen([sys.executable, "-m", __name__, "apply", "--root", str(root)], stdin=subprocess.DEVNULL,
                         stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except OSError as e:
        log("apply did not start:", "%s: %s" % (type(e).__name__, e))
        return
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int):
        from ... import task_io
        try:
            start = task_io.start_time(pid)
        except Exception:  # noqa: BLE001 - liveness then rests on the pid alone
            start = None
        try:
            model_switch.ModelSwitch(str(records.state_dir(root)), str(root), None, NAME, None, quoted, log).save(
                **{APPLY_KEY: {"pid": pid, "start": start}})
        except OSError as e:
            log("apply record not kept:", "%s: %s" % (type(e).__name__, e))


def task_io_consumer(root) -> dict:
    """The consumer generation now holding the root ({pid, start}, as a local claim binds it), pid None for none."""
    from ... import task_io
    consumer = task_io.holder_consumer(root) or {}
    return {"pid": consumer.get("pid"), "start": consumer.get("start")}


def busy(root) -> bool:
    """Whether the session is in a task's turn (an offer in flight in the Mod's record, claimed by the session
    now running); an unreadable record counts as busy, so nothing is typed into a turn this cannot see. An
    entry left by an earlier session (its claim bound to another consumer generation, or gone) is not a turn:
    a restart never clears the Mod's record, and such an entry would block every switch for good."""
    try:
        inflight = records.load_plugin(root).get("inflight") or {}
    except Exception:  # noqa: BLE001 - unknown is not idle
        return True
    if not inflight:
        return False
    from . import inbox       # the adapter's one reader of the claims
    now = task_io_consumer(root)
    for task_id in inflight:
        gen = inbox.claim_generation(root, task_id)
        if gen is not None and (not isinstance(gen, dict) or (now["pid"] and {"pid": gen.get("pid"), "start": gen.get("start")} == now)):
            return True
        log("inflight", task_id, "is from another session (generation %s, now %s): not busy" % (gen, now))
    return False


def wait_idle(root, deadline: float, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Poll until the session is out of a task's turn; False when `deadline` came first."""
    while busy(root):
        if clock() >= deadline:
            return False
        sleep(min(IDLE_POLL_S, max(0.0, deadline - clock())))
    return True


def apply(root, cfg: dict, config_dir, *, launcher_factory=None, sleep=time.sleep, clock=time.monotonic) -> str:
    """Switch the session to the owner's wanted model once it is idle, then tell them in the DM whether it
    took, and why not when it did not: the `ModelSwitch.tell` outcome, '' when nothing is wanted."""
    ms = switcher(root, cfg, settle=False)
    wanted = ms.pending(ms.state())
    if not wanted:
        return ""
    try:
        return _apply(root, cfg, config_dir, ms, wanted, launcher_factory, sleep, clock)
    except Exception as e:  # noqa: BLE001 - a pick never stays pending behind a crash: told as not switched
        log("apply failed:", "%s: %s" % (type(e).__name__, e))
        before = str(ms.state().get("current") or "")
        return ms.tell(True, before if before != wanted else "", why="the switch failed (%s)" % type(e).__name__)


def _apply(root, cfg, config_dir, ms, wanted, launcher_factory, sleep, clock) -> str:
    if launcher_factory is None:
        from .launcher import Launcher as launcher_factory
    lc = launcher_factory(cfg, Path(config_dir))
    sleep(APPLY_DELAY_S)          # the claim that started this returns first, and the Mod moves on
    deadline = clock() + IDLE_WAIT_S
    switched, why = False, ""
    for n in range(APPLY_TRIES):
        if not wait_idle(root, deadline, clock, sleep):
            why = BUSY
            log("switch to", wanted, "-> not sent:", BUSY)
            break
        switched, line = lc.switch_model(records.load_supervisor(root), wanted, confirm=True)
        log("switch to", wanted, "->", line)
        if switched:
            why = ""
            break
        retry = next((r for key, r in RETRYABLE.items() if key in line), "")
        why = retry or line.split(";")[0][:120]
        if not retry or n + 1 == APPLY_TRIES:
            break
        sleep(IDLE_POLL_S)
    before = str(ms.state().get("current") or "")
    ran = wanted if switched else (before if before != wanted else "")
    return ms.tell(True, ran, why=why)


def main(argv=None) -> int:
    from . import inbox
    p = argparse.ArgumentParser(prog="model_card", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--root", required=True)
    args = p.parse_args(argv)
    cfg, why = cm.load_config_at(LOCAL, inbox._installed_config())
    if why:
        log("the installed config is not usable:", why)
        return 1
    print(apply(args.root, cfg, inbox._config_dir()) or "nothing to switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
