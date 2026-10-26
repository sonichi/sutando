#!/usr/bin/env python3
"""public-write-privacy-guard — PreToolUse[Bash] hook that refuses a `gh` write
to a non-private repo when the text it would publish names something private.

Covered writes: `gh pr create|edit|comment|review|merge|close|reopen`, `gh issue
create|comment|edit|close|reopen`, `gh release create|edit`, `gh label create|edit`,
`gh gist create --public|edit`, `gh repo create --public|edit`, and `gh api` POST/PATCH/PUT to any `repos/<o>/<r>/…`
endpoint, `gists`, or a GraphQL mutation. Scanned: text flags (long, `=`, attached
and clustered short forms), their -file forms, `$(cat FILE)` values, stdin from the
command's own heredoc or here-string, `gh api` fields, and gh run inside wrappers, compound commands,
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
_body_file = None


def _load_modules():
    """Imported lazily so a Bash call with no `gh` pays nothing, and an import
    failure lands inside main()'s fail-closed handler."""
    global _shell_scan, policy, _body_file
    if policy is None:
        for d in (str(HERE), str(HERE.parent / "src")):
            if d not in sys.path:
                sys.path.insert(0, d)
        import _shell_scan as scan_mod
        import body_file as body_mod
        import public_write_privacy as policy_mod
        _shell_scan, policy, _body_file = scan_mod, policy_mod, body_mod


TAG = "[public-write-privacy-guard]"
SEEDER = "python3 scripts/seed-public-denylist.py"


class Spec:
    """One publishing gh verb: which flags carry text, which name a file, and
    which short flags take a value (so a cluster like `-dbTEXT` splits right)."""

    def __init__(self, texts, files=(), shorts=None, when=None, positional="ignore"):
        self.texts = set(texts)
        self.files = set(files)
        self.shorts = dict(shorts or {})
        self.when = when                    # a long flag that must be present, or None
        self.positional = positional        # ignore | files | text | gist-edit


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
                                {"R": "--repo", "n": "--notes", "F": "--notes-file", "t": "--title"},
                                positional="assets"),
    ("release", "edit"): Spec({"--notes", "--title"}, {"--notes-file"},
                              {"R": "--repo", "n": "--notes", "F": "--notes-file", "t": "--title"}),
    ("release", "upload"): Spec(set(), (), {"R": "--repo"}, positional="assets"),
    ("pr", "reopen"): Spec({"--comment"}, (), {"R": "--repo", "c": "--comment"}),
    ("issue", "reopen"): Spec({"--comment"}, (), {"R": "--repo", "c": "--comment"}),
    ("label", "create"): Spec({"--description"}, (), {"R": "--repo", "d": "--description", "c": "--color"},
                              positional="text"),
    ("label", "edit"): Spec({"--description", "--name"}, (), {"R": "--repo", "d": "--description",
                            "c": "--color", "n": "--name"}, positional="text"),
    ("gist", "create"): Spec({"--desc"}, (), {"d": "--desc", "f": "--filename"}, when="--public",
                             positional="files"),
    ("gist", "edit"): Spec({"--desc"}, {"--add"}, {"d": "--desc", "a": "--add", "f": "--filename",
                           "r": "--remove"}, positional="gist-edit"),
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
API_NEW_REPO = re.compile(r"^(?:https://api\.github\.com)?/?(?:user|orgs/[^/\s?]+)/repos/?(?:\?|$)")
API_GIST = re.compile(r"^(?:https://api\.github\.com)?/?gists(?:/|$|\?)")
HEREDOC_CAT = re.compile(r"""^"?\$\(\s*cat\s*<<-?\s*(['"]?)[A-Za-z_][A-Za-z0-9_]*\1\s*\)"?$""")
WHOLE_CAT = re.compile(r"""^"?\$\(\s*(?:cat\s+|<\s*)(['"]?)([^'"\s$`()]+)\1\s*\)"?$""")
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
HEREDOC_WORD = re.compile(r"^<<-?(?!<)")
REDIRECT_OP = re.compile(r"^(?:\d*|&)(<<<|<<-?|<>|<&|>&|&>>|&>|>>|>\||<|>)")
GH_WRITE_HINT = re.compile(r"\bgh\b[\s\S]*\b(?:pr|issue|release|api|gist|repo|label)\b", re.I)
# Programs that never write a file they name; any other program naming the body
# file in the same command may write it, so that body is refused.
READERS = {"cat", "head", "tail", "less", "more", "wc", "grep", "egrep", "fgrep", "rg", "ugrep", "diff",
           "cmp", "ls", "stat", "test", "[", "file", "echo", "printf", "md5", "md5sum", "shasum",
           "sha1sum", "sha256sum", "gh", "cd", "true", "false", "jq", "bat", "rm", "unlink", "chmod",
           "realpath", "readlink", "basename", "dirname", "du", "sleep", "mkdir", "export", "unset",
           "declare", "typeset"}


