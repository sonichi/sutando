#!/usr/bin/env python3
"""shell-trap-guard — PreToolUse hook on Bash that catches shell commands which
fail, or quietly do the wrong thing, on a macOS/zsh host.

Blocks (exit 2, the fix on stderr), each only where the host tool is the BSD one:
  timeout      `timeout ...` when no `timeout` is on PATH (stock macOS has none) and the
               command does not probe for it (`command -v timeout && ...`)
  sed_inplace  GNU `sed -i` / `--in-place` without a suffix, with /usr/bin/sed on macOS
  base64_file  GNU `base64 <file>`, with /usr/bin/base64 on macOS
Warns (exit 0, JSON ``additionalContext``; the call still runs):
  pgrep_self        `pgrep -f pat` with no self-exclusion in its own pipeline
                    (`[p]at`, `$$`, or `| grep -v`): it also matches the invoking shell
  for_unquoted_var  `for x in $VAR` where VAR holds command output, when $SHELL is zsh
                    (zsh does not word-split it, so the loop runs once)

`timeout` counts as present when it is on the hook's PATH or on the user's shell PATH:
the PATH a login `$SHELL` reports after sourcing its rc file, as Claude Code's shell
snapshot does. That probe runs at most once per SHELL_PATH_TTL (2s timeout, failures
cached too) and is cached at ``<workspace>/state/guards/shell_path_cache.json``
(``SHELL_TRAP_GUARD_SHELL_PATH_CACHE`` overrides); with no cache location it is skipped.

A command named by path (`<dir>/timeout`) is checked against that path, not
PATH: it is blocked only when it resolves to the BSD tool in /usr/bin.

Tokenizes with the shared ``_shell_scan`` scanner, so quoted text, comments and
heredoc bodies are data rather than commands.

Each hit is appended as JSONL to ``<workspace>/state/guards/shell_trap_hits.jsonl``
(``SHELL_TRAP_GUARD_LOG`` overrides the path), with token, key, password and
Authorization values redacted. The workspace comes from
``workspace_default.resolve_workspace`` under a CONFIGURED repo root: ``--repo <path>``
in the registration, else ``$SUTANDO_REPO_ROOT``. With neither, nothing is logged
and the guard still decides.

``SHELL_TRAP_GUARD_RULES`` (comma list of rule names, e.g. ``pgrep_self,timeout``)
replaces the host-derived rule set; ``SHELL_TRAP_GUARD_DISABLE=1`` turns the hook off.
Fail-OPEN: any internal error exits 0 with no output.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _shell_scan  # noqa: E402  (sibling module; path set above)
except Exception:
    _shell_scan = None  # decide() then allows everything

BLOCK, WARN = "blocked", "warned"
ALL_RULES = ("timeout", "sed_inplace", "base64_file", "pgrep_self", "for_unquoted_var")
LOG_CMD_CHARS = 2000
SHELL_PATH_TTL = 600
SHELL_PROBE_TIMEOUT = 2
PATH_MARK = "__shell_trap_guard_path__"
RC_FILES = {"zsh": (".zshenv", ".zprofile", ".zshrc", ".zlogin"),
            "bash": (".bash_profile", ".bash_login", ".profile", ".bashrc")}
RC_SOURCED = {"zsh": ".zshrc", "bash": ".bashrc"}
BSD_DIR = "/usr/bin"
PROBE_WORDS = {"command", "type", "which", "hash", "whence"}
PROBE_SKIP = {"if", "elif", "!", "while", "until", "then", "do", "{", "&&", "||"}
SECRET_RES = (
    re.compile(r"(?i)(--(?:token|password|passwd|secret|api-?key|auth)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"),
    re.compile(r"(?i)(authorization:\s*)([^\"'\n]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"(?i)(\b\w*(?:token|key|secret|password|passwd)\w*=)(\"[^\"]*\"|'[^']*'|\S+)"),
)

PREFIX_WORDS = {"sudo", "command", "exec", "nohup", "time", "builtin", "then", "do", "else",
                "elif", "if", "while", "until", "!", "{", "noglob", "nice"}
PIPE_OPS = {"|"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT_RE = re.compile(r"^[0-9]*(?:<<<|<<|<>|<&|<|>>|>\||>&|>)")
HEREDOC_RE = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
CMD_SUBST_ASSIGN_RE = re.compile(
    r"(?:^|[\s;&|(])(?:local\s+|export\s+|typeset\s+|declare\s+|readonly\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)=(?:\$\(|`)")
UNQUOTED_VAR_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")
SED_INPLACE_RE = re.compile(r"^-[EnrsuIl]*i$")

MSG = {
    "timeout": "`timeout` is GNU coreutils and is not on this host's PATH; pass the Bash tool's "
               "`timeout` parameter, or run `python3 -c 'import subprocess; subprocess.run([...], timeout=N)'`.",
    "sed_inplace": "GNU-style `sed -i` on macOS treats the next argument as a backup suffix; "
                   "use `sed -i '' ...` (BSD sed needs a suffix argument).",
    "base64_file": "`base64 <file>` is the GNU form; on macOS use `base64 -i <file>` or `base64 < <file>`.",
    "pgrep_self": "`pgrep -f` also matches the invoking shell whose command line contains the pattern; "
                  "use the bracket trick (`pgrep -f '[m]yproc'`) or exclude `$$`.",
    "for_unquoted_var": "`for x in $%s` does not word-split in zsh, so the loop runs once over the whole "
                        "output; use `for x in ${=%s}`, `${(f)%s}` for lines, or `while read -r x`.",
}


def host_rules(argv=(), need_timeout=True):
    """The rules that apply on this host, unless SHELL_TRAP_GUARD_RULES names them."""
    override = os.environ.get("SHELL_TRAP_GUARD_RULES")
    if override is not None:
        return {r.strip() for r in override.split(",") if r.strip() in ALL_RULES}
    rules = {"pgrep_self"}
    darwin = sys.platform == "darwin"
    if need_timeout and shutil.which("timeout") is None and not _on_shell_path("timeout", argv):
        rules.add("timeout")
    if darwin and shutil.which("sed") == "/usr/bin/sed":
        rules.add("sed_inplace")
    if darwin and shutil.which("base64") == "/usr/bin/base64":
        rules.add("base64_file")
    if os.path.basename(os.environ.get("SHELL", "")) == "zsh":
        rules.add("for_unquoted_var")
    return rules


def _on_shell_path(tool, argv):
    try:
        path = user_shell_path(argv)
        return bool(path) and shutil.which(tool, path=path) is not None
    except Exception:
        return False


def _shell_cache_key(shell):
    home = os.path.expanduser("~")
    parts = [shell, os.environ.get("PATH", "")]
    for rc in RC_FILES.get(os.path.basename(shell), ()):
        try:
            parts.append("%s:%s" % (rc, os.stat(os.path.join(home, rc)).st_mtime_ns))
        except OSError:
            parts.append(rc + ":-")
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _clean_path(text):
    """Absolute entries only (a relative or `~` entry is skipped); None when nothing is left."""
    if len(text) >= 65536 or "\0" in text:
        return None
    return ":".join(e for e in text.split(":") if e.startswith("/")) or None


def probe_shell_path(shell):
    """PATH from a login `shell` that sources its rc file; None on any failure."""
    rc = RC_SOURCED.get(os.path.basename(shell))
    src = '[ -f "$HOME/%s" ] && . "$HOME/%s"; ' % (rc, rc) if rc else ""
    script = src + "printf '\\n%s%%s\\n' \"$PATH\"" % PATH_MARK
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG")
           if k in os.environ}
    env.update(SHELL=shell, TERM="dumb")
    p = subprocess.Popen([shell, "-l", "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=SHELL_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Kill the whole group: a grandchild holding stdout open would block communicate().
        with contextlib.suppress(OSError):
            os.killpg(p.pid, 9)
        p.stdout.close()
        p.wait()
        return None
    lines = [ln[len(PATH_MARK):] for ln in out.decode("utf-8", "replace").splitlines()
             if ln.startswith(PATH_MARK)]
    return _clean_path(lines[-1]) if lines else None


def _shell_cache_path(argv):
    override = os.environ.get("SHELL_TRAP_GUARD_SHELL_PATH_CACHE")
    if override:
        return override
    ws = _workspace(argv)
    return os.path.join(ws, "state", "guards", "shell_path_cache.json") if ws else None


def user_shell_path(argv=()):
    """The user's shell PATH, cached on disk; None when unknown (the caller then ignores it)."""
    shell = os.environ.get("SHELL", "")
    cache = _shell_cache_path(argv)
    if not shell.startswith("/") or not cache:
        return None
    key = _shell_cache_key(shell)
    try:
        with open(cache) as f:
            rec = json.load(f)
        if rec.get("key") == key and 0 <= time.time() - rec.get("ts", 0) < SHELL_PATH_TTL:
            return rec.get("path") or None
    except Exception:
        pass
    try:
        path = probe_shell_path(shell)
    except Exception:
        path = None
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = "%s.%d.tmp" % (cache, os.getpid())
        with open(tmp, "w") as f:
            json.dump({"key": key, "ts": time.time(), "path": path}, f)
        os.replace(tmp, cache)
    except Exception:
        pass
    return path


