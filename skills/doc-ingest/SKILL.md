# doc-ingest

Extract readable text from document files — PDF, XLSX/CSV/TSV, DOCX, PPTX, and plain-text formats — so any task that arrives with an attachment can actually consume it.

**Usage**:
```bash
python3 skills/doc-ingest/scripts/ingest.py <file> [<file> ...] [--json] [--max-chars N]
```

Prints the extracted text to stdout (default cap 200k chars per file, `--max-chars 0` = uncapped). `--json` wraps each file's result in `{"file", "kind", "ok", "text"|"error"}` lines (JSONL) for programmatic callers.

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
| `.txt` `.md` `.json` `.jsonl` `.xml` `.html` code files | direct read (UTF-8, errors replaced) | — |
| `.rtf` `.doc` | `textutil -convert txt` (macOS) | error naming the gap |

Exit codes: `0` all files extracted · `1` at least one failed · `2` bad invocation · `3` file type is handled elsewhere (image/audio pointer printed).

Row caps for tabular formats default to 500 rows per sheet (`--max-rows`); a truncation notice is appended whenever any cap fires, so a consumer never mistakes a prefix for the whole document.

## Design notes

Pure stdlib + optional libraries probed at runtime — the skill works (with reduced format coverage) on a host with no extras installed, and never hard-depends on a library CI lacks. No network, no temp files, read-only on inputs.
