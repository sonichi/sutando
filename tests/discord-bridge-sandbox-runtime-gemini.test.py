#!/usr/bin/env python3
"""sandbox.runtime: gemini must swap the non-owner Stage-1 command and nothing else.

An install without Codex sends every team/other task into the Stage-2 fallback
sentinel. With the runtime set to gemini the rulebooks delegate to the Gemini
CLI through gemini-sandbox.sh, which keeps codex's `-o FILE -- PROMPT` contract,
so Stage 2 and the fallback are unchanged. The codex rulebooks must stay
byte-identical, and the PR-review branch (codex-specific) must not be touched.
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


check(mod.SANDBOX_RUNTIME == "gemini", "SUTANDO_SANDBOX_RUNTIME=gemini is honoured at import")

# A codex-worded rulebook the way the bridge writes it, with the PR-review paragraph.
results = str(mod.RESULTS_DIR)
codex_team = (
    "1. RUN CODEX — for genuine requests.\n"
    f"   - Stage 1: {mod._CODEX_STAGE1_TEAM.format(results=results)}\"$(cat /p)\" < /dev/null   (kills the codex tree)\n"
    f"   - Stage 2: if codex exits 0 AND {results}/.codex-staging-{{id}}.txt is non-empty: mv it\n"
    "   - Stage 2 fallback: 'Sandbox unavailable (codex exit <rc>) — no reply generated.'\n"
    "2. PR-REVIEW REQUEST — run review-pr.sh, which inlines the diff into `codex exec --sandbox read-only`.\n"
    "2b. MESSAGE OWNER — write results/proactive-{ts}.txt\n"
    "3. NO-REPLY — 'Sandbox unavailable (codex exited 0 with no output) — no reply generated.'. No codex call.\n"
)
codex_other = (
    "You MUST delegate to a sandboxed Codex agent.\n"
    f"  Stage 1: {mod._CODEX_STAGE1_OTHER.format(results=results)}\"$(cat /p)\" < /dev/null\n"
    "- -C /tmp sets cwd so Codex cannot read project files.\n"
)

check(mod._render_sandbox_rulebook(codex_team, "codex") == codex_team, "codex rendering is the identity (team)")
check(mod._render_sandbox_rulebook(codex_other, "codex") == codex_other, "codex rendering is the identity (other)")

team = mod._render_sandbox_rulebook(codex_team, "gemini", repo="/ws", results=results, pr_review=True)
other = mod._render_sandbox_rulebook(codex_other, "gemini", repo="/ws", results=results, pr_review=False)

check(f"bash skills/claude-gemini/scripts/gemini-sandbox.sh --cd /ws -o {results}/.codex-staging-{{id}}.txt -- \"$(cat /p)\" < /dev/null" in team,
      "team Stage 1 becomes the gemini wrapper in the workspace, same -o and prompt")
check("codex-bounded.sh --stall 45 --max 240 -- bash skills/claude-gemini" in team,
      "the bounded runner still wraps it")
check(f"gemini-sandbox.sh --cd /tmp -o {results}/.codex-staging-{{id}}.txt -- " in other,
      "other Stage 1 runs in /tmp")
stripped = other.replace("skills/claude-codex/", "").replace("codex-bounded.sh", "").replace(".codex-staging-", "")
check("codex" not in stripped.lower(), "no codex wording is left in the other rulebook, only the two path names")
check("skills/claude-codex/scripts/codex-bounded.sh" in other and ".codex-staging-" in other,
      "the bounded runner path and the staging file name are kept, so Stage 2 is unchanged")
check("1. RUN GEMINI" in team and "if gemini exits 0" in team and "No gemini call" in team,
      "codex wording becomes gemini outside the PR-review paragraph")
check("Sandbox unavailable (gemini exit <rc>)" in team
      and "Sandbox unavailable (gemini exited 0 with no output)" in team,
      "the sentinels the core is told to write name gemini")
pr = team[team.index("2. PR-REVIEW"):team.index("2b. MESSAGE OWNER")]
check("codex exec --sandbox read-only" in pr and "gemini" not in pr,
      "the PR-review paragraph is untouched")
check("so Gemini cannot read project files" in other, "capitalised mentions follow too")

# Both spellings of the sentinel are recognised, and the helpers produce them.
check(mod.is_sandbox_fallback_sentinel(mod.sandbox_fallback_nonzero(125)), "gemini nonzero sentinel is recognised")
check(mod.is_sandbox_fallback_sentinel(mod.sandbox_fallback_no_output()), "gemini no-output sentinel is recognised")
check(mod.is_sandbox_fallback_sentinel(mod.SANDBOX_FALLBACK_NONZERO.format(rc=124)), "codex nonzero sentinel is still recognised")
check(mod.sandbox_fallback_nonzero(7, "codex") == mod.SANDBOX_FALLBACK_NONZERO.format(rc=7),
      "the helper reproduces the codex constant exactly")
check(not mod.is_sandbox_fallback_sentinel("Sandbox unavailable (gemini exit 1) — no reply generated. Also this."),
      "still an exact match, never a prefix match")

# The guard is loud, not silent: a reworded heading raises instead of renaming the
# codex-specific paragraph into instructions for a command that does not exist.
reworded = codex_team.replace("2b. MESSAGE OWNER", "2b. NOTIFY THE OWNER")
try:
    mod._render_sandbox_rulebook(reworded, "gemini", repo="/ws", results=results, pr_review=True)
    check(False, "a reworded PR-review heading raises")
except ValueError as exc:
    check("PR-review paragraph markers" in str(exc), "a reworded PR-review heading raises, naming the markers")
try:
    mod._render_sandbox_rulebook(codex_other + "2. PR-REVIEW REQUEST stray\n", "gemini", repo="/ws", results=results, pr_review=False)
    check(False, "a PR-review marker in the other rulebook raises")
except ValueError:
    check(True, "a PR-review marker in the other rulebook raises")
check(mod._render_sandbox_rulebook(reworded, "codex", pr_review=True) == reworded,
      "codex stays the identity even when the markers are missing")

# The builder hands the whole dict through _apply_sandbox_runtime.
books = {"owner": "", "team-collaborator": "engage", "team": codex_team, "other": codex_other}
check(mod._apply_sandbox_runtime(books, "codex") is books, "codex hands the dict back untouched")
applied = mod._apply_sandbox_runtime(books, "gemini")
check(applied["owner"] == "" and applied["team-collaborator"] == "engage", "owner and collaborator rulebooks are not rendered")
check("gemini-sandbox.sh" in applied["team"] and "gemini-sandbox.sh --cd /tmp" in applied["other"], "team and other are")
check(mod._apply_sandbox_runtime(books)["other"] == applied["other"], "the default runtime is the configured one")

try:
    mod._render_sandbox_rulebook(codex_team, "claude")
    check(False, "an unknown runtime is refused")
except ValueError:
    check(True, "an unknown runtime is refused")

if fails:
    print(f"\n{len(fails)} FAILED")
    sys.exit(1)
print("\nall passed")
