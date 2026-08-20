# doc-ingest

Extract readable text from document files — PDF, XLSX/CSV/TSV, DOCX, PPTX, and plain-text formats — so any task that arrives with an attachment can actually consume it.

**Usage**:
```bash
python3 skills/doc-ingest/scripts/ingest.py <file> [<file> ...] [--json] [--csv] [--max-chars N]
```

Prints the extracted text to stdout (default cap 200k chars per file, `--max-chars 0` = uncapped). `--json` wraps each file's result in `{"file", "kind", "ok", "text"|"error"}` lines (JSONL) for programmatic callers. `--csv` switches tabular files to the compute-exact view (below).

## Quantitative questions: compute, don't read

When the ask is quantitative over a tabular file — "how many …", "total …", "average …",
"what percentage …", any filter/sum/count — do NOT answer by reading the extracted markdown.
Load the exact table and **compute**:

```bash
python3 skills/doc-ingest/scripts/ingest.py sheet.xlsx --csv   # exact, uncapped per-sheet CSV
```

then aggregate programmatically (pandas or stdlib `csv`), keeping a per-row breakdown so the
result is auditable. Two exactness guarantees distinguish `--csv` from the reading view:
no default row/char caps (a silently truncated table computes a silently wrong aggregate),
and xlsx without `openpyxl` is refused with a clear error rather than served by the
approximate zip-XML fallback (approximate cells are fine to read, not to compute with).

No caps does not mean no bounds — attachments are untrusted, so `--csv` carries a
fail-closed compute budget: inputs over 32 MiB, renders over 64 MiB, or tables over the
1M-cell cap are **refused with a loud error** (never truncated). A caller that genuinely
needs a bigger table passes the explicit `--csv-no-budget` override.

Why this is a rule and not a preference: on the GAIA file-attached benchmark subset
(2026-07-30), switching solvers from reading extracted text to computing over the loaded
table flipped 3/3 computable misses (multi-row Whyte-notation sums, filtered counts,
parity logic) with no other change — 84.2% → 92.1%. The markdown view is for humans and
summaries; numbers come from computation.

## When to use

- A bridge task carries `[File attached: …]` with a document the task needs read (report summarization, spreadsheet questions, contract review).
- The agent-eval harness runs benchmark tasks that reference attached files.
- Any script needs file-contents-as-text without caring about the format.

Not for:
- **Images** — the agent reads images natively with the Read tool; the script says so and exits 3.
- **Audio** — use `skills/audio-transcribe` (the script points there and exits 3).

## Extraction matrix (best-available chain, graceful fallbacks)

| Format | Primary | Fallback |
|---|---|---|
| `.pdf` | `pdftotext -layout` (poppler) | `pypdf`/`fitz` if importable, else a clear error naming the missing tool |
| `.xlsx` `.xlsm` | `openpyxl` (every sheet → markdown table, row-capped) | dependency-free XML extraction from the zip |
| `.csv` `.tsv` | stdlib `csv` → markdown table (row-capped) | — |
| `.docx` | `python-docx` (paragraphs + tables) | `textutil -convert txt` (macOS), else zip XML extraction |
| `.pptx` | zip XML extraction (per-slide text, dependency-free) | — |
| `.zip` | member manifest + recursive extraction of the first 20 supported members (flattened basenames — zip-slip safe) | — |
| `.txt` `.md` `.json` `.jsonl` `.xml` `.html` code files | bounded streaming read (UTF-8, errors replaced) | — |
| `.rtf` `.doc` | `textutil -convert txt` (macOS) | error naming the gap |

Exit codes: `0` all files extracted · `1` at least one failed · `2` bad invocation · `3` file type is handled elsewhere (image/audio pointer printed).

Tabular rendering defaults to 500 rows per sheet (`--max-rows`). CSV and XLSX
rows are consumed incrementally: only the rendered prefix is retained while the
computed summary is updated over the stream. Hard shared safety budgets cap
compressed/uncompressed table bytes, rows, cells, and cell text; exceeding one
fails the file explicitly instead of risking unbounded attachment memory use.
The dependency-free XLSX reader rejects sparse cell references before they can
expand into a dense row beyond the remaining cell budget or Excel's column
limit. Plain-text inputs are decoded in fixed-size chunks and retain only the
`--max-chars` prefix (`--max-chars 0` is the explicit uncapped mode).
A display-truncation notice is appended whenever the rendered row cap fires, so
a consumer never mistakes a prefix for the whole document.

## Design notes

Pure stdlib + optional libraries probed at runtime — the skill works (with reduced format coverage) on a host with no extras installed, and never hard-depends on a library CI lacks. No network, no temp files, read-only on inputs.
