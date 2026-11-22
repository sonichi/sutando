#!/usr/bin/env python3
"""A stand-in for `codex app-server` (stdio JSON-RPC): just initialize, hooks/list and
config/value/write, with trust kept in $CODEX_HOME/fake-trust.json and every write logged to
$CODEX_HOME/writes.jsonl. FAKE_FOREIGN=1 adds a hook that is not ours; FAKE_MODE=exit quits at
once, =stuck never trusts anything, =error refuses hooks/list, =partial lists two hooks."""
import hashlib
import json
import os
import re
import sys

home = os.environ["CODEX_HOME"]
trust_file = os.path.join(home, "fake-trust.json")
args = sys.argv[1:]
if args[:1] == ["app-server"]:
    args = args[1:]
if os.environ.get("FAKE_MODE") == "exit":
    sys.exit(0)

hooks = []
for i, a in enumerate(args):
    m = re.match(r"hooks\.(\w+)=.*command=\"((?:[^\"\\]|\\.)*)\"", a)
    if m and args[i - 1] == "-c":
        ev = re.sub(r"(?<!^)([A-Z])", r"_\1", m.group(1)).lower()
        hooks.append({"key": f"/<session-flags>/config.toml:{ev}:0:0", "eventName": ev,
                      "command": m.group(2).replace('\\"', '"').replace("\\\\", "\\"),
                      "source": "sessionFlags"})
if os.environ.get("FAKE_FOREIGN") == "1":
    hooks.append({"key": "/u/hooks.json:stop:0:0", "eventName": "stop", "command": "evil", "source": "user"})


def trust():
    try:
        with open(trust_file) as f:
            return json.load(f)
    except OSError:
        return {}


def listed():
    t = trust()
    out = []
    for h in hooks:
        cur = "sha256:" + hashlib.sha256(h["command"].encode()).hexdigest()
        rec = t.get(h["key"], {}).get("trusted_hash")
        status = "untrusted" if rec is None else ("trusted" if rec == cur else "modified")
        if os.environ.get("FAKE_MODE") == "stuck":
            status = "untrusted"
        out.append({**h, "currentHash": cur, "trustStatus": status})
    return {"data": [{"cwd": "x", "hooks": out, "warnings": [], "errors": []}]}


for line in sys.stdin:
    msg = json.loads(line)
    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if rid is None:
        continue
    if method == "initialize":
        result = {}
    elif method == "hooks/list" and os.environ.get("FAKE_MODE") == "error":
        print(json.dumps({"id": rid, "error": {"message": "hooks unavailable"}}), flush=True)
        continue
    elif method == "hooks/list":
        result = listed()
        if os.environ.get("FAKE_MODE") == "partial":
            result["data"][0]["hooks"] = result["data"][0]["hooks"][:2]
    elif method == "config/value/write":
        with open(os.path.join(home, "writes.jsonl"), "a") as f:
            f.write(json.dumps(params) + "\n")
        t = trust()
        t.update(params["value"])
        with open(trust_file, "w") as f:
            json.dump(t, f)
        result = {"status": "ok"}
    else:
        print(json.dumps({"id": rid, "error": {"message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"id": rid, "result": result}), flush=True)
