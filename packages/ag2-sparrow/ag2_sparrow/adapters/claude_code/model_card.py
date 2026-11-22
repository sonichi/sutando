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
import os
import subprocess
import sys
import time
from pathlib import Path

from ... import connect_bridge as cb, connect_common as cm, connector_cards, model_switch
from ... import claude_local_records as records
from ...acp_host import quoted
from .shape import LOCAL

NAME = "Claude Code"
# The core's own menu (skills/model-switch MODEL_SWITCH_CHOICES), in its order: one card for every Claude.
CHOICES = (("default", "Default"), ("opus", "Opus"), ("sonnet[1m]", "Sonnet (1M context)"),
           ("haiku", "Haiku"), ("fable[1m]", "Fable (1M context)"))
ALLOWED = frozenset(mid for mid, _ in CHOICES)
APPLY_DELAY_S, APPLY_TRIES, APPLY_RETRY_S = 2.0, 3, 5.0
# The live switch's answers worth another try: the box held a draft, or the switch was not seen in time.
RETRYABLE = ("unsent text", "unconfirmed", "does not show its whole input box")


def log(*parts):
    print("model-card:", *parts, file=sys.stderr)


def current_model(root) -> str:
    """The model the session is on as far as this agent knows: the last live switch's, else the default."""
    from .launcher import switched_model
    return switched_model(root) or "default"


def switcher(root, cfg: dict, publish=None) -> model_switch.ModelSwitch:
    """The card's state and rules for this agent, its offer set to Claude's menu and its model in use."""
    ms = model_switch.ModelSwitch(str(records.state_dir(root)), str(root), str(cb.bridge_state(cfg)), NAME,
                                  publish or (lambda path, body: None), quoted, log, allowed=ALLOWED)
    ms.seen(CHOICES, str(ms.state().get("confirmed") or "") or current_model(root))
    return ms


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
    ms = switcher(root, cfg, publish=lambda result_file, body: said.update(body=body))
    outcome = ms.card_task(path, os.path.join(root, "results", task_id + ".txt"))
    if not outcome:
        return "", ""
    if outcome == "model-picked":
        start_apply(root, popen, environ)
    return outcome, said.get("body") or model_switch.ANSWERED


def start_apply(root, popen=subprocess.Popen, environ=None) -> None:
    """`apply` in a session of its own: the claim that started it returns at once, and the switch waits for
    the session to take /model."""
    env = dict(os.environ if environ is None else environ)
    log_path = records.state_dir(root) / "model-apply.log"
    try:
        with open(log_path, "ab") as fh:
            popen([sys.executable, "-m", __name__, "apply", "--root", str(root)], stdin=subprocess.DEVNULL,
                  stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except OSError as e:
        log("apply did not start:", "%s: %s" % (type(e).__name__, e))


def apply(root, cfg: dict, config_dir, *, launcher_factory=None, sleep=time.sleep) -> str:
    """Switch the session to the owner's wanted model, then tell them in the DM whether it took: the
    `ModelSwitch.tell` outcome, '' when nothing is wanted."""
    ms = switcher(root, cfg)
    wanted = ms.pending(ms.state())
    if not wanted:
        return ""
    if launcher_factory is None:
        from .launcher import Launcher as launcher_factory
    lc = launcher_factory(cfg, Path(config_dir))
    sleep(APPLY_DELAY_S)          # the claim that started this returns first, and the Mod moves on
    switched, line = False, ""
    for n in range(APPLY_TRIES):
        switched, line = lc.switch_model(records.load_supervisor(root), wanted, confirm=True)
        log("switch to", wanted, "->", line)
        if switched or not any(r in line for r in RETRYABLE) or n + 1 == APPLY_TRIES:
            break
        sleep(APPLY_RETRY_S)
    before = str(ms.state().get("current") or "")
    ran = wanted if switched else (before if before != wanted else "")
    return ms.tell(True, ran)


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