def strip_heredocs(cmd):
    """Drop heredoc bodies: they are data on stdin, not commands."""
    out, pending = [], []
    for line in cmd.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in HEREDOC_RE.finditer(line))
    return "\n".join(out)


def pipelines(cmd):
    """[[segment, ...], ...]: simple commands grouped by the pipeline they sit in.

    The scanner reads every `&` as an operator, so the `&` of `2>&1`, `&>f` and `|&` is
    folded back here; only a background `&` ends a pipeline.
    """
    ws = _shell_scan.words(cmd)
    out, pipe, seg = [], [], []
    i = 0
    while i < len(ws):
        w = ws[i]
        if w.is_operator and w.text == "&":
            prev = ws[i - 1] if i else None
            nxt = ws[i + 1] if i + 1 < len(ws) else None
            if seg and seg[-1] is prev and _is_redirect(prev) and prev.text[-1] in "<>":
                if nxt is not None and not nxt.is_operator:
                    seg[-1] = _shell_scan.Word(raw=prev.raw + "&" + nxt.raw, text=prev.text + "&" + nxt.text)
                    i += 1
                i += 1
                continue
            if nxt is not None and not nxt.is_operator and nxt.text.startswith(">"):
                i += 1
                continue
            if prev is not None and prev.is_operator and prev.text == "|":
                i += 1
                continue
        if not w.is_operator:
            seg.append(w)
            i += 1
            continue
        if seg:
            pipe.append(seg)
        seg = []
        if w.text not in PIPE_OPS and pipe:
            out.append(pipe)
            pipe = []
        i += 1
    if seg:
        pipe.append(seg)
    if pipe:
        out.append(pipe)
    return out


