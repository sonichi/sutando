#!/usr/bin/env python3
"""public-write-privacy-guard — PreToolUse[Bash] hook that refuses a `gh` write
to a non-private repo when the text it would publish names something private.

Covered writes: `gh pr create|edit|comment|review|merge|close`, `gh issue
create|comment|edit|close`, `gh release create|edit`, `gh gist create --public`,
`gh repo create --public|edit`, and `gh api` POST/PATCH/PUT to any `repos/<o>/<r>/…`
endpoint, `gists`, or a GraphQL mutation. Scanned: text flags (long, `=`, attached
and clustered short forms), their -file forms, `$(cat FILE)` values, heredoc and
here-string stdin, `gh api` fields, and gh run inside wrappers, compound commands,
substitutions, `bash -c`, `eval` and shell stdin scripts.
What counts as private is src/public_write_privacy.py; this hook only parses.

Fails CLOSED: a body it cannot read (missing file, stdin it cannot see, a value
or unquoted heredoc that expands at run time, a file the same command writes, a
line that does not lex, a crash or unreadable hook input) is refused. With no
denylist it still checks the built-in shapes and warns on stderr. The escape for
a name already public is the allowlist file, not an environment variable.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_shell_scan = None
policy = None


def _load_modules():
    """Imported lazily so a Bash call with no `gh` pays nothing, and an import
    failure lands inside main()'s fail-closed handler."""
    global _shell_scan, policy
    if policy is None:
        for d in (str(HERE), str(HERE.parent / "src")):
            if d not in sys.path:
                sys.path.insert(0, d)
        import _shell_scan as scan_mod
        import public_write_privacy as policy_mod
        _shell_scan, policy = scan_mod, policy_mod


TAG = "[public-write-privacy-guard]"
SEEDER = "python3 scripts/seed-public-denylist.py"


class Spec:
    """One publishing gh verb: which flags carry text, which name a file, and
    which short flags take a value (so a cluster like `-dbTEXT` splits right)."""

    def __init__(self, texts, files=(), shorts=None, when=None, positional_files=False):
        self.texts = set(texts)
        self.files = set(files)
        self.shorts = dict(shorts or {})
        self.when = when                    # a long flag that must be present, or None
        self.positional_files = positional_files


_COMMON = {"R": "--repo", "b": "--body", "F": "--body-file", "t": "--title"}
VERBS = {
    ("pr", "create"): Spec({"--body", "--title"}, {"--body-file"}, {**_COMMON, "B": "--base", "H": "--head",
                           "a": "--assignee", "l": "--label", "m": "--milestone", "p": "--project",
                           "r": "--reviewer", "T": "--template"}),
    ("pr", "edit"): Spec({"--body", "--title"}, {"--body-file"}, {**_COMMON, "B": "--base"}),
    ("pr", "comment"): Spec({"--body"}, {"--body-file"}, _COMMON),
    ("pr", "review"): Spec({"--body"}, {"--body-file"}, _COMMON),
    ("pr", "merge"): Spec({"--body", "--subject"}, {"--body-file"},
                          {"R": "--repo", "b": "--body", "F": "--body-file", "t": "--subject",
                           "A": "--author-email"}),
    ("pr", "close"): Spec({"--comment"}, (), {"R": "--repo", "c": "--comment"}),
    ("issue", "create"): Spec({"--body", "--title"}, {"--body-file"}, {**_COMMON, "a": "--assignee",
                              "l": "--label", "m": "--milestone", "p": "--project", "T": "--template"}),
    ("issue", "comment"): Spec({"--body"}, {"--body-file"}, _COMMON),
    ("issue", "edit"): Spec({"--body", "--title"}, {"--body-file"}, {**_COMMON, "m": "--milestone"}),
    ("issue", "close"): Spec({"--comment"}, (), {"R": "--repo", "c": "--comment", "r": "--reason"}),
    ("release", "create"): Spec({"--notes", "--title"}, {"--notes-file"},
                                {"R": "--repo", "n": "--notes", "F": "--notes-file", "t": "--title"}),
    ("release", "edit"): Spec({"--notes", "--title"}, {"--notes-file"},
                              {"R": "--repo", "n": "--notes", "F": "--notes-file", "t": "--title"}),
    ("gist", "create"): Spec({"--desc"}, (), {"d": "--desc", "f": "--filename"}, when="--public",
                             positional_files=True),
    ("repo", "edit"): Spec({"--description", "--homepage"}, (), {"d": "--description", "h": "--homepage"}),
    ("repo", "create"): Spec({"--description", "--homepage"}, (), {"d": "--description", "h": "--homepage",
                             "g": "--gitignore", "l": "--license", "p": "--template", "t": "--team"},
                             when="--public"),
}
FLAG_ALIASES = {"-p": "--public"}       # gist create's bool short for --public
REPO_FLAGS = {"-R", "--repo"}
API_SHORTS = {"X": "--method", "f": "--raw-field", "F": "--field", "H": "--header", "q": "--jq",
              "t": "--template", "p": "--preview"}
