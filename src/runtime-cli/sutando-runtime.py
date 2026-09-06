#!/usr/bin/env python3
"""sutando-runtime — CLI face of the local runtime API (v0).

The agent-facing command for human-collaboration requests; the agent never
touches the socket, JSON-RPC, or any remote API directly:

  sutando-runtime approval request --action change.apply \
      --resource '{"target":"item-42"}' --reason "checks green"
  sutando-runtime elicitation request --question "Deploy where?" \
      --type single_select --options '["staging","production"]'
  sutando-runtime capability list
  sutando-runtime capability read --capability example.activity \
      --operation records.list --resource '{"scope":"mine"}'
  sutando-runtime capability execute --action message.send \
      --resource '{"roomId":"!r:hs"}' --input '{"body":"hi"}'
  sutando-runtime request get <requestId>
  sutando-runtime request wait <requestId> --timeout 300
  sutando-runtime request cancel <requestId>
  sutando-runtime agent list
  sutando-runtime agent status <agentId>
  sutando-runtime sutando info|status|owner|allowlist
  sutando-runtime task submit "do the thing" [--priority normal]
  sutando-runtime task results   # all results, newest first, with preview
  sutando-runtime task chat [--activity] [--raw]   # one-screen DM (+step feed / raw tmux)
  sutando-runtime task watch [--activity] [--raw]  # stream results (+step feed / raw tmux)
  sutando-runtime task status|details|cancel <taskId> | get-result [taskId]

Issuing commands return immediately with {"requestId", "status": "pending"};
`request wait` blocks (bounded) for the resolution. Output is JSON on stdout;
exit 0 on a well-formed response (whatever the status), 1 on transport or
protocol error — status interpretation belongs to the caller.

Env: SUTANDO_RUNTIME_SOCKET (default <run dir>/sutando-runtime.sock).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import uuid
from pathlib import Path


# Canonical socket resolution shared with the daemon (review blocker: both
# sides duplicated a macOS-only fallback; rundir.py is the one policy).
_RUNTIME_API_DIR = Path(__file__).resolve().parent.with_name("runtime-api")
sys.path.insert(0, str(_RUNTIME_API_DIR))
from rundir import socket_path as _socket_path  # noqa: E402


def _wss_url() -> str | None:
    """Remote SCP target, if set — the SAME client, over the network
    WebSocket transport instead of the local Unix socket (one SCP, N
    transports; ws:// is cleartext, wss:// only against the TLS sibling).
    Env-driven so every command routes remotely with no per-command flag:
      SUTANDO_SCP_WSS_URL    ws://<host>:<port>/scp   (legacy _WSS_ name)
      SUTANDO_SCP_WSS_TOKEN  credential (shared bearer or a paired-device
                             credential)
    One-shot commands route through the remote transport when the URL is set
    (persistent surfaces differ: task watch streams remotely, task chat
    refuses — it is Unix-socket-only). What the server serves depends on the
    credential — the shared bearer gets only READ_ONLY_METHODS, a paired
    device its per-device grants (task.submit/cancel/voice by default)."""
    return os.environ.get("SUTANDO_SCP_WSS_URL") or None


def _auth_dir() -> str:
    """The Server's auth dir (device credentials + pairing) — resolved the same
    way the daemon resolves state, so owner-local pair commands touch the same
    files the running Server reads."""
    st = os.environ.get("SUTANDO_RUNTIME_STATE")
    if st:
        return str(Path(st) / "auth")
    sys.path.insert(0, str(_RUNTIME_API_DIR.parent))
    from workspace_default import resolve_workspace  # noqa: PLC0415
    return str(Path(resolve_workspace()) / "state" / "auth")


def _rpc_wss(method: str, params: dict, timeout: float) -> dict:
    import asyncio  # noqa: PLC0415
    import aiohttp  # noqa: PLC0415
    url = os.environ["SUTANDO_SCP_WSS_URL"]
    token = os.environ.get("SUTANDO_SCP_WSS_TOKEN", "")
    rid = f"cli-{uuid.uuid4().hex[:8]}"
    frame = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params}, ensure_ascii=False)

    async def _go() -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url, headers=headers,
                                       timeout=timeout) as ws:
                await ws.send_str(frame)
                msg = await asyncio.wait_for(ws.receive(), timeout)
                return json.loads(msg.data)

    resp = asyncio.run(_go())
    if "error" in resp:
        raise RuntimeError(
            f"{resp['error'].get('code')}: {resp['error'].get('message')}")
    return resp["result"]


def _rpc(method: str, params: dict, timeout: float) -> dict:
    if _wss_url():
        return _rpc_wss(method, params, timeout)
    frame = json.dumps({"jsonrpc": "2.0", "id": f"cli-{uuid.uuid4().hex[:8]}",
                        "method": method, "params": params},
                       ensure_ascii=False) + "\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(_socket_path())
        s.sendall(frame.encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = json.loads(buf.decode("utf-8"))
    if "error" in resp:
        raise RuntimeError(f"{resp['error'].get('code')}: {resp['error'].get('message')}")
    return resp["result"]


def _jarg(v):
    return json.loads(v) if v else None


def _raw_tmux() -> int:
    # RAW = a live READ-ONLY view of the core's tmux window (everything it
    # prints). Uses tmux's own read-only attach — the firehose stays on the
    # tmux socket, never through the daemon push path. Ctrl-b d to detach.
    if _wss_url():
        # Gate at the chokepoint: raw is inherently LOCAL, and attaching the
        # local tmux under a remote URL views the WRONG agent.
        print(json.dumps({"error": "raw view attaches the LOCAL tmux and is "
                          "not served over the remote WebSocket transport — "
                          "unset SUTANDO_SCP_WSS_URL for the local raw view"}),
              flush=True)
        return 2
    import subprocess
    sock = os.environ.get("SUTANDO_TMUX_SOCKET") or "/tmp/sutando-tmux.sock"
    session = os.environ.get("SUTANDO_TMUX_SESSION") or "sutando-core"
    print("⚠ RAW tmux view (read-only) — shows EVERYTHING the core prints, "
          "including secrets / tokens / private task content. Ctrl-b then d to "
          "detach.", flush=True)
    return subprocess.call(["tmux", "-S", sock, "attach-session",
                            "-t", session, "-r"])


def _watch_wss(activity: bool = False) -> int:
    # PUSH mode over the remote WebSocket transport — a read stream; what a
    # credential may DO is resolved server-side per connection.
    import asyncio  # noqa: PLC0415
    import aiohttp  # noqa: PLC0415
    url = os.environ["SUTANDO_SCP_WSS_URL"]
    token = os.environ.get("SUTANDO_SCP_WSS_TOKEN", "")

    async def _go() -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url, headers=headers) as ws:
                await ws.send_str(json.dumps({
                    "jsonrpc": "2.0", "id": "watch", "method": "task.subscribe",
                    "params": {"activity": activity}}))
                scheme = url.split(":", 1)[0] if ":" in url else "ws"
                print(json.dumps({"watching": True, "activity": activity,
                                  "transport": scheme}), flush=True)
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    obj = json.loads(msg.data)
                    m = obj.get("method")
                    if m == "task.result":
                        p = obj.get("params", {})
                        print(json.dumps({"result": p.get("taskId"),
                                          "body": p.get("result"),
                                          "ts": p.get("ts")}, ensure_ascii=False),
                              flush=True)
                    elif m == "activity":
                        p = obj.get("params", {})
                        print(json.dumps({"activity": p.get("step"),
                                          "ts": p.get("ts")}, ensure_ascii=False),
                              flush=True)

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        pass
    return 0


def _watch(activity: bool = False, raw: bool = False) -> int:
    # PUSH mode: subscribe and stream notifications live. Persistent connection
    # (not the one-shot _rpc) — blocks until Ctrl-C.
    if raw:
        # RAW is the local tmux firehose; the WSS equivalent is terminal.attach
        # (a later slice), so raw stays Unix-socket-only.
        return _raw_tmux()
    if _wss_url():
        return _watch_wss(activity)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(_socket_path())
    s.sendall((json.dumps({"jsonrpc": "2.0", "id": "watch",
                           "method": "task.subscribe",
                           "params": {"activity": activity}}) + "\n")
              .encode("utf-8"))
    print(json.dumps({"watching": True, "activity": activity}), flush=True)
    buf = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                msg = json.loads(line.decode("utf-8"))
                m = msg.get("method")
                if m == "task.result":
                    p = msg.get("params", {})
                    print(json.dumps({"result": p.get("taskId"),
                                      "body": p.get("result"),
                                      "ts": p.get("ts")}, ensure_ascii=False),
                          flush=True)
                elif m == "activity":
                    p = msg.get("params", {})
                    print(json.dumps({"activity": p.get("step"),
                                      "ts": p.get("ts")}, ensure_ascii=False),
                          flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    return 0


async def _chat_async(activity: bool = False, verbose: bool = False,
                      full: bool = False) -> None:
    # One-screen DM: ONE connection multiplexes subscribe + submit + the pushed
    # notifications. stdin lines become tasks; results print inline. Task msgs
    # (you) and result msgs (agent) render distinctly so they're easy to tell
    # apart; --activity also shows the step feed.
    reader, writer = await asyncio.open_unix_connection(_socket_path())

    def _send(method, params, rid):
        writer.write((json.dumps({"jsonrpc": "2.0", "id": rid,
                                  "method": method, "params": params},
                                 ensure_ascii=False) + "\n").encode("utf-8"))

    # Agent identity for the reply header (its ag2space mxid if connected).
    agent_id = None
    try:
        _send("sutando.info", {}, "chat-info")
        await writer.drain()
        for _ in range(20):
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            if not line:
                break
            m = json.loads(line.decode("utf-8"))
            if m.get("id") == "chat-info":
                agent_id = (m.get("result") or {}).get("agentId")
                break
    except Exception:
        pass

    # Always stream activity frames; the client shows/hides them so the mode
    # can be toggled mid-chat (/activity, /quiet) without a daemon round-trip.
    _send("task.subscribe", {"activity": True}, "chat-sub")
    await writer.drain()
    level = 3 if full else 2 if verbose else 1 if activity else 0  # quiet·steps·tool·+content
    if sys.stdin.isatty() and sys.stdout.isatty():
        await _chat_tui(reader, writer, _send, level, agent_id)
    else:
        await _chat_line(reader, writer, _send, level, agent_id)


async def _chat_line(reader, writer, _send, level=0, agent_id=None) -> None:
    # Fallback for pipes / non-tty (scripts, tests): no fixed UI.
    print("sutando chat — type a task, enter to send; results stream. Ctrl-D exits.\n",
          flush=True)
    loop = asyncio.get_event_loop()

    async def pump_stdin():
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            t = line.strip()
            if t:
                print(f"\n╭─ you\n{t}\n╰─", flush=True)
                _send("task.submit", {"task": t}, "chat-submit")
                await writer.drain()
        writer.close()

    async def pump_socket():
        b = b""
        while True:
            c = await reader.read(65536)
            if not c:
                break
            b += c
            while b"\n" in b:
                raw, b = b.split(b"\n", 1)
                if not raw.strip():
                    continue
                m = json.loads(raw.decode("utf-8"))
                if m.get("method") == "task.result":
                    p = m.get("params", {})
                    who = f"agent · {agent_id}" if agent_id else "agent"
                    print(f"\n╭─ {who}  ({p.get('taskId')})\n"
                          f"{p.get('result', '').rstrip()}\n╰─\n", flush=True)
                elif m.get("method") == "activity":
                    p = m.get("params", {})
                    need = 2 if p.get("kind") == "tool" else 1
                    if level >= need:
                        print(f"  ⚙ {p.get('step')}", flush=True)
                        if level >= 3 and p.get("detail"):
                            for dl in str(p["detail"]).split("\n"):
                                print(f"    {dl}", flush=True)
    await asyncio.gather(pump_stdin(), pump_socket())


async def _chat_tui(reader, writer, _send, level=0, agent_id=None) -> None:
    # Fixed-bottom compose box: a pinned "› " input at the bottom; everything
    # you send + all streamed output scrolls in the region ABOVE it, so the
    # input line is never clobbered mid-typing (the flooding fix). No deps —
    # terminal scroll-region + a raw-mode line buffer (UTF-8 aware).
    import termios
    import tty
    import unicodedata
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout
    loop = asyncio.get_event_loop()
    buf: list[str] = []
    pend = bytearray()
    done = loop.create_future()
    orow = [2]  # next output row; transcript fills TOP-down (like Claude Code)
    lvl = [int(level)]  # 0 quiet · 1 steps · 2 per-tool; toggled by /quiet /activity /verbose
    compose_h = [0]  # current compose-box height (rows); 0 forces the first layout
    esc = bytearray()   # in-progress ANSI escape sequence (arrow keys, paste markers)
    pasting = [False]   # inside a bracketed paste (\033[200~ … \033[201~)
    paste_run = [-1.0]  # monotonic ts of the last unmarked multi-line paste burst

    def dims():
        s = shutil.get_terminal_size((80, 24))
        return s.lines, s.columns

    def vwidth(s):
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

    def wrap(s, w):
        # Width-aware wrap (CJK counts as 2) so a long line spans multiple rows
        # instead of overflowing one.
        rows_out, cur, cw = [], "", 0
        for ch in s:
            chw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if cw + chw > w:
                rows_out.append(cur)
                cur, cw = ch, chw
            else:
                cur += ch
                cw += chw
        rows_out.append(cur)
        return rows_out or [""]

    def draw_input():
        # Multi-line compose box pinned to the bottom: the input WRAPS across as
        # many rows as it needs (up to a cap) and grows upward, so the whole
        # message is visible while typing — not just the tail. The transcript
        # scroll region shrinks/grows to match.
        rows, cols = dims()
        text = "".join(buf)
        max_h = max(1, min(6, rows // 3))
        ilines = wrap(text, max(1, cols - 2)) if text else [""]
        if len(ilines) > max_h:
            ilines = ilines[-max_h:]              # keep the tail (where the cursor is)
        h = len(ilines)
        sep_row = rows - h                        # separator sits just above the box
        top = rows - h + 1                        # first compose row
        if h != compose_h[0]:
            # Clear the separator+compose band (only ever the bottom band, never
            # the transcript above the separator), then re-pin the scroll region.
            band_top = max(2, rows - max(compose_h[0], h))
            for r in range(band_top, rows + 1):
                out.write(f"\033[{r};1H\033[K")
            out.write(f"\033[2;{sep_row - 1}r")           # region = rows 2..(rows-h-1)
            out.write(f"\033[{sep_row};1H" + "─" * cols)  # separator
            compose_h[0] = h
        for i, ln in enumerate(ilines):
            prefix = "\033[33m› \033[0m" if i == 0 else "  "
            out.write(f"\033[{top + i};1H\033[K" + prefix + ln)
        out.flush()

    def _row(text):
        # Place one physical row of transcript. Region = rows 2..(rows-h-1),
        # where h is the current compose height; fill top-down, scroll when full.
        rows, _ = dims()
        bot = rows - compose_h[0] - 1
        out.write("\0337")                       # save cursor (DECSC)
        if orow[0] <= bot:
            out.write(f"\033[{orow[0]};1H\033[K" + text)
            orow[0] += 1
        else:
            out.write(f"\033[{bot};1H\n\033[K" + text)  # region full → scroll up 1
        out.write("\0338")                       # restore cursor (DECRC)
        draw_input()

    def emit(line):
        # Wrap long PLAIN lines to the width so a long message spans rows rather
        # than getting squeezed onto one (short colored border/label lines pass
        # through as-is — measuring visible width through ANSI isn't worth it).
        _, cols = dims()
        line = line.replace("\n", " ")
        if "\033" not in line and vwidth(line) > cols:
            for chunk in wrap(line, cols):
                _row(chunk)
        else:
            _row(line)

    def add(ch):
        buf.append(ch)
        draw_input()  # full redraw so wrapping / box growth is handled

    def command(text):
        # Client-side chat commands (not sent to the core as tasks).
        cmd = text.split()[0].lower()
        if cmd in ("/quiet", "/nothing", "/off"):
            lvl[0] = 0
            emit("\033[2m⚙ quiet — replies only\033[0m")
        elif cmd in ("/activity", "/steps"):
            lvl[0] = 1
            emit("\033[2m⚙ activity — step-level feed on\033[0m")
        elif cmd == "/verbose":
            lvl[0] = 2
            emit("\033[2m⚙ verbose — per-tool activity (tool + target)\033[0m")
        elif cmd == "/full":
            lvl[0] = 3
            emit("\033[2m⚙ full — per-tool WITH content (diffs / commands). "
                 "⚠ shows secrets.\033[0m")
        elif cmd == "/raw":
            emit("\033[2mraw = the whole tmux firehose (incl. my reasoning) — exit "
                 "and relaunch with --raw (read-only tmux view). /full shows tool "
                 "content inline; /raw is the full terminal.\033[0m")
        elif cmd == "/help":
            emit("\033[2mcommands: /quiet · /activity (steps) · /verbose (per-tool) · "
                 "/full (+content) · /raw (full tmux) · Ctrl-D exit\033[0m")
        else:
            emit(f"\033[2munknown command {cmd} — try /help\033[0m")

    def on_enter():
        text = "".join(buf).strip()
        buf.clear()
        if text.startswith("/"):
            command(text)
        elif text:
            # Symmetric with the agent box below (yellow "you" vs cyan "agent"),
            # so the sent message reads as ONE unit — not text with a detached
            # "(sent)" label under it.
            emit("\033[33m╭─ you\033[0m")
            emit(text)
            emit("\033[33m╰─\033[0m")
            _send("task.submit", {"task": text}, "chat-submit")
        draw_input()

    def on_key():
        try:
            data = os.read(fd, 4096)
        except OSError:
            data = b""
        if not data:
            if not done.done():
                done.set_result(None)
            return
        # Paste WITHOUT bracketed markers: some paste paths never send
        # \x1b[200~, so each newline hit Enter and a multi-line paste became N
        # separate tasks (live 2026-08-09: one message → 17 tasks). Detect by
        # burst shape — a typed Enter arrives as a lone trailing newline, while
        # a paste arrives as one read burst with INTERIOR newlines. Treat every
        # newline in such a burst (and in continuation bursts of a big split
        # paste, within 150ms) as a literal separator, never a submit.
        import time as _t
        core = data[:-1]
        if b"\r" in core or b"\n" in core:
            paste_run[0] = _t.monotonic()
        burst_paste = _t.monotonic() - paste_run[0] < 0.15
        for b in data:
            if esc:                               # collecting an escape sequence
                esc.append(b)
                eb = bytes(esc)
                if eb == b"\x1b[200~":            # bracketed paste START
                    pasting[0] = True; esc.clear(); continue
                if eb == b"\x1b[201~":            # bracketed paste END
                    pasting[0] = False; esc.clear(); draw_input(); continue
                if len(esc) >= 3 and esc[1] == 0x5b and 0x40 <= b <= 0x7e:
                    esc.clear(); continue         # other CSI (arrow key etc) → ignore
                if len(esc) > 8:
                    esc.clear()
                continue
            if b == 0x1b and not pend:            # ESC → start an escape sequence
                esc.append(b); continue
            if pend:
                pend.append(b)
                try:
                    add(pend.decode()); pend.clear()
                except UnicodeDecodeError:
                    pass
                continue
            if pasting[0]:                        # inside a paste: literal, no submit
                if b in (0x0d, 0x0a, 0x09):
                    add(" ")                      # collapse newlines/tabs → one line
                elif b < 0x20:
                    pass
                elif b < 0x80:
                    add(chr(b))
                else:
                    pend.append(b)
                    try:
                        add(pend.decode()); pend.clear()
                    except UnicodeDecodeError:
                        pass
                continue
            if b in (0x0d, 0x0a):                 # Enter — or a paste newline
                if burst_paste:
                    add(" ")                      # unmarked paste: never submit
                else:
                    on_enter()
            elif b in (0x7f, 0x08):               # Backspace
                if buf:
                    buf.pop(); draw_input()
            elif b == 0x04:                       # Ctrl-D (empty) → exit
                if not buf and not done.done():
                    done.set_result(None)
            elif b == 0x03:                       # Ctrl-C → exit
                if not done.done():
                    done.set_result(None)
            elif b < 0x20:                        # other control → ignore
                pass
            elif b < 0x80:                        # ASCII printable
                add(chr(b))
            else:                                 # UTF-8 lead byte
                pend.append(b)
                try:
                    add(pend.decode()); pend.clear()
                except UnicodeDecodeError:
                    pass

    async def pump_socket():
        b = b""
        while not done.done():
            c = await reader.read(65536)
            if not c:
                break
            b += c
            while b"\n" in b:
                raw, b = b.split(b"\n", 1)
                if not raw.strip():
                    continue
                m = json.loads(raw.decode("utf-8"))
                meth = m.get("method")
                if meth == "task.result":
                    p = m.get("params", {})
                    who = f"agent · {agent_id}" if agent_id else "agent"
                    emit(f"\033[36m╭─ {who}\033[0m  \033[2m{p.get('taskId')}\033[0m")
                    for ln in p.get("result", "").rstrip().split("\n"):
                        emit(ln)
                    emit("\033[36m╰─\033[0m")
                elif meth == "activity":
                    p = m.get("params", {})
                    need = 2 if p.get("kind") == "tool" else 1
                    if lvl[0] >= need:
                        emit(f"  \033[2m⚙ {p.get('step')}\033[0m")
                        if lvl[0] >= 3 and p.get("detail"):   # /full: show content
                            for dl in str(p["detail"]).split("\n"):
                                emit(f"    \033[2m{dl}\033[0m")
        if not done.done():
            done.set_result(None)

    tty.setcbreak(fd)                             # raw-ish; keeps Ctrl-C signal off (we read 0x03)
    rows, cols = dims()
    out.write("\033[2J\033[H\033[?2004h")         # clear + home + bracketed paste on
    # Fixed header (row 1, outside the scroll region so it never scrolls away).
    hdr = " sutando · chat"
    who = f"  \033[2m{agent_id}\033[0m" if agent_id else ""
    hint = "/help · /activity · /quiet · Ctrl-D exit"
    out.write(f"\033[1;1H\033[K\033[1m{hdr}\033[0m{who}  \033[2m{hint}\033[0m")
    draw_input()  # owns the scroll region + separator + compose box (grows with input)
    out.flush()
    loop.add_reader(fd, on_key)
    ps = asyncio.ensure_future(pump_socket())
    try:
        await done
    finally:
        loop.remove_reader(fd)
        ps.cancel()
        out.write("\033[?2004l\033[r")            # bracketed paste off + reset region
        out.write(f"\033[{dims()[0]};1H\r\n")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        out.flush()
        writer.close()


def _chat(activity: bool = False, verbose: bool = False, full: bool = False) -> int:
    if _wss_url():
        # chat multiplexes over the Unix socket only; opening the local UDS
        # under a remote URL silently targets the WRONG agent — refuse loudly.
        print(json.dumps({"error": "task chat is not served over the remote "
                          "WebSocket transport yet — unset SUTANDO_SCP_WSS_URL "
                          "to chat with the local agent"}), flush=True)
        return 2
    try:
        asyncio.run(_chat_async(activity, verbose, full))
    except (KeyboardInterrupt, EOFError):
        pass
    return 0


def _fmt_subject(provider: str, subject: str) -> str:
    if subject.startswith("@"):
        return subject
    return f"{provider}:user:{subject}"


_PROFILE_LABELS = {"ag2space": "ag2space [production]",
                   "dev-ag2space": "ag2space [dev]"}


def _print_entrance_rows(ents: list, width: int) -> None:
    width = max(width, len("ag2space [production]")) + 2
    for e in ents:
        label = _PROFILE_LABELS.get(e.get("provider", ""), e.get("provider", ""))
        print(f"  {label.ljust(width)}{e.get('status','')}")
        ident = e.get("identity") or {}
        if ident:
            sub = ident.get("id", "")
            if ident.get("type"):
                sub = f"{ident['type']}:{sub}"
            disp = (e.get("display") or {}).get("name")
            if disp:
                sub = f"{disp}   {sub}"
            print(f"    {'identity'.ljust(width)}{sub}")
        ver = e.get("verification") or {}
        if ver.get("method"):
            print(f"    {'verified by'.ljust(width)}{ver['method']}"
                  + (f" ({ver['verified_at']})" if ver.get("verified_at") else ""))
        cred = e.get("credential") or {}
        if cred.get("fingerprint"):
            print(f"    {'fingerprint'.ljust(width)}{cred['fingerprint']}")
        ev = e.get("evidence") or {}
        subj_label = ("identity" if e.get("status") == "active"
                      else "subject evidence")
        for key, label in (("subject_evidence", subj_label),
                           ("owner_id", "owner id"),
                           ("credential_present", "credential"),
                           ("policy_present", "policy")):
            if key in ev:
                val = ev[key] if isinstance(ev[key], str) else "present"
                if key == "owner_id":
                    val = _fmt_subject(e.get("provider", ""), val)
                print(f"    {label.ljust(width)}{val}")
        if e.get("stand_binding"):
            print(f"    {'Stand binding'.ljust(width)}{e['stand_binding']}")
        st = e.get("storage") or {}
        if st.get("directory"):
            print(f"    {'storage'.ljust(width)}{st['directory']}")


def _print_resolve(result: dict) -> int:
    if result.get("conflict"):
        print("error: provider identity is linked to multiple Stands",
              file=sys.stderr)
        for c in result.get("candidates", []):
            ver = (c.get("verification") or {}).get("method", "unverified")
            print(f"  {c.get('stand_id','?')} — {ver}", file=sys.stderr)
        print("manual resolution required", file=sys.stderr)
        return 3
    if not result.get("resolved"):
        print(f"not linked: {result.get('provider')} "
              f"{result.get('subject')}", file=sys.stderr)
        return 1
    lk = result.get("link") or {}
    width = 12
    print(f"{'Stand'.ljust(width)}{result.get('stand_id','')}")
    print(f"{'Provider'.ljust(width)}{lk.get('provider','')}")
    subj = lk.get("provider_subject") or {}
    sv = subj.get("id", "")
    if subj.get("type"):
        sv = f"{subj['type']}:{sv}"
    print(f"{'Subject'.ljust(width)}{sv}")
    disp = (lk.get("display") or {}).get("name")
    if disp:
        print(f"{'Display'.ljust(width)}{disp}")
    print(f"{'Link'.ljust(width)}{lk.get('link_id','')}")
    print(f"{'Status'.ljust(width)}{lk.get('status','')}")
    ver = lk.get("verification") or {}
    if ver.get("method"):
        print(f"{'Verified'.ljust(width)}{ver['method']}"
              + (f", {ver['verified_at']}" if ver.get("verified_at") else ""))
    return 0


def _print_stand_card(card: dict, section: "str | None") -> int:
    stand = card.get("stand") or {}
    if section == "id":
        sid = stand.get("stand_id")
        if not sid:
            print("no stand record", file=sys.stderr)
            return 1
        print(sid)
        return 0
    width = 16
    show = lambda name: section is None or section == name  # noqa: E731
    if section is None:
        label = stand.get("stand_id", "")
        if stand.get("display_name"):
            label = f"{stand['display_name']}   {label}"
        print(f"Stand   {label}")
        if stand.get("status"):
            print(f"  {'Status'.ljust(width)}{stand['status']}")
        print()
    if show("owner"):
        owners = card.get("owners") or []
        if owners:
            o = owners[0]
            oid = o.get("person_id", "")
            if o.get("display_name"):
                oid = f"{o['display_name']} ({oid})"
            role = f"   {o['role']}" if o.get("role") else ""
            print(f"Owner   {oid}{role}")
        else:
            print("Owner   Not established")
            for ev in card.get("owner_evidence") or []:
                print(f"  {'Evidence'.ljust(width)}"
                      f"{_fmt_subject(ev['provider'], ev['subject'])}"
                      f" via {ev['provider']}")
        print()
    if show("channels") or section == "entrances":
        ents = card.get("channels") or []
        print("Channels")
        if ents:
            _print_entrance_rows(ents, width)
        else:
            print("  none configured")
        print()
    if show("devices"):
        devs = card.get("devices") or []
        print("Devices")
        if devs:
            for d in devs:
                row = f"  {d.get('label', d.get('device_id', '')).ljust(width)}"
                row += (d.get("device_type") or "unknown").ljust(10)
                row += d.get("status", "")
                print(row.rstrip())
        else:
            print("  No enrolled devices")
        print()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sutando-runtime")
    sub = ap.add_subparsers(dest="group", required=True)

    apr = sub.add_parser("approval").add_subparsers(dest="cmd", required=True) \
             .add_parser("request")
    apr.add_argument("--task-id")
    apr.add_argument("--action", required=True)
    apr.add_argument("--resource")
    apr.add_argument("--input",
                     help="JSON input of the effect being approved (for "
                          "governed actions the daemon binds the approval to "
                          "the EXACT action+resource+input — e.g. the message "
                          "body for message.send)")
    apr.add_argument("--reason")
    apr.add_argument("--expires-in", type=float)

    eli = sub.add_parser("elicitation").add_subparsers(dest="cmd", required=True) \
             .add_parser("request")
    eli.add_argument("--task-id")
    eli.add_argument("--question", required=True)
    eli.add_argument("--type", default="single_select",
                     choices=["free_text", "single_select", "multi_select",
                              "confirmation"],
                     help="elicitation type (default: single_select; the v0 "
                          "server rejects free_text — card transport is "
                          "options-based)")
    eli.add_argument("--options")
    eli.add_argument("--expires-in", type=float)

    capability_commands = sub.add_parser("capability").add_subparsers(
        dest="cmd", required=True)
    capability_commands.add_parser("list")

    read = capability_commands.add_parser("read")
    read.add_argument("--capability", required=True)
    read.add_argument("--operation", required=True)
    read.add_argument("--resource")
    read.add_argument("--cursor")
    read.add_argument("--limit", type=int)

    cap = capability_commands.add_parser("execute")
    cap.add_argument("--task-id")
    cap.add_argument("--action", required=True)
    cap.add_argument("--resource")
    cap.add_argument("--input")
    cap.add_argument("--idempotency-key")
    cap.add_argument("--approval", help="approval requestId that authorizes this execution (consumed once)")

    req = sub.add_parser("request").add_subparsers(dest="cmd", required=True)
    req.add_parser("list")
    for name in ("get", "wait", "cancel"):
        p = req.add_parser(name)
        p.add_argument("request_id")
        if name == "wait":
            p.add_argument("--timeout", type=float, default=300.0)

    agt = sub.add_parser("agent").add_subparsers(dest="cmd", required=True)
    agt.add_parser("list")
    agt.add_parser("status").add_argument("agent_id")

    idn = sub.add_parser("sutando").add_subparsers(dest="cmd", required=True)
    for name in ("info", "status", "owner", "allowlist"):
        idn.add_parser(name)
    std = idn.add_parser("stand")
    std.add_argument("sub", nargs="?",
                     choices=["id", "owner", "channels", "entrances",
                              "devices", "resolve"])
    std.add_argument("extra", nargs="*")
    std.add_argument("--json", action="store_true", dest="as_json")
    std.add_argument("--details", action="store_true", dest="details")

    rt = sub.add_parser("runtime").add_subparsers(dest="cmd", required=True)
    for name in ("health", "details"):
        rt.add_parser(name)

    hac = sub.add_parser("human-action").add_subparsers(dest="cmd", required=True)
    hreq = hac.add_parser("request")
    hreq.add_argument("--task-id")
    hreq.add_argument("--action", required=True)
    hreq.add_argument("--instructions")
    hreq.add_argument("--deadline")
    hreq.add_argument("--expires-in", type=float)
    for name in ("complete", "decline", "status"):
        p2 = hac.add_parser(name)
        p2.add_argument("request_id")
        if name in ("complete", "decline"):
            p2.add_argument("--note")

    ins = sub.add_parser("instance").add_subparsers(dest="cmd", required=True)
    ins.add_parser("list")
    ist = ins.add_parser("start")
    ist.add_argument("agent_id")
    ist.add_argument("--wait", type=float, default=10.0)
    iat = ins.add_parser("attach")
    iat.add_argument("agent_id")
    iat.add_argument("--print", action="store_true",
                     help="print the tmux command instead of exec'ing it")
    iop = ins.add_parser("open")
    iop.add_argument("agent_id")
    iop.add_argument("--window", action="store_true")

    tsk = sub.add_parser("task").add_subparsers(dest="cmd", required=True)
    tsk.add_parser("list")
    tsk.add_parser("results")  # list all available results (newest first)
    tw = tsk.add_parser("watch")   # PUSH mode: stream results live as they land
    tw.add_argument("--activity", action="store_true",
                    help="also stream the agent's activity (step feed)")
    tw.add_argument("--raw", action="store_true",
                    help="stream the raw tmux window — SHOWS EVERYTHING incl. secrets")
    tc = tsk.add_parser("chat")    # one-screen DM: submit + live result stream
    tc.add_argument("--activity", action="store_true",
                    help="also stream the agent's activity (step feed) inline")
    tc.add_argument("--verbose", action="store_true",
                    help="stream per-tool activity too (needs the PostToolUse hook)")
    tc.add_argument("--full", action="store_true",
                    help="per-tool activity WITH content (diffs/commands) — shows secrets")
    tc.add_argument("--raw", action="store_true",
                    help="stream the raw tmux window — SHOWS EVERYTHING incl. secrets")
    tsub = tsk.add_parser("submit")
    tsub.add_argument("text")
    tsub.add_argument("--priority", default="normal",
                      choices=["urgent", "normal", "low"])
    for name in ("status", "details", "cancel"):
        tsk.add_parser(name).add_argument("task_id")
    # get-result's id is OPTIONAL — no id means "the newest result".
    tsk.add_parser("get-result").add_argument("task_id", nargs="?")

    # pair: device pairing + per-device credentials. new/list/revoke are
    # owner-local (touch the Server's auth dir directly, no daemon needed);
    # redeem runs on the NEW device over WSS (pairing token → credential).
    prg = sub.add_parser("pair").add_subparsers(dest="cmd", required=True)
    pnew = prg.add_parser("new")
    pnew.add_argument("--label", required=True)
    pnew.add_argument("--grant", action="append", default=None,
                      help="method to grant (repeatable); default = read + task.submit/cancel")
    pnew.add_argument("--ttl", type=int, default=600)
    prg.add_parser("list")
    prg.add_parser("revoke").add_argument("device_id")
    pred = prg.add_parser("redeem")
    pred.add_argument("token")
    pred.add_argument("--label", default=None)

    args = ap.parse_args(argv)
    if args.group == "instance":
        # Registry discovery/start are FILE-based by design — they must work
        # with no daemon running, so they never require the socket.
        import instance_registry
        if args.cmd == "start":
            out = instance_registry.start_instance(args.agent_id,
                                                   wait_s=args.wait)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out.get("ok") else 1
        if args.cmd == "attach":
            out = instance_registry.attach(args.agent_id)
            if not out.get("ok"):
                print(json.dumps(out, ensure_ascii=False), file=sys.stderr)
                return 1
            if args.print:
                print(" ".join(out["argv"]))
                return 0
            import os as _os
            _os.execvp(out["argv"][0], out["argv"])  # hand the tty to tmux
            return 0
        if args.cmd == "open":
            import terminal_open
            out = terminal_open.open_instance(args.agent_id, window=args.window)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out.get("ok") else 1
        print(json.dumps({"instances": instance_registry.list_instances()},
                         ensure_ascii=False, indent=1))
        return 0
    if args.group == "pair":
        from device_store import DeviceStore  # noqa: PLC0415
        if args.cmd == "redeem":
            # New device: present the pairing token as the bearer, exchange it
            # for a long-term credential over WSS. Needs SUTANDO_SCP_WSS_URL.
            if not _wss_url():
                print(json.dumps({"error": "set SUTANDO_SCP_WSS_URL to the Server "
                                  "endpoint (ws://host:port/scp)"}), file=sys.stderr)
                return 1
            os.environ["SUTANDO_SCP_WSS_TOKEN"] = args.token
            res = _rpc_wss("pair.redeem",
                           {"label": args.label} if args.label else {}, timeout=15)
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0
        store = DeviceStore(_auth_dir())  # owner-local
        if args.cmd == "new":
            tok = store.mint_pairing(args.label, grants=args.grant, ttl_s=args.ttl)
            endpoint = _wss_url() or "ws://<server-lan-ip>:8787/scp"
            print(json.dumps({
                "pairing_token": tok, "label": args.label,
                "endpoint": endpoint, "ttl_s": args.ttl,
                "redeem_with": f"SUTANDO_SCP_WSS_URL={endpoint} "
                               f"sutando pair redeem {tok}"},
                ensure_ascii=False, indent=1))
            return 0
        if args.cmd == "list":
            print(json.dumps({"devices": store.list_devices()},
                             ensure_ascii=False, indent=1))
            return 0
        if args.cmd == "revoke":
            print(json.dumps({"revoked": store.revoke(args.device_id)}))
            return 0
    try:
        if args.group == "approval":
            result = _rpc("approval.request", {
                "taskId": args.task_id, "action": args.action,
                "resource": _jarg(args.resource), "input": _jarg(args.input),
                "reason": args.reason,
                "expiresInS": args.expires_in}, timeout=15)
        elif args.group == "elicitation":
            result = _rpc("elicitation.request", {
                "taskId": args.task_id, "question": args.question,
                "type": args.type, "options": _jarg(args.options),
                "expiresInS": args.expires_in}, timeout=15)
        elif args.group == "capability":
            if args.cmd == "list":
                result = _rpc("capability.list", {}, timeout=15)
            elif args.cmd == "read":
                params = {
                    "capabilityId": args.capability,
                    "operation": args.operation,
                    "resource": _jarg(args.resource),
                    "cursor": _jarg(args.cursor),
                    "limit": args.limit,
                }
                result = _rpc("capability.read",
                              {key: value for key, value in params.items()
                               if value is not None}, timeout=15)
            else:
                result = _rpc("capability.execute", {
                    "taskId": args.task_id, "action": args.action,
                    "resource": _jarg(args.resource), "input": _jarg(args.input),
                    "idempotencyKey": args.idempotency_key,
                    "approvalRequestId": args.approval}, timeout=60)
        elif args.group == "agent":
            result = (_rpc("agent.list", {}, timeout=15) if args.cmd == "list"
                      else _rpc("agent.status", {"agentId": args.agent_id},
                                timeout=15))
        elif args.group == "sutando":
            params = {}
            method = f"sutando.{args.cmd}"
            if args.cmd == "stand":
                if getattr(args, "details", False):
                    params["details"] = True
                if args.sub == "resolve":
                    if len(getattr(args, "extra", []) or []) != 2:
                        print("usage: sutando stand resolve <provider> <subject>",
                              file=sys.stderr)
                        return 2
                    method = "sutando.resolve"
                    params = {"provider": args.extra[0],
                              "subject": args.extra[1]}
            result = _rpc(method, params, timeout=15)
            if args.cmd == "stand" and not args.as_json:
                if args.sub == "resolve":
                    return _print_resolve(result)
                return _print_stand_card(result, args.sub)
            if args.cmd == "stand" and args.sub == "resolve"                     and not result.get("resolved"):
                print(json.dumps(result, ensure_ascii=False, indent=1))
                return 1
        elif args.group == "runtime":
            result = _rpc(f"runtime.{args.cmd}", {}, timeout=15)
        elif args.group == "human-action":
            if args.cmd == "request":
                result = _rpc("human_action.request", {
                    "taskId": args.task_id, "action": args.action,
                    "instructions": args.instructions,
                    "deadline": args.deadline,
                    "expiresInS": args.expires_in}, timeout=15)
            else:
                params = {"requestId": args.request_id}
                if getattr(args, "note", None):
                    params["note"] = args.note
                result = _rpc(f"human_action.{args.cmd}", params, timeout=15)
        elif args.group == "task":
            if args.cmd == "watch":
                return _watch(activity=args.activity, raw=args.raw)
            if args.cmd == "chat":
                if args.raw:
                    return _raw_tmux()
                return _chat(activity=args.activity, verbose=args.verbose,
                             full=args.full)
            if args.cmd == "list":
                result = _rpc("task.list", {}, timeout=15)
            elif args.cmd == "results":
                result = _rpc("task.list_results", {}, timeout=15)
            elif args.cmd == "submit":
                result = _rpc("task.submit", {"task": args.text,
                                              "priority": args.priority},
                              timeout=15)
            else:
                params = {}
                if getattr(args, "task_id", None):
                    params["taskId"] = args.task_id
                result = _rpc(f"task.{args.cmd.replace('-', '_')}",
                              params, timeout=15)
        elif args.group == "request" and args.cmd == "list":
            result = _rpc("request.list", {}, timeout=15)
        else:
            method = f"request.{args.cmd}"
            params = {"requestId": args.request_id}
            timeout = 15.0
            if args.cmd == "wait":
                params["timeoutS"] = args.timeout
                timeout = args.timeout + 10
            result = _rpc(method, params, timeout=timeout)
    except (OSError, RuntimeError, ValueError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
