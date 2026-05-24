"""Sutando Obsidian Dream — LLM-judged content linking for the vault.

Replaces the regex relink (vetoed 2026-05-24). For each candidate pair of
notes, asks Claude:
  - Does note A inline-reference note B? (if yes: append `(cf. [[B]])`
    after the referencing sentence)
  - How related are A and B overall? → placed in a tiered, sentinel-marked
    footer block: `## Strongly Related` / `## Related` / `## See also`.

Designed to run nightly (cron) or on-demand (the `run_dream` voice tool in
the obsidian-vault skill's tools.ts).

Model: claude-opus-4-7 by default. Override via `SUTANDO_DREAM_MODEL`.

Scope: only files under `Sutando/Notes/` and `Sutando/Agent/Notes/` —
long-form content. Tasks.md, Asks.md, Thoughts/ are skipped (high churn,
short, ephemeral).

Pre-filter: only LLM a pair if they share at least one capitalized token
of length ≥6 (cheap noun heuristic). Keeps API spend bounded as the vault
grows.

Edit semantics:
  - Footer is FULLY MANAGED by dream — replaces everything between
    `<!-- sutando-dream:start -->` and `<!-- sutando-dream:end -->`.
    User edits above the start marker are preserved.
  - Inline citations are conservative: model must return a verbatim 20-100
    char excerpt from note A's body. We do a substring search; if found
    and `(cf. [[stem]])` isn't already adjacent, insert it after the
    enclosing sentence. Anything we can't anchor verbatim is skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import subprocess

import anthropic

DEFAULT_MODEL = os.environ.get("SUTANDO_DREAM_MODEL", "claude-opus-4-7")
MAX_TOKENS = 1024
SENTINEL_START = "<!-- sutando-dream:start -->"
SENTINEL_END = "<!-- sutando-dream:end -->"
MIN_TOKEN_LEN = 6

# Vault layout — only LLM long-form notes
ELIGIBLE_SUBDIRS = ("Sutando/Notes", "Sutando/Agent/Notes")

FRONTMATTER_BLOCK_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
H1_RE = re.compile(r"^# (.+?)\s*$", re.MULTILINE)
PROPER_TOKEN_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]{5,}\b")
SENTINEL_BLOCK_RE = re.compile(
    re.escape(SENTINEL_START) + r".*?" + re.escape(SENTINEL_END) + r"\n?",
    re.DOTALL,
)

JUDGE_SYSTEM = """You are Sutando's "dream" linker. You read two markdown notes from a personal knowledge vault and judge how they relate.

Return JSON ONLY (no prose, no markdown fences). Schema:
{
  "inline_refs": [
    {"from": "a" | "b", "quote": "<verbatim 20-100 char excerpt from the referencing note that quotes, paraphrases, or directly mentions content from the other note>"}
  ],
  "tier": "strongly_related" | "related" | "see_also" | "none",
  "rationale": "<one short sentence>"
}

Tier rubric:
- strongly_related: same research thread, explicit cross-reference, or the notes are clearly written together.
- related: significant topical overlap (same domain, shared concepts) but independent.
- see_also: tangentially adjacent — might be useful but no strong overlap.
- none: unrelated or only superficial token overlap.

