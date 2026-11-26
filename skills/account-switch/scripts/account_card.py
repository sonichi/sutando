#!/usr/bin/env python3
"""A card in the owner's DM that says which account the core is signed in to and switches it.

    python3 account_card.py [--workspace WS] post [--room ROOM | --owner-dm] [--reply-to EVENT] [--print]
                                              [--signed-out] [--lead TEXT]
    python3 account_card.py [--workspace WS] reply --task-file PATH
    python3 account_card.py [--workspace WS] switch --room ROOM --key KEY --attempt ID
    python3 account_card.py [--workspace WS] status

`post` sends the card to the owner's checked DM and records it (src/issued_cards.py), so only the
owner's tap on it acts (agent_card.click_verdict); without `--room` it goes to that DM. `--signed-out`
(the Codex seat, when the core's login is refused even after a restart) leads with "Codex isn't signed
in anymore." instead of the account in use. It names the account in use: on a Codex core
(TUI or app-server seat) from src/codex_account.py, on a Claude core from `claude auth status --json`
under the core's config dir. Addresses are shown masked (first letter and domain). `--owner-dm`
names that same DM explicitly (the core's quota notice, src/core_quota_notice.py, uses it), and
`--lead` puts a line before the card's words, saying why it was sent unasked; with `--signed-out` it
replaces the "isn't signed in anymore" line.

Saved accounts are the desktop's store for the Sutando core on Codex
(`<app support>/accounts/codex/<key>.json`, {label, auth}, folder 0700, files 0600; desktop
ag2space-cinny-desktop#1264). The key is 16 hex of the SHA-256 of "<Codex account id>|<email>", so a
key on a button names no account. `post` first saves the signed-in account there, as the desktop does, so a
later sign-in never loses it. Each other saved account gets a "Use" button. A Claude core has no
saved accounts (its sign-in is in the Keychain), so its card offers only "Add another account".

"Add another account" never signs in from chat: it points the owner to Settings → Connections →
the Sutando core's row → "Switch account", where the sign-in terminal takes the code in the app's
own box. A sign-in code or token never goes in a message, a card or a log.

A "Use" tap answers "switching" at once and starts `switch` detached, because the core restarts.
It comes from any client the owner uses (the desktop app, the web, a phone): it carries only the
choice, and the saved sign-in it names never leaves this computer. A tap message acts once (a resent
one is answered, never switched again), and each tap's outcome is one line in the owner-action audit
(`state/recovery/audit.jsonl`, src/hitl/recovery.py): ids only, never an address.
`switch` refuses a saved account another Codex here holds a login of its own for (held_elsewhere: two
copies of one login end each other on refresh). Otherwise it keeps the current `auth.json` in memory and
saves it to the store, puts the saved one in
place (0600), and runs src/agent/graceful-restart.sh, which waits for the core to finish its turn.
A sign-in that cannot be read or saved to switch back to is never replaced. One attempt runs at a
time: admission and the switch each hold a lock, and only that attempt's record is cleared. After
the restart request it waits for a fresh heartbeat from the core session and asks Codex
(`account/read`) which account the home's saved sign-in is, then posts the outcome in the same DM:
the restart was requested, whether a fresh heartbeat came, that the saved sign-in is verified as X,
and that the running core's own account isn't confirmed; or why not. It never says the core
restarted: a heartbeat shows the session alive, not which process or account answers in it. When the restart definitely did not happen (graceful-restart exit 3/4/5), the
previous state, absence included, is put back; after any other failure the new sign-in stays.

Every command prints one JSON object. Exit 0 done, 1 a negative answer, 2 refused. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for _p in (str(_REPO / "src"), str(_REPO / "skills" / "worker-pool" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_card as ac  # noqa: E402
import issued_cards  # noqa: E402

CARD_ID, KIND, TARGET = "core-account-switch", "account", "core"
# The "switching" answer and the outcome have an id of their own: under the menu's id they would
# retire the menu (one live card per id and room) while its buttons are still on screen.
NOTE_ID = "core-account-switch-status"
HANDLER = "skills/account-switch/scripts/account_card.py reply"
ADD = "add"
RESTART = _REPO / "src" / "agent" / "graceful-restart.sh"
KEY_RE = re.compile(r"[0-9a-f]{16}")
STORE_SUB = ("accounts", "codex")
ISSUED = ("state", "account-card-keys.json")
ISSUED_KEPT = 20
# The owner's taps that started a switch: a tap delivered again (a client resending on reconnect)
# never starts a second one.
TAPS = ("state", "account-card-taps.json")
TAPS_KEPT = 50
RUNNING = ("state", "account-card-switch.json")
RUNNING_LOCK = ("state", "account-card-switch.lock")
EXEC_LOCK = ("state", "account-card-switch.exec.lock")
RESTART_TIMEOUT_S = 1500
READY_TIMEOUT_S, READY_POLL_S = 300, 5
RUNNING_STALE_S = RESTART_TIMEOUT_S + READY_TIMEOUT_S + 600
# graceful-restart.sh exits 3/4/5 before it stops the core; any other failure may have restarted it.
NOT_RESTARTED = (3, 4, 5)
SETTLE_TRIES, SETTLE_PAUSE_S = 4, 15
USE_BUTTONS = ac.LIMITS["buttons"] - 1          # one place is "Add another account"
SETTINGS = ("Settings → Connections → the Sutando (on this computer) row → \"Switch account\". "
            "A sign-in opens there; paste the code only into that box in the app, never in this chat.")
NO_CODE = "Never send me a sign-in code or token here."
SIGNED_OUT = "Codex isn't signed in anymore."
CONTROLS = "account_controls"  # the desktop draws the core's account controls in a card carrying it
RETRY_WORDS = "retry now"  # core_limit_hold's bare owner reply
GONE = "That account is no longer on my list. Ask me for the account card again."
# A button keyed by the workspace id alone: that file may since hold another person of the workspace.
OLD_CARD = "That card is from before an update, so nothing changed. Ask me for the account card again."
BUSY = "An account switch is already under way. I'll confirm here when it has finished."
ACTED = "I already acted on that tap. Ask me for the account card again to switch."
# admit()'s answer when the attempt on record had lost its worker: that record is replaced.
RECOVERED = "recovered"
STOPPED = "The previous switch stopped before it finished."
NOT_CODEX = "Saved accounts are kept only for a core on Codex. Add the account in Settings instead."
# graceful-restart.sh's exit codes, as one short reason the owner reads.
RESTART_REASONS = {3: "the core could not get ready to restart",
                   4: "the core was busy, so the restart was put off",
                   5: "the restart was only rehearsed"}


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return code


def masked(email: str) -> str:
    """An address as the owner reads it in their own DM: in full (one way to show an
    account everywhere: the full email, no plan); "" when it is not an address."""
    name, at, domain = (email or "").strip().partition("@")
    return f"{name}@{domain}" if name and at and domain and " " not in name + domain else ""


def masked_label(label: str) -> str:
    """A desktop store label ("me@example.com (plus)") as the owner reads it: the address alone, no plan."""
    label = (label or "").strip()
    return re.sub(r"\s*\([^()]*\)$", "", label) if "@" in label else label


# ── the core and its sign-in ──────────────────────────────────────────────────────────────────────

def runtime() -> str:
    """"codex" or "claude": the core's runtime as its launcher resolves it; "claude" when unreadable."""
    try:
        from sutando_config import resolve_core_runtime
        return "codex" if resolve_core_runtime() == "codex" else "claude"
    except Exception:  # noqa: BLE001 - an unreadable config is the default runtime
        return "claude"