def _is_redirect(w):
    return not w.quoted and bool(REDIRECT_RE.match(w.text))


def command_words(seg):
    """[cmd, *args] as text: env assignments, prefix words and redirections stripped."""
    i = 0
    while i < len(seg):
        t = seg[i].text
        if ASSIGN_RE.match(t) or t in PREFIX_WORDS:
            i += 1
            continue
        if t == "env":
            i += 1
            while i < len(seg) and (ASSIGN_RE.match(seg[i].text) or seg[i].text.startswith("-")):
                i += 1
            continue
        break
    words, rest = [], seg[i:]
    j = 0
    while j < len(rest):
        w = rest[j]
        if _is_redirect(w):
            m = REDIRECT_RE.match(w.text)
            if m.group(0).lstrip("0123456789").startswith("<"):
                words.append("<")  # stdin is redirected; checks read this as "not a file arg"
            j += 1 if w.text[m.end():] else 2  # a bare `>` / `2>` takes the next word
            continue
        words.append(w.text)
        j += 1
    if words:
        words[0] = words[0].lstrip("`")
    return words


def _base(words):
    return os.path.basename(words[0]) if words else ""


def _is_bsd(words, tool):
    """words[0] runs `tool`: a bare name (PATH gating is host_rules'), or a path to the BSD one."""
    if _base(words) != tool:
        return False
    if "/" not in words[0]:
        return True
    bsd = os.path.join(BSD_DIR, tool)
    try:
        return os.path.isfile(bsd) and os.path.realpath(words[0]) == os.path.realpath(bsd)
    except (OSError, ValueError):
        return False