GH_VALUE_LONGS = {"--repo", "--base", "--head", "--label", "--assignee", "--reviewer", "--project",
                  "--milestone", "--template", "--reason", "--target", "--add-label", "--remove-label",
                  "--add-reviewer", "--remove-reviewer", "--add-assignee", "--remove-assignee",
                  "--add-project", "--remove-project", "--discussion-category", "--author-email",
                  "--match-head-commit", "--add-topic", "--remove-topic", "--default-branch",
                  "--visibility", "--source", "--remote", "--team", "--gitignore", "--license",
                  "--json", "--jq", "--filename", "--duplicate-of"}


class Unreadable(Exception):
    pass


class Unscannable(Unreadable):
    """A file the shared reader will not read (too large, not UTF-8, not regular, missing)."""


class Body:
    def __init__(self, text, expands):
        self.text = text
        self.expands = expands      # bash rewrites it ($, backticks) before anyone reads it


def _heredoc_expands(text: str) -> bool:
    """Does bash rewrite this unquoted heredoc body? A backslash escapes the next
    character, so parity decides: `\\$X` is a literal backslash then an expansion."""
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "`" or (c == "$" and re.match(r"[\w{(@*#?!$-]", text[i + 1:i + 2] or " ")):
            return True
        i += 1
    return False


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
                bodies.append(Body(text, not quoted and _heredoc_expands(text)))
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


class Act:
    """One publishing gh invocation. repo None: gh takes it from GH_REPO or the cwd's
    remote; "" means the target has no repo. `also` are repos it may target instead."""

    def __init__(self, repo, label, texts, cwd, env_ok=True, also=(), public=False, invalidate=False):
        self.repo, self.label, self.texts, self.cwd = repo, label, texts, cwd
        self.env_ok = env_ok
        self.also = list(also)
        self.public = public            # the write makes or keeps the target public
        self.invalidate = invalidate    # the write changes the target's visibility


class Ctx:
    """seg_bodies: each segment's own heredocs; own/assigns: the segment being read;
    exported: earlier export/unset; maybe: earlier bare NAME=value, live only if exported."""

    def __init__(self, cwd, bodies, segments, seg_bodies, command=""):
        self.command = command
        self.cwd = cwd
        self.bodies = bodies
        self.segments = segments
        self.seg_bodies = seg_bodies
        self.own = []
        self.raw_seg = []
        self.dirs = []
        self.oldpwd = None
        self.assigns = {}
        self.exported = {}
        self.maybe = {}


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
    real = os.path.realpath(str(path))
    if re.match(r"^/(?:dev/(?:stdin|fd/|tty)|proc/(?:self|\d+)/fd/)", real) or \
            re.match(r"^/(?:dev/(?:stdin|fd/)|proc/(?:self|\d+)/fd/)", str(path)):
        raise Unreadable(f"{label} `{text}` reads a stream, which this guard cannot see; "
                         "pass --body-file <path> to a regular file")
    writes, mentions = _writes(ctx)
    named = re.compile(r"(?<![\w.-])" + re.escape(path.name) + r"(?![\w.-])")
    if "*" in writes or any(named.search(m) for m in mentions):
        raise Unreadable(f"{label} `{text}` may be written by another program in this same command; "
                         "write it in a separate step so its content can be checked")
    if path in writes:
        if writes[path] is None:
            raise Unreadable(f"{label} `{text}` is written by this same command; write it in a "
                             "separate step so its content can be checked")
        return _checked(writes[path], label)
    try:
        return [_body_file.read_body_file(str(path))]
    except SystemExit as e:
        raise Unscannable(f"cannot read {label} `{text}`: {str(e.code).replace('ERROR: ', '')}")