def codex_home() -> Path:
    """The Codex core's home as its launcher resolves it; raises codex_account.NoHome when refused."""
    import codex_account
    return codex_account.home()


class Unreadable(Exception):
    """The live sign-in exists but cannot be read, so it must not be replaced."""


def store_dir() -> Optional[Path]:
    """The desktop's saved-account store for the core on Codex; None when no desktop hosts this engine."""
    app = os.environ.get("SUTANDO_APP_SUPPORT", "").strip()
    if not app:
        try:
            from sutando_config import desktop_runtime_selection_path
            path = desktop_runtime_selection_path()
        except Exception:  # noqa: BLE001 - no config: no desktop
            path = None
        app = str(path.parent) if path else ""
    return Path(app).joinpath(*STORE_SUB) if app else None


def _claims(jwt: str) -> dict:
    import codex_account
    return codex_account._claims(jwt)


def identity(auth_text: str) -> Optional[tuple]:
    """(store key, label) of a ChatGPT sign-in in a Codex `auth.json`, the desktop's rule: the id is
    "<workspace>|<person>", the workspace Codex's `tokens.account_id` (else the token's account id
    claim) and the person its email (else `sub`), either alone when the other is missing; the label
    is the email. None for an API key or anything unreadable. No token is returned."""
    try:
        auth = json.loads(auth_text)
    except (TypeError, ValueError):
        return None
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if not isinstance(tokens, dict):
        return None
    raw = tokens.get("id_token")
    raw = raw.get("raw_jwt") if isinstance(raw, dict) else raw
    claims = _claims(raw) if isinstance(raw, str) else {}
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    workspace = next((v for v in (tokens.get("account_id"), auth_claims.get("chatgpt_account_id"))
                      if isinstance(v, str) and v), "")
    person = next((v for v in (claims.get("email"), claims.get("sub")) if isinstance(v, str) and v), "")
    # One ChatGPT workspace id is shared by everyone in it, so the person is part of the key (desktop codex_identity).
    ident = f"{workspace}|{person}" if workspace and person else (workspace or person)
    if not ident:
        return None
    email = claims.get("email") if isinstance(claims.get("email"), str) else ""
    plan = auth_claims.get("chatgpt_plan_type") if isinstance(auth_claims.get("chatgpt_plan_type"), str) else ""
    label = email or "ChatGPT account"  # the email only: no plan name
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16], label


def _live_auth(home: Path) -> Optional[str]:
    try:
        return (home / "auth.json").read_text(encoding="utf-8")
    except OSError:
        return None


def _previous_auth(home: Path) -> Optional[str]:
    """The live `auth.json`, None when absent; raises Unreadable when it exists but cannot be read."""
    try:
        return (home / "auth.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise Unreadable(type(e).__name__) from None


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def save_current(store: Path, auth_text: Optional[str]) -> bool:
    """Save a ChatGPT sign-in in the store when its copy is missing or different; True when it wrote."""
    from local_record import write_text_whole
    ident = identity(auth_text) if auth_text else None
    if ident is None:
        return False
    key, label = ident
    path = store / f"{key}.json"
    body = json.dumps({"label": label, "auth": auth_text}, separators=(",", ":"))
    try:
        if path.read_text(encoding="utf-8") == body:
            return False
    except OSError:
        pass
    _private_dir(store)
    write_text_whole(path, body)
    return True


def _entries(store: Optional[Path]) -> list:
    """[(file key, auth text, label)] of every readable store file, by file name."""
    out = []
    try:
        files = sorted(store.iterdir()) if store else []
    except OSError:
        files = []
    for entry in files:
        key = entry.name[:-5] if entry.name.endswith(".json") else ""
        if not KEY_RE.fullmatch(key):
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        auth, label = (data.get("auth"), data.get("label")) if isinstance(data, dict) else (None, None)
        if isinstance(auth, str) and isinstance(label, str) and label:
            out.append((key, auth, label))
    return out


def _legacy_key(auth_text: str) -> Optional[str]:
    """The key an earlier engine filed a sign-in under: the workspace id alone (else `sub`)."""
    try:
        tokens = json.loads(auth_text).get("tokens") or {}
        raw = tokens.get("id_token")
        raw = raw.get("raw_jwt") if isinstance(raw, dict) else raw
        claims = _claims(raw) if isinstance(raw, str) else {}
    except (AttributeError, TypeError, ValueError):
        return None
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    ident = next((v for v in (tokens.get("account_id"), auth_claims.get("chatgpt_account_id"), claims.get("sub"))
                  if isinstance(v, str) and v), "")
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16] if ident else None


