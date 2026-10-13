"""Offline `gh` stand-in for review-pr.sh tests: one PR, served from a JSON spec.

spec = {"url", "sha", "diff", "files": {path: text | raw contents-API dict}}.
`answer(spec, argv)` is the in-process form; `install(bindir, spec)` puts an
executable `gh` on a PATH dir that logs its argv and delegates to `answer`.
"""
import base64
import json
import urllib.parse
from pathlib import Path

GH = """#!/usr/bin/env python3
import json, sys
sys.path.insert(0, {here!r})
from review_pr_fixture import answer
with open({log!r}, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
out = answer(json.load(open({spec!r})), sys.argv[1:])
if out is None:
    sys.exit(1)
sys.stdout.write(out)
"""


def answer(spec: dict, args: list):
    """What `gh <args>` prints for this PR, or None when it would fail."""
    if args[:2] == ["pr", "diff"]:
        return spec["diff"]
    if args[:2] == ["pr", "view"]:
        if args[args.index("--json") + 1] == "url":
            return spec["url"] + "\n"
        return json.dumps({"url": spec["url"], "headRefOid": spec["sha"],
                           "files": [{"path": p} for p in spec["files"]]})
    if args[:1] == ["api"]:
        path, _, query = args[1].partition("?")
        item = spec["files"].get(urllib.parse.unquote(path.split("/contents/", 1)[1]))
        if item is None or query != "ref=" + spec["sha"]:
            return None
        if isinstance(item, dict):
            return json.dumps(item)
        return json.dumps({"type": "file", "encoding": "base64",
                           "content": base64.b64encode(item.encode()).decode()})
    return None


def install(bindir: Path, spec: dict) -> Path:
    """Write `gh` into `bindir`; returns the argv log path."""
    bindir.mkdir(parents=True, exist_ok=True)
    spec_path = bindir / "gh-spec.json"
    spec_path.write_text(json.dumps(spec))
    log = bindir / "gh-argv.log"
    gh = bindir / "gh"
    gh.write_text(GH.format(here=str(Path(__file__).parent),
                            log=str(log), spec=str(spec_path)))
    gh.chmod(0o755)
    return log
