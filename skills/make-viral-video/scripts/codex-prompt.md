# Codex prompt template — make-viral-video script generation

This is the prompt template fed to `codex exec` (or `codex /goal exec`) during
Phase 1 of the build. Produces a `final_script.md` + `source_table.json` +
asset manifest that downstream phases consume.

The template optimizes for **specificity-shape** output (Chi's 2026-05-09 pick):
one striking moment per video — not comprehensive coverage. This is the explicit
counterpoint to Lucy's v8-v12 lineage, which produced informationally-dense
news-summary-shape output that Chi judged "far from viral."

## Variables

When invoking, substitute:
- `{{TOPIC}}` — short topic name, e.g. "DoW UAP file release"
- `{{SOURCE_URL}}` — primary source URL, e.g. "https://war.gov/UFO"
- `{{TARGET_DURATION_S}}` — target wall-clock seconds, default 45
- `{{ASSET_DIR}}` — where to drop fetched assets, e.g. `state/viral-{ts}/fetched_assets/`
- `{{OUTPUT_DIR}}` — where to write script + tables, e.g. `state/viral-{ts}/artifacts/`

## Prompt body

```
You are generating a script for a short news-explainer video, ~{{TARGET_DURATION_S}}s,
1280×720 landscape, on the topic: {{TOPIC}}.

The video must achieve **specificity-shape virality**: one striking moment that
makes someone share. NOT a comprehensive overview. NOT a news-summary.

# Inputs

Primary source URL: {{SOURCE_URL}}

You will fetch this URL (use curl + Playwright fallback for JS-rendered pages),
read its content, and identify the ONE most striking specific element — a number,
an image, a quote, an event, or a juxtaposition — that lands as a share-moment.

# Output structure (3 parts) — STRICT length budget

**Total narration budget: ≤ {{TARGET_DURATION_S}} seconds at TTS pace.**
At ~120 words/minute (typical TTS rate for Aoede / sage), this means
≤ ⌊{{TARGET_DURATION_S}} × 2.0⌋ words across HOOK + SUPPORT + CLOSER combined.
For a 45s target, that's ~90 words. For a 30s target, ~60 words.

(Empirically: Aoede TTS = ~117 wpm on 2026-05-10 UAP re-run. Conservative budget at 120 wpm leaves 3-5s headroom for inter-sentence silence.)

The ffprobe pre-publish gate enforces narration duration ≤ {{TARGET_DURATION_S}}+5s.
Drift past that is a planning bug — count words before you finalize the script.

## Part 1: HOOK (3-5s narration, ~10-13 words, single striking claim)
- One sentence
- The MOST specific thing in the source — NOT "162 files released" but
  "an FBI report from 1947 about an unidentified object the public has never
  seen" or whatever the most specific anchor in the source actually is
- No hedging modifiers ("reportedly", "may have", etc.) unless the source itself hedges
- Must be defensible from the source — no embellishment
- **Forbidden hook shapes** (Chi 2026-05-10 feedback: "too dry, hook weak"):
  - "X shows Y": "War.gov shows Apollo 17's photo..." — reportorial, not visceral
  - "X says Y": "AP says..." — wire-service lede, doesn't pull viewer in
  - Any sentence that opens with the source name as subject
- **Required hook shape**: open with the surprising element itself, not its provenance.
  Examples (paraphrase only):
  - "Three lights hover above the moon in this 1972 photo." (subject = the thing)
  - "A 1972 Apollo photo just got declassified — and there are three lights." (the reveal IS the hook)
  - "NASA said the dots were nothing. The Pentagon now says: physical object, possibly."
  Attribution belongs in SUPPORT, not HOOK.

## Part 2: SUPPORT (3-5 specific facts, ~5-8s each, total 25-35s, ≤90 words for a 45s target)
- 3-5 supporting facts that build on the hook
- Each fact MUST be attributed (somewhere — not necessarily as the sentence's opening clause).
- **Corroboration rule**: across the 3-5 SUPPORT facts, you MUST cite at least
  2 distinct sources (e.g., war.gov + AP, or war.gov + Reuters + AeroTime).
  A 4-fact script that's "war.gov says... war.gov says... war.gov says...
  war.gov says..." reads as single-sourced and lacks credibility.
  - HOOK can stay single-sourced (the strongest anchor for the one striking
    element).
  - SUPPORT must spread across multiple authoritative sources for
    corroboration. (Susan's 2026-05-10 review: "better to have more than 1 source.")
- **Anti-enumeration rule** (Susan's 2026-05-10 second review: "you just ended up
  enumerating all sources"): SUPPORT must NOT read as a list where every fact
  opens with attribution. Forbidden:
    "war.gov says X. CBS News says Y. war.gov says Z. CBS News says W."
  Required: vary the opening structure across the 3-5 facts. At most ONE fact
  may open with a "<source> says..." clause; the rest must lead with the fact
  itself and cite the source mid-sentence or at the end:
    "Three dots sit in a triangular formation, per the Pentagon caption."
    "Preliminary analysis called a physical object possible — though no consensus, CBS reported."
    "Pilot Ronald Evans logged 'bright fragments drifting' during the maneuver."
- **Storytelling rule** (Susan's 2026-05-10 review: "could use more asks of the
  WHY questions between different [facts]"): at least ONE transition between
  SUPPORT facts should bridge with a curiosity beat — a question, a contrast,
  or an unexpected pivot — not just a flat sequence of independent facts.
  Examples of bridge shapes:
    "...visible only after magnification. Why didn't anyone catch this in 1972?"
    "...called a physical object possible. But the Pentagon stopped short of saying what."
  Bridges count toward the 90-word budget. Use them only if they earn their cost.
- Each fact MUST be PAIRED with a real fetched image OR a clear PIL-rendered
  data card. Do NOT pad with stock footage, generic ufo art, or symbolic imagery.
- The image manifest you produce drives Phase 2 asset validation — see "Asset rules" below.
- If you find yourself needing more than 90 words to support the hook,
  CUT support facts down to 3 strongest. Coverage is the failure mode the
  specificity-shape framing is designed to prevent.

## Part 3: CLOSER (3-5s, ~10-13 words, the share-moment)
- NOT a recap
- NOT "the useful question is ..." (too academic)
- NOT a colon-list-of-attributions ("Three dots, one caption: no consensus, maybe physical object" — Chi 2026-05-10 flagged this as recap-shape disguised as commentary)
- A pointed observation, surprising number, open-ended provocation, or a direct
  participant quote that rewards the viewer for finishing — gives them something
  to share/think about
- **Required closer shape** — pick ONE of:
  1. **Direct quote** from a named participant: "Evans called them 'bright particles drifting.'"
  2. **Anchor stat with implication**: "162 cases. Zero closed."
  3. **Provocation question**: "What did the camera catch that the astronauts couldn't?"
  4. **Stakes-flip**: "The Pentagon won't rule it out. Will you?"
- The closer must end the video with energy that makes someone tap share or
  comment — not with a tidy summary that says "video over, you can move on."

## Length self-check before exit

Count words in your final HOOK + SUPPORT + CLOSER. Multiply by 60 / 155 to
estimate seconds. If estimate exceeds {{TARGET_DURATION_S}}+3s, trim the
weakest support fact and recount. Do this BEFORE writing files. The gate
is a hard contract — drift past it means you ship nothing.

# Asset rules (Phase 2 validation will enforce)

Produce `source_table.json` with one entry per source. Each entry MUST have:
```
{
  "source_title": "...",
  "url_or_path": "<verbatim URL — must literally appear in the source page or be directly fetched>",
  "source_type": "official|wire|secondary",
  "date": "YYYY-MM-DD",
  "key_fact": "<one sentence>",
  "strongest_quote_or_claim": "<verbatim from source>",
  "available_visual_material": "<URL of a real image hosted by source — NOT a guessed pattern>",
  "reliability_level": "high|medium|low"
}
```

**The `url_or_path` and `available_visual_material` fields MUST be URLs you have
actually loaded** — either via your fetch in this run, or that appear verbatim
in HTML you fetched. **Do NOT extrapolate URL patterns.** (Lucy's v12 hallucinated
PR46 URLs that 404'd; Mini's gate will reject any URL not provenance-trailed.)

If the source has fewer than 3 verifiable visual assets, say so in the script
and use PIL-rendered data cards for the gap — do NOT fabricate URLs.

# Output files

Write to {{OUTPUT_DIR}}:
- `final_script.md` — the narration text, sectioned HOOK / SUPPORT / CLOSER
- `source_table.json` — JSON array per the schema above
- `asset_manifest.json` — `[{"url": "...", "alt": "...", "purpose": "hook|support|closer", "provenance": "<which fetched-page or source_table.url_or_path it came from>"}]`

Fetch all assets to {{ASSET_DIR}}. Mini's validator will run on each before
proceeding to render.

# Forbidden patterns (will fail Phase 2 gate)

1. URLs you did not actually load (no PR46-pattern hallucinations)
2. Generic UFO art / stock symbolic imagery as primary visuals
3. Comprehensive coverage shape ("AP says... Reuters says... AeroTime says...
   Live Science says...") — pick the strongest 2-3, not all
4. Closer that summarizes ("So in summary, ..."); closer must be share-shape
5. Hook that starts with the headline number ("162 files released") instead
   of the most specific element

# Self-validation before exit

Before returning, run:
1. `python3 skills/make-viral-video/scripts/validate_asset.py <each fetched asset>`
   — all must return `valid:true`
2. Verify each `url_or_path` and `available_visual_material` either appears in
   `<raw_html_you_fetched>` OR was directly fetched by you (provenance trail)

If any validation fails, drop that asset from the manifest and re-fetch a
substitute or substitute a PIL data card. Do NOT proceed with broken assets.

Return when self-validation passes. Phase 2 of the build will re-run validation
as a gate; your output should match.
```

## Notes on the prompt design

- **Why the hook example "FBI report from 1947"** is specificity-shape: it
  picks ONE thing from the 162 files, makes a viewer think "wait, what?" —
  not "162 files = a lot." The 162-headline lands as the second beat, not
  the first.
- **Why "no comprehensive AP/Reuters/Axios listing"**: Lucy's v12 narrated
  "AP says... AeroTime says... Axios reports... Live Science says..." in
  one breath. It reads as wire-summary-shape; specificity-shape picks 2-3
  strongest sources and quotes them by name, not by enumeration.
- **Why provenance trail required on URLs**: Lucy's PR46 URLs were extrapolated
  from a real pattern (codex saw `PR19` and guessed `PR20-PR99`). The provenance
  rule (URL must literally appear in fetched HTML or be directly loaded by
  codex in this session) catches that pattern without requiring a static
  domain whitelist.
- **Why self-validate before exit**: forces codex to encounter its own broken
  asset URLs before Mini's external gate does. Cuts iteration count.
