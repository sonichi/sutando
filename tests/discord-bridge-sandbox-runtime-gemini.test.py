#!/usr/bin/env python3
"""sandbox.runtime: gemini must swap the non-owner Stage-1 command and nothing else.

An install without Codex sends every team/other task into the Stage-2 fallback
sentinel. With the runtime set to gemini the rulebooks delegate to the Gemini CLI
through gemini-sandbox.sh, which keeps codex's `-o FILE -- PROMPT` contract, so Stage 2
and the fallback are unchanged. The codex rulebooks must stay byte identical, and the
PR-review branch (codex specific) must not be touched.

These tests render the rulebook the bridge really writes, through the same builder the
handler calls, rather than a fixture that agrees with the templates by construction.
Review of the first version found three ways the rendering could go quietly wrong:
a reworded heading, a one token drift in the Stage-1 text, and a long workspace path
moving the protected paragraph under stale offsets. Each is a case below.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-sandbox-runtime-")
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')
os.environ.setdefault("DISCORD_BOT_TOKEN", "faketoken")
os.environ["SUTANDO_SANDBOX_RUNTIME"] = "gemini"

try:
    import discord  # noqa: F401
except ImportError:
    print("SKIP — discord.py not importable")
    sys.exit(0)

spec = importlib.util.spec_from_file_location("dbridge", REPO / "src" / "discord-bridge.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["dbridge"] = mod
spec.loader.exec_module(mod)

fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def raises(fn, label, needle=None):
    try:
        fn()
    except ValueError as exc:
        check(needle is None or needle in str(exc), label)
    else:
        check(False, label)


check(mod.SANDBOX_RUNTIME == "gemini", "SUTANDO_SANDBOX_RUNTIME=gemini is honoured at import")
check(hasattr(mod, "_tier_rulebooks"), "the rulebooks are built by a function the tests can call")

# The live text, exactly as the handler builds it.
books = mod._tier_rulebooks('"$(cat /tmp/p)"')
results = str(mod.RESULTS_DIR)
check(set(books) == {"owner", "team-collaborator", "team", "other"}, "four rulebooks, as before")
check(books["owner"] == "" and "gemini" not in books["team-collaborator"].lower(),
      "owner and collaborator books carry no sandbox wording")

# Codex is the identity, on the live text.
check(mod._apply_sandbox_runtime(books, "codex") is books, "codex hands the dict back untouched")
check(mod._render_sandbox_rulebook(books["team"], "codex", tier="team") == books["team"],
      "codex rendering is the identity (team)")
check(mod._render_sandbox_rulebook(books["other"], "codex", tier="other") == books["other"],
      "codex rendering is the identity (other)")

# Gemini, on the live text, with the repo path the bridge would use.
team = mod._render_sandbox_rulebook(books["team"], "gemini", tier="team")
other = mod._render_sandbox_rulebook(books["other"], "gemini", tier="other")
check(f"bash skills/claude-gemini/scripts/gemini-sandbox.sh --cd {mod.REPO} -o {results}/.codex-staging-{{id}}.txt -- " in team,
      "team Stage 1 becomes the gemini wrapper in the workspace, same -o and prompt")
check("codex-bounded.sh --stall 45 --max 240 -- bash skills/claude-gemini" in team,
      "the bounded runner still wraps it")
check(f"gemini-sandbox.sh --cd /tmp -o {results}/.codex-staging-{{id}}.txt -- " in other,
      "other Stage 1 runs in /tmp")
stripped = other.replace("skills/claude-codex/", "").replace("codex-bounded.sh", "").replace(".codex-staging-", "")
check("codex" not in stripped.lower(), "no codex wording is left in the other rulebook, only the two path names")
check("1. RUN GEMINI" in team and "if gemini exits 0" in team and "No gemini call" in team,
      "codex wording becomes gemini outside the PR-review paragraph")
check("Sandbox unavailable (gemini exit <rc>)" in team
      and "Sandbox unavailable (gemini exited 0 with no output)" in team,
      "the sentinels the core is told to write name gemini")
pr_before = books["team"][books["team"].index("2. PR-REVIEW"):books["team"].index("2b. MESSAGE OWNER")]
pr_after = team[team.index("2. PR-REVIEW"):team.index("2b. MESSAGE OWNER")]
check(pr_after == pr_before and "codex exec --sandbox read-only" in pr_after,
      "the PR-review paragraph is byte identical to the codex one")

# A long workspace path must not move the protected paragraph under stale offsets.
long_repo = "/Users/someone/" + "/".join(f"directory-{i:02d}" for i in range(20))
check(len(long_repo) > 250, "the long path is longer than the one that broke the first version")
team_long = mod._render_sandbox_rulebook(books["team"], "gemini", repo=long_repo, tier="team")
pr_long = team_long[team_long.index("2. PR-REVIEW"):team_long.index("2b. MESSAGE OWNER")]
check(pr_long == pr_before, "with a long workspace path the PR-review paragraph is still untouched")
check(f"--cd {long_repo} -o" in team_long, "and the long path is the one the wrapper is given")

# Drift is loud, in every direction that was found.
reworded = books["team"].replace("2b. MESSAGE OWNER", "2b. NOTIFY THE OWNER")
raises(lambda: mod._render_sandbox_rulebook(reworded, "gemini", tier="team"),
       "a reworded PR-review heading raises, naming the markers", "PR-review paragraph markers")
raises(lambda: mod._render_sandbox_rulebook(books["other"] + "2. PR-REVIEW REQUEST stray\n", "gemini", tier="other"),
       "a PR-review marker in the other rulebook raises", "other rulebook")
drifted = books["team"].replace("--stall 45 --max 240 -- codex exec", "--stall 60 --max 240 -- codex exec", 1)
raises(lambda: mod._render_sandbox_rulebook(drifted, "gemini", tier="team"),
       "a one token drift in the team Stage-1 text raises instead of renaming codex exec", "found 0 time(s)")
drifted_other = books["other"].replace("-C /tmp -o", "-C /var/tmp -o", 1)
raises(lambda: mod._render_sandbox_rulebook(drifted_other, "gemini", tier="other"),
       "a one token drift in the other Stage-1 text raises too", "found 0 time(s)")
doubled = books["other"] + "\n" + mod._CODEX_STAGE1_OTHER.format(results=results) + "x < /dev/null\n"
raises(lambda: mod._render_sandbox_rulebook(doubled, "gemini", tier="other"),
       "two Stage-1 commands raise, exactly one is the contract", "found 2 time(s)")
raises(lambda: mod._render_sandbox_rulebook(books["team"], "gemini", tier="owner"),
       "an owner tier is refused by the renderer")
raises(lambda: mod._render_sandbox_rulebook(books["team"], "claude", tier="team"),
       "an unknown runtime is refused")
check(mod._render_sandbox_rulebook(reworded, "codex", tier="team") == reworded,
      "codex stays the identity even when the markers are missing")

# A message carries only the book it uses, so a fault in one book cannot reach another.
sel = mod._select_rulebook
check(sel(books, "owner") == "", "owner messages get the owner book, untouched")
check(sel(books, "team-collaborator") == books["team-collaborator"], "collaborator messages get theirs, untouched")
check("gemini-sandbox.sh" in sel(books, "team") and "gemini-sandbox.sh --cd /tmp" in sel(books, "other"),
      "team and other messages get rendered books")
check(sel(books, "nonsense") == sel(books, "other"), "an unknown key falls back to other, as the handler did")
check(sel(books, "team", runtime="codex") == books["team"], "with codex the selection is the identity")
broken = dict(books, team=reworded)
check(sel(broken, "owner") == "" and sel(broken, "other") == sel(books, "other"),
      "a broken team book does not touch owner or other messages")
raises(lambda: sel(broken, "team"), "and the team message itself raises rather than carrying nonsense")

# Both spellings of the sentinel are recognised, and the helpers produce them.
check(mod.is_sandbox_fallback_sentinel(mod.sandbox_fallback_nonzero(125)), "gemini nonzero sentinel is recognised")
check(mod.is_sandbox_fallback_sentinel(mod.sandbox_fallback_no_output()), "gemini no-output sentinel is recognised")
check(mod.is_sandbox_fallback_sentinel(mod.SANDBOX_FALLBACK_NONZERO.format(rc=124)), "codex nonzero sentinel is still recognised")
check(mod.sandbox_fallback_nonzero(7, "codex") == mod.SANDBOX_FALLBACK_NONZERO.format(rc=7),
      "the helper reproduces the codex constant exactly")
check(not mod.is_sandbox_fallback_sentinel("Sandbox unavailable (gemini exit 1) — no reply generated. Also this."),
      "still an exact match, never a prefix match")

# The startup check is the same renderer over the same live text, so the bridge that
# just imported under runtime=gemini has already proven it renders.
check(re.search(r"^if SANDBOX_RUNTIME != \"codex\":", (REPO / "src" / "discord-bridge.py").read_text(), re.M) is not None,
      "the bridge checks the rendering once at import and refuses to start otherwise")

if fails:
    print(f"\n{len(fails)} FAILED")
    sys.exit(1)
print("\nall passed")
