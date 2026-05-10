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

# Output structure (3 parts)

## Part 1: HOOK (3-5s narration, single striking claim)
- One sentence
- The MOST specific thing in the source — NOT "162 files released" but
  "an FBI report from 1947 about an unidentified object the public has never
  seen" or whatever the most specific anchor in the source actually is
- No hedging modifiers ("reportedly", "may have", etc.) unless the source itself hedges
- Must be defensible from the source — no embellishment

## Part 2: SUPPORT (3-5 specific facts, ~5-8s each, 25-35s total)
- 3-5 supporting facts that build on the hook
- Each fact MUST be attributed: "war.gov says...", "AP reports..."
- Each fact MUST be PAIRED with a real fetched image OR a clear PIL-rendered
  data card. Do NOT pad with stock footage, generic ufo art, or symbolic imagery.
- The image manifest you produce drives Phase 2 asset validation — see "Asset rules" below.

## Part 3: CLOSER (3-5s, the share-moment)
- NOT a recap
- NOT "the useful question is ..." (too academic)
- A pointed observation, surprising number, or open-ended provocation that
  rewards the viewer for finishing — gives them something to share/think about
- Examples of closer-shape (paraphrase, not literal):
  - "{specific surprising fact} — and we are still finding more."
  - "{specific number} cases. Government says it cannot resolve them."
  - "{striking quote from a participant}."

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