API_VALUE_LONGS = {"--method", "--raw-field", "--field", "--header", "--input", "--jq", "--template",
                   "--hostname", "--cache", "--preview"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# Shell words that precede a command without being it: `if gh …`, `! gh …`, `{ gh …; }`.
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}", "coproc"}
# Wrapper -> (flags that take a separate value, positional args before the command).
WRAPPERS = {
    "env": ({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}, 0),
    "command": (set(), 0), "builtin": (set(), 0), "exec": ({"-a"}, 0),
    "nohup": (set(), 0), "setsid": (set(), 0), "chronic": (set(), 0), "unbuffer": (set(), 0),
    "time": ({"-f", "--format", "-o", "--output"}, 0),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "nice": ({"-n", "--adjustment"}, 0), "ionice": ({"-c", "-n", "-p"}, 0),
    "stdbuf": ({"-i", "-o", "-e"}, 0), "caffeinate": ({"-t", "-w"}, 0),
    "sudo": ({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}, 0), "doas": ({"-u"}, 0),
}
GH_URL = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:pull|issues)/\d+")
API_REPO = re.compile(r"^(?:https://api\.github\.com)?/?repos/([^/\s?]+/[^/\s?]+)(?:/([^/\s?]+))?")
API_GIST = re.compile(r"^(?:https://api\.github\.com)?/?gists(?:/|$|\?)")
HEREDOC_CAT = re.compile(r"""^"?\$\(\s*cat\s*<<-?\s*(['"]?)[A-Za-z_][A-Za-z0-9_]*\1\s*\)"?$""")
WHOLE_CAT = re.compile(r"""^"?\$\(\s*(?:cat\s+|<\s*)(['"]?)([^'"\s$`()]+)\1\s*\)"?$""")
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
HEREDOC_WORD = re.compile(r"^<<-?(?!<)")
REDIRECT = re.compile(r"^(?:\d|&)?>>?(.*)$")
EXPANSION = re.compile(r"(?<!\\)(?:\$[\w{(@*#?!$-]|`)")
GH_WRITE_HINT = re.compile(r"\bgh\b[\s\S]*\b(?:pr|issue|release|api|gist|repo)\b", re.I)
# Programs that only read the files they name; any other program naming the body
# file in the same command may be writing it.
READERS = {"cat", "head", "tail", "less", "more", "wc", "grep", "rg", "ugrep", "diff", "ls", "stat",
           "test", "[", "file", "echo", "printf", "md5", "md5sum", "shasum", "sha256sum", "gh", "cd",
           "true", "jq", "bat"}
COPIERS = {"cp", "mv", "ln", "install", "rsync", "tee"}