def _canonical(file_key: str, auth: str) -> Optional[str]:
    """The account's key now, for a file filed under it or under its earlier (workspace-only) key."""
    ident = identity(auth)
    if ident is None:
        return None
    return ident[0] if file_key in (ident[0], _legacy_key(auth)) else None


def saved(store: Optional[Path], current_key: str = "") -> list:
    """[{key, label, current}] of the store, one per account by its current key (a file an earlier
    engine filed under the workspace-only key included), by label; labels as kept (unmasked)."""
    seen = {}
    for file_key, auth, label in _entries(store):
        key = _canonical(file_key, auth)
        if key and (key not in seen or file_key == key):
            seen[key] = label
    return sorted(({"key": k, "label": v, "current": k == current_key} for k, v in seen.items()),
                  key=lambda s: s["label"])


def shown_names(entries: list) -> dict:
    """{key: masked label}; labels that mask alike get " #1", " #2"… by saved-key order, so each names one key."""
    groups = {}
    for s in sorted(entries, key=lambda s: s["key"]):
        groups.setdefault(masked_label(s["label"]), []).append(s["key"])
    return {k: name + (f" #{i}" if len(keys) > 1 else "")
            for name, keys in groups.items() for i, k in enumerate(keys, 1)}


def local_codex_homes(user_home: Optional[Path] = None) -> list:
    """CODEX_HOME of every local Codex agent ag2-sparrow set up for this user (each `codex-local.conf`),
    as the desktop reads them (sparrow_local::recorded_codex_homes)."""
    root = (user_home or Path.home()) / ".config" / "ag2-sparrow"
    try:
        own = sorted(d / "codex-local.conf" for d in (root / "agents").iterdir())
    except OSError:
        own = []
    homes = []
    for conf in [root / "codex-local.conf", *own]:
        try:
            lines = conf.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        raw = next((line[len("CODEX_HOME="):] for line in lines if line.startswith("CODEX_HOME=")), "").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            raw = raw[1:-1]
        if raw and os.path.isabs(raw):
            homes.append(Path(raw))
    return homes


def held_elsewhere(core_home: Path, key: str, homes=None) -> bool:
    """Whether another Codex home here keeps a login of its own for account `key`. A ChatGPT login's refresh
    token is replaced on each refresh, so two homes holding one login end each other; a home whose
    `auth.json` links to the core's own file shares it and is not another holder."""
    own = os.path.realpath(core_home / "auth.json")
    for home in (local_codex_homes() if homes is None else homes):
        path = Path(home) / "auth.json"
        if os.path.realpath(path) == own:
            continue
        ident = identity(_live_auth(Path(home)) or "")
        if ident and ident[0] == key:
            return True
    return False


def _saved_auth(store: Path, key: str) -> Optional[str]:
    """The saved sign-in of the account whose current key is `key`, from its own file or from one an
    earlier engine filed under the workspace id alone; the file under the current key is preferred.
    An earlier key is never resolved: that file was shared by everyone in the workspace."""
    for file_key, auth, _ in sorted(_entries(store), key=lambda e: e[0] != key):
        if _canonical(file_key, auth) == key:
            return auth
    return None


def _old_card_key(store: Path, key: str) -> bool:
    """Whether `key` is the workspace-only key of a file an earlier engine filed (a card made then)."""
    return any(file_key == key and _legacy_key(auth) == key and identity(auth) and identity(auth)[0] != key
               for file_key, auth, _ in _entries(store))


def claude_status(run=subprocess.run) -> dict:
    """{signed_in, email, plan} of the Claude core's config dir, by `claude auth status --json`;
    signed_in None when it could not be read."""
    env = dict(os.environ)
    try:
        from sutando_config import resolve_claude_sutando_config_dir
        env["CLAUDE_CONFIG_DIR"] = str(resolve_claude_sutando_config_dir())
    except Exception:  # noqa: BLE001 - the core's own environment names it then
        pass
    try:
        r = run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=30, env=env,
                stdin=subprocess.DEVNULL)
        data = json.loads(r.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"signed_in": None, "email": "", "plan": ""}
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return {"signed_in": None, "email": "", "plan": ""}
    return {"signed_in": data["loggedIn"], "email": str(data.get("email") or ""),
            "plan": str(data.get("subscriptionType") or "")}


def current(rt: str, *, run=subprocess.run, codex_read=None) -> dict:
    """{runtime, signed_in, shown, key}: the core's sign-in as the owner may read it (masked)."""
    if rt == "codex":
        import codex_account
        try:
            home = codex_home()
        except codex_account.NoHome:
            home = _seat_home()
            if home is None:
                return {"runtime": rt, "signed_in": None, "shown": "", "key": ""}
        ident = identity(_live_auth(home) or "")
        if codex_read is None:
            import codex_account
            codex_read = codex_account.read
        acct = codex_read(home) or {}
        # A sign-in counts only once Codex itself confirms it (`verified`); auth.json alone may hold a dead one.
        signed = True if acct.get("verified") is True else (None if acct.get("kind") else False)
        if acct.get("kind") == "apiKey":
            return {"runtime": rt, "signed_in": signed, "shown": "an OpenAI API key", "key": ""}
        if not acct.get("kind") and ident is None:
            return {"runtime": rt, "signed_in": False, "shown": "", "key": ""}
        email = masked(str(acct.get("email") or "")) or (masked_label(ident[1]) if ident else "")
        return {"runtime": rt, "signed_in": signed, "shown": email or "a ChatGPT account",
                "key": ident[0] if ident else ""}
    st = claude_status(run)
    shown = masked(st["email"]) or "an Anthropic account"  # the email only: the owner reads no plan name
    return {"runtime": rt, "signed_in": st["signed_in"], "shown": shown if st["signed_in"] else "", "key": ""}


