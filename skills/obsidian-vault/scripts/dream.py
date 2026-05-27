#!/usr/bin/env python3
"""
dream.py — nightly LLM cross-linking pass for the Obsidian vault.

Scans all .md files under <vault>/Sutando/, identifies pairs that share
proper nouns, then asks the Anthropic API whether they should be cross-linked.
Appends `(cf. [[stem]])` inline for strong/definitive connections.

Opt-in gate: requires SUTANDO_OBSIDIAN_MIRROR=1 or --force flag.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

DEFAULT_MODEL = os.environ.get("SUTANDO_DREAM_MODEL", "claude-opus-4-7")
PROPER_TOKEN_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]{5,}\b")
SENTINEL_START = "<!-- sutando-dream:start -->"
SENTINEL_END = "<!-- sutando-dream:end -->"

MIN_BODY_CHARS = 100   # skip tiny stubs


def _resolve_vault() -> Path:
    workspace = os.environ.get("SUTANDO_WORKSPACE", str(Path.home() / ".sutando" / "workspace"))
    workspace = workspace.replace("~", str(Path.home()))
    return Path(workspace) / "obsidian-vault"


def _opt_in_gate(force: bool) -> bool:
    if force:
        return True
    if os.environ.get("SUTANDO_OBSIDIAN_MIRROR", "").strip() == "1":
        return True
    print(
        "[dream] SUTANDO_OBSIDIAN_MIRROR not set — skipping. "
        "Set to 1 in .env or pass --force to run.",
        file=sys.stderr,
    )
    return False


def _collect_notes(vault: Path) -> list[Path]:
    base = vault / "Sutando"
    if not base.exists():
        return []
    return [
        p for p in base.rglob("*.md")
        if p.stat().st_size >= MIN_BODY_CHARS
    ]


def _proper_nouns(text: str) -> set[str]:
    return set(PROPER_TOKEN_RE.findall(text))


def _pairs_to_check(notes: list[Path]) -> list[tuple[Path, Path]]:
    """Return pairs that share ≥1 proper noun (pre-filter before API call)."""
    noun_map: dict[Path, set[str]] = {}
    for p in notes:
        try:
            noun_map[p] = _proper_nouns(p.read_text(errors="replace"))
        except OSError:
            noun_map[p] = set()

    pairs: list[tuple[Path, Path]] = []
    note_list = list(noun_map.keys())
    for i, a in enumerate(note_list):
        for b in note_list[i + 1:]:
            if noun_map[a] & noun_map[b]:
                pairs.append((a, b))
    return pairs


def _already_linked(body_a: str, stem_b: str, body_b: str, stem_a: str) -> bool:
    return f"[[{stem_b}]]" in body_a or f"[[{stem_a}]]" in body_b


def _existing_dream_stems(body: str) -> set[str]:
    block_match = re.search(
        re.escape(SENTINEL_START) + r"(.*?)" + re.escape(SENTINEL_END),
        body,
        re.DOTALL,
    )
    if not block_match:
        return set()
    return set(re.findall(r"\[\[([^\]]+)\]\]", block_match.group(1)))


def judge_pair(
    client: "anthropic.Anthropic",
    model: str,
    path_a: Path,
    body_a: str,
    path_b: Path,
    body_b: str,
) -> dict:
    """Ask the LLM whether two notes should be cross-linked.

    Returns dict with keys: tier (none/weak/strong/definitive), rationale, inline_refs (list[str]).
    """
    prompt = f"""You are a knowledge graph curator for a personal AI assistant's Obsidian vault.

Two notes are shown below. Decide whether they should be cross-linked.

Note A: {path_a.stem}
---
{body_a[:1500]}

Note B: {path_b.stem}
---
{body_b[:1500]}

Respond with a JSON object (no markdown fences) with these fields:
- "tier": one of "none", "weak", "strong", "definitive"
  - none: no meaningful connection
  - weak: tangential overlap, not worth a link
  - strong: clear conceptual relationship worth cross-referencing
  - definitive: one note is incomplete without the other
- "rationale": one sentence explaining the tier choice
- "inline_refs": list of 0–2 strings, each a sentence fragment ending the paragraph that contains the link.
  Format: "concept shared between the two notes (cf. [[{path_b.stem}]])"
  Only populate for strong/definitive. Empty list for none/weak.

