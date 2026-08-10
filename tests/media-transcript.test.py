#!/usr/bin/env python3
"""Tests for skills/media-transcript — hermetic, NO network.

The end-to-end path is exercised against a STUB yt-dlp placed first on PATH
(it writes a canned VTT into the -o target dir), so orchestration, language
preference, dedupe parsing and JSON mode all run without touching the network.
The parser is additionally unit-tested against a rolling-caption VTT (the
auto-caption shape that triples text when joined naively).

Run: python3 tests/media-transcript.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "media_transcript", REPO / "skills" / "media-transcript" / "scripts" / "transcript.py")
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def run_main(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    old_env = dict(os.environ)
    if env is not None:
        os.environ.clear()
        os.environ.update(env)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = mt.main(argv)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# parse_vtt — rolling-caption dedupe (the auto-caption shape)
# ---------------------------------------------------------------------------
ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
in the future machines will think

00:00:03.000 --> 00:00:05.000
in the future machines will think
perhaps in ten or fifteen years

00:00:05.000 --> 00:00:07.000
perhaps in ten or fifteen years
one hundred million users
"""

flat = mt.parse_vtt(ROLLING_VTT)
check("vtt: rolling windows deduped",
      flat.splitlines() == ["in the future machines will think",
                            "perhaps in ten or fifteen years",
                            "one hundred million users"], repr(flat))
ts = mt.parse_vtt(ROLLING_VTT, timestamps=True)
check("vtt: timestamps mode prefixes cue starts", ts.startswith("[00:01] "), repr(ts[:40]))
check("vtt: later fresh line timestamped", "[00:03] perhaps" in ts or "perhaps" in ts, repr(ts))

TAGGED_VTT = """WEBVTT

00:01:02.000 --> 00:01:04.000
<c.colorE5E5E5>dinosaurs</c> first appeared &amp; thrived
"""

# A line already emitted two cues ago resurfaces after an intervening cue —
# the per-line output-tail check must drop it without losing its neighbors.
RESURFACE_VTT = """WEBVTT

00:00:01.000 --> 00:00:02.000
alpha line
beta line

00:00:02.000 --> 00:00:03.000
alpha line

00:00:03.000 --> 00:00:04.000
beta line
gamma line
"""
check("vtt: line resurfacing across cues deduped against output tail",
      mt.parse_vtt(RESURFACE_VTT).splitlines() == ["alpha line", "beta line", "gamma line"],
      repr(mt.parse_vtt(RESURFACE_VTT)))

NUMBERED_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:02.000
first cue