def _seat_home() -> Optional[Path]:
    """The Codex home the running seat was started with (its own CODEX_HOME), when the launcher's
    resolution is refused from here: an absolute directory holding a sign-in file, else None. Read only."""
    raw = os.environ.get("CODEX_HOME", "").strip()
    if not raw or not os.path.isabs(raw):
        return None
    path = Path(raw)
    return path if (path / "auth.json").is_file() else None


def in_use_words(cur: dict) -> str:
    """The sentence naming the core's account; empty when it cannot be read, so the card's own
    account controls (which the desktop fills) are never contradicted."""
    cli = "Codex" if cur["runtime"] == "codex" else "Claude Code"
    if cur["signed_in"] is None:
        return ""
    if not cur["signed_in"]:
        return f"The core ({cli}) is not signed in."
    return f"I'm using {ac.md_escape(cur['shown'])}."


# ── the card ──────────────────────────────────────────────────────────────────────────────────────

def menu_card(cur: dict, others: list, signed_out: bool = False, lead: str = "", retry: bool = False,
              names: Optional[dict] = None) -> tuple:
    """(body, card): a "Use" button per other saved account (up to five), then "Add another account".
    `lead`, when given, opens the card's words and its fallback line. `signed_out`: the core's login was
    refused, so the card drops the account in use and, with no `lead`, opens with SIGNED_OUT. `retry`
    (the quota card) adds a Retry button whose words are the owner's own "retry now", which the Claude
    core's limit hold acts on; a Codex core has no such hold, so its card carries none."""
    buttons = []
    names = names if names is not None else shown_names(others)
    for s in others[:USE_BUTTONS]:
        name = names.get(s["key"]) or masked_label(s["label"])
        tag = name[len(masked_label(s["label"])):]   # the " #N" kept whole when the label is cut
        label = ("Use " + name[: len(name) - len(tag)])[: ac.LIMITS["label"] - len(tag)] + tag
        buttons.append(ac.button(label, "reply", style="secondary", reply=label,
                                 command=ac.reply(KIND, TARGET, s["key"])))
    buttons.append(ac.button("Add another account", "reply", style="primary", reply="Add another account",
                             command=ac.reply(KIND, TARGET, ADD)))
    if retry and cur["runtime"] != "codex":
        buttons.append(ac.button("Retry", "reply", style="secondary", reply=RETRY_WORDS))
    if signed_out:
        lead = lead or SIGNED_OUT
    if cur["runtime"] == "codex":
        more = (" Pick an account to switch to; the core restarts on it." if others else
                " No other account is saved on this computer yet.")
    else:
        more = ""
    note = NO_CODE
    lead = ac.md_escape(lead)[:1000]
    # The quota card says the limit and its reset alone: a probe at the limit may read the account as signed out.
    using = "" if signed_out or retry else in_use_words(cur)
    words = " ".join(w for w in (lead, using, more.strip()) if w)
    words = words or "Pick an account below."
    # Out of usage or signed out, the core waits on the owner: `pending`, as the app's needs-you bar lists it.
    stuck = signed_out or retry
    body, card = ac.build(CARD_ID, "pending" if stuck else "info", title="Account", avatar_=ac.avatar(initial="A"),
                          text=words, buttons=buttons, note=note, note_tone="dim",
                          body=" ".join(w for w in (lead, using, 'Reply "switch account" for options.') if w))
    # The marker by which the desktop app on the core's computer draws the core's own account controls;
    # `signed_out` makes it lead with "Sign in again" for this account, above the account dropdown.
    marker = {"runtime": cur["runtime"], "core": True, **({"signed_out": True} if signed_out else {}),
              **({"limit": True} if retry and not signed_out else {})}
    return body, dict(card, **{CONTROLS: marker})


def notice_card(state: str, status: str, words: str) -> tuple:
    return ac.build(NOTE_ID, state, title="Account", avatar_=ac.avatar(initial="A"), status=status,
                    text=words, body=words)


def _core(ws: Path) -> dict:
    import pool_provision
    return pool_provision.core_lane(ws, None)


def _execute(ws: Path, lane: Optional[str]):
    import pool_provision
    return pool_provision.core_actions(ws, lane)[0].execute


def _owner_dm(ws: Path, core: dict) -> str:
    import connections
    return connections.owner_dm(core, ws)


