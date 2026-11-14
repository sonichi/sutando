#!/usr/bin/env python3
"""Set Twilio up from the chat: list numbers, buy one, point its webhook here.

Usage:
    python3 twilio-setup.py status                       # account, numbers, webhook drift
    python3 twilio-setup.py numbers [--country US] [--area 415] [--limit 10]
    python3 twilio-setup.py buy +14155551234 [--base https://x.ngrok-free.app]
    python3 twilio-setup.py set-webhook [BASE] [--number +14155551234]

Why (user feedback): after signing up for Twilio the person was sent to the
Twilio console to buy a number and paste a webhook URL by hand. Everything
after sign-up + card is an API call, so the agent does it: the account SID and
auth token are the only manual inputs (vault them: `vault set
TWILIO_ACCOUNT_SID …`, `vault set TWILIO_AUTH_TOKEN …`, or put them in
<repo>/.env).

Credentials resolve process env -> <repo>/.env -> vault through the same
resolver the channel bridges use (src/channel_token.py), so a vaulted token
works here without copying it anywhere.

The webhook base is, in order: the explicit argument; what the running server
bound (GET localhost:<PHONE_PORT|3100>/health -> webhookUrl — the tunnel Twilio
must post to, whatever started it); TWILIO_WEBHOOK_URL (an operator-set fixed
external URL such as a Funnel, which the server binds instead of starting
ngrok); WEBHOOK_BASE_URL (startup.sh's record of its ngrok tunnel).

`buy` and `set-webhook` write TWILIO_PHONE_NUMBER and TWILIO_WEBHOOK_PUSHED (the
last base pushed to Twilio, for drift reports) into .env in place: mode,
line endings, a symlinked file and every other byte are preserved; a commented
template placeholder gets the live line right under it. They never write
TWILIO_WEBHOOK_URL: the server treats that key as authoritative and skips its
own tunnel, so recording a moving ngrok URL there would bind the stale URL on
the next restart and, with TWILIO_AUTO_WEBHOOK=1, push it to Twilio again.

Twilio's own error text is printed verbatim (a trial account cannot buy a
number, an unverified account cannot call out): the fix is on their side and
their words say what it is. Exit 0 on success, 1 on a Twilio/network error,
2 when credentials are missing.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))

API_BASE = (os.environ.get("TWILIO_API_BASE") or "https://api.twilio.com").rstrip("/")
VOICE_PATH = "/twilio/connect"     # the TwiML route conversation-server serves
STATUS_PATH = "/twilio/status"     # its status-callback route
LOCAL_HEALTH = (os.environ.get("PHONE_SERVER_HEALTH")
                or f"http://localhost:{os.environ.get('PHONE_PORT') or 3100}/health")
DEFAULT_ENV_FILE = _REPO / ".env"
PUSHED_KEY = "TWILIO_WEBHOOK_PUSHED"   # the last base pushed to Twilio; never a base to push
_LINE_END = re.compile(r"\r?\n$")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------- credentials ----------

def _load_resolver():
    try:
        from channel_token import resolve_channel_token  # type: ignore
        return resolve_channel_token
    except Exception:
        return None


_resolve = _load_resolver()


def env_file_dict(path: Path) -> dict[str, str]:
    """Active KEY=value lines of an .env file ({} when unreadable)."""
    out: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        v = v.split(" #", 1)[0].strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


def credential(var: str, env_file: Path) -> str:
    """process env -> .env -> vault (channel_token), degrading to the first
    two tiers when src/ is not importable."""
    if _resolve is not None:
        try:
            return (_resolve(var, env_file=env_file) or "").strip()
        except Exception:
            pass
    return (os.environ.get(var, "") or env_file_dict(env_file).get(var, "")).strip()


# ---------- .env writer ----------

def set_env_var(path: Path, key: str, value: str) -> None:
    """Set KEY=value in an .env file, in place and atomically.

    An active line is replaced where it is. When only the commented template
    placeholder (`# KEY=…`) exists, the live line goes right under it so the
    file keeps its documentation. Otherwise the line is appended. Every other
    byte is preserved: the file's mode, its line endings, bytes that are not
    UTF-8, and a symlink (the target is rewritten, the link stays).

    A line break or NUL in the key or the value is refused (ValueError): a
    value such as "x\\nEVIL=1" would otherwise land as a second line. The key
    must be an env-var name. The temp file is created at the final mode from
    its first byte (O_CREAT|O_EXCL with the mode, then fchmod for the bits
    the umask stripped), so a token is never readable by others in between.
    """
    if not _ENV_KEY.fullmatch(key):
        raise ValueError(f"set_env_var: key {key!r} is not an env-var name")
    if any(ch in value for ch in ("\r", "\n", "\0")):
        raise ValueError(f"set_env_var: value for {key} must not contain a line break or NUL")
    real = Path(os.path.realpath(path))
    try:
        raw = real.read_bytes()
        mode = stat.S_IMODE(real.stat().st_mode)
    except OSError:
        raw, mode = b"", 0o600   # a new .env holds secrets: owner-only
    text = raw.decode("utf-8", "surrogateescape")
    lines = [ln for ln in re.split(r"(?<=\n)", text) if ln]
    nl = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"

    def ending(line: str) -> str:
        m = _LINE_END.search(line)
        return m.group(0) if m else ""

    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(f"{key}=") and not s.startswith("#"):
            lines[i] = new_line + (ending(line) or nl)
            replaced = True
            break
    if not replaced:
        placeholder = None
        for i, line in enumerate(lines):
            s = line.lstrip()
            if s.startswith("#") and s.lstrip("#").strip().startswith(f"{key}="):
                placeholder = i
        at = len(lines) if placeholder is None else placeholder + 1
        if at and not ending(lines[at - 1]):
            lines[at - 1] += nl
        lines.insert(at, new_line + nl)
    tmp = real.with_name(f".{real.name}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)   # a leftover of an interrupted run: ours by name
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(fd, mode)   # the umask can only have narrowed the creation mode
        f.write("".join(lines).encode("utf-8", "surrogateescape"))
    os.replace(tmp, real)


# ---------- Twilio REST ----------

class TwilioError(Exception):
    def __init__(self, status: int, message: str, code: int | None = None, more: str = ""):
        super().__init__(message)
        self.status, self.message, self.code, self.more = status, message, code, more


def _request(method: str, path: str, sid: str, token: str,
             form: dict | None = None, query: dict | None = None) -> dict:
    url = f"{API_BASE}/2010-04-01/Accounts/{sid}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    data = urllib.parse.urlencode(form).encode() if form else None
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {auth}",
        "User-Agent": "sutando-twilio-setup/1.0",
        **({"Content-Type": "application/x-www-form-urlencoded"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            j = json.loads(body)
            raise TwilioError(e.code, j.get("message") or body, j.get("code"),
                              j.get("more_info") or "") from None
        except ValueError:
            raise TwilioError(e.code, body.strip() or f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise TwilioError(0, f"network error: {e}") from None


def account(sid, token):
    return _request("GET", ".json", sid, token)


def owned_numbers(sid, token) -> list[dict]:
    return _request("GET", "/IncomingPhoneNumbers.json", sid, token,
                    query={"PageSize": 50}).get("incoming_phone_numbers", [])


def available_numbers(sid, token, country: str, area: str | None, limit: int) -> list[dict]:
    return _request("GET", f"/AvailablePhoneNumbers/{country.upper()}/Local.json", sid, token,
                    query={"VoiceEnabled": "true", "AreaCode": area, "PageSize": limit},
                    ).get("available_phone_numbers", [])


def buy_number(sid, token, e164: str, base: str | None) -> dict:
    form = {"PhoneNumber": e164}
    if base:
        form.update(webhook_form(base))
    return _request("POST", "/IncomingPhoneNumbers.json", sid, token, form=form)


def update_number(sid, token, number_sid: str, base: str) -> dict:
    return _request("POST", f"/IncomingPhoneNumbers/{number_sid}.json", sid, token,
                    form=webhook_form(base))


def webhook_form(base: str) -> dict:
    base = base.rstrip("/")
    return {"VoiceUrl": base + VOICE_PATH, "VoiceMethod": "POST",
            "StatusCallback": base + STATUS_PATH, "StatusCallbackMethod": "POST"}


# ---------- webhook base discovery ----------

def running_server_webhook() -> str:
    """What the local conversation-server bound (its /health reports it)."""
    try:
        with urllib.request.urlopen(LOCAL_HEALTH, timeout=3) as resp:
            j = json.loads(resp.read() or b"{}")
        return (j.get("webhookUrl") or "").rstrip("/")
    except Exception:
        return ""


def webhook_base(explicit: str | None, env_file: Path) -> str:
    """The base Twilio should post to; TWILIO_WEBHOOK_PUSHED is never one (it is the record)."""
    if explicit:
        return explicit.rstrip("/")
    live = running_server_webhook()
    if live:
        return live
    env = env_file_dict(env_file)
    for k in ("TWILIO_WEBHOOK_URL", "WEBHOOK_BASE_URL"):
        v = (os.environ.get(k) or env.get(k) or "").rstrip("/")
        if v:
            return v
    return ""


# ---------- commands ----------

def _out(args, payload: dict, human: str) -> None:
    print(json.dumps(payload, indent=2) if args.json else human)


def cmd_status(args, sid, token, env_file) -> int:
    acct = account(sid, token)
    nums = owned_numbers(sid, token)
    env = env_file_dict(env_file)
    configured = os.environ.get("TWILIO_PHONE_NUMBER") or env.get("TWILIO_PHONE_NUMBER", "")
    base = webhook_base(None, env_file)
    pushed = env.get(PUSHED_KEY, "").rstrip("/")
    expected_voice = (base + VOICE_PATH) if base else ""
    rows = []
    for n in nums:
        drift = bool(expected_voice) and (n.get("voice_url") or "") != expected_voice
        rows.append({"number": n.get("phone_number"), "sid": n.get("sid"),
                     "voice_url": n.get("voice_url"), "configured": n.get("phone_number") == configured,
                     "webhook_drift": drift})
    payload = {"account": {"friendly_name": acct.get("friendly_name"), "status": acct.get("status"),
                           "type": acct.get("type")},
               "numbers": rows, "configured_number": configured, "webhook_base": base,
               "last_pushed": pushed, "pushed_stale": bool(base and pushed and pushed != base),
               "server_running": bool(running_server_webhook())}
    lines = [f"Twilio account: {acct.get('friendly_name')} ({acct.get('type')}, {acct.get('status')})"]
    if acct.get("type") == "Trial":
        lines.append("  trial account: it can call only verified numbers and cannot buy a number "
                     "until upgraded (https://console.twilio.com/billing)")
    if not nums:
        lines.append("Numbers: none owned yet — run `numbers` then `buy <E.164>`")
    for r in rows:
        mark = "✓ configured" if r["configured"] else "  not in .env"
        lines.append(f"Number {r['number']} {mark}; voice webhook: {r['voice_url'] or '(none)'}"
                     + ("  ⚠ differs from this machine — run set-webhook" if r["webhook_drift"] else ""))
    lines.append(f"Webhook base here: {base or '(unknown — server not running and nothing in .env)'}")
    if payload["pushed_stale"]:
        lines.append(f"  ⚠ last pushed to Twilio: {pushed} — run set-webhook, or set TWILIO_AUTO_WEBHOOK=1")
    _out(args, payload, "\n".join(lines))
    return 0


def cmd_numbers(args, sid, token, env_file) -> int:
    nums = available_numbers(sid, token, args.country, args.area, args.limit)
    payload = {"country": args.country.upper(), "area": args.area,
               "numbers": [{"number": n.get("phone_number"), "locality": n.get("locality"),
                            "region": n.get("region")} for n in nums]}
    if not nums:
        human = f"No voice-capable local numbers available in {args.country.upper()}" + \
                (f" area {args.area}" if args.area else "") + " — try another area code."
    else:
        human = "Available numbers (ask the owner which one, then `buy <number>`):\n" + "\n".join(
            f"  {n.get('phone_number')}  {n.get('locality') or ''} {n.get('region') or ''}".rstrip()
            for n in nums)
    _out(args, payload, human)
    return 0


def cmd_buy(args, sid, token, env_file) -> int:
    base = webhook_base(args.base, env_file)
    bought = buy_number(sid, token, args.number, base or None)
    number = bought.get("phone_number") or args.number
    set_env_var(env_file, "TWILIO_PHONE_NUMBER", number)
    if base:
        set_env_var(env_file, PUSHED_KEY, base)
    payload = {"number": number, "sid": bought.get("sid"), "voice_url": bought.get("voice_url"),
               "webhook_base": base, "env_file": str(env_file)}
    human = f"Bought {number} (sid {bought.get('sid')}); TWILIO_PHONE_NUMBER written to {env_file}."
    human += (f"\nVoice webhook set to {base}{VOICE_PATH}." if base else
              "\nNo webhook base known yet — start the phone server, then run `set-webhook`.")
    human += "\nRestart the phone conversation server so it binds the new number."
    _out(args, payload, human)
    return 0


def cmd_set_webhook(args, sid, token, env_file) -> int:
    base = webhook_base(args.base, env_file)
    if not base:
        print("[twilio-setup] no webhook base: pass one, or start the phone server "
              "(its /health reports the tunnel)", file=sys.stderr)
        return 1
    env = env_file_dict(env_file)
    want = args.number or os.environ.get("TWILIO_PHONE_NUMBER") or env.get("TWILIO_PHONE_NUMBER", "")
    nums = owned_numbers(sid, token)
    if not nums:
        print("[twilio-setup] this account owns no numbers — run `numbers` then `buy`", file=sys.stderr)
        return 1
    target = next((n for n in nums if n.get("phone_number") == want), None) if want else None
    if target is None:
        if len(nums) == 1 and not want:
            target = nums[0]
        else:
            owned = ", ".join(n.get("phone_number", "?") for n in nums)
            print(f"[twilio-setup] which number? owned: {owned} (pass --number)", file=sys.stderr)
            return 1
    updated = update_number(sid, token, target["sid"], base)
    set_env_var(env_file, PUSHED_KEY, base)
    if not want:
        set_env_var(env_file, "TWILIO_PHONE_NUMBER", target["phone_number"])
    payload = {"number": target.get("phone_number"), "voice_url": updated.get("voice_url"),
               "status_callback": updated.get("status_callback"), "webhook_base": base}
    _out(args, payload, f"{target.get('phone_number')} now posts calls to {base}{VOICE_PATH} "
                        f"(status → {base}{STATUS_PATH}); {PUSHED_KEY} written to {env_file}.")
    return 0


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Twilio setup from the chat.")
    p.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    n = sub.add_parser("numbers")
    n.add_argument("--country", default="US")
    n.add_argument("--area", default=None, help="area code, e.g. 415")
    n.add_argument("--limit", type=int, default=10)
    b = sub.add_parser("buy")
    b.add_argument("number", help="E.164, e.g. +14155551234")
    b.add_argument("--base", default=None, help="webhook base URL (default: the running server's)")
    w = sub.add_parser("set-webhook")
    w.add_argument("base", nargs="?", default=None)
    w.add_argument("--number", default=None)
    args = p.parse_args(argv)

    env_file = Path(args.env_file).expanduser()
    sid = credential("TWILIO_ACCOUNT_SID", env_file)
    token = credential("TWILIO_AUTH_TOKEN", env_file)
    if not sid or not token:
        print("[twilio-setup] TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not found. Ask the owner "
              "for both (Twilio console → Account info), then `vault set TWILIO_ACCOUNT_SID …` "
              f"and `vault set TWILIO_AUTH_TOKEN …` (or put them in {env_file}).", file=sys.stderr)
        return 2
    if args.cmd == "buy" and not args.number.startswith("+"):
        print("[twilio-setup] number must be E.164 (starts with +)", file=sys.stderr)
        return 1
    handler = {"status": cmd_status, "numbers": cmd_numbers, "buy": cmd_buy,
               "set-webhook": cmd_set_webhook}[args.cmd]
    try:
        return handler(args, sid, token, env_file)
    except TwilioError as e:
        where = f"HTTP {e.status}" if e.status else "no response"
        code = f" (Twilio error {e.code})" if e.code else ""
        print(f"[twilio-setup] Twilio refused{code}, {where}: {e.message}"
              + (f"\n  more: {e.more}" if e.more else ""), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