def probes_for(pipes, tool):
    """True when a segment checks for `tool` (`command -v`, `type`, `which`, `hash`, `whence`)."""
    for pipe in pipes:
        for seg in pipe:
            t = [w.text for w in seg]
            while t and t[0] in PROBE_SKIP:
                t = t[1:]
            if not t or t[0] not in PROBE_WORDS or tool not in t[1:]:
                continue
            if t[0] != "command" or any(a in ("-v", "-V") for a in t[1:]):
                return True
    return False


def check_timeout(words):
    if _is_bsd(words, "timeout") and len(words) > 1:
        return MSG["timeout"]
    return None


def check_sed(words):
    if not _is_bsd(words, "sed"):
        return None
    for j, t in enumerate(words[1:], 1):
        if t.startswith("--in-place"):
            return MSG["sed_inplace"]
        if SED_INPLACE_RE.match(t):
            nxt = words[j + 1] if j + 1 < len(words) else None
            if nxt is None or (nxt != "" and not nxt.startswith(".")):
                return MSG["sed_inplace"]
    return None


def check_base64(words):
    if not _is_bsd(words, "base64"):
        return None
    args, j, positional = words[1:], 0, False
    while j < len(args):
        t = args[j]
        if t in ("-i", "--input", "<") or t.startswith("--input="):
            return None
        if t in ("-o", "--output", "-b", "--break", "-w", "--wrap"):
            j += 2
            continue
        if t.startswith("-") and t != "-":
            j += 1
            continue
        positional = True
        j += 1
    return MSG["base64_file"] if positional else None


def check_pgrep(pipe):
    """Warn on `pgrep -f` unless the pattern or its own pipeline excludes the caller."""
    for k, seg in enumerate(pipe):
        words = command_words(seg)
        if _base(words) != "pgrep":
            continue
        flags = [t for t in words[1:] if t.startswith("-") and not t.startswith("--")]
        if not any("f" in t[1:] for t in flags):
            continue
        pattern = next((t for t in words[1:] if not t.startswith("-")), "")
        if re.search(r"\[[^\]]+\]", pattern):
            continue
        if any("$$" in w.raw for s in pipe for w in s):
            continue
        later = [command_words(s) for s in pipe[k + 1:]]
        if any(_base(w) == "grep" and any(a.startswith("-") and "v" in a[1:] for a in w[1:])
               for w in later):
            continue
        return MSG["pgrep_self"]
    return None


def check_for_unquoted(pipes, body):
    assigned = set(CMD_SUBST_ASSIGN_RE.findall(body))
    for pipe in pipes:
        for seg in pipe:
            if len(seg) < 4 or seg[0].text != "for" or seg[2].text != "in":
                continue
            m = UNQUOTED_VAR_RE.match(seg[3].raw)
            if m and m.group(1) in assigned:
                return MSG["for_unquoted_var"] % ((m.group(1),) * 3)
    return None


SEGMENT_RULES = (("timeout", check_timeout), ("sed_inplace", check_sed), ("base64_file", check_base64))