def _post(ws: Path, room: str, body: str, card: dict, operation_id: str, *, reply_to: str = "", core=None,
          execute=None) -> dict:
    """Post `card` in `room`, which must be the owner's checked DM, and record it for its taps."""
    try:
        core = core if core is not None else _core(ws)
        lane = str(core.get("instance") or "")
        dm = _owner_dm(ws, core)
        execute = execute if execute is not None else _execute(ws, lane or None)
        issued = issued_cards.intend(ws, operation_id)  # unwritable ledger: no card, its buttons could not act
        eid = ac.send_card(execute, room, body, card, operation_id=operation_id, reply_to=reply_to, owner_dm=dm)
    except ac.CardError as e:
        return {"status": "failed", "why": str(e)}
    except Exception as e:  # noqa: BLE001 - no core lane or no credential: named, nothing posted
        return {"status": "failed", "why": f"{type(e).__name__}: {e}"}
    try:
        kept = issued_cards.record(ws, eid, card, room=dm, lane=lane, handler=HANDLER, issued_at=issued)
    except OSError as e:
        return {"status": "posted", "event_id": eid, "why": f"its buttons will not act ({type(e).__name__})"}
    if not kept and (issued_cards.offered(card) or issued_cards.has_picker(card)):
        return {"status": "posted", "event_id": eid, "why": "a newer card is the one that acts"}
    return {"status": "posted", "event_id": eid}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_keys(ws: Path, event_id: str, keys: list, now: float) -> None:
    """Keep which saved keys the card posted as `event_id` offered; fixed once kept."""
    from file_lock import locked_file
    from local_record import write_whole
    path = ws.joinpath(*ISSUED)
    with locked_file(path.with_name(path.name + ".lock")):
        cards = _read_json(path).get("cards")
        cards = cards if isinstance(cards, dict) else {}
        if event_id not in cards:
            cards[event_id] = {"keys": list(keys), "ts": now}
        newest = sorted(cards.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0))[-ISSUED_KEPT:]
        write_whole(path, {"cards": dict(newest)})


def offered_keys(ws: Path, event_id: str) -> list:
    entry = (_read_json(ws.joinpath(*ISSUED)).get("cards") or {}).get(event_id)
    keys = entry.get("keys") if isinstance(entry, dict) else None
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def cmd_post(ws: Path, args, *, core=None, execute=None, now=time.time, run=subprocess.run, codex_read=None) -> int:
    rt = runtime()
    cur = current(rt, run=run, codex_read=codex_read)
    store = store_dir() if rt == "codex" else None
    if store is not None:
        import codex_account
        try:
            save_current(store, _live_auth(codex_home()))
        except (OSError, codex_account.NoHome):
            pass  # the card still names the account; only the copy for switching back is missing
    every = saved(store, cur["key"])
    others = [s for s in every if not s["current"]]
    try:
        body, card = menu_card(cur, others, signed_out=args.signed_out, lead=args.lead or "",
                               retry=args.retry, names=shown_names(every))
    except ac.CardError as e:
        return _emit({"status": "failed", "why": str(e)}, 1)
    if args.print:
        return _emit({"body": body, "card": card, "handler": HANDLER})
    room = args.room
    if not room:
        try:
            core = core if core is not None else _core(ws)
            room = _owner_dm(ws, core)
        except Exception as e:  # noqa: BLE001 - no core lane: named, nothing posted
            return _emit({"status": "failed", "why": f"{type(e).__name__}: {e}"}, 1)
        if not room:
            return _emit({"status": "failed", "why": "the owner's DM is not known"}, 1)
    shown = [s["key"] for s in others[:USE_BUTTONS]]
    tag = hashlib.sha256(",".join(shown).encode()).hexdigest()[:12]
    kind = "signed-out" if args.signed_out else "menu"
    reply_to = args.reply_to or ""
    out = _post(ws, room, body, card, f"account-card:{kind}:{ac.send_key(room, reply_to, now)}:{tag}",
                reply_to=reply_to, core=core, execute=execute)
    if out.get("event_id"):
        try:
            remember_keys(ws, out["event_id"], shown, now())
        except OSError as e:
            out["why"] = f"its buttons will not act ({type(e).__name__})"
    return _emit(out, 0 if out["status"] == "posted" else 1)


# ── a tap ─────────────────────────────────────────────────────────────────────────────────────────

def take_tap(ws: Path, tap: str, now: float) -> bool:
    """Record `tap` (the tap message's id) as acted on; False when it already was. Check and write are
    one transaction under the ledger's lock."""
    from file_lock import locked_file
    from local_record import write_whole
    path = ws.joinpath(*TAPS)
    with locked_file(path.with_name(path.name + ".lock"), create_mode=0o600):
        taps = _read_json(path).get("taps")
        taps = taps if isinstance(taps, dict) else {}
        if tap in taps:
            return False
        taps[tap] = now
        newest = sorted(taps.items(), key=lambda kv: float(kv[1] or 0))[-TAPS_KEPT:]
        write_whole(path, {"taps": dict(newest)})
    return True


def release_tap(ws: Path, tap: str) -> None:
    """Forget `tap` after a start that changed nothing, so the same tap delivered again may act."""
    from file_lock import locked_file
    from local_record import write_whole
    path = ws.joinpath(*TAPS)
    try:
        with locked_file(path.with_name(path.name + ".lock"), create_mode=0o600):
            taps = _read_json(path).get("taps")
            if isinstance(taps, dict) and taps.pop(tap, None) is not None:
                write_whole(path, {"taps": taps})
    except OSError:
        pass


def audit(ws: Path, headers: dict, key: str, outcome: str, now: float) -> None:
    """One line in the owner-action audit for a "Use" tap: ids and the outcome, never an address or
    message text. A line that cannot be written stops nothing (the cards are the record)."""
    try:
        from hitl import recovery
    except ImportError:
        return
    recovery.audit(ws, {"at": int(now), "card": CARD_ID, "event": str(headers.get("reply_to_event") or ""),
                        "tap": str(headers.get("source_message_id") or ""), "action_id": f"switch_account:{key}",
                        "outcome": outcome, "sender": str(headers.get("user_id") or ""),
                        "room": str(headers.get("channel_id") or "")})


def running(ws: Path, now: float) -> dict:
    rec = _read_json(ws.joinpath(*RUNNING))
    return rec if rec.get("attempt") and now - float(rec.get("ts") or 0) < RUNNING_STALE_S else {}