2
00:00:02.000 --> 00:00:03.000
second cue
"""
check("vtt: numeric cue-sequence lines skipped",
      mt.parse_vtt(NUMBERED_VTT).splitlines() == ["first cue", "second cue"],
      repr(mt.parse_vtt(NUMBERED_VTT)))
check("vtt: tags and entities stripped",
      mt.parse_vtt(TAGGED_VTT) == "dinosaurs first appeared & thrived",
      repr(mt.parse_vtt(TAGGED_VTT)))
check("vtt: hour-long cue formats h:mm:ss",
      mt.parse_vtt("WEBVTT\n\n01:02:03.000 --> 01:02:04.000\nhello\n",
                   timestamps=True) == "[1:02:03] hello",
      repr(mt.parse_vtt("WEBVTT\n\n01:02:03.000 --> 01:02:04.000\nhello\n", timestamps=True)))

# ---------------------------------------------------------------------------
# end-to-end with a STUB yt-dlp on PATH (no network)
# ---------------------------------------------------------------------------
def make_stub_bin(vtt_body: str, fail: bool = False, no_output: bool = False,
                  args_log: Optional[str] = None) -> str:
    d = tempfile.mkdtemp(prefix="mt-stub-")
    stub = Path(d) / "yt-dlp"
    log_line = f"printf '%s\\n' \"$@\" > {args_log}\n" if args_log else ""
    if fail:
        stub.write_text(f"#!/bin/bash\n{log_line}echo 'ERROR: Video unavailable' >&2\nexit 1\n")
    elif no_output:
        stub.write_text(f"#!/bin/bash\n{log_line}exit 0\n")
    else:
        # find the value after -o, write the vtt next to it
        stub.write_text(
            "#!/bin/bash\n"
            f"{log_line}"
            "out=''\n"
            "prev=''\n"
            "for a in \"$@\"; do if [ \"$prev\" = '-o' ]; then out=\"$a\"; fi; prev=\"$a\"; done\n"
            "dir=$(dirname \"$out\")\n"
            f"cat > \"$dir/sub.en.vtt\" << 'VTT'\n{vtt_body}\nVTT\n"
            "exit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return d


BASE_ENV = {k: v for k, v in os.environ.items() if k in ("HOME", "TMPDIR", "LANG")}

stub_dir = make_stub_bin(ROLLING_VTT)
rc, out, err = run_main(["https://youtube.com/watch?v=fake"],
                        env={**BASE_ENV, "PATH": f"{stub_dir}:/usr/bin:/bin"})
check("e2e: stub yt-dlp → transcript printed, exit 0",
      rc == 0 and "machines will think" in out, f"rc={rc} err={err.strip()[:120]}")

rc, out, err = run_main(["https://youtube.com/watch?v=fake", "--json"],
                        env={**BASE_ENV, "PATH": f"{stub_dir}:/usr/bin:/bin"})
check("e2e: --json shape", rc == 0 and json.loads(out.strip())["ok"] is True
      and "machines" in json.loads(out.strip())["text"], out[:120])

rc, out, err = run_main(["https://youtube.com/watch?v=fake", "--timestamps"],
                        env={**BASE_ENV, "PATH": f"{stub_dir}:/usr/bin:/bin"})
check("e2e: --timestamps flag threads through to cue markers",
      rc == 0 and out.startswith("[00:01] "), f"rc={rc} out={out[:40]!r}")

# non-en language: the sub-langs request must be exact codes with the en
# fallback pair appended (the 429 fix — never a glob)
langs_log = str(Path(tempfile.mkdtemp(prefix="mt-log-")) / "args.txt")
stub_de = make_stub_bin(ROLLING_VTT, args_log=langs_log)
rc, out, err = run_main(["https://youtube.com/watch?v=fake", "--lang", "de"],
                        env={**BASE_ENV, "PATH": f"{stub_de}:/usr/bin:/bin"})
logged = Path(langs_log).read_text().splitlines()
sub_langs = logged[logged.index("--sub-langs") + 1] if "--sub-langs" in logged else ""
check("e2e: --lang de requests de,de-orig,en,en-orig exactly",
      rc == 0 and sub_langs == "de,de-orig,en,en-orig", f"rc={rc} sub_langs={sub_langs!r}")

stub_fail = make_stub_bin("", fail=True)
rc, out, err = run_main(["https://youtube.com/watch?v=gone"],
                        env={**BASE_ENV, "PATH": f"{stub_fail}:/usr/bin:/bin"})
check("e2e: extractor failure → exit 1 with its message",
      rc == 1 and "Video unavailable" in err, f"rc={rc} err={err.strip()[:120]}")

rc, out, err = run_main(["https://youtube.com/watch?v=gone", "--json"],
                        env={**BASE_ENV, "PATH": f"{stub_fail}:/usr/bin:/bin"})
check("e2e: failure in --json mode → ok:false with the error",
      rc == 1 and json.loads(out.strip())["ok"] is False
      and "Video unavailable" in json.loads(out.strip())["error"], out[:120])

stub_none = make_stub_bin("", no_output=True)
rc, out, err = run_main(["https://youtube.com/watch?v=nocaps"],
                        env={**BASE_ENV, "PATH": f"{stub_none}:/usr/bin:/bin"})
check("e2e: no caption track produced → 'no captions' error, exit 1",
      rc == 1 and "no captions available" in err, f"rc={rc} err={err.strip()[:120]}")

stub_empty = make_stub_bin("WEBVTT\nKind: captions\n")
rc, out, err = run_main(["https://youtube.com/watch?v=empty"],
                        env={**BASE_ENV, "PATH": f"{stub_empty}:/usr/bin:/bin"})
check("e2e: caption track empty after parsing → exit 1",
      rc == 1 and "empty after parsing" in err, f"rc={rc} err={err.strip()[:120]}")

# no PATH binary but the python module IS importable → module-invocation fallback
import types
sys.modules["yt_dlp"] = types.ModuleType("yt_dlp")
real_which = mt.shutil.which
mt.shutil.which = lambda name: None
try:
    resolved = mt._resolve_ytdlp()
finally:
    mt.shutil.which = real_which
    del sys.modules["yt_dlp"]
check("resolver: PATH miss + importable module → python -m yt_dlp",
      resolved == [sys.executable, "-m", "yt_dlp"], repr(resolved))

# yt-dlp entirely absent (PATH empty and module import blocked)
import builtins
real_import = builtins.__import__
def _no_ytdlp(name, *a, **k):
    if name == "yt_dlp":
        raise ImportError("blocked for test")
    return real_import(name, *a, **k)
builtins.__import__ = _no_ytdlp
try:
    rc, out, err = run_main(["https://youtube.com/watch?v=x"],
                            env={**BASE_ENV, "PATH": tempfile.mkdtemp(prefix="mt-empty-")})
finally:
    builtins.__import__ = real_import
check("no yt-dlp anywhere → actionable install error, exit 1",
      rc == 1 and "yt-dlp not found" in err and "brew install" in err, err.strip()[:140])

# ---------------------------------------------------------------------------
# invocation + pointer paths
# ---------------------------------------------------------------------------
rc, _, err = run_main([])
check("no args → usage, exit 2", rc == 2 and "usage:" in err)

rc, _, err = run_main(["--lang"])
check("--lang without value → exit 2", rc == 2)

local = Path(tempfile.mkdtemp(prefix="mt-local-")) / "clip.mp4"
local.write_bytes(b"\x00")
rc, out, err = run_main([str(local)])
check("local media file → audio-transcribe pointer, exit 3",
      rc == 3 and "audio-transcribe" in err, f"rc={rc} err={err.strip()[:100]}")
rc, out, _ = run_main([str(local), "--json"])
check("local media pointer in --json mode",
      rc == 3 and json.loads(out.strip())["ok"] is False)

# ---------------------------------------------------------------------------
# CR regressions (qingyun 2026-07-31): option injection + subtitle ranking
# ---------------------------------------------------------------------------
# 1. Option-shaped / non-URL targets are refused BEFORE yt-dlp is invoked.
_logd = tempfile.mkdtemp(prefix="mt-args-")
_arglog = f"{_logd}/args.txt"
_stub_log = make_stub_bin(ROLLING_VTT, args_log=_arglog)
for bad in ("--exec=touch /tmp/pwned", "ftp://example.com/x", "watch this video"):
    rc, out, err = run_main([bad], env={**BASE_ENV, "PATH": f"{_stub_log}:/usr/bin:/bin"})
    check(f"non-URL target refused ({bad[:12]!r}...): exit 2, yt-dlp never ran",
          rc == 2 and "http(s) URL" in err and not os.path.exists(_arglog),
          f"rc={rc} err={err.strip()[:80]} ran={os.path.exists(_arglog)}")
rc, out, _ = run_main(["--exec=x", "--json"],
                      env={**BASE_ENV, "PATH": f"{_stub_log}:/usr/bin:/bin"})
check("non-URL target --json shape", rc == 2 and json.loads(out.strip())["ok"] is False)

# 2. The real invocation passes `--` immediately before the URL (second wall).
rc, out, err = run_main(["https://youtube.com/watch?v=fake"],
                        env={**BASE_ENV, "PATH": f"{_stub_log}:/usr/bin:/bin"})
_argv = Path(_arglog).read_text().splitlines() if os.path.exists(_arglog) else []
check("yt-dlp argv ends with `--` then the URL",
      rc == 0 and _argv[-2:] == ["--", "https://youtube.com/watch?v=fake"],
      f"rc={rc} tail={_argv[-3:]}")

# 3. Two-track regression: uploader subtitles (sub.en.vtt) beat auto-captions
# (sub.en-orig.vtt) — lexicographic sort alone puts en-orig FIRST ('-' < '.'),
# which is exactly the bug.
MANUAL_VTT = ROLLING_VTT.replace("in the future machines will think",
                                 "uploader subtitle track wins")
AUTO_VTT = ROLLING_VTT.replace("in the future machines will think",
                               "auto caption track must lose")
_d2 = tempfile.mkdtemp(prefix="mt-stub2-")
_stub2 = Path(_d2) / "yt-dlp"
_stub2.write_text(
    "#!/bin/bash\n"
    "out=''\nprev=''\n"
    "for a in \"$@\"; do if [ \"$prev\" = '-o' ]; then out=\"$a\"; fi; prev=\"$a\"; done\n"
    "dir=$(dirname \"$out\")\n"
    f"cat > \"$dir/sub.en.vtt\" << 'VTT'\n{MANUAL_VTT}\nVTT\n"
    f"cat > \"$dir/sub.en-orig.vtt\" << 'VTT'\n{AUTO_VTT}\nVTT\n"
    "exit 0\n")
_stub2.chmod(_stub2.stat().st_mode | stat.S_IEXEC)
rc, out, err = run_main(["https://youtube.com/watch?v=fake"],
                        env={**BASE_ENV, "PATH": f"{_d2}:/usr/bin:/bin"})
check("two tracks: uploader subtitles win over auto-captions",
      rc == 0 and "uploader subtitle track wins" in out
      and "auto caption track must lose" not in out,
      f"rc={rc} out={out.strip()[:80]}")

# Regression guard (qingyun CR on #2435): this skill's Python must use
# typing.Optional/Union, not PEP-604 `X | Y` unions — CONTRIBUTING.md's 3.9
# source convention. The repo's python39-compat gate can't catch this: it is
# compile-only (PEP-604 parses fine under `from __future__ import annotations`)
# and scans only src/, not skills/. Scoped to THIS skill on purpose — a
# repo-wide skills/ scan would flag 65 pre-existing usages across 17 other
# skills, which is a separate cleanup, not this feature's concern.
def _pep604_union_lines(path: Path) -> list[int]:
    def _has_bitor(ann) -> bool:
        return ann is not None and any(
            isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
            for n in ast.walk(ann))

    hits: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            anns.append(node.returns)
            anns += [a.annotation for a in (*node.args.posonlyargs,
                     *node.args.args, *node.args.kwonlyargs)]
        elif isinstance(node, ast.AnnAssign):
            anns.append(node.annotation)
        if any(_has_bitor(a) for a in anns):
            hits.append(node.lineno)
    return hits


_guard_targets = sorted(
    (REPO / "skills" / "media-transcript" / "scripts").glob("*.py")) + [Path(__file__)]
_pep604_bad = {str(p.relative_to(REPO)): ls
               for p in _guard_targets if (ls := _pep604_union_lines(p))}
check("no PEP-604 (X | Y) unions in skill + test (use Optional/Union — CONTRIBUTING 3.9 floor)",
      not _pep604_bad, f"found at {_pep604_bad}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All media-transcript checks passed.")