GH_VALUE_LONGS = {"--repo", "--base", "--head", "--label", "--assignee", "--reviewer", "--project",
                  "--milestone", "--template", "--reason", "--target", "--add-label", "--remove-label",
                  "--add-reviewer", "--remove-reviewer", "--add-assignee", "--remove-assignee",
                  "--add-project", "--remove-project", "--discussion-category", "--author-email",
                  "--match-head-commit", "--add-topic", "--remove-topic", "--default-branch",
                  "--visibility", "--source", "--remote", "--team", "--gitignore", "--license",
                  "--json", "--jq", "--filename", "--duplicate-of"}


class Unreadable(Exception):
    pass


class Body:
    def __init__(self, text, expands):
        self.text = text
        self.expands = expands      # bash rewrites it ($, backticks) before anyone reads it


def split_heredocs(command: str):
    """(command without heredoc bodies, [Body]). A body can hold an unbalanced
    quote that would make the whole line unlexable, so it is lifted out first."""
    lines = command.split("\n")
    kept, bodies, pending, current = [], [], [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if pending:
            strip_tabs, tag, quoted = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == tag:
                pending.pop(0)
                text = "\n".join(current)
                bodies.append(Body(text, not quoted and bool(EXPANSION.search(text))))
                current = []
            else:
                current.append(line)
            continue
        kept.append(line)
        for m in HEREDOC.finditer(line.replace("<<<", "   ")):
            pending.append((m.group(1) == "-", m.group(3), bool(m.group(2))))
    if pending:
        raise Unreadable("a heredoc is never terminated")
    return "\n".join(kept), bodies


def _substitutions(s: str):
    """Text inside every `...` and $(...) bash would run, outside single quotes."""
    out, i, n, quote = [], 0, len(s), ""
    while i < n:
        c = s[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote == "'":
            quote = "" if c == "'" else quote
            i += 1
            continue
        if c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif c == "`":
            j = i + 1
            while j < n and s[j] != "`":
                j += 2 if s[j] == "\\" else 1
            out.append(s[i + 1:j])
            i = j + 1
            continue
        elif s.startswith("$(", i) and not s.startswith("$((", i):
            j, depth, q = i + 2, 1, ""
            while j < n and depth:
                d = s[j]
                if d == "\\" and q != "'":
                    j += 2
                    continue
                if q:
                    q = "" if d == q else q
                elif d in "'\"":
                    q = d
                elif d == "(":
                    depth += 1
                elif d == ")":
                    depth -= 1
                j += 1
            out.append(s[i + 2:j - 1])
            i = j
            continue
        i += 1
    return out


def _literal_or_raise(word) -> str:
    raw = word.raw
    if raw.startswith("$'"):
        return word.text
    quote, escaped = "", False
    for idx, ch in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if ch in "'\"" and (not quote or quote == ch):
            quote = "" if quote else ch
            continue
        if quote != "'" and (ch == "`" or (ch == "$" and re.match(r"[\w{(@*#?!$-]", raw[idx + 1:idx + 2] or " "))):
            raise Unreadable(f"`{raw[:60]}` expands at run time, so its text cannot be checked")
    return word.text


class Ctx:
    def __init__(self, cwd, bodies, segments, seg_bodies):
        self.cwd = cwd
        self.bodies = bodies          # every heredoc + here-string in this command
        self.segments = segments
        self.seg_bodies = seg_bodies  # the bodies each segment itself redirects in
        self.own = []                 # bodies of the segment being read


def _resolve(path_text: str, cwd):
    p = Path(os.path.expanduser(path_text))
    if not p.is_absolute():
        if cwd is None:
            raise Unreadable(f"relative path `{path_text}` after an unresolvable `cd`")
        p = Path(cwd) / p
    return Path(os.path.normpath(str(p)))


def _checked(bodies, label):
    if any(b.expands for b in bodies):
        raise Unreadable(f"{label} comes from an unquoted heredoc or here-string that bash expands "
                         "(`$`, backticks) before gh reads it; quote the delimiter (<<'EOF')")
    return [b.text for b in bodies]


def _read_file(word, ctx, label):
    text = _literal_or_raise(word)
    if text == "-":
        return _stdin(ctx, label)
    path = _resolve(text, ctx.cwd)
    writes = _writes(ctx)
    if path in writes:
        if writes[path] is None:
            raise Unreadable(f"{label} `{text}` is written by this same command; write it in a "
                             "separate step so its content can be checked")
        return _checked(writes[path], label)
    try:
        return [path.read_text(encoding="utf-8", errors="replace")]
    except OSError as e:
        raise Unreadable(f"cannot read {label} `{text}` ({e.strerror or e})")


def _stdin(ctx, label):
    bodies = ctx.own or ctx.bodies
    if not bodies:
        raise Unreadable(f"{label} reads stdin, which this guard cannot see; pass --body-file <path>")
    return _checked(bodies, label)


def _inline(word, ctx):
    if HEREDOC_CAT.match(word.raw):
        return _stdin(ctx, "the heredoc value")
    m = WHOLE_CAT.match(word.raw)
    if m:
        return _read_file(_shell_scan.Word(raw=m.group(2), text=m.group(2)), ctx, "file")
    return [_literal_or_raise(word)]


def _named(word, names):
    return any(word.basename_is(n) for n in names)


def _is_assignment(text):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", text, re.S))


def _strip_prefix(seg):
    """Drop shell keywords, assignments and wrappers (`timeout 60`, `env -u X`,
    `nice -n 5`) in front of the command that actually runs."""
    while seg:
        text = seg[0].text
        if text in KEYWORDS or _is_assignment(text):
            seg = seg[1:]
            continue
        name = os.path.basename(text)
        if name not in WRAPPERS:
            return seg
        valued, positional = WRAPPERS[name]
        i = 1
        while i < len(seg):
            a = seg[i].text
            if a == "--":
                i += 1
                break
            if a.startswith("-") and len(a) > 1:
                i += 2 if a in valued else 1
                continue
            if name == "env" and _is_assignment(a):
                i += 1
                continue
            break
        seg = seg[i + positional:]
    return seg


def _parse_args(args, shorts, valued_longs, aliases=None):
    """([(long flag, value Word|None)], {bool flags}, [positional Words]),
    splitting `--flag=value`, `-bVALUE`, `-b=VALUE` and clusters like `-dbVALUE`."""
    values, flags, positional, i = [], set(), [], 0
    while i < len(args):
        w = args[i]
        t = w.text
        if t == "--":
            positional += args[i + 1:]
            break
        if t.startswith("--"):
            name, eq, val = t.partition("=")
            if eq:
                raw = w.raw.split("=", 1)[1] if "=" in w.raw else val
                values.append((name, _shell_scan.Word(raw=raw, text=val, quoted=w.quoted)))
                i += 1
            elif name in valued_longs:
                values.append((name, args[i + 1] if i + 1 < len(args) else None))
                i += 2
            else:
                flags.add(name)
                i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            i += 1
            for j in range(1, len(t)):
                c = t[j]
                if c not in shorts:
                    flags.add((aliases or {}).get("-" + c, "-" + c))
                    continue
                rest = t[j + 1:]
                if rest:
                    raw = w.raw[j + 1:] if w.raw.startswith(t[:j + 1]) else w.raw
                    if rest.startswith("="):
                        rest, raw = rest[1:], raw[1:] if raw.startswith("=") else raw
                    values.append((shorts[c], _shell_scan.Word(raw=raw, text=rest, quoted=w.quoted)))
                else:
                    values.append((shorts[c], args[i] if i < len(args) else None))
                    i += 1
                break
            continue
        positional.append(w)
        i += 1
    return values, flags, positional


def _need(name, val):
    if val is None:
        raise Unreadable(f"`{name}` has no value")
    return val


def _gh_action(seg, ctx):
    """None, or (repo, label, [texts], cwd) for a publishing gh invocation.
    repo is None when gh would take it from the cwd's remote, "" when no repo."""
    tail, i, pre, words = seg[1:], 0, [], []
    while i < len(tail) and len(words) < 2:
        t = tail[i].text
        if t.startswith("-"):
            n = 2 if t in REPO_FLAGS else 1
            pre += tail[i:i + n]
            i += n
            continue
        words.append(t)
        i += 1
        if t == "api":
            break
    if not words:
        return None
    if words[0] == "api":
        return _api_action(pre + tail[i:], ctx)
    spec = VERBS.get(tuple(words))
    if spec is None:
        return None
    valued = set(spec.shorts.values()) | spec.texts | spec.files | GH_VALUE_LONGS
    values, flags, positional = _parse_args(pre + tail[i:], {"R": "--repo", **spec.shorts}, valued,
                                            FLAG_ALIASES if words[0] == "gist" else None)
    if spec.when and spec.when not in flags:
        return None
    repo = "" if words[0] == "gist" else None
    texts = []
    for name, val in values:
        if name == "--repo":
            repo = _literal_or_raise(_need(name, val))
        elif name in spec.texts:
            texts += _inline(_need(name, val), ctx)
        elif name in spec.files:
            texts += _read_file(_need(name, val), ctx, name)
    for w in positional:
        if spec.positional_files:
            texts += _read_file(w, ctx, "file")
            continue
        m = GH_URL.search(w.text)
        if m and not repo:
            repo = m.group(1)
        elif words[0] == "repo" and repo is None and w.text.count("/") == 1 and "{" not in w.text:
            repo = w.text
    return repo, f"gh {words[0]} {words[1]}", texts, ctx.cwd


def _api_action(args, ctx):
    values, flags, positional = _parse_args(args, API_SHORTS, API_VALUE_LONGS)
    method, fields, texts, repo = None, False, [], None
    for name, val in values:
        if name == "--method":
            method = _need(name, val).text.upper()
        elif name in ("--raw-field", "--field"):
            fields = True
            val = _need(name, val)
            key, _, value = val.text.partition("=")
            raw_value = val.raw.split("=", 1)[1] if "=" in val.raw else value
            if name == "--field" and value.startswith("@"):
                target = value[1:]
                texts += _read_file(_shell_scan.Word(raw=target, text=target), ctx, f"field {key}")
            else:
                texts.append(_literal_or_raise(_shell_scan.Word(raw=raw_value, text=value, quoted=val.quoted)))
        elif name == "--input":
            fields = True
            texts += _read_file(_need(name, val), ctx, "--input")
        elif name == "--repo" and val is not None:
            repo = val.text
    endpoint = positional[0].text if positional else None
    method = method or ("POST" if fields else "GET")
    if method not in ("POST", "PATCH", "PUT") or endpoint is None:
        return None
    m = API_REPO.match(endpoint)
    if m:
        target = None if "{" in m.group(1) else m.group(1)
        return target or repo, f"gh api {method} {m.group(2) or 'repo'}", texts, ctx.cwd
    if API_GIST.match(endpoint):
        return "", f"gh api {method} gists", texts, ctx.cwd
    if endpoint.strip("/") == "graphql" and any("mutation" in t for t in texts):
        return "", "gh api graphql mutation", texts, ctx.cwd
    return None


def _non_flags(seg):
    return [w for w in seg[1:] if not w.text.startswith("-")]


def _writes(ctx):
    """{path: bodies it receives, or None when its content cannot be known}
    for every file this command writes."""
    out = {}
    cwd = ctx.cwd
    if cwd is None:
        return out

    def add(target, bodies):
        if target and "$" not in target:
            out[_resolve(target, cwd)] = bodies

    for idx, seg in enumerate(ctx.segments):
        own = ctx.seg_bodies[idx]
        prog = os.path.basename(seg[0].text)
        feeds = own if prog in ("cat", "tee") and own else None
        for i, w in enumerate(seg):
            m = REDIRECT.match(w.text)
            if m and w.text[:1] in ">&0123456789" and ">" in w.text:
                add(m.group(1) or (seg[i + 1].text if i + 1 < len(seg) else None), feeds)
        args = _non_flags(seg)
        if prog == "tee":
            for w in args:
                add(w.text, feeds)
        elif prog in COPIERS or (prog in ("sed", "perl") and any(
                w.text.startswith(("-i", "--in-place")) or (w.text.startswith("-") and not w.text.startswith("--") and "i" in w.text[1:])
                for w in seg[1:])):
            for w in args:
                add(w.text, None)
        elif prog == "dd":
            for w in seg[1:]:
                if w.text.startswith("of="):
                    add(w.text[3:], None)
    return out


def _assign_bodies(segments, bodies):
    """Each segment's own heredoc bodies and here-strings, in source order."""
    out, k = [], 0
    for seg in segments:
        own = []
        for i, w in enumerate(seg):
            if w.text.startswith("<<<"):
                word = w if len(w.text) > 3 else (seg[i + 1] if i + 1 < len(seg) else None)
                if word is None:
                    continue
                text = w.text[3:] if word is w else word.text
                raw = w.raw[w.raw.index("<<<") + 3:] if word is w and "<<<" in w.raw else word.raw
                try:
                    _literal_or_raise(_shell_scan.Word(raw=raw, text=text))
                    expands = False
                except Unreadable:
                    expands = True
                own.append(Body(text, expands))
        for _ in HEREDOC.finditer(" ".join(w.raw for w in seg).replace("<<<", "   ")):
            if k < len(bodies):
                own.append(bodies[k])
                k += 1
        out.append(own)
    return out


def _shell_scripts(seg, own, ctx):
    """The command text a shell segment runs: its -c string, or its stdin script."""
    if seg[0].basename_is("eval"):
        return [Body(" ".join(w.text for w in seg[1:]), False)]
    has_c = any(w.text.startswith("-") and not w.text.startswith("--") and "c" in w.text[1:]
                for w in seg[1:])
    args, skip = [], False
    for w in seg[1:]:
        if skip:
            skip = False
            continue
        m = re.match(r"^(?:\d|&)?[<>]+-?", w.text)
        if m:
            skip = m.group(0) == w.text
            continue
        if not w.text.startswith("-") or w.text == "-":
            args.append(w)
    if has_c:
        return [Body(args[0].text, False)] if args else []
    if not args or args[0].text == "-":
        return list(own or ctx.bodies)
    return []


def actions(command: str, cwd, depth: int = 0):
    """Every publishing gh invocation in `command`: (repo, label, [texts], cwd)."""
    stripped, bodies = split_heredocs(command)
    segments = _shell_scan.segments(stripped)
    if not segments and stripped.strip():
        if GH_WRITE_HINT.search(command):
            raise Unreadable("the command does not parse as shell")
        return []
    seg_bodies = _assign_bodies(segments, bodies)
    all_bodies = bodies + [b for own in seg_bodies for b in own if b not in bodies]
    ctx = Ctx(cwd, all_bodies, segments, seg_bodies)
    found = []
    if depth < 3:
        inner = _substitutions(stripped)
        for b in bodies:
            if b.expands:
                inner += _substitutions(b.text)
        for text in inner:
            if "gh" in text.lower():
                found += actions(text, cwd, depth + 1)
    for idx, seg in enumerate(segments):
        seg = _strip_prefix(seg)
        ctx.own = seg_bodies[idx]
        if not seg:
            continue
        if seg[0].basename_is("cd"):
            target = seg[1].text if len(seg) > 1 else os.path.expanduser("~")
            ctx.cwd = None if ("$" in target or ctx.cwd is None and not target.startswith(("/", "~"))) \
                else str(_resolve(target, ctx.cwd or "/"))
            continue
        if _named(seg[0], SHELLS | {"eval"}):
            if depth >= 3:
                continue
            for script in _shell_scripts(seg, ctx.own, ctx):
                if "gh" not in script.text.lower():
                    continue
                inner_acts = actions(script.text, ctx.cwd, depth + 1)
                if inner_acts and script.expands:
                    raise Unreadable("a script fed to a shell from an unquoted heredoc expands "
                                     "`$`/backticks first; quote the delimiter (<<'EOF')")
                found += inner_acts
            continue
        if seg[0].basename_is("gh", fold=True):
            act = _gh_action(seg, ctx)
            if act:
                found.append(act)
    unique, seen = [], set()
    for act in found:
        key = (act[0], act[1], tuple(act[2]))
        if key not in seen:
            seen.add(key)
            unique.append(act)
    return unique


def decide(command: str, cwd, workspace: Path, lookup=None, remote=None):
    """None to allow, or the refusal text."""
    if not isinstance(command, str) or not GH_WRITE_HINT.search(command):
        return None
    _load_modules()
    try:
        acts = actions(command, cwd)
    except Unreadable as e:
        return (f"PUBLIC-WRITE PRIVACY GATE: refused — {e}. A body this guard cannot read is "
                f"treated as unsafe to publish. {TAG}")
    if not acts:
        return None
    remote = remote or policy.repo_from_remote
    pdir = policy.privacy_dir(workspace)
    kwargs = {"lookup": lookup} if lookup else {}
    deny_terms = None
    problems = []
    for repo, label, texts, act_cwd in acts:
        if not any(t.strip() for t in texts):
            continue
        if repo is None and act_cwd:
            repo = remote(act_cwd)
        vis = policy.repo_visibility(repo, pdir / policy.VISIBILITY_CACHE_NAME, **kwargs)
        if vis == "PRIVATE":
            continue
        if deny_terms is None:
            deny_terms = policy.load_terms(pdir / policy.DENYLIST_NAME)
            if deny_terms is None:
                deny_terms = []
                print(f"{TAG} warning: no private-term denylist at {pdir / policy.DENYLIST_NAME}; "
                      f"checking only home paths, Matrix IDs and room IDs. Seed it with `{SEEDER}`.",
                      file=sys.stderr)
            allow = [t.text for t in (policy.load_terms(pdir / policy.ALLOWLIST_NAME) or [])]
            allow += policy.own_identities()
        hits = policy.scan("\n".join(texts), deny_terms, allow)
        if hits:
            where = repo or "a repo this guard could not resolve (pass --repo OWNER/NAME)"
            problems.append(f"{label} to {where} ({(vis or 'visibility unknown').lower()}): "
                            + "; ".join(f"`{h.matched}` [{h.category}]" for h in hits))
    if not problems:
        return None
    return ("PUBLIC-WRITE PRIVACY GATE: refused — the text would publish private details where "
            "anyone can read them, and GitHub keeps the first version in edit history even after "
            "an edit.\n" + "\n".join(problems) + "\nDescribe them by function instead (\"a private "
            "backend repo\", \"agent A\", \"a test room\", \"<repo>/path\"). If a term is already "
            f"public, add it to {pdir / policy.ALLOWLIST_NAME}. {TAG}")


def _deny(reason):
    import json
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


def main(argv):
    raw = sys.stdin.read()
    if "gh" not in raw.lower():
        return 0
    import json
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except ValueError:
        if GH_WRITE_HINT.search(raw):
            _deny(f"PUBLIC-WRITE PRIVACY GATE: refused — the hook input is not valid JSON, so a "
                  f"possible gh write cannot be checked. {TAG}")
        return 0
    command = ""
    try:
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not isinstance(command, str) or not GH_WRITE_HINT.search(command):
            return 0
        _load_modules()
        workspace = Path(argv[argv.index("--workspace") + 1]) if "--workspace" in argv \
            else policy.resolve_workspace_dir()
        reason = decide(command, payload.get("cwd") or os.getcwd(), workspace)
    except Exception as e:  # a gh command we failed to check is not a cleared one
        reason = f"PUBLIC-WRITE PRIVACY GATE: refused — the guard failed ({type(e).__name__}: {e}). {TAG}"
    if reason:
        _deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
