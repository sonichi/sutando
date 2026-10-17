#!/usr/bin/env python3
"""room-ops · post_command — print the shell command a remote agent runs to post a message.

For an agent that runs somewhere this skill is not installed (a cloud sandbox), this prints one
line: a `python3 ... | curl` pipeline that reads the answer from `--reply-file`, JSON-encodes
it, and posts it as `op:message` to the gateway, with the relay token taken from the remote
environment variable named by `--token-env`. It never reads, takes or prints a token value.

Every id is checked against a strict pattern before it is printed inside single quotes, so no
quote, backslash or whitespace can reach the shell; any invalid argument exits 2 and prints
nothing. Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys

ROOM_RE = re.compile(r"![A-Za-z0-9._=~-]{1,200}:[A-Za-z0-9.-]{1,200}(?::[0-9]{1,5})?")
EVENT_RE = re.compile(r"\$[A-Za-z0-9._~+/=-]{1,200}(?::[A-Za-z0-9.-]{1,200}(?::[0-9]{1,5})?)?")
GATEWAY_RE = re.compile(r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*")
ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
DEDUPE_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,200}")


def build(gateway, room, token_env, dedupe_key, reply_file, thread_root=None, reply_to=None):
    """The command line, or ValueError naming the first argument that fails its pattern."""
    gateway = (gateway or "").rstrip("/")
    checks = [("--gateway-url", gateway, GATEWAY_RE), ("--room", room, ROOM_RE),
              ("--token-env", token_env, ENV_NAME_RE), ("--dedupe-key", dedupe_key, DEDUPE_RE),
              ("--reply-file", reply_file, PATH_RE)]
    if thread_root:
        checks.append(("--thread-root", thread_root, EVENT_RE))
    if reply_to:
        checks.append(("--reply-to", reply_to, EVENT_RE))
    for flag, value, pattern in checks:
        if not pattern.fullmatch(value or ""):
            raise ValueError("%s is not a valid value" % flag)
    fields = [("op", "message"), ("room_id", room)]
    if thread_root:
        fields.append(("thread_root", thread_root))
    if reply_to:
        fields.append(("reply_to", reply_to))
    fields.append(("dedupe_key", dedupe_key))
    literal = ", ".join('"%s": "%s"' % kv for kv in fields)
    return ("python3 -c 'import json, sys; print(json.dumps({%s, \"body\": sys.stdin.read()}))' < %s"
            " | curl -sS --fail-with-body -X POST \"%s/v1/room\" -H \"Authorization: Bearer $%s\""
            " -H \"Content-Type: application/json\" --data-binary @-"
            % (literal, reply_file, gateway, token_env))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway-url", required=True, help="gateway base URL, https")
    ap.add_argument("--room", required=True, help="room id, !opaque:server")
    ap.add_argument("--thread-root", default="", help="event id of the thread root, $...")
    ap.add_argument("--reply-to", default="", help="event id the post replies to, $...")
    ap.add_argument("--dedupe-key", required=True, help="fixed per message, so a repeat is recognisable")
    ap.add_argument("--token-env", default="AG2_RELAY_TOKEN",
                    help="NAME of the remote environment variable holding the relay token")
    ap.add_argument("--reply-file", default="/tmp/ag2-reply.md", help="absolute path the answer is read from")
    args = ap.parse_args(argv)
    try:
        line = build(args.gateway_url, args.room, args.token_env, args.dedupe_key, args.reply_file,
                     args.thread_root, args.reply_to)
    except ValueError as e:
        sys.stderr.write("post_command: %s\n" % e)
        return 2
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
