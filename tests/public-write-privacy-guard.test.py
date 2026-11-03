#!/usr/bin/env python3
"""Public-write privacy gate: a `gh` write to a non-private repo whose text names
a private term, a Matrix ID or room ID, or a local home path is refused before
it runs (hooks/public-write-privacy-guard.py + src/public_write_privacy.py).

Each command shape is driven both clean (allowed) and dirty (denied); the end-to-
end cases pipe a real PreToolUse payload into the hook as Claude Code does.

Run:  python3 tests/public-write-privacy-guard.test.py
"""
import contextlib
import importlib.util
import io
import json
import shutil
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "public-write-privacy-guard.py"
SEED = REPO / "scripts" / "seed-public-denylist.py"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "hooks"))
import public_write_privacy as policy  # noqa: E402

spec = importlib.util.spec_from_file_location("guard", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

TERM = "zq-private-backend"
AGENT = "Zephyrine"
LOCAL = "/" + "Users" + "/jdoe/work/x.md"
MXID = "@zq-agent:matrix.zq.chat"
ROOM_ID = "!AbCdEfGh12:matrix.zq.chat"
DIRTY = (TERM, AGENT, LOCAL, MXID, ROOM_ID)

PUBLIC = lambda repo: "PUBLIC"  # noqa: E731
PRIVATE = lambda repo: "PRIVATE"  # noqa: E731


def shapes(text, tmp):
    """Every publishing command shape, carrying `text` as its published prose."""
    body = Path(tmp) / "body.md"
    body.write_text(text)
    q = text.replace("'", "")
    return {
        "pr create --body": f"gh pr create --repo o/r --title t --body '{q}'",
        "pr create --title": f"gh pr create -R o/r --title '{q}' --body ok",
        "pr create --body-file": f"gh pr create -R o/r --title t --body-file {body}",
        "pr edit -F": f"gh pr edit 5 -R o/r -F {body}",
        "pr comment -b": f"gh pr comment 5 -R o/r -b '{q}'",
        "pr comment --body=": f"gh pr comment 5 -R o/r --body='{q}'",
        "pr comment url": f"gh pr comment https://github.com/o/r/pull/5 --body '{q}'",
        "pr review": f"gh pr review 5 -R o/r --comment --body '{q}'",
        "issue create": f"gh issue create -R o/r --title t --body '{q}'",
        "issue comment": f"gh -R o/r issue comment 7 --body-file {body}",
        "issue edit": f"gh issue edit 7 -R o/r --body \"$(cat {body})\"",
        "release create": f"gh release create v1 -R o/r --notes '{q}'",
        "api POST comments": f"gh api repos/o/r/issues/5/comments -f body='{q}'",
        "api PATCH pull": f"gh api -X PATCH repos/o/r/pulls/5 -f body='{q}'",
        "api --method POST review": f"gh api --method POST /repos/o/r/pulls/5/reviews -F body=@{body}",
        "api --input": f"gh api -X POST repos/o/r/issues --input {body}",
        "heredoc stdin": f"gh pr comment 5 -R o/r --body-file - <<'EOF'\n{text}\nEOF",
        "here-string": f"gh pr comment 5 -R o/r -F - <<< '{q}'",
        "written then posted": f"cat > {Path(tmp) / 'new.md'} <<'EOF'\n{text}\nEOF\n"
                               f"gh pr comment 5 -R o/r --body-file {Path(tmp) / 'new.md'}",
        "bash -c": f"bash -c \"gh pr comment 5 -R o/r -b '{q}'\"",
        "env prefix": f"GH_PAGER= gh pr comment 5 -R o/r -b '{q}'",
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = Path(self.tmp) / "ws"
        pdir = policy.privacy_dir(self.ws)
        pdir.mkdir(parents=True)
        (pdir / policy.DENYLIST_NAME).write_text(f"# test\nrepo: {TERM}\nagent: {AGENT}\n")

    def decide(self, command, lookup=PUBLIC):
        # Each call asks `lookup` afresh rather than reading an earlier call's cached answer.
        (policy.privacy_dir(self.ws) / policy.VISIBILITY_CACHE_NAME).unlink(missing_ok=True)
        return guard.decide(command, self.tmp, self.ws, lookup=lookup)


class EachShape(Base):
    def test_every_shape_blocks_every_kind_of_private_detail(self):
        for needle in DIRTY:
            for name, cmd in shapes(f"see {needle} for details", self.tmp).items():
                with self.subTest(shape=name, needle=needle):
                    reason = self.decide(cmd)
                    self.assertIsNotNone(reason, cmd)
                    self.assertIn(needle.split("/work")[0], reason)

    def test_refusal_names_term_and_category(self):
        reason = self.decide(f"gh pr comment 5 -R o/r -b 'uses {TERM}'")
        self.assertIn(f"`{TERM}` [repo]", reason)
        reason = self.decide(f"gh pr comment 5 -R o/r -b 'ping {MXID}'")
        self.assertIn("[Matrix ID]", reason)

    def test_clean_text_passes_every_shape(self):
        clean = "A private backend repo and agent A, described by function; see src/x.py."
        for name, cmd in shapes(clean, self.tmp).items():
            with self.subTest(shape=name):
                self.assertIsNone(self.decide(cmd), cmd)


class Boundaries(Base):
    def test_private_repo_is_skipped(self):
        for name, cmd in shapes(f"uses {TERM} at {LOCAL}", self.tmp).items():
            if "-R o/r" not in cmd and "repos/o/r" not in cmd and "/o/r/" not in cmd:
                continue
            with self.subTest(shape=name):
                self.assertIsNone(self.decide(cmd, lookup=PRIVATE), cmd)

    def test_unknown_visibility_is_scanned(self):
        self.assertIsNotNone(self.decide(f"gh pr comment 5 -b '{TERM}'", lookup=lambda r: None))

    def test_reads_are_not_writes(self):
        for cmd in (f"gh pr view 5 -R o/r --json body | grep {TERM}",
                    f"gh api repos/o/r/pulls/5 --jq .body  # {TERM}",
                    f"echo {TERM} {LOCAL}", "gh pr review 5 -R o/r --approve"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decide(cmd))

    def test_case_rules(self):
        self.assertIsNone(self.decide("gh pr comment 5 -R o/r -b 'zephyrine is lowercase prose'"))
        self.assertIsNotNone(self.decide(f"gh pr comment 5 -R o/r -b '{TERM.upper()}'"))
        self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b '{TERM}x2'"))

    def test_placeholders_are_not_findings(self):
        text = "e.g. @bob:example.org in !room123:example.com under /" + "Users/runner/work"
        self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b '{text}'"))

    def test_allowlist_and_own_signature(self):
        (policy.privacy_dir(self.ws) / policy.ALLOWLIST_NAME).write_text(f"{TERM}\n")
        self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b 'uses {TERM}'"))
        os.environ["SUTANDO_AGENT_MXID"] = MXID
        try:
            self.assertIsNone(self.decide(f"gh pr comment 5 -R o/r -b '— me ({MXID})'"))
        finally:
            del os.environ["SUTANDO_AGENT_MXID"]


class FailsClosed(Base):
    def assertRefused(self, cmd, why):
        reason = self.decide(cmd)
        self.assertIsNotNone(reason, cmd)
        self.assertIn(why, reason)

    def test_unreadable_body_file(self):
        self.assertRefused("gh pr create -R o/r -t t --body-file /nonexistent/body.md", "cannot read")
        self.assertRefused("gh api repos/o/r/issues/5/comments -F body=@/nonexistent/b", "cannot read")

    def test_stdin_it_cannot_see(self):
        self.assertRefused("printf x | gh pr comment 5 -R o/r --body-file -", "stdin")

    def test_runtime_expansion(self):
        self.assertRefused('gh pr comment 5 -R o/r --body "$BODY"', "expands at run time")
        self.assertRefused("gh pr comment 5 -R o/r --body \"`cat f`\"", "expands at run time")

    def test_file_written_by_same_command_without_heredoc(self):
        f = Path(self.tmp) / "gen.md"
        f.write_text("stale clean text")
        self.assertRefused(f"python3 gen.py > {f} && gh pr comment 5 -R o/r -F {f}", "same command")

    def test_unparseable_line(self):
        self.assertRefused("gh pr comment 5 -R o/r -b 'unterminated", "does not parse")



class Unseeded(Base):
    """No denylist yet: built-in shapes are still enforced, the list is skipped, and
    stderr names the seeder, so an unseeded host is not locked out of public writes."""

    def setUp(self):
        super().setUp()
        (policy.privacy_dir(self.ws) / policy.DENYLIST_NAME).unlink()

    def decide_quiet(self, cmd):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return self.decide(cmd), err.getvalue()

    def test_clean_write_is_allowed_with_a_warning(self):
        reason, err = self.decide_quiet("gh pr comment 5 -R o/r -b 'anything'")
        self.assertIsNone(reason)
        self.assertIn("seed-public-denylist.py", err)

    def test_builtin_shapes_still_refused(self):
        for needle in (LOCAL, MXID, ROOM_ID):
            with self.subTest(needle=needle):
                reason, _ = self.decide_quiet(f"gh pr comment 5 -R o/r -b 'see {needle}'")
                self.assertIsNotNone(reason)

    def test_list_terms_are_not_checked(self):
        reason, _ = self.decide_quiet(f"gh pr comment 5 -R o/r -b 'uses {TERM}'")
        self.assertIsNone(reason)


class ReviewedSlips(Base):
    """Shapes a review found slipping past the first version; each must now be read."""

    def dirty(self):
        return f"see {TERM} for details"

    def assertBlocked(self, cmd):
        reason = self.decide(cmd)
        self.assertIsNotNone(reason, cmd)
        return reason

    def test_wrappers(self):
        b = f"gh pr comment 5 -R o/r -b '{self.dirty()}'"
        for prefix in ("timeout 60 ", "timeout -s KILL 60 ", "timeout --preserve-status 5m ", "nice ",
                       "nice -n 5 ", "nice -10 ", "env -u X ", "env -i PATH=/bin ", "env -- ",
                       "nohup ", "time -p ", "command -p ", "exec -a x ", "sudo -u me ",
                       "stdbuf -oL ", "ionice -c 3 ", "caffeinate -i ", "setsid ",
                       "timeout 60 env -u X nice "):
            with self.subTest(prefix=prefix):
                self.assertBlocked(prefix + b)

    def test_compound_commands(self):
        b = f"gh pr comment 5 -R o/r -b '{self.dirty()}'"
        for cmd in (f"if true; then {b}; fi", f"if {b}; then :; fi",
                    f"for i in 1; do {b}; done", f"while false; do :; done; {b}",
                    f"{{ {b}; }}", f"! {b}", f"`{b}`", f'echo "`{b}`"', f'x="$({b})"',
                    f"f() {{ {b}; }}; f", f"case x in x) {b};; esac"):
            with self.subTest(cmd=cmd):
                self.assertBlocked(cmd)

    def test_script_on_shell_stdin(self):
        for shell in ("bash", "sh -s", "zsh -", "bash -x"):
            cmd = f"{shell} <<'EOF'\ngh pr comment 5 -R o/r -b '{self.dirty()}'\nEOF"
            with self.subTest(shell=shell):
                self.assertBlocked(cmd)
        self.assertBlocked(f"bash <<< \"gh pr comment 5 -R o/r -b '{self.dirty()}'\"")
        self.assertBlocked(f"cat <<'EOF' | bash\ngh pr comment 5 -R o/r -b '{self.dirty()}'\nEOF")
        self.assertIsNone(self.decide("bash <<'EOF'\ngh pr comment 5 -R o/r -b 'clean'\nEOF"))

    def test_unquoted_heredoc_that_expands_is_refused(self):
        for inner in ("$(cat x.md)", "$BODY", "`cat x.md`", "${BODY}"):
            with self.subTest(inner=inner):
                reason = self.assertBlocked(f"gh pr comment 5 -R o/r -F - <<EOF\nhi {inner}\nEOF")
                self.assertIn("unquoted", reason)
        reason = self.assertBlocked("gh pr comment 5 -R o/r -F - <<< \"$BODY\"")
        self.assertIn("unquoted", reason)
        self.assertBlocked("bash <<EOF\ngh pr comment 5 -R o/r -b \"$X\"\nEOF")
        self.assertBlocked(f"cat <<EOF\n$(gh pr comment 5 -R o/r -b '{self.dirty()}')\nEOF")
        # Unquoted with no expansion, or quoted with `$` inside: the text is literal.
        self.assertIsNone(self.decide("gh pr comment 5 -R o/r -F - <<EOF\nplain text\nEOF"))
        self.assertIsNone(self.decide("gh pr comment 5 -R o/r -F - <<'EOF'\ncosts $5 and $(x)\nEOF"))
        self.assertIsNone(self.decide("gh pr comment 5 -R o/r -F - <<EOF\ncosts \\$HOME\nEOF"))

    def test_heredoc_inside_a_cat_substitution(self):
        cmd = "gh pr comment 5 -R o/r --body \"$(cat <<'EOF'\n{}\nEOF\n)\""
        self.assertBlocked(cmd.format(self.dirty()))
        self.assertIsNone(self.decide(cmd.format("clean text, with a ' quote")))
        self.assertBlocked("gh pr comment 5 -R o/r --body \"$(cat <<EOF\n$X\nEOF\n)\"")
        two = ("cat > {f} <<'EOF'\nclean\nEOF\ngh pr comment 5 -R o/r --body \"$(cat <<'EOF'\n{t}\nEOF\n)\"")
        self.assertBlocked(two.format(f=Path(self.tmp) / "a.md", t=self.dirty()))

    def test_attached_short_flags(self):
        body = Path(self.tmp) / "b.md"
        body.write_text(self.dirty())
        d = self.dirty().replace(" ", "_")
        for cmd in (f"gh pr comment 5 -R o/r -b{d}", f"gh pr comment 5 -R o/r -b='{self.dirty()}'",
                    f"gh pr comment 5 -Ro/r '-b{self.dirty()}'", f"gh pr comment 5 -R o/r -F{body}",
                    f"gh pr create -R o/r -dt{d} -b ok", f"gh issue create -R o/r -t{d} -b ok",
                    f"gh api repos/o/r/issues/5/comments -fbody={d}",
                    f"gh api repos/o/r/issues/5/comments -Fbody=@{body}",
                    f"gh api -XPATCH repos/o/r/pulls/5 -fbody={d}"):
            with self.subTest(cmd=cmd):
                self.assertBlocked(cmd)
        self.assertIsNone(self.decide(f"gh api repos/o/r/issues/5 -X GET -fstate={d}"))

    def test_more_verbs(self):
        q = self.dirty()
        for cmd in (f"gh pr merge 5 -R o/r --squash --body '{q}'",
                    f"gh pr merge 5 -R o/r --subject '{q}'", f"gh pr merge 5 -R o/r -st '{q}'",
                    f"gh pr close 5 -R o/r --comment '{q}'", f"gh pr close 5 -R o/r -c '{q}'",
                    f"gh issue close 7 -R o/r -c '{q}'", f"gh issue close 7 -R o/r --comment='{q}'",
                    f"gh repo edit o/r --description '{q}'", f"gh repo edit -d '{q}' o/r",
                    f"gh repo create o/new --public -d '{q}'"):
            with self.subTest(cmd=cmd):
                self.assertBlocked(cmd)
        self.assertIsNone(self.decide("gh pr merge 5 -R o/r --squash"))
        self.assertIsNone(self.decide(f"gh repo edit o/r --description '{q}'", lookup=PRIVATE))

    def test_public_gist(self):
        f = Path(self.tmp) / "notes.md"
        f.write_text(self.dirty())
        self.assertBlocked(f"gh gist create --public {f}")
        clean = Path(self.tmp) / "clean.md"
        clean.write_text("nothing private")
        self.assertBlocked(f"gh gist create -p -d '{self.dirty()}' {clean}")
        self.assertBlocked(f"gh gist create --public - <<'EOF'\n{self.dirty()}\nEOF")
        # A public gist has no repo, so a PRIVATE lookup cannot excuse it.
        self.assertIsNotNone(self.decide(f"gh gist create --public {f}", lookup=PRIVATE))
        self.assertIsNone(self.decide(f"gh gist create --public {clean}"))
        self.assertIsNone(self.decide(f"gh gist create {f}"))

    def test_api_endpoints(self):
        q = self.dirty()
        for ep in ("repos/o/r/releases", "repos/o/r/releases/9", "repos/o/r/commits/abc123/comments",
                   "repos/o/r/comments/4", "/repos/o/r/pulls/5/merge", "repos/o/r", "gists"):
            with self.subTest(ep=ep):
                self.assertBlocked(f"gh api -X POST {ep} -f body='{q}'")
        self.assertIsNone(self.decide(f"gh api -X POST repos/o/r/releases -f body='{q}'", lookup=PRIVATE))

    def test_api_found_by_first_non_flag_word(self):
        q = self.dirty()
        self.assertBlocked(f"gh issue create -R o/r -t api -b '{q}'")
        self.assertBlocked(f"gh pr comment 5 -R o/r -b 'call the api {TERM}'")
        self.assertBlocked(f"gh -R o/r issue comment 7 -b '{q}'")

    def test_body_file_replaced_in_same_command(self):
        body = Path(self.tmp) / "body.md"
        body.write_text("clean text")
        for pre in (f"cp x.md {body}", f"mv x.md {body}", f"sed -i '' 's/a/b/' {body}",
                    f"sed -i.bak 's/a/b/' {body}", f"perl -pi -e 's/a/b/' {body}",
                    f"ln -sf x.md {body}", f"dd if=x.md of={body}", f"printf x | tee {body}"):
            with self.subTest(pre=pre):
                reason = self.assertBlocked(f"{pre} && gh pr comment 5 -R o/r -F {body}")
                self.assertIn("same command", reason)
        self.assertIsNone(self.decide(f"cat {body} && gh pr comment 5 -R o/r -F {body}"))


class RepoFromRemote(Base):
    def git_repo(self, url):
        d = Path(self.tmp) / "checkout"
        d.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "init", "-q", str(d)], check=True, env=env)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True, env=env)
        return d

    def test_bare_write_uses_the_cwd_remote(self):
        d = self.git_repo("git@github.com:o/priv.git")
        self.assertEqual(policy.repo_from_remote(d), "o/priv")
        seen = []
        look = lambda repo: seen.append(repo) or "PRIVATE"  # noqa: E731
        self.assertIsNone(guard.decide(f"gh pr comment 5 -b '{TERM}'", str(d), self.ws, lookup=look))
        self.assertEqual(seen, ["o/priv"])
        (policy.privacy_dir(self.ws) / policy.VISIBILITY_CACHE_NAME).unlink()
        self.assertIsNotNone(guard.decide(f"gh pr comment 5 -b '{TERM}'", str(d), self.ws, lookup=PUBLIC))

    def test_api_placeholder_uses_the_cwd_remote(self):
        d = self.git_repo("https://github.com/o/priv")
        self.assertIsNone(guard.decide(f"gh api repos/{{owner}}/{{repo}}/issues -f body='{TERM}'",
                                       str(d), self.ws, lookup=PRIVATE))

    def test_no_remote_is_unknown(self):
        self.assertIsNone(policy.repo_from_remote(self.tmp))