def executing(ws: Path) -> bool:
    """True while a `switch` holds the execution lock."""
    from file_lock import try_lock_fd, unlock_fd
    path = ws.joinpath(*EXEC_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if try_lock_fd(fd):
            unlock_fd(fd)
            return False
        return True
    finally:
        os.close(fd)


def admit(ws: Path, rec: dict, now: float) -> bool:
    """Record `rec` as the one attempt under way; False when another is running, RECOVERED when the one
    on record had a dead worker. Check and write are one transaction under the record's lock."""
    from file_lock import locked_file
    from local_record import write_whole
    import switch_worker
    with locked_file(ws.joinpath(*RUNNING_LOCK), create_mode=0o600):
        prev, busy = running(ws, now), executing(ws)
        recovered = bool(prev) and not busy and switch_worker.dead(ws.joinpath(*RUNNING), prev["attempt"])
        if (prev or busy) and not recovered:
            return False
        write_whole(ws.joinpath(*RUNNING), rec)
    return RECOVERED if recovered else True


def is_recorded(ws: Path, rec: dict) -> bool:
    from file_lock import locked_file
    with locked_file(ws.joinpath(*RUNNING_LOCK), create_mode=0o600):
        return _read_json(ws.joinpath(*RUNNING)) == rec


def settle(ws: Path, rec: dict) -> None:
    """Clear the record only while it is still exactly this attempt's."""
    from file_lock import locked_file
    with locked_file(ws.joinpath(*RUNNING_LOCK), create_mode=0o600):
        if _read_json(ws.joinpath(*RUNNING)) == rec:
            ws.joinpath(*RUNNING).unlink(missing_ok=True)


def start_switch(ws: Path, room: str, key: str, attempt: str, popen=subprocess.Popen) -> int:
    """Start `switch` in a session of its own, so it outlives the core's restart; its pid."""
    log = ws / "logs" / "account-card.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = ["nohup", sys.executable, str(Path(__file__).resolve()), "--workspace", str(ws), "switch",
            "--room", room, "--key", key, "--attempt", attempt]
    with open(log, "ab") as fh:
        fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} account switch ({attempt})\n".encode())
        fh.flush()
        proc = popen(argv, cwd=str(_REPO), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                     start_new_session=True)
    import switch_worker
    switch_worker.note(ws.joinpath(*RUNNING), attempt, int(proc.pid))
    return int(proc.pid)


def cmd_reply(ws: Path, args, *, core=None, execute=None, popen=subprocess.Popen, now=time.time,
              run=subprocess.run, codex_read=None) -> int:
    try:
        headers, said = ac.reply_words(Path(args.task_file).read_text(encoding="utf-8"))
    except OSError as e:
        return _emit({"status": "refused", "why": f"the task file is unreadable ({type(e).__name__})"}, 2)
    try:
        core = core if core is not None else _core(ws)
    except Exception:  # noqa: BLE001 - a card records its own lane; only one without it needs the core's owner
        core = {}
    verdict = ac.click_verdict(headers, said, ws, ac.lane_owner(ws, str(core.get("owner_mxid") or "")))
    other = ac.click_answer(verdict, said, (KIND,))
    if other is not None:
        return _emit(other, 1)
    verb, room = verdict.command.verb, str(headers.get("channel_id") or "")
    event = str(headers.get("reply_to_event") or "")
    if verdict.command.target != TARGET:
        return _emit({"status": "refused", "say": GONE}, 2)
    if verb == ADD:
        words = (f"Adding an account needs the computer that runs the core. There, open {SETTINGS} "
                 "Then ask me for the account card again.")
        body, card = notice_card("info", "In Settings", words)
        op = f"account-card:add:{ac.send_key(room, str(headers.get('source_message_id') or ''), now)}"
        posted = _post(ws, room, body, card, op, reply_to=event, core=core or None,
                       execute=execute)
        return _emit({"status": "say", "say": words, **({"event_id": posted["event_id"]} if posted.get("event_id") else {})})
    t = now()
    key = verb if KEY_RE.fullmatch(verb) else "-"
    if runtime() != "codex":
        audit(ws, headers, key, "refused:not_codex", t)
        return _emit({"status": "refused", "say": NOT_CODEX}, 2)
    store = store_dir()
    if not KEY_RE.fullmatch(verb) or verb not in offered_keys(ws, event) or store is None:
        audit(ws, headers, key, "refused:gone", t)
        return _emit({"status": "refused", "say": GONE}, 2)
    if _saved_auth(store, verb) is None:
        audit(ws, headers, key, "refused:old_card" if _old_card_key(store, verb) else "refused:gone", t)
        return _emit({"status": "refused", "say": OLD_CARD if _old_card_key(store, verb) else GONE}, 2)
    cur = current("codex", run=run, codex_read=codex_read)
    label = shown_names(saved(store)).get(verb) or "that account"
    if cur["key"] == verb:
        audit(ws, headers, key, "already_using", t)
        return _emit({"status": "say", "say": f"Already using {label} — nothing to change."})
    tap = str(headers.get("source_message_id") or "")
    attempt = f"{int(t)}-{secrets.token_hex(4)}"
    rec = {"attempt": attempt, "key": verb, "room": room, "ts": t}
    try:
        if tap and not take_tap(ws, tap, t):
            audit(ws, headers, key, "refused:tap_acted_on", t)
            return _emit({"status": "say", "say": ACTED})
        admitted = admit(ws, rec, t)
        if not admitted:
            audit(ws, headers, key, "busy", t)
            return _emit({"status": "say", "say": BUSY})
    except OSError as e:
        release_tap(ws, tap)
        audit(ws, headers, key, "failed", t)
        return _emit({"status": "failed", "say": "I couldn't start the switch. Nothing changed.",
                      "why": type(e).__name__}, 1)
    say = (f"{STOPPED} {in_use_words(cur)} " if admitted == RECOVERED else "") + (
        f"Switching the core to {label}. It restarts once this turn has finished; I'll confirm here when it is back.")
    body, card = notice_card("pending", "Switching", say)
    posted = _post(ws, room, body, card, f"account-card:{attempt}:switching", reply_to=event, core=core or None,
                   execute=execute)
    try:
        pid = start_switch(ws, room, verb, attempt, popen)
    except OSError as e:
        settle(ws, rec)
        release_tap(ws, tap)
        audit(ws, headers, key, "failed", t)
        return _emit({"status": "failed", "say": "I couldn't start the switch. Nothing changed.",
                      "why": f"{type(e).__name__}: {e}"}, 1)
    audit(ws, headers, key, "started", t)
    out = {"status": "started", "attempt": attempt, "pid": pid, "say": say}
    out.update({"event_id": posted["event_id"]} if posted.get("event_id") else {"card_why": posted.get("why", "")})
    return _emit(out)