def _stdin_sources(ctx, label):
    """What the segment being read feeds its stdin: [Body], [] when nothing of its
    own, refusing more than one source (bash's last redirect wins) or a dup."""
    files, i, seg = [], 0, ctx.raw_seg
    while i < len(seg):
        m = _redirect_op(seg[i])
        op = m.group(1) if m else ""
        fd = m.group(0)[:-len(op)] if m else ""
        if op in ("<", "<>", "<&") and fd in ("", "0"):
            rest = seg[i].text[m.end():]
            target = rest or (seg[i + 1].text if i + 1 < len(seg) else "")
            if op == "<&":
                raise Unreadable(f"{label} reads a duplicated stdin (`<&`), which this guard cannot see")
            files.append(target)
            i += 1 if rest else 2
            continue
        i += 1
    if len(files) + len(ctx.own) > 1:
        raise Unreadable(f"{label}: this command gives stdin more than one source (heredoc, here-string "
                         "or `<` file), and bash's last one wins; give it exactly one")
    if files:
        word = _shell_scan.Word(raw=files[0], text=files[0])
        return [Body(t, False) for t in _read_file(word, ctx, f"{label} (stdin from `<`)")]
    return list(ctx.own)


def _stdin(ctx, label):
    bodies = _stdin_sources(ctx, label)
    if not bodies:
        raise Unreadable(f"{label} reads stdin that is not this command's own heredoc, here-string "
                         "or `<` file, which this guard cannot see; pass --body-file <path>")
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


def _assignment(word):
    """(NAME, value) for NAME=value; a value bash expands becomes an unresolvable repo."""
    name, _, _ = word.text.partition("=")
    try:
        _literal_or_raise(_shell_scan.Word(raw=word.raw.split("=", 1)[-1], text=""))
        return name, word.text.split("=", 1)[1]
    except Unreadable:
        return name, "{unresolved}"


def _redirect_op(word):
    if word.raw[:1] in ("'", '"'):
        return None
    m = REDIRECT_OP.match(word.raw)
    return m


def _drop_redirects(seg):
    """The segment without its redirections, which bash allows anywhere, first included."""
    out, skip = [], False
    for w in seg:
        if skip:
            skip = False
            continue
        m = _redirect_op(w)
        if m:
            skip = m.group(0) == w.raw
            continue
        out.append(w)
    return out