Conservative rule: prefer "none" over a wrong tier. Only emit an inline_ref when one note specifically references content (a sentence, claim, or quote) from the other — not just shared topic words. The `quote` must appear verbatim in the source note.
"""


# ---- IO / paths ----

def resolve_vault() -> Path:
    ws = os.environ.get("SUTANDO_WORKSPACE")
    base = Path(ws).expanduser() if ws else Path.home() / ".sutando" / "workspace"
    return base / "obsidian-vault"


@dataclass(frozen=True)
class Note:
    path: Path
    stem: str
    title: str
    body: str
    proper_tokens: frozenset[str]


def _strip_frontmatter(body: str) -> str:
    return FRONTMATTER_BLOCK_RE.sub("", body, count=1)


def _strip_dream_block(body: str) -> str:
    return SENTINEL_BLOCK_RE.sub("", body)


def collect_notes(vault: Path) -> list[Note]:
    notes: list[Note] = []
    for subdir in ELIGIBLE_SUBDIRS:
        d = vault / subdir
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                raw = p.read_text(encoding="utf-8")
            except Exception:
                continue
            body_clean = _strip_dream_block(_strip_frontmatter(raw))
            h1 = H1_RE.search(body_clean)
            title = h1.group(1).strip() if h1 else p.stem.replace("-", " ")
            tokens = frozenset(t for t in PROPER_TOKEN_RE.findall(body_clean) if len(t) >= MIN_TOKEN_LEN)
            notes.append(Note(path=p, stem=p.stem, title=title, body=raw, proper_tokens=tokens))
    return notes


def candidate_pairs(notes: list[Note]) -> list[tuple[Note, Note]]:
    """Pre-filter: pairs must share ≥1 proper noun of length ≥6."""
    pairs = []
    for i, a in enumerate(notes):
        for b in notes[i + 1:]:
            if a.proper_tokens & b.proper_tokens:
                pairs.append((a, b))
    return pairs


# ---- LLM judgment ----

@dataclass
class Judgment:
    tier: str  # strongly_related | related | see_also | none
    rationale: str
    inline_refs: list[dict]  # [{"from": "a"|"b", "quote": "..."}]


def judge_pair(client: anthropic.Anthropic, model: str, a: Note, b: Note) -> Judgment | None:
    user_msg = (
        f"NOTE A (stem: {a.stem}, title: {a.title})\n\n"
        f"{_strip_frontmatter(_strip_dream_block(a.body)).strip()[:8000]}\n\n"
        f"---\n\n"
        f"NOTE B (stem: {b.stem}, title: {b.title})\n\n"
        f"{_strip_frontmatter(_strip_dream_block(b.body)).strip()[:8000]}\n"
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        print(f"[dream] judge call failed for {a.stem} ↔ {b.stem}: {exc}", flush=True)
        return None
    text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text").strip()
    # Try strict JSON first; fall back to stripping codefences if present.
    for candidate in (text, _strip_codefence(text)):
        try:
            data = json.loads(candidate)
            return Judgment(
                tier=str(data.get("tier", "none")).strip(),
                rationale=str(data.get("rationale", "")).strip(),
                inline_refs=[r for r in data.get("inline_refs", []) if isinstance(r, dict)],
            )
        except json.JSONDecodeError:
            continue
    print(f"[dream] non-JSON response for {a.stem} ↔ {b.stem}: {text[:200]}", flush=True)
    return None


def _strip_codefence(s: str) -> str:
    if s.startswith("```"):
        # strip first line and trailing fence
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        return s.strip()
    return s


# ---- Edit application ----

def apply_inline_ref(body: str, quote: str, target_stem: str) -> tuple[str, bool]:
    """Append `(cf. [[target_stem]])` at the end of the paragraph containing `quote`.

    Paragraph-end (next `\\n\\n` or EOF) — not sentence-end. Reason: periods
    inside code spans like `langfuse.*` confuse sentence detection. Paragraph
    boundaries are unambiguous. Skips if the citation is already present
    anywhere in the file (per-pair de-dup).
    """
    if quote not in body:
        return body, False
    citation = f"(cf. [[{target_stem}]])"
    if citation in body:
        return body, False
    idx = body.index(quote) + len(quote)
    # Walk forward to the end of the current paragraph (next blank line) or EOF.
    para_end = body.find("\n\n", idx)
    if para_end == -1:
        para_end = len(body)
    # Trim trailing whitespace inside the paragraph so the citation sits flush.
    insert_at = para_end
    while insert_at > idx and body[insert_at - 1] in " \t":
        insert_at -= 1
    new_body = body[:insert_at] + f" {citation}" + body[insert_at:]
    return new_body, True


def build_footer(tiered: dict[str, list[tuple[str, str]]]) -> str:
    """tiered: {tier_label: [(stem, rationale), ...]} → markdown."""
    out = [SENTINEL_START]
    label_to_heading = [
        ("strongly_related", "## Strongly Related"),
        ("related", "## Related"),
        ("see_also", "## See also"),
    ]
    any_emitted = False
    for label, heading in label_to_heading:
        items = tiered.get(label, [])
        if not items:
            continue
        any_emitted = True
        out.append("")
        out.append(heading)
        out.append("")
        for stem, rationale in sorted(items):
            line = f"- [[{stem}]]"
            if rationale:
                line += f" — *{rationale}*"
            out.append(line)
    out.append("")
    out.append(SENTINEL_END)
    if not any_emitted:
        # Empty block is fine — keeps the sentinels for future passes.
        out.insert(1, "")
        out.insert(2, "<!-- (no related notes yet) -->")
        out.insert(3, "")
    return "\n".join(out) + "\n"


def upsert_footer(body: str, footer_block: str) -> str:
    """Replace existing dream block (if any) or append. Always keeps a single
    blank line before the block."""
    new = SENTINEL_BLOCK_RE.sub("", body).rstrip()
    return new + "\n\n" + footer_block


# ---- Main pass ----

def run(vault: Path, model: str, dry_run: bool = False) -> int:
    notes = collect_notes(vault)
    if len(notes) < 2:
        print(f"[dream] only {len(notes)} eligible note(s) — skipping", flush=True)
        return 0
    pairs = candidate_pairs(notes)
    print(f"[dream] {len(notes)} eligible notes, {len(pairs)} candidate pairs, model={model}", flush=True)

    client = anthropic.Anthropic()
    # accumulators per-note: stem -> {tier_label: [(other_stem, rationale)]}
    footer_data: dict[str, dict[str, list[tuple[str, str]]]] = {n.stem: {} for n in notes}
    # per-note running body (for inline edits)
    bodies: dict[str, str] = {n.stem: n.body for n in notes}
    inline_count = 0

    for a, b in pairs:
        judgment = judge_pair(client, model, a, b)
        if judgment is None:
            continue
        if judgment.tier in ("strongly_related", "related", "see_also"):
            footer_data[a.stem].setdefault(judgment.tier, []).append((b.stem, judgment.rationale))
            footer_data[b.stem].setdefault(judgment.tier, []).append((a.stem, judgment.rationale))
        for ref in judgment.inline_refs:
            direction = ref.get("from")
            quote = ref.get("quote", "")
            if not isinstance(quote, str) or len(quote) < 12:
                continue
            if direction == "a":
                new_body, edited = apply_inline_ref(bodies[a.stem], quote, b.stem)
                if edited:
                    bodies[a.stem] = new_body
                    inline_count += 1
            elif direction == "b":
                new_body, edited = apply_inline_ref(bodies[b.stem], quote, a.stem)
                if edited:
                    bodies[b.stem] = new_body
                    inline_count += 1
        # Light throttle to be polite to the proxy.
        time.sleep(0.2)

    # Write footers + inline edits.
    written = 0
    for n in notes:
        body = bodies[n.stem]
        footer = build_footer(footer_data.get(n.stem, {}))
        new_body = upsert_footer(body, footer)
        if new_body != n.body:
            if not dry_run:
                n.path.write_text(new_body, encoding="utf-8")
            written += 1
    print(f"[dream] complete — {len(pairs)} pairs judged, {inline_count} inline citations, {written} files updated", flush=True)
    return 0


def main(argv: Iterable[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vault", help="Vault root override.")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model (default: {DEFAULT_MODEL}).")
    p.add_argument("--dry-run", action="store_true", help="Judge + log, but don't write changes.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Bypass the SUTANDO_OBSIDIAN_MIRROR opt-in gate. Use only for explicit user invocations (e.g. the `run_dream` voice tool).",
    )
    args = p.parse_args(list(argv))
    # Opt-in gate. The nightly cron respects this; explicit user invocations
    # (the voice tool) pass --force.
    if not args.force and os.environ.get("SUTANDO_OBSIDIAN_MIRROR", "").lower() not in ("1", "true", "yes", "on"):
        print(
            "[dream] not enabled — set SUTANDO_OBSIDIAN_MIRROR=1 in .env to opt in, "
            "or call with --force for an explicit one-shot run. Exiting.",
            flush=True,
        )
        return 0
    vault = Path(args.vault).expanduser() if args.vault else resolve_vault()
    if not vault.exists():
        print(f"[dream] vault missing: {vault}", file=sys.stderr)
        return 2
    # Sweep the mirror first so the vault reflects the latest agent state
    # before the model judges. Single cron entry covers both halves.
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    mirror_script = repo_root / "src" / "obsidian-mirror.py"
    if mirror_script.exists():
        try:
            subprocess.run(["python3", str(mirror_script), "--force"], check=False, timeout=60)
        except Exception as exc:
            print(f"[dream] pre-sweep mirror failed (continuing): {exc}", flush=True)
    return run(vault, args.model, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