class SecondReviewFindings(Base):
    """Stdin read only from the gh segment's own heredoc, GH_REPO honoured as gh
    honours it, and the reopen / label / gist edit verbs."""

    ONLY_PUB = staticmethod(lambda repo: "PUBLIC" if repo == "o/pub" else "PRIVATE")

    def test_stdin_is_never_matched_to_an_unrelated_heredoc(self):
        cmd = f"cat <<'EOF' >/dev/null\nharmless\nEOF\necho {TERM} | gh issue comment 1 -R o/r -F -"
        self.assertIn("stdin", self.decide(cmd))
        self.assertIn("stdin", self.decide("cat <<'EOF' | gh pr comment 5 -R o/r -F -\nclean\nEOF"))
        self.assertIsNone(self.decide("gh pr comment 5 -R o/r -F - <<'EOF'\nclean\nEOF"))

    def remote_checkout(self, url):
        d = Path(self.tmp) / "co"
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "init", "-q", str(d)], check=True, env=env)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True, env=env)
        return str(d)

    def run_in(self, cwd, cmd, lookup):
        (policy.privacy_dir(self.ws) / policy.VISIBILITY_CACHE_NAME).unlink(missing_ok=True)
        return guard.decide(cmd, cwd, self.ws, lookup=lookup)

    def test_gh_repo_is_honoured(self):
        priv = self.remote_checkout("https://github.com/o/priv.git")
        b = f"gh pr comment 5 -b '{TERM}'"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GH_REPO", None)
            for cmd in (f"GH_REPO=o/pub {b}", f"env GH_REPO=o/pub {b}", f"export GH_REPO=o/pub; {b}",
                        f"GH_REPO=o/pub; {b}", f"GH_REPO=\"$X\" {b}",
                        f"GH_REPO=o/pub gh api repos/{{owner}}/{{repo}}/issues -f body='{TERM}'"):
                with self.subTest(cmd=cmd):
                    self.assertIsNotNone(self.run_in(priv, cmd, self.ONLY_PUB))
            self.assertIsNone(self.run_in(priv, b, self.ONLY_PUB))
            self.assertIsNone(self.run_in(priv, f"GH_REPO=o/pub gh pr comment 5 -R o/priv -b '{TERM}'",
                                          self.ONLY_PUB))
        with mock.patch.dict(os.environ, {"GH_REPO": "o/pub"}):
            self.assertIsNotNone(self.run_in(priv, b, self.ONLY_PUB))
            self.assertIsNone(self.run_in(priv, f"env -u GH_REPO {b}", self.ONLY_PUB))
            self.assertIsNone(self.run_in(priv, f"unset GH_REPO; {b}", self.ONLY_PUB))
            self.assertIsNone(self.run_in(priv, f"GH_REPO=o/priv {b}", self.ONLY_PUB))

    def test_reopen_label_and_gist_edit(self):
        f = Path(self.tmp) / "g.md"
        f.write_text(f"see {TERM}")
        q = f"see {TERM}"
        for cmd in (f"gh pr reopen 5 -R o/r --comment '{q}'", f"gh issue reopen 7 -R o/r -c '{q}'",
                    f"gh label create '{TERM}' -R o/r", f"gh label create bug -R o/r -d '{q}'",
                    f"gh label edit bug -R o/r --name '{TERM}'", f"gh label edit bug -R o/r --description='{q}'",
                    f"gh gist edit abc123 --desc '{q}'", f"gh gist edit abc123 --add {f}",
                    f"gh gist edit abc123 {f}", f"gh gist edit abc123 -f g.md - <<'EOF'\n{q}\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.decide(cmd))
        self.assertIsNone(self.decide("gh pr reopen 5 -R o/r"))
        self.assertIsNone(self.decide("gh gist edit abc123 -f notes.md"))
        self.assertIsNotNone(self.decide(f"gh gist edit abc123 --desc '{q}'", lookup=PRIVATE))


FAKE_GH = """#!/bin/bash
{ printf '%s\\n' "$@"
  for a in "$@"; do f="${a#--body-file=}"; f="${f#-F}"; [ -f "$f" ] && cat "$f"; done
  [ -t 0 ] || cat; } >> "$GH_SINK"
"""


class RealHookThenBash(unittest.TestCase):
    """Each command goes through the real hook as Claude Code runs it; when the hook
    allows it, real bash runs it against a recording `gh`. Invariant: a denied term
    never reaches the sink of an allowed public write."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "ws"
        pdir = policy.privacy_dir(self.ws)
        pdir.mkdir(parents=True)
        (pdir / policy.DENYLIST_NAME).write_text(f"repo: {TERM}\n")
        self.cache = pdir / policy.VISIBILITY_CACHE_NAME
        now = time.time()
        self.cache.write_text(json.dumps({"o/r": {"visibility": "PUBLIC", "at": now},
                                          "o/priv": {"visibility": "PRIVATE", "at": now}}))
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        (self.cwd / "dirty.md").write_text(f"see {TERM}\n")
        (self.cwd / "body.md").write_text("clean marker\n")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH)
        (self.bin / "gh").chmod(0o755)
        self.sink = self.tmp / "sink.txt"

    def hook(self, command):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.cwd),
                   "hook_event_name": "PreToolUse"}
        env = {k: v for k, v in os.environ.items() if k != "GH_REPO"}
        r = subprocess.run([sys.executable, str(HOOK), "--workspace", str(self.ws)],
                           input=json.dumps(payload), capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def run_command(self, command, extra_env=None):
        """("deny", reason) or ("allow", what the recording gh received)."""
        out = self.hook(command)
        if out:
            self.assertEqual(out["permissionDecision"], "deny")
            return "deny", out["permissionDecisionReason"]
        env = {k: v for k, v in os.environ.items() if k != "GH_REPO"}
        env.update(PATH=f"{self.bin}:{env.get('PATH', '')}", GH_SINK=str(self.sink), **(extra_env or {}))
        subprocess.run(["bash", "-c", command], cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=60)
        return "allow", self.sink.read_text() if self.sink.exists() else ""

    def assertDenied(self, command, why=None, extra_env=None):
        decision, detail = self.run_command(command, extra_env)
        self.assertEqual(decision, "deny", f"{command!r} published: {detail!r}")
        if why:
            self.assertIn(why, detail)

    def assertAllowedClean(self, command, marker="clean marker"):
        decision, sink = self.run_command(command)
        self.assertEqual(decision, "allow", sink)
        self.assertIn(marker, sink)
        self.assertNotIn(TERM, sink)

    def test_the_five_published_shapes_are_denied(self):
        for cmd in (f"> /dev/null gh pr comment 1 -R o/r -b {TERM}",
                    f"2>/dev/null >/dev/null gh pr comment 1 -R o/r -b {TERM}",
                    "cat >> dirty.md <<'EOF'\nclean\nEOF\ngh pr comment 1 -R o/r -F dirty.md",
                    "cat dirty.md > body.md <<'EOF'\nclean\nEOF\ngh pr comment 1 -R o/r -F body.md",
                    "env cp dirty.md body.md\ngh pr comment 1 -R o/r -F body.md",
                    "timeout 5 cp dirty.md body.md && gh pr comment 1 -R o/r -F body.md",
                    f"bash -o pipefail -c \"gh pr comment 1 -R o/r -b {TERM}\"",
                    f"bash -euo pipefail -c \"gh pr comment 1 -R o/r -b {TERM}\"",
                    f"bash -O extglob +o histexpand -c \"gh pr comment 1 -R o/r -b {TERM}\""):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_unknown_writers_and_expanding_targets_fail_closed(self):
        writer = "python3 -c 'import sys,shutil; shutil.copy(\"dirty.md\", sys.argv[1])' body.md"
        for cmd in (f"{writer} && gh pr comment 1 -R o/r -F body.md",
                    "F=body.md; cp dirty.md \"$F\"; gh pr comment 1 -R o/r -F body.md",
                    "cat dirty.md | tee -a body.md >/dev/null; gh pr comment 1 -R o/r -F body.md",
                    "C='gh pr comment 1 -R o/r -F dirty.md'; bash -c \"$C\""):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_clean_controls_reach_gh(self):
        self.assertAllowedClean("gh pr comment 1 -R o/r -F body.md")
        self.assertAllowedClean("> /dev/null gh pr comment 1 -R o/r -b 'clean marker'")
        self.assertAllowedClean("cat > body.md <<'EOF'\nclean marker\nEOF\ngh pr comment 1 -R o/r -F body.md")
        self.assertAllowedClean("bash -o pipefail -c \"gh pr comment 1 -R o/r -b 'clean marker'\"")
        self.assertAllowedClean("cat body.md && gh pr comment 1 -R o/r -F body.md && rm -f other.md")

    def test_public_true_counts_as_public(self):
        self.assertDenied(f"gh repo create o/new --public=true --description 'at {LOCAL}'")
        self.assertDenied("gh gist create --public=true dirty.md")
        self.assertDenied("gh gist create --public=1 dirty.md")
        self.assertIsNone(self.hook("gh gist create --public=false dirty.md"))

    def test_private_to_public_uses_the_destination_and_drops_the_cache(self):
        self.assertDenied(f"gh repo edit o/priv --visibility public --accept-visibility-change-consequences "
                          f"--description 'at {LOCAL}'")
        self.assertDenied(f"gh repo edit o/priv --visibility=public --description '{TERM}'")
        self.assertDenied(f"gh api -X PATCH repos/o/priv -F private=false -f description='{TERM}'")
        self.assertDenied(f"gh api -X PATCH repos/o/priv -f visibility=public -f description='{TERM}'")
        cache = json.loads(self.cache.read_text())
        cache["o/priv"] = {"visibility": "PRIVATE", "at": time.time()}
        self.cache.write_text(json.dumps(cache))
        self.assertIsNone(self.hook("gh repo edit o/priv --visibility public --accept-visibility-change-consequences"))
        self.assertNotIn("o/priv", json.loads(self.cache.read_text()))
        self.assertIn("o/r", json.loads(self.cache.read_text()))

    def test_body_files_go_through_the_shared_bounded_reader(self):
        big = self.cwd / "big.md"
        big.write_text("x" * 65537)
        self.assertDenied("gh pr comment 1 -R o/r -F big.md", "65536")
        (self.cwd / "bad.md").write_bytes(b"clean \xff\xfe")
        self.assertDenied("gh pr comment 1 -R o/r -F bad.md", "UTF-8")
        os.mkfifo(self.cwd / "pipe.md")
        started = time.time()
        self.assertDenied("gh pr comment 1 -R o/r -F pipe.md", "not a regular file")
        self.assertLess(time.time() - started, 30)
        (self.cwd / "max.md").write_text("clean marker " + "y" * (65536 - 13))
        self.assertAllowedClean("gh pr comment 1 -R o/r -F max.md")

    def test_heredoc_escape_parity(self):
        env = {"BODY": LOCAL}
        self.assertDenied("gh pr comment 1 -R o/r -F - <<EOF\nsee \\\\$BODY\nEOF", "unquoted", env)
        self.assertDenied("gh pr comment 1 -R o/r -F - <<EOF\nsee \\\\\\\\$BODY\nEOF", "unquoted", env)
        decision, sink = self.run_command("gh pr comment 1 -R o/r -F - <<EOF\nclean marker \\$BODY\nEOF",
                                          env)
        self.assertEqual(decision, "allow")
        self.assertIn("clean marker $BODY", sink)
        self.assertNotIn(LOCAL, sink)


class FourthReviewFindings(RealHookThenBash):
    """Stream paths, competing stdin sources, and GH_REPO carried into nested shells."""

    def test_stream_body_paths_are_refused(self):
        for path in ("/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"):
            for flag in (f"-F {path}", f"--body-file={path}"):
                cmd = f"cat dirty.md | gh pr comment 1 -R o/r {flag}"
                with self.subTest(cmd=cmd):
                    self.assertDenied(cmd)
        os.symlink("/dev/stdin", self.cwd / "link.md")
        self.assertDenied("cat dirty.md | gh pr comment 1 -R o/r -F link.md")

    def test_one_stdin_source_only(self):
        for cmd in ("gh pr comment 1 -R o/r -F - <<'EOF' < dirty.md\nclean\nEOF",
                    "gh pr comment 1 -R o/r -F - <<'EOF' 0< dirty.md\nclean\nEOF",
                    "gh pr comment 1 -R o/r -F - <<< 'clean' < dirty.md",
                    "< dirty.md gh pr comment 1 -R o/r -F - <<< 'clean'",
                    "gh pr comment 1 -R o/r -F - < dirty.md",
                    "gh pr comment 1 -R o/r -F - 0<&3 <<< clean",
                    "bash <<'EOF' < run.sh\ngh pr comment 1 -R o/r -b clean\nEOF"):
            with self.subTest(cmd=cmd):
                (self.cwd / "run.sh").write_text(f"gh pr comment 1 -R o/r -b {TERM}\n")
                self.assertDenied(cmd)
        self.assertAllowedClean("gh pr comment 1 -R o/r -F - < body.md")
        self.assertAllowedClean("gh pr comment 1 -R o/r -F - <<'EOF'\nclean marker\nEOF")

    def private_checkout(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "init", "-q", str(self.cwd)], check=True, env=env)
        subprocess.run(["git", "-C", str(self.cwd), "remote", "add", "origin",
                        "https://github.com/o/priv.git"], check=True, env=env)

    def test_gh_repo_reaches_nested_shells(self):
        self.private_checkout()
        b = f"gh pr comment 1 -b {TERM}"
        for cmd in (f"GH_REPO=o/r bash -c \"{b}\"", f"env GH_REPO=o/r sh -c \"{b}\"",
                    f"export GH_REPO=o/r; eval \"{b}\"", f"export GH_REPO=o/r; bash -o pipefail -c \"{b}\"",
                    f"export GH_REPO=o/r; x=$({b})", f"GH_REPO=o/r bash <<'EOF'\n{b}\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        # The same nested writes stay allowed when they really land in the private checkout's repo.
        self.assertIsNone(self.hook(f"bash -c \"{b}\""))
        self.assertIsNone(self.hook(f"GH_REPO=o/priv bash -c \"{b}\""))


class FifthReviewFindings(RealHookThenBash):
    """URL targets over --repo, pushd/popd, command runners, de-quoted gh, API repo
    creation, release assets, base64 contents, and the launch-log warning."""

    def allowed(self, cmd):
        self.assertIsNone(self.hook(cmd), cmd)

    def test_a_url_target_beats_repo_flag(self):
        for cmd in (f"gh pr comment https://github.com/o/r/pull/1 -R o/priv -b {TERM}",
                    f"gh issue comment https://github.com/o/r/issues/1 --repo o/priv -b {TERM}",
                    f"gh pr comment -R o/r https://github.com/o/priv/pull/1 -b {TERM}"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        self.allowed(f"gh pr comment https://github.com/o/priv/pull/1 -R o/priv -b {TERM}")
        self.allowed(f"gh pr comment https://github.com/o/priv/pull/1 -b {TERM}")

    def test_directory_stack_is_tracked(self):
        (self.cwd / "sub").mkdir()
        (self.cwd / "sub" / "body.md").write_text(f"see {TERM}\n")
        for cmd in ("pushd sub >/dev/null && gh pr comment 1 -R o/r -F body.md",
                    "cd sub; gh pr comment 1 -R o/r -F body.md",
                    "pushd sub >/dev/null; pushd .. >/dev/null; popd >/dev/null; gh pr comment 1 -R o/r -F body.md",
                    "pushd +1 >/dev/null; gh pr comment 1 -R o/r -F body.md",
                    "(cd sub) && gh pr comment 1 -R o/r -F body.md"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        self.assertAllowedClean("pushd sub >/dev/null && popd >/dev/null && gh pr comment 1 -R o/r -F body.md")
        self.assertAllowedClean("cd sub && cd - >/dev/null && gh pr comment 1 -R o/r -F body.md")
        self.assertAllowedClean(f"pushd sub >/dev/null && gh pr comment 1 -R o/r -F {self.cwd / 'body.md'}")

    def test_gh_under_a_command_runner_is_refused(self):
        for cmd in (f"echo 1 | xargs gh pr comment -R o/r -b {TERM}",
                    f"echo 1 | GH_REPO=o/r xargs gh pr comment -b {TERM}",
                    "echo 1 | xargs -I{} gh pr comment {} -R o/r -b clean",
                    r"find . -name dirty.md -exec gh pr comment 1 -R o/r -F {} \;",
                    f"parallel gh pr comment {{}} -R o/r -b {TERM} ::: 1",
                    f"watch -n 1 gh pr comment 1 -R o/r -b {TERM}",
                    f"script -q /dev/null -c \"gh pr comment 1 -R o/r -b {TERM}\"",
                    f"env -S \"gh pr comment 1 -R o/r -b {TERM}\"",
                    "echo x | xargs gh api repos/o/r/issues -f body=x"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd, "runs under")
        for cmd in ("echo 5 | xargs gh pr view -R o/r", "watch -n 30 gh pr checks 5 -R o/r",
                    "find . -name 'gh-pr*'"):
            with self.subTest(cmd=cmd):
                self.allowed(cmd)

    def test_quoting_cannot_hide_gh(self):
        for cmd in (f"g''h pr comment 1 -R o/r -b {TERM}", f"g\\h pr comment 1 -R o/r -b {TERM}",
                    f"gh p''r comment 1 -R o/r -b {TERM}", f"\"gh\" \"pr\" comment 1 -R o/r -b {TERM}"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_api_repo_creation(self):
        for cmd in (f"gh api user/repos -f name=x -f description='see {TERM}'",
                    f"gh api -X POST /orgs/o/repos -f name=x -f description={TERM}",
                    f"gh api orgs/o/repos -f name=x -F private=false -f description={TERM}"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        self.allowed(f"gh api user/repos -f name=x -F private=true -f description={TERM}")
        self.allowed(f"gh api orgs/o/repos -f name=x -f visibility=internal -f description={TERM}")

    def test_release_asset_text_is_scanned(self):
        (self.cwd / "notes.txt").write_text(f"see {TERM}\n")
        (self.cwd / "ok.txt").write_text("clean\n")
        (self.cwd / "app.bin").write_bytes(b"\x00\xff\xfe binary")
        for cmd in ("gh release create v1 -R o/r notes.txt", "gh release create v1 ok.txt notes.txt -R o/r",
                    "gh release upload v1 -R o/r 'notes.txt#Release notes'"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        self.allowed("gh release create v1 -R o/r ok.txt app.bin -n clean")
        self.allowed("gh release upload v1 -R o/r ok.txt")

    def test_base64_contents_are_decoded(self):
        import base64
        dirty = base64.b64encode(f"see {TERM}\n".encode()).decode()
        clean = base64.b64encode(b"nothing private\n").decode()
        (self.cwd / "c.b64").write_text(dirty)
        for cmd in (f"gh api -X PUT repos/o/r/contents/x.md -f message=add -f content={dirty}",
                    "gh api -X PUT repos/o/r/contents/x.md -f message=add -F content=@c.b64"):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)
        self.allowed(f"gh api -X PUT repos/o/r/contents/x.md -f message=add -f content={clean}")

    def test_the_launch_log_names_the_seeder_until_seeded(self):
        launcher = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"
        ws = self.tmp / "unseeded"

        def warn():
            r = subprocess.run(["bash", "-c", 'REPO="$1"; . "$2"; warn_unseeded_public_denylist "$3"', "_",
                                str(REPO), str(launcher), str(ws)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stderr
        self.assertIn("seed-public-denylist.py", warn())
        target = policy.privacy_dir(ws) / policy.DENYLIST_NAME
        target.parent.mkdir(parents=True)
        target.write_text("# seeded\n")
        self.assertEqual(warn(), "")
        self.assertIn("warn_unseeded_public_denylist\n", launcher.read_text())


class Patterns(unittest.TestCase):
    def test_mixed_case_mxid(self):
        self.assertTrue(policy.scan("ping @Zq-Agent:matrix.zq.chat", []))

    def test_ci_home_paths_are_placeholders(self):
        for user in ("runner", "ubuntu", "linuxbrew"):
            with self.subTest(user=user):
                self.assertEqual(policy.scan(f"at /home/{user}/work/x", []), [])
        self.assertTrue(policy.scan("at /home/jdoe/work/x", []))


class VisibilityCache(unittest.TestCase):
    def test_cached_and_expiring(self):
        cache = Path(tempfile.mkdtemp()) / "vis.json"
        calls = []
        look = lambda r: calls.append(r) or "PRIVATE"  # noqa: E731
        now = time.time()
        self.assertEqual(policy.repo_visibility("o/r", cache, look, now), "PRIVATE")
        self.assertEqual(policy.repo_visibility("O/R", cache, look, now + 60), "PRIVATE")
        self.assertEqual(len(calls), 1)
        policy.repo_visibility("o/r", cache, look, now + policy.TTL_SECONDS["PRIVATE"] + 1)
        self.assertEqual(len(calls), 2)

    def test_unresolvable_repo(self):
        self.assertIsNone(policy.repo_visibility("{owner}/{repo}", None, PUBLIC))
        self.assertIsNone(policy.repo_visibility(None, None, PUBLIC))


class Hook(Base):
    def run_hook(self, command):
        cache = policy.privacy_dir(self.ws) / policy.VISIBILITY_CACHE_NAME
        cache.write_text(json.dumps({"o/r": {"visibility": "PUBLIC", "at": time.time()}}))
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.tmp,
                   "hook_event_name": "PreToolUse"}
        r = subprocess.run([sys.executable, str(HOOK), "--workspace", str(self.ws)],
                           input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def test_end_to_end_deny_and_allow(self):
        out = self.run_hook(f"gh pr create -R o/r -t t --body 'names {TERM}'")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(TERM, out["permissionDecisionReason"])
        self.assertIsNone(self.run_hook("gh pr create -R o/r -t t --body 'clean'"))
        self.assertIsNone(self.run_hook("ls -la"))


    def test_no_gh_skips_the_heavy_imports(self):
        code = ("import sys, runpy, io; sys.stdin = io.StringIO(%r); sys.argv = ['h'];\n"
                "try:\n    runpy.run_path(%r, run_name='__main__')\nexcept SystemExit:\n    pass\n"
                "print('public_write_privacy' in sys.modules, '_shell_scan' in sys.modules)")
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        r = subprocess.run([sys.executable, "-c", code % (payload, str(HOOK))], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.stdout.split(), ["False", "False"], r.stderr)


class HookFailsClosed(unittest.TestCase):
    """A crash or unreadable input on a gh write denies; anything else is left alone."""

    def broken_hook(self):
        root = Path(tempfile.mkdtemp())
        (root / "hooks").mkdir()
        (root / "src").mkdir()
        shutil.copy(HOOK, root / "hooks" / HOOK.name)
        (root / "hooks" / "_shell_scan.py").write_text("raise ImportError('broken on purpose')\n")
        return root / "hooks" / HOOK.name

    def hook(self, hook_path, stdin):
        r = subprocess.run([sys.executable, str(hook_path), "--workspace", tempfile.mkdtemp()],
                           input=stdin, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def test_import_failure_denies_a_gh_write(self):
        hook = self.broken_hook()
        payload = {"tool_name": "Bash", "tool_input": {"command": "gh pr comment 5 -R o/r -b hi"}}
        out = self.hook(hook, json.dumps(payload))
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("guard failed", out["permissionDecisionReason"])
        self.assertIsNone(self.hook(hook, json.dumps({"tool_name": "Bash",
                                                      "tool_input": {"command": "gh --version"}})))

    def test_bad_json_denies_a_gh_write(self):
        out = self.hook(HOOK, '{"tool_name": "Bash", "tool_input": {"command": "gh pr comment 5 -b x"')
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIsNone(self.hook(HOOK, "not json at all"))

    def test_malformed_payload_denies_a_gh_write(self):
        out = self.hook(HOOK, json.dumps({"tool_name": "Bash", "tool_input": "gh pr comment 5 -b x"}))
        self.assertEqual(out["permissionDecision"], "deny")


class DenylistStaysOutOfTheRepo(unittest.TestCase):
    def test_no_tracked_denylist(self):
        tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True,
                                 text=True).stdout.splitlines()
        names = {policy.DENYLIST_NAME, policy.ALLOWLIST_NAME, policy.VISIBILITY_CACHE_NAME}
        self.assertEqual([t for t in tracked if Path(t).name in names], [])

    def test_default_location_is_ignored_by_git(self):
        target = policy.privacy_dir(REPO / "workspace") / policy.DENYLIST_NAME
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", str(target)])
        self.assertEqual(r.returncode, 0, f"{target} would be committable")

    def test_seed_writes_only_the_workspace_and_prints_no_terms(self):
        ws = Path(tempfile.mkdtemp())
        roster = ws / "hosts" / "h1" / "data" / "collaboration-intelligence" / "reviewer-stands.json"
        roster.parent.mkdir(parents=True)
        roster.write_text(json.dumps({"k": {"human": "Quillon Vext", "stand_name": AGENT,
                                            "github": "qvext", "discord_id": 123456789012345678}}))
        (ws / "state").mkdir()
        (ws / "state" / "ag2space-rooms.json").write_text(json.dumps(
            {"rooms": [{"name": "Zq Triage Room", "room_id": ROOM_ID}]}))
        before = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        r = subprocess.run([sys.executable, str(SEED), "--workspace", str(ws), "--no-github"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        written = (policy.privacy_dir(ws) / policy.DENYLIST_NAME).read_text()
        for term in ("Quillon Vext", AGENT, "Zq Triage Room", ROOM_ID, "123456789012345678"):
            self.assertIn(term, written)
            self.assertNotIn(term, r.stdout + r.stderr)
        self.assertNotIn("qvext", written)  # a GitHub login is already public
        after = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
        self.assertEqual(before, after)

    def test_reseed_keeps_manual_lines(self):
        ws = Path(tempfile.mkdtemp())
        target = policy.privacy_dir(ws) / policy.DENYLIST_NAME
        for _ in range(2):
            subprocess.run([sys.executable, str(SEED), "--workspace", str(ws), "--no-github"],
                           capture_output=True, text=True, timeout=120, check=True)
            if "agent: Kept" not in target.read_text():
                target.write_text(target.read_text() + "agent: Kept\n")
        self.assertEqual(target.read_text().count("agent: Kept"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