def _strip_prefix(seg):
    """(command words, {NAME: value or None when unset}) after dropping shell
    keywords, assignments and wrappers (`timeout 60`, `env -u X`, `nice -n 5`)."""
    assigns = {}
    seg = _drop_redirects(seg)
    while seg:
        text = seg[0].text
        if text in KEYWORDS:
            seg = seg[1:]
            continue
        if _is_assignment(text):
            name, value = _assignment(seg[0])
            assigns[name] = value
            seg = seg[1:]
            continue
        name = os.path.basename(text)
        if name not in WRAPPERS:
            return seg, assigns
        valued, positional = WRAPPERS[name]
        i = 1
        while i < len(seg):
            a = seg[i].text
            if a == "--":
                i += 1
                break
            if a.startswith("-") and len(a) > 1:
                if name == "env" and a in ("-u", "--unset") and i + 1 < len(seg):
                    assigns[seg[i + 1].text] = None
                elif name == "env" and a.startswith(("-u", "--unset=")) and len(a) > 2:
                    assigns[a.split("=", 1)[1] if a.startswith("--") else a[2:]] = None
                i += 2 if a in valued else 1
                continue
            if name == "env" and _is_assignment(a):
                key, value = _assignment(seg[i])
                assigns[key] = value
                i += 1
                continue
            break
        seg = seg[i + positional:]
    return seg, assigns


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
    def on(name):
        return name in flags or any(n == name and v is not None and v.text.lower() not in ("false", "0")
                                    for n, v in values)

    if spec.when and not on(spec.when):
        return None
    visibility = [v.text.lower() for n, v in values if n == "--visibility" and v is not None]
    repo = "" if words[0] == "gist" else None
    texts, first, url_repo = [], True, None
    for name, val in values:
        if name == "--repo":
            repo = _literal_or_raise(_need(name, val))
        elif name in spec.texts:
            texts += _inline(_need(name, val), ctx)
        elif name in spec.files:
            texts += _read_file(_need(name, val), ctx, name)
    for w in positional:
        if spec.positional == "files":
            texts += _read_file(w, ctx, "file")
            continue
        if spec.positional == "text":
            texts.append(_literal_or_raise(w))
            continue
        if spec.positional == "gist-edit":
            if not first and (w.text == "-" or _resolve(_literal_or_raise(w), ctx.cwd).is_file()):
                texts += _read_file(w, ctx, "file")
            first = False
            continue
        if spec.positional == "assets":
            if not first:
                texts += _asset_text(w, ctx)
            first = False
            continue
        m = GH_URL.search(w.text)
        if m and not url_repo:
            url_repo = m.group(1)
        elif words[0] == "repo" and repo is None and w.text.count("/") == 1 and "{" not in w.text:
            repo = w.text
    public = "public" in visibility or (words == ["repo", "create"] and on("--public"))
    label = f"gh {words[0]} {words[1]}"
    if url_repo:
        # gh posts to the URL's repo whatever --repo says; a disagreeing --repo is scanned too.
        also = [repo] if repo and repo.lower() != url_repo.lower() else []
        return Act(url_repo, label, texts, ctx.cwd, also=also, public=public, invalidate=bool(visibility))
    return _with_env_repo(repo, label, texts, ctx, public=public, invalidate=bool(visibility))


def _asset_text(word, ctx):
    """A release asset's text when it is a readable text file; a binary or oversized
    asset cannot be scanned and is let through (listed under Not covered)."""
    text = _literal_or_raise(word).split("#", 1)[0]
    try:
        return _read_file(_shell_scan.Word(raw=text, text=text), ctx, "release asset")
    except Unscannable:
        return []


def _with_env_repo(repo, label, texts, ctx, **kw):
    """Apply GH_REPO the way gh does: a --repo flag or URL wins, then GH_REPO."""
    if repo is not None:
        return Act(repo, label, texts, ctx.cwd, **kw)
    for scope in (ctx.assigns, ctx.exported):
        if "GH_REPO" in scope:
            value = scope["GH_REPO"]
            if value:
                return Act(value, label, texts, ctx.cwd, **kw)
            return Act(None, label, texts, ctx.cwd, env_ok=False, **kw)
    return Act(None, label, texts, ctx.cwd, also=ctx.maybe.get("GH_REPO", ()), **kw)


def _api_action(args, ctx):
    values, flags, positional = _parse_args(args, API_SHORTS, API_VALUE_LONGS)
    method, fields, texts, repo, kv = None, False, [], None, {}
    for name, val in values:
        if name == "--method":
            method = _need(name, val).text.upper()
        elif name in ("--raw-field", "--field"):
            fields = True
            val = _need(name, val)
            key, _, value = val.text.partition("=")
            kv[key] = value.lower()
            raw_value = val.raw.split("=", 1)[1] if "=" in val.raw else value
            if name == "--field" and value.startswith("@"):
                target = value[1:]
                got = _read_file(_shell_scan.Word(raw=target, text=target), ctx, f"field {key}")
            else:
                got = [_literal_or_raise(_shell_scan.Word(raw=raw_value, text=value, quoted=val.quoted))]
            texts += got
            if key == "content":
                texts += [d for d in map(_base64_text, got) if d]
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
        changes = m.group(2) is None and ("private" in kv or "visibility" in kv)
        public = m.group(2) is None and (kv.get("private") == "false" or kv.get("visibility") == "public")
        return _with_env_repo(target or repo, f"gh api {method} {m.group(2) or 'repo'}", texts, ctx,
                              public=public, invalidate=changes)
    if API_NEW_REPO.match(endpoint) and method == "POST":
        if kv.get("private") == "true" or kv.get("visibility") in ("private", "internal"):
            return None
        return Act("", f"gh api {method} new repo", texts, ctx.cwd, public=True)
    if API_GIST.match(endpoint):
        return Act("", f"gh api {method} gists", texts, ctx.cwd)
    if endpoint.strip("/") == "graphql" and any("mutation" in t for t in texts):
        return Act("", "gh api graphql mutation", texts, ctx.cwd)
    return None