# ── the detached switch ───────────────────────────────────────────────────────────────────────────

def restart_core(run=subprocess.run) -> int:
    """The graceful restart's exit code; it waits for the core's turn to end. Launched outside the
    core's own session (this process has a session of its own), which the in-session guard allows."""
    env = dict(os.environ, SUTANDO_ALLOW_INSESSION_RESTART="1")
    try:
        return run(["bash", str(RESTART)], cwd=str(_REPO), env=env, stdin=subprocess.DEVNULL,
                   timeout=RESTART_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def _alive(ws: Path) -> Optional[Path]:
    try:
        from util_paths import _host_label
        return ws / "state" / "cores" / f"{_host_label()}.alive"
    except Exception:  # noqa: BLE001 - no host label: no heartbeat to read
        return None


def fresh_beat(ws: Path, since: float) -> bool:
    """Whether the core session's heartbeat is newer than `since`: its `last_beat_at`, with the session
    and socket the core is launched with. It shows the core session is alive, not which process or
    account answers in it."""
    alive = _alive(ws)
    try:
        rec = json.loads(alive.read_text(encoding="utf-8")) if alive else None
        from core_heartbeat import _socket_path, core_session
        intended = (core_session(), _socket_path())
    except Exception:  # noqa: BLE001 - unreadable: no fresh beat seen
        return False
    if not isinstance(rec, dict) or (rec.get("session"), rec.get("socket")) != intended:
        return False
    beat = rec.get("last_beat_at")
    return isinstance(beat, (int, float)) and not isinstance(beat, bool) and beat > since


def core_ready(ws: Path, since: float, *, sleep=time.sleep, clock=time.time) -> bool:
    """True once `fresh_beat` holds, within READY_TIMEOUT_S."""
    deadline = clock() + READY_TIMEOUT_S
    while True:
        if fresh_beat(ws, since):
            return True
        if clock() >= deadline:
            return False
        sleep(READY_POLL_S)


def live_account(home: Path) -> dict:
    """Codex's own answer for the sign-in saved in the core's home (never the file alone). It runs a
    separate app-server, so it verifies the saved sign-in, not the identity the running core uses."""
    import codex_account
    return codex_account.from_app_server(home) or {}


def _email(auth_text: str) -> str:
    try:
        tokens = json.loads(auth_text).get("tokens") or {}
        raw = tokens.get("id_token")
        raw = raw.get("raw_jwt") if isinstance(raw, dict) else raw
        return str(_claims(raw).get("email") or "") if isinstance(raw, str) else ""
    except (AttributeError, TypeError, ValueError):
        return ""


def _restore(home: Path, before: Optional[str]) -> None:
    from local_record import write_text_whole
    if before is None:
        (home / "auth.json").unlink(missing_ok=True)
    else:
        write_text_whole(home / "auth.json", before)


def do_switch(ws: Path, key: str, *, restart=restart_core, ready=core_ready, live=live_account,
              clock=time.time, held=held_elsewhere) -> tuple:
    """(state, words). The previous `auth.json` is kept in memory and saved in the store first; a
    sign-in that cannot be read or saved is never replaced. When the restart definitely did not
    happen, the previous state (absence included) is put back."""
    import codex_account
    from local_record import write_text_whole
    store = store_dir()
    try:
        home = codex_home()
    except codex_account.NoHome as e:
        return "error", f"Couldn't switch: the core's Codex home is not usable ({e}). Nothing changed."
    want = _saved_auth(store, key) if store else None
    if want is None:
        return "error", "Couldn't switch: that account is no longer saved here. Nothing changed."
    label = masked_label(identity(want)[1])
    if held(home, key):
        return "error", (f"Couldn't switch to {label}: another Codex on this computer is signed in with that "
                         "account, and two copies of one sign-in end each other when either refreshes. Sign in to "
                         "it here with \"Add another account\" instead. Nothing changed.")
    try:
        before = _previous_auth(home)
    except Unreadable as e:
        return "error", (f"Couldn't switch to {label}: the core's current sign-in could not be read ({e}), "
                         "so I left it in place. Nothing changed.")
    if before is not None and identity(before) is None:
        return "error", (f"Couldn't switch to {label}: the core's current sign-in is not one I can save to "
                         "switch back to (an API key or an unreadable file), so I left it in place. Nothing changed.")
    was = masked_label(identity(before)[1]) if before else "no account"
    try:
        save_current(store, before)
        if before is not None and _saved_auth(store, identity(before)[0]) != before:
            raise OSError("the current sign-in was not saved")
        write_text_whole(home / "auth.json", want)
    except OSError as e:
        try:
            _restore(home, before)
        except OSError:
            pass
        return "error", f"Couldn't switch to {label}: the sign-in could not be written ({type(e).__name__}). I'm still signed in as {was}."
    started = clock()
    rc = restart()
    if rc in NOT_RESTARTED:
        try:
            _restore(home, before)
        except OSError:
            return "error", (f"The core did not restart ({RESTART_REASONS.get(rc, 'the restart failed')}), and the "
                             f"previous sign-in could not be put back: the core uses {label} from its next start.")
        reason = RESTART_REASONS.get(rc, "the restart failed")
        return "error", f"Couldn't switch to {label}: {reason}. I'm still signed in as {was}."
    if rc != 0:
        return "error", (f"The restart did not finish cleanly (exit {rc}), so I can't tell which account the core "
                         f"is on. Its saved sign-in is now {label}. Ask me for the account card to check.")
    beat = ready(ws, started)
    seen = ("its heartbeat came back after the request" if beat else
            "I haven't seen a heartbeat from the core session since")
    asked = f"I asked the core to restart with {label}'s sign-in, and {seen}."
    unconfirmed = "The running core's own account isn't confirmed."
    now_ident = identity(_live_auth(home) or "")
    acct = live(home)
    want_email = _email(want).lower()
    if now_ident and now_ident[0] == key and acct.get("kind") == "chatgpt" and \
            (not want_email or str(acct.get("email") or "").lower() == want_email):
        # Not "Switched … ✓": only the saved sign-in is verified, never the running core's own account.
        return ("done" if beat else "info"), f"{asked} Saved sign-in verified: Codex confirms it is {label}. {unconfirmed}"
    if not acct:
        return "error", f"{asked} Codex did not confirm the saved sign-in. Ask me for the account card to check."
    shown = masked(str(acct.get("email") or ""))
    shown = shown or ("no ChatGPT account" if not acct.get("kind") else "another account")
    return "error", f"{asked} But its saved sign-in is {shown}, not {label}. Ask me for the account card to try again."


def cmd_switch(ws: Path, args, *, core=None, execute=None, restart=restart_core, sleep=time.sleep,
               ready=core_ready, live=live_account) -> int:
    from file_lock import try_lock_fd, unlock_fd
    rec = _read_json(ws.joinpath(*RUNNING))
    if rec.get("attempt") != args.attempt or rec.get("key") != args.key or rec.get("room") != args.room \
            or not is_recorded(ws, rec):
        return _emit({"status": "refused", "why": "that attempt is not the one under way"}, 2)
    lock = ws.joinpath(*EXEC_LOCK)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not try_lock_fd(fd):
            return _emit({"status": "refused", "why": "another switch is running"}, 2)
        try:
            if not is_recorded(ws, rec):
                return _emit({"status": "refused", "why": "that attempt is not the one under way"}, 2)
            asked = []

            def request_restart():
                asked.append(True)
                return restart()
            state, words = do_switch(ws, args.key, restart=request_restart, ready=ready, live=live)
        finally:
            settle(ws, rec)
            unlock_fd(fd)
    finally:
        os.close(fd)
    status = {"done": "Sign-in switched", "info": "Sign-in switched"}.get(state, "Not switched")
    body, card = notice_card(state, status, words)
    out = {"status": "failed"}
    for i in range(SETTLE_TRIES):
        out = _post(ws, args.room, body, card, f"account-card:{args.attempt}:outcome", core=core, execute=execute)
        if out["status"] == "posted":
            break
        if i + 1 < SETTLE_TRIES:
            sleep(SETTLE_PAUSE_S)
    return _emit({**out, "outcome": state, "say": words,
                  "verified": "saved-sign-in" if state in ("done", "info") else None,
                  "restart": "requested" if asked else "not-requested", "core_identity": "unconfirmed"},
                 0 if state == "done" else 1)


def main(argv=None, *, core=None, execute=None, popen=subprocess.Popen, run=subprocess.run, codex_read=None,
         restart=restart_core, sleep=time.sleep, ready=core_ready, live=live_account) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("post")
    to = s.add_mutually_exclusive_group()
    to.add_argument("--room", default="", help="the owner's DM (default: the core's checked owner DM)")
    to.add_argument("--owner-dm", action="store_true",
                    help="the core lane's checked owner DM (the default when --room is not given)")
    s.add_argument("--reply-to", default="")
    s.add_argument("--lead", default="", help="a line that opens the card's words")
    s.add_argument("--retry", action="store_true", help="add a Retry button (the quota card)")
    s.add_argument("--print", action="store_true")
    s.add_argument("--signed-out", action="store_true", help="the core's login was refused: say so first")
    r = sub.add_parser("reply")
    r.add_argument("--task-file", required=True)
    w = sub.add_parser("switch")
    w.add_argument("--room", required=True)
    w.add_argument("--key", required=True)
    w.add_argument("--attempt", required=True)
    sub.add_parser("status")
    args = p.parse_args(argv)
    if args.workspace:
        ws = Path(args.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    if args.cmd == "post":
        return cmd_post(ws, args, core=core, execute=execute, run=run, codex_read=codex_read)
    if args.cmd == "reply":
        return cmd_reply(ws, args, core=core, execute=execute, popen=popen, run=run, codex_read=codex_read)
    if args.cmd == "switch":
        if not KEY_RE.fullmatch(args.key):
            return _emit({"status": "refused", "why": "that is not a saved account's key"}, 2)
        return cmd_switch(ws, args, core=core, execute=execute, restart=restart, sleep=sleep, ready=ready, live=live)
    rt = runtime()
    cur = current(rt, run=run, codex_read=codex_read)
    store = store_dir() if rt == "codex" else None
    return _emit({**cur, "words": in_use_words(cur),
                  "saved": [{"key": s["key"], "label": masked_label(s["label"]), "current": s["current"]}
                            for s in saved(store, cur["key"])]})


if __name__ == "__main__":
    sys.exit(main())