Respond ONLY with the JSON object."""

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        return json.loads(raw)
    except Exception as e:
        return {"tier": "none", "rationale": f"API error: {e}", "inline_refs": []}


def apply_inline_ref(body: str, ref_sentence: str) -> str:
    """Append ref_sentence to the last non-empty paragraph."""
    paragraphs = body.split("\n\n")
    for i in range(len(paragraphs) - 1, -1, -1):
        stripped = paragraphs[i].strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
            paragraphs[i] = paragraphs[i].rstrip() + "\n" + ref_sentence
            break
    return "\n\n".join(paragraphs)


def _update_dream_footer(body: str, new_stems: list[str]) -> str:
    existing_stems = list(_existing_dream_stems(body))
    all_stems = list(dict.fromkeys(existing_stems + new_stems))  # dedupe, preserve order
    if not all_stems:
        return body

    links = "  ".join(f"[[{s}]]" for s in all_stems)
    block = f"{SENTINEL_START}\n> **See also:** {links}\n{SENTINEL_END}"

    # Replace existing block if present
    body = re.sub(
        re.escape(SENTINEL_START) + r".*?" + re.escape(SENTINEL_END),
        block,
        body,
        flags=re.DOTALL,
    )
    if SENTINEL_START not in body:
        body = body.rstrip() + "\n\n" + block + "\n"
    return body


def run(
    vault: Path,
    model: str,
    dry_run: bool,
    quiet: bool,
) -> None:
    if anthropic is None:
        print("[dream] anthropic package not installed — pip install anthropic", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[dream] ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    notes = _collect_notes(vault)
    if not quiet:
        print(f"[dream] {len(notes)} notes collected", flush=True)

    pairs = _pairs_to_check(notes)
    if not quiet:
        print(f"[dream] {len(pairs)} candidate pairs (shared proper nouns)", flush=True)

    updates: dict[Path, list[str]] = {}  # path → stems to add to footer

    for path_a, path_b in pairs:
        try:
            body_a = path_a.read_text(errors="replace")
            body_b = path_b.read_text(errors="replace")
        except OSError:
            continue

        stem_a, stem_b = path_a.stem, path_b.stem

        if _already_linked(body_a, stem_b, body_b, stem_a):
            continue

        if stem_b in _existing_dream_stems(body_a) or stem_a in _existing_dream_stems(body_b):
            continue

        result = judge_pair(client, model, path_a, body_a, path_b, body_b)
        tier = result.get("tier", "none")

        if not quiet:
            print(f"  [{tier}] {stem_a} ↔ {stem_b}: {result.get('rationale','')}", flush=True)

        if tier in ("strong", "definitive"):
            inline_refs = result.get("inline_refs", [])
            if dry_run:
                for ref in inline_refs:
                    print(f"    → would append: {ref!r}", flush=True)
            else:
                if inline_refs:
                    body_a = apply_inline_ref(body_a, inline_refs[0])
                updates.setdefault(path_a, []).append(stem_b)
                updates.setdefault(path_b, []).append(stem_a)

    if dry_run:
        print(f"[dream] dry-run complete — {len(updates)} files would be updated")
        return

    for path, stems in updates.items():
        try:
            body = path.read_text(errors="replace")
            body = _update_dream_footer(body, stems)
            path.write_text(body)
            if not quiet:
                print(f"  [wrote] {path.name}", flush=True)
        except OSError as e:
            print(f"  [err] {path.name}: {e}", file=sys.stderr)

    print(f"[dream] done — {len(updates)} files updated", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Obsidian dream: LLM cross-linking pass")
    parser.add_argument("--force", action="store_true", help="bypass SUTANDO_OBSIDIAN_MIRROR gate")
    parser.add_argument("--dry-run", action="store_true", help="show proposed links, no writes")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model to use")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = parser.parse_args()

    if not _opt_in_gate(args.force):
        sys.exit(0)

    vault = _resolve_vault()
    if not vault.exists():
        print(f"[dream] vault not found at {vault} — run obsidian-mirror.py first", file=sys.stderr)
        sys.exit(1)

    # Run mirror first so vault is up-to-date before linking
    mirror = Path(__file__).resolve().parent.parent.parent / "src" / "obsidian-mirror.py"
    if mirror.exists():
        import subprocess
        cmd = [sys.executable, str(mirror)]
        if args.force:
            cmd.append("--force")
        subprocess.run(cmd, check=False)

    run(vault, model=args.model, dry_run=args.dry_run, quiet=args.quiet)


if __name__ == "__main__":
    main()