def _base64_text(value):
    import base64
    import binascii
    try:
        return base64.b64decode("".join(value.split()), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return ""


def _non_flags(seg):
    return [w for w in seg[1:] if not w.text.startswith("-")]


def _writes(ctx):
    """({path: bodies it receives, or None when its content cannot be known}, [words
    other programs name]); "*" as a path means a write to a target that expands."""
    out, mentions = {}, []
    cwd = ctx.cwd

    def add(target, bodies):
        if not target or target.startswith("&") or target in ("-", "/dev/null"):
            return
        if "$" in target or "`" in target or (cwd is None and not target.startswith(("/", "~"))):
            out["*"] = None
            return
        path = _resolve(target, cwd)
        out[path] = None if path in out else bodies

    for idx, raw_seg in enumerate(ctx.segments):
        own = ctx.seg_bodies[idx]
        seg, _ = _strip_prefix(raw_seg)
        prog = os.path.basename(seg[0].text) if seg else ""
        args = _non_flags(seg) if seg else []
        reads_stdin = prog == "tee" or (prog == "cat" and all(w.text == "-" for w in args))
        feeds = own if own and reads_stdin else None
        for i, w in enumerate(raw_seg):
            m = _redirect_op(w)
            if not m or "<" in m.group(1):
                continue
            op = m.group(1)
            target = w.text[m.end():] or (raw_seg[i + 1].text if i + 1 < len(raw_seg) else None)
            if op == ">&" and target and (target.isdigit() or target == "-"):
                continue
            add(target, None if op in (">>", "&>>") else feeds)
        if not seg:
            continue
        if prog == "tee":
            appending = any(w.text in ("-a", "--append") for w in seg[1:])
            for w in args:
                add(w.text, None if appending else feeds)
        elif prog not in READERS:
            for w in seg[1:]:
                if not _redirect_op(w):
                    mentions.append(w.text)
            if prog == "dd":
                for w in seg[1:]:
                    if w.text.startswith("of="):
                        add(w.text[3:], None)
            else:
                for w in args:
                    add(w.text, None)
    return out, mentions


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
        words = seg[1:]
        positional, has_c, stdin = words, True, False
    else:
        args, i, has_c, stdin, positional = _drop_redirects(seg[1:]), 0, False, False, []
        while i < len(args):
            t = args[i].text
            if t == "--":
                positional = args[i + 1:]
                break
            if t in ("--rcfile", "--init-file"):
                i += 2
                continue
            if t.startswith("--"):
                i += 1
                continue
            if t[:1] in "-+" and len(t) > 1:
                letters = t[1:]
                has_c = has_c or (t[0] == "-" and "c" in letters)
                stdin = stdin or (t[0] == "-" and "s" in letters)
                i += 1 + letters.count("o") + letters.count("O")
                continue
            positional = args[i:]
            break
        words = positional[:1]
    if has_c:
        scripts = []
        for w in words:
            try:
                _literal_or_raise(w)
            except Unreadable:
                if GH_WRITE_HINT.search(_unquoted(ctx.command)):
                    raise Unreadable("a shell or eval runs a command string that expands at run time, "
                                     "so a gh write in it cannot be checked")
            scripts.append(w.text)
        return [Body(" ".join(scripts), False)] if scripts else []
    if stdin or not positional or positional[0].text == "-":
        return _stdin_sources(ctx, "a shell's stdin script") or list(ctx.bodies)
    return []


# Programs that run a command whose arguments are built at run time: gh under them is refused.
RUNNERS = {"xargs", "parallel", "sem", "rush", "watch", "script", "find", "flock", "su", "runuser",
           "ssh", "entr", "tmux", "screen", "chroot", "nsenter", "dbus-run-session", "fswatch"}


def _unquoted(text):
    return re.sub(r"[\"'\\]", "", text)


def _runs_gh_write(texts):
    """Does this runner's argv (or a command string in it) start a gh write or gh api?"""
    tokens = [t for t in re.split(r"[\s;&|()]+", _unquoted(" ".join(texts))) if t]
    for i, t in enumerate(tokens):
        if os.path.basename(t).lower() != "gh":
            continue
        words, j = [], i + 1
        while j < len(tokens) and len(words) < 2:
            if tokens[j].startswith("-"):
                j += 2 if tokens[j] in REPO_FLAGS else 1
                continue
            words.append(tokens[j])
            j += 1
            if words[0] == "api":
                break
        if words[:1] == ["api"] or tuple(words) in VERBS:
            return True
    return False


def _env_split(raw_seg):
    return any(w.basename_is("env") for w in raw_seg) and any(
        w.text.startswith(("-S", "--split-string")) for w in raw_seg[1:])


def _refuse_runner(prog):
    raise Unreadable(f"gh runs under `{prog}`, which builds its arguments at run time where "
                     "this guard cannot see them; run gh directly")


def _change_dir(ctx, prog, args):
    """The cwd after cd/pushd/popd, or None when it cannot be known."""
    args = [a for a in args if a not in ("-L", "-P", "-q", "--")]
    if prog == "popd":
        return ctx.dirs.pop() if ctx.dirs and not args else None
    target = args[0] if args else ("~" if prog == "cd" else None)
    if target is None or target.startswith(("+", "-")) and target != "-" or "$" in target or "`" in target:
        return None
    if target == "-":
        target = ctx.oldpwd
        if target is None:
            return None
    elif ctx.cwd is None and not target.startswith(("/", "~")):
        return None
    new = str(_resolve(target, ctx.cwd or "/"))
    if prog == "pushd":
        ctx.dirs.append(ctx.cwd)
    ctx.oldpwd = ctx.cwd
    return new


GH_REPO_ANYWHERE = re.compile(r"""\bGH_REPO=(['"]?)([^'"\s;&|)]*)\1""")


def actions(command: str, cwd, depth: int = 0, inherited=None):
    """Every publishing gh invocation in `command`. `inherited` is ({exported},
    {maybe}) from the shell that runs this one."""
    stripped, bodies = split_heredocs(command)
    segments = _shell_scan.segments(stripped)
    if not segments and stripped.strip():
        if GH_WRITE_HINT.search(_unquoted(command)):
            raise Unreadable("the command does not parse as shell")
        return []
    seg_bodies = _assign_bodies(segments, bodies)
    all_bodies = bodies + [b for own in seg_bodies for b in own if b not in bodies]
    if cwd is not None and re.search(r"\b(?:cd|pushd|popd)\b", stripped) and \
            any(w.is_operator and w.text == "(" for w in _shell_scan.words(stripped)):
        cwd = None
    ctx = Ctx(cwd, all_bodies, segments, seg_bodies, command)
    if inherited:
        ctx.exported = dict(inherited[0])
        ctx.maybe = {k: list(v) for k, v in inherited[1].items()}
    found = []
    if depth < 3:
        # A substitution may run after any assignment on the line: every GH_REPO it sets is a candidate.
        maybe = {k: list(v) for k, v in ctx.maybe.items()}
        for _, value in GH_REPO_ANYWHERE.findall(command):
            maybe.setdefault("GH_REPO", []).append("{unresolved}" if "$" in value or "`" in value else value)
        sub_env = (ctx.exported, maybe)
        inner = _substitutions(stripped)
        for b in bodies:
            if b.expands:
                inner += _substitutions(b.text)
        for text in inner:
            if "gh" in text.lower():
                found += actions(text, cwd, depth + 1, sub_env)
    for idx, seg in enumerate(segments):
        ctx.raw_seg = seg
        if _env_split(seg) and _runs_gh_write([w.text for w in seg[1:]]):
            _refuse_runner("env -S")
        seg, ctx.assigns = _strip_prefix(seg)
        ctx.own = seg_bodies[idx]
        if not seg:
            for name, value in ctx.assigns.items():
                ctx.maybe.setdefault(name, []).append(value)
            continue
        if seg[0].text in ("export", "declare", "typeset", "unset"):
            for w in seg[1:]:
                if seg[0].text == "unset" and not w.text.startswith("-"):
                    ctx.exported[w.text] = None
                elif _is_assignment(w.text):
                    name, value = _assignment(w)
                    ctx.exported[name] = value
            continue
        prog = os.path.basename(seg[0].text)
        if prog in ("cd", "pushd", "popd"):
            ctx.cwd = _change_dir(ctx, prog, [w.text for w in seg[1:]])
            continue
        if prog in RUNNERS and _runs_gh_write([w.text for w in seg[1:]]):
            _refuse_runner(prog)
        if _named(seg[0], SHELLS | {"eval"}):
            if depth >= 3:
                continue
            for script in _shell_scripts(seg, ctx.own, ctx):
                if "gh" not in script.text.lower():
                    continue
                inner_acts = actions(script.text, ctx.cwd, depth + 1,
                                     ({**ctx.exported, **ctx.assigns}, ctx.maybe))
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
        key = (act.repo, act.label, tuple(act.texts), tuple(act.also), act.public, act.invalidate)
        if key not in seen:
            seen.add(key)
            unique.append(act)
    return unique


def decide(command: str, cwd, workspace: Path, lookup=None, remote=None):
    """None to allow, or the refusal text."""
    if not isinstance(command, str) or not GH_WRITE_HINT.search(_unquoted(command)):
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
    cache = pdir / policy.VISIBILITY_CACHE_NAME
    for act in acts:
        if not act.invalidate and not any(t.strip() for t in act.texts):
            continue
        repo = act.repo
        if repo is None:
            repo = (os.environ.get("GH_REPO") if act.env_ok else None) or \
                (remote(act.cwd) if act.cwd else None)
        if act.invalidate:
            for r in [repo] + act.also:
                policy.forget_visibility(r, cache)
        if not any(t.strip() for t in act.texts):
            continue
        # A write that makes the target public is judged by where it lands, not where it is.
        targets = [(r, "PUBLIC" if act.public else policy.repo_visibility(r, cache, **kwargs))
                   for r in [repo] + act.also]
        if all(vis == "PRIVATE" for _, vis in targets):
            continue
        repo, vis = next((r, v) for r, v in targets if v != "PRIVATE")
        if deny_terms is None:
            deny_terms = policy.load_terms(pdir / policy.DENYLIST_NAME)
            if deny_terms is None:
                deny_terms = []
                print(f"{TAG} warning: no private-term denylist at {pdir / policy.DENYLIST_NAME}; "
                      f"checking only home paths, Matrix IDs and room IDs. Seed it with `{SEEDER}`.",
                      file=sys.stderr)
            allow = [t.text for t in (policy.load_terms(pdir / policy.ALLOWLIST_NAME) or [])]
            allow += policy.own_identities()
        hits = policy.scan("\n".join(act.texts), deny_terms, allow)
        if hits:
            where = repo or "a repo this guard could not resolve (pass --repo OWNER/NAME)"
            problems.append(f"{act.label} to {where} ({(vis or 'visibility unknown').lower()}): "
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
    if "gh" not in _unquoted(raw).lower():
        return 0
    import json
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except ValueError:
        if GH_WRITE_HINT.search(_unquoted(raw)):
            _deny(f"PUBLIC-WRITE PRIVACY GATE: refused — the hook input is not valid JSON, so a "
                  f"possible gh write cannot be checked. {TAG}")
        return 0
    command = ""
    try:
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not isinstance(command, str) or not GH_WRITE_HINT.search(_unquoted(command)):
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