def analyze(cmd, rules=None, argv=()):
    """[(rule, action, message), ...] for the rules enabled (default: this host's)."""
    body = strip_heredocs(cmd)
    rules = host_rules(argv, need_timeout="timeout" in body) if rules is None else set(rules)
    pipes = pipelines(body)
    hits, seen = [], set()
    if "timeout" in rules and probes_for(pipes, "timeout"):
        rules = rules - {"timeout"}
    for pipe in pipes:
        for seg in pipe:
            words = command_words(seg)
            for rule, fn in SEGMENT_RULES:
                if rule in rules and rule not in seen:
                    msg = fn(words)
                    if msg:
                        seen.add(rule)
                        hits.append((rule, BLOCK, msg))
        if "pgrep_self" in rules and "pgrep_self" not in seen:
            msg = check_pgrep(pipe)
            if msg:
                seen.add("pgrep_self")
                hits.append(("pgrep_self", WARN, msg))
    if "for_unquoted_var" in rules:
        msg = check_for_unquoted(pipes, body)
        if msg:
            hits.append(("for_unquoted_var", WARN, msg))
    return hits


def _repo_root(argv):
    """Configured, never discovered: ``--repo <path>`` / ``--repo=<path>``, else $SUTANDO_REPO_ROOT."""
    for i, a in enumerate(argv):
        if a == "--repo" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--repo="):
            return a.split("=", 1)[1]
    return os.environ.get("SUTANDO_REPO_ROOT") or None


def _workspace(argv):
    repo = _repo_root(argv)
    if not repo:
        return None
    sys.path.insert(0, os.path.join(repo, "src"))
    # The resolver may print migration notices; a blocking hook's stderr reaches the model.
    with contextlib.redirect_stderr(io.StringIO()):
        from workspace_default import resolve_workspace
        return str(resolve_workspace(migrate=False))


def log_path(argv):
    override = os.environ.get("SHELL_TRAP_GUARD_LOG")
    if override:
        return override
    ws = _workspace(argv)
    return os.path.join(ws, "state", "guards", "shell_trap_hits.jsonl") if ws else None


def redact(cmd):
    for rx in SECRET_RES:
        cmd = rx.sub(lambda m: m.group(1) + "[REDACTED]", cmd)
    return cmd


def log_hits(hits, cmd, argv):
    try:
        path = log_path(argv)
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a") as f:
            for rule, action, _ in hits:
                f.write(json.dumps({"ts": ts, "rule": rule, "action": action,
                                    "command": redact(cmd)[:LOG_CMD_CHARS]}) + "\n")
    except Exception:
        pass


def decide(raw, argv=()):
    """(exit_code, stdout_text, stderr_text) for one hook invocation."""
    if os.environ.get("SHELL_TRAP_GUARD_DISABLE") == "1" or _shell_scan is None:
        return 0, "", ""
    data = json.loads(raw)
    if data.get("tool_name", "Bash") != "Bash":
        return 0, "", ""
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not isinstance(cmd, str) or not cmd.strip():
        return 0, "", ""
    hits = analyze(cmd, argv=list(argv))
    if not hits:
        return 0, "", ""
    log_hits(hits, cmd, list(argv))
    blocks = [h for h in hits if h[1] == BLOCK]
    warns = [h for h in hits if h[1] == WARN]
    if blocks:
        lines = ["shell-trap-guard blocked: " + m for _, _, m in blocks]
        lines += ["shell-trap-guard warning: " + m for _, _, m in warns]
        return 2, "", "\n".join(lines) + "\n"
    ctx = " ".join("shell-trap-guard warning (%s): %s" % (r, m) for r, _, m in warns)
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}})
    return 0, out + "\n", ""


def main():
    try:
        code, out, err = decide(sys.stdin.read(), sys.argv[1:])
    except Exception:
        sys.exit(0)
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.exit(code)


if __name__ == "__main__":
    main()
