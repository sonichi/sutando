#!/usr/bin/env python3
"""doc-ingest: extract readable text from document files (PDF/XLSX/CSV/DOCX/PPTX/...).

Best-available extraction with graceful fallbacks — works with stdlib alone,
uses poppler/openpyxl/python-docx/textutil when present. See SKILL.md.
"""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

DEFAULT_MAX_CHARS = 200_000
DEFAULT_MAX_ROWS = 500
MAX_ARCHIVE_DEPTH = 8

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".xml", ".html", ".htm",
    ".yaml", ".yml", ".toml", ".ini", ".log", ".py", ".js", ".ts", ".sh",
    ".c", ".cpp", ".h", ".java", ".go", ".rs", ".rb", ".sql",
}


def _truncate(text: str, max_chars: int) -> str:
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + f"\n\n[doc-ingest: truncated at {max_chars} chars — original {len(text)} chars]"
    return text


def _rows_to_markdown(rows: list[list[str]], max_rows: int) -> str:
    if not rows:
        return "(empty table)"
    shown = rows[: max_rows or None]
    width = max(len(r) for r in shown)
    norm = [r + [""] * (width - len(r)) for r in shown]
    lines = ["| " + " | ".join(str(c) for c in norm[0]) + " |",
             "|" + "---|" * width]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in norm[1:]]
    if max_rows and len(rows) > max_rows:
        lines.append(f"\n[doc-ingest: showing {max_rows} of {len(rows)} rows]")
    return "\n".join(lines)


def _xml_text(payload: bytes) -> str:
    # Dependency-free OOXML text scrape: drop tags, keep text runs.
    text = payload.decode("utf-8", errors="replace")
    text = re.sub(r"<w:p[ >]", "\n<", text)  # paragraph boundaries → newlines
    text = re.sub(r"<a:p>", "\n<", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_pdf(path: Path) -> str:
    tried = []
    if shutil.which("pdftotext"):
        tried.append("pdftotext")
        proc = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    for mod, call in (("pypdf", lambda m: "\n".join((p.extract_text() or "") for p in m.PdfReader(str(path)).pages)),
                      ("fitz", lambda m: "\n".join(p.get_text() for p in m.open(str(path))))):
        try:
            extractor = __import__(mod)
        except ImportError:
            continue
        tried.append(mod)
        try:
            text = call(extractor)
        except Exception:  # noqa: BLE001 — fall through to the next extractor
            continue
        if text.strip():
            return text
        # An extractor that returns no text has NOT succeeded — a no-text-layer
        # or image-only PDF must fall through to the next extractor and then
        # fail honestly, never return an empty string as a successful result.
    if tried:
        raise RuntimeError(f"PDF extraction failed (tried: {', '.join(tried)}) — no text extracted; file may be image-only or corrupt")
    raise RuntimeError("no PDF extractor available (need poppler's pdftotext, pypdf, or PyMuPDF)")


def _col_index(cell_ref: str) -> int:
    """'B2' -> 1 (0-based column). Falls back to 0 if the ref has no letters."""
    letters = re.match(r"[A-Za-z]+", cell_ref or "")
    if not letters:
        return 0
    idx = 0
    for ch in letters.group(0).upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _xlsx_zip_fallback(path: Path, max_rows: int) -> str:
    """Dependency-free worksheet parse (no openpyxl): reads shared strings,
    inline strings, and numeric/raw cell values, placing each cell by its
    column reference so sparse rows stay aligned. This is the extraction the
    skill promises — the old shared-strings-only path silently dropped inline
    strings and numeric cells."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415 — stdlib, lazy for import cost

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(t.text or "" for t in si.iter() if local(t.tag) == "t"))

        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        parts: list[str] = []
        for i, sheet_name in enumerate(sheets, 1):
            root = ET.fromstring(zf.read(sheet_name))
            sheet_data = next((c for c in root if local(c.tag) == "sheetData"), None)
            rows: list[list[str]] = []
            for row in sheet_data or []:
                cells: dict[int, str] = {}
                for c in row:
                    if local(c.tag) != "c":
                        continue
                    col = _col_index(c.get("r", ""))
                    ctype = c.get("t")
                    if ctype == "s":  # shared string: <v> holds the index
                        v = next((x for x in c if local(x.tag) == "v"), None)
                        try:
                            val = shared[int(v.text)] if v is not None and v.text else ""
                        except (ValueError, IndexError):
                            val = ""
                    elif ctype == "inlineStr":  # inline: <is><t>…</t></is>
                        val = "".join(x.text or "" for x in c.iter() if local(x.tag) == "t")
                    else:  # numeric/boolean/raw: <v>
                        v = next((x for x in c if local(x.tag) == "v"), None)
                        val = (v.text or "") if v is not None else ""
                    cells[col] = val
                width = max(cells) + 1 if cells else 0
                rows.append([cells.get(j, "") for j in range(width)])
            body = _rows_to_markdown(rows, max_rows) if rows else "(empty sheet)"
            parts.append(f"## Sheet {i}\n\n{body}")
        return "\n\n".join(parts) if parts else "(xlsx: no worksheet data found)"


def extract_xlsx(path: Path, max_rows: int) -> str:
    try:
        import openpyxl  # noqa: PLC0415

        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = [["" if c is None else c for c in row] for row in ws.iter_rows(values_only=True)]
            parts.append(f"## Sheet: {ws.title}\n\n" + _rows_to_markdown(rows, max_rows))
        return "\n\n".join(parts)
    except ImportError:
        return _xlsx_zip_fallback(path, max_rows)


def extract_csv(path: Path, max_rows: int) -> str:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rows = [list(r) for r in csv.reader(fh, delimiter=delimiter)]
    return _rows_to_markdown(rows, max_rows)


def extract_docx(path: Path, max_rows: int) -> str:
    try:
        import docx  # noqa: PLC0415

        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            rows = [[c.text for c in row.cells] for row in table.rows]
            parts.append(_rows_to_markdown(rows, max_rows))
        return "\n\n".join(parts)
    except ImportError:
        pass
    if shutil.which("textutil"):
        proc = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return proc.stdout
    with zipfile.ZipFile(path) as zf:
        return _xml_text(zf.read("word/document.xml"))


def extract_pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        slides = sorted(n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        return "\n\n".join(f"## Slide {i + 1}\n\n{_xml_text(zf.read(n))}" for i, n in enumerate(slides))


def extract_zip(path: Path, max_rows: int, member_cap: int = 20,
                total_budget: int = 64 * 1024 * 1024,
                _budget: dict[str, int] | None = None, _depth: int = 0) -> str:
    # Archive → manifest + recursive extraction of the first N supported members.
    # RESOURCE BOUNDS (attachments are untrusted): cap the member count AND the
    # cumulative uncompressed bytes we materialize, so a zip-bomb / oversized
    # archive can't exhaust the host's disk or memory. A member whose declared
    # uncompressed size would blow the budget is skipped, not written.
    if _depth >= MAX_ARCHIVE_DEPTH:
        return f"[skipped — archive nesting exceeds {MAX_ARCHIVE_DEPTH} levels]"
    budget = _budget if _budget is not None else {
        "members": member_cap,
        "bytes": total_budget,
    }
    with zipfile.ZipFile(path) as zf:
        infos = [inf for inf in zf.infolist() if not inf.filename.endswith("/")]
        names = [inf.filename for inf in infos]
        parts = ["## Archive contents\n\n" + "\n".join(f"- {n}" for n in names[:200])]
        import tempfile  # noqa: PLC0415

        stopped = False
        processed = 0
        with tempfile.TemporaryDirectory() as td:
            for inf in infos:
                name = inf.filename
                if budget["members"] <= 0:
                    if _depth == 0 and processed == member_cap:
                        parts.append(
                            f"[doc-ingest: extracted first {member_cap} of "
                            f"{len(names)} members — shared archive member budget reached]"
                        )
                    else:
                        parts.append(f"[doc-ingest: shared archive member budget "
                                     f"({member_cap}) reached]")
                    stopped = True
                    break
                if inf.file_size > budget["bytes"]:
                    parts.append(f"## {name}\n\n[skipped — shared archive extraction budget "
                                 f"({total_budget // (1024 * 1024)} MiB) reached]")
                    stopped = True
                    break
                budget["members"] -= 1
                budget["bytes"] -= inf.file_size
                processed += 1
                target = Path(td) / Path(name).name  # flatten: zip-slip-safe, basename only
                target.write_bytes(zf.read(name))
                try:
                    kind, text = extract(
                        target, max_rows,
                        _archive_budget=budget,
                        _archive_depth=_depth + 1,
                    )
                    if kind in {"image", "audio"}:
                        parts.append(f"## {name}\n\n[{kind} member — handled natively, not extracted]")
                    else:
                        parts.append(f"## {name} ({kind})\n\n{text}")
                except Exception as exc:  # noqa: BLE001 — one bad member must not sink the archive
                    parts.append(f"## {name}\n\n[extraction failed: {exc}]")
        if len(names) > member_cap and not stopped:
            parts.append(f"[doc-ingest: extracted first {member_cap} of {len(names)} members]")
    return "\n\n".join(parts)


def extract_textutil(path: Path) -> str:
    if not shutil.which("textutil"):
        raise RuntimeError(f"no extractor for {path.suffix} (textutil unavailable on this host)")
    proc = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"textutil failed on {path.name}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def extract(path: Path, max_rows: int,
            _archive_budget: dict[str, int] | None = None,
            _archive_depth: int = 0) -> tuple[str, str]:
    """Returns (kind, text). Raises on failure; special kinds 'image'/'audio' carry a pointer."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image", "doc-ingest: images are read natively by the agent (Read tool) — no extraction here."
    if suffix in AUDIO_SUFFIXES:
        return "audio", "doc-ingest: use skills/audio-transcribe for audio files."
    if suffix == ".pdf":
        return "pdf", extract_pdf(path)
    if suffix in {".xlsx", ".xlsm"}:
        return "xlsx", extract_xlsx(path, max_rows)
    if suffix in {".csv", ".tsv"}:
        return "table", extract_csv(path, max_rows)
    if suffix == ".docx":
        return "docx", extract_docx(path, max_rows)
    if suffix == ".pptx":
        return "pptx", extract_pptx(path)
    if suffix == ".zip":
        return "zip", extract_zip(
            path, max_rows,
            _budget=_archive_budget,
            _depth=_archive_depth,
        )
    if suffix in {".rtf", ".doc"}:
        return "textutil", extract_textutil(path)
    if suffix in TEXT_SUFFIXES or not suffix:
        return "text", path.read_text(encoding="utf-8", errors="replace")
    # Unknown suffix: try text read — better a replaced-chars dump than a refusal.
    return "text?", path.read_text(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    args = list(argv)
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    max_chars, max_rows = DEFAULT_MAX_CHARS, DEFAULT_MAX_ROWS
    for flag, setter in (("--max-chars", "chars"), ("--max-rows", "rows")):
        if flag in args:
            i = args.index(flag)
            try:
                value = int(args[i + 1])
            except (IndexError, ValueError):
                print(f"doc-ingest: {flag} needs an integer", file=sys.stderr)
                return 2
            if setter == "chars":
                max_chars = value
            else:
                max_rows = value
            del args[i:i + 2]
    if not args:
        print("usage: ingest.py <file> [<file> ...] [--json] [--max-chars N] [--max-rows N]", file=sys.stderr)
        return 2

    had_failure = had_pointer = False
    for name in args:
        path = Path(name)
        if not path.is_file():
            had_failure = True
            result = {"file": name, "kind": "missing", "ok": False, "error": "not a file"}
        else:
            try:
                kind, text = extract(path, max_rows)
                if kind in {"image", "audio"}:
                    had_pointer = True
                    result = {"file": name, "kind": kind, "ok": False, "error": text}
                else:
                    result = {"file": name, "kind": kind, "ok": True, "text": _truncate(text, max_chars)}
            except Exception as exc:  # noqa: BLE001 — every per-file failure must be reported, not raised
                had_failure = True
                result = {"file": name, "kind": "error", "ok": False, "error": str(exc)}
        if as_json:
            print(json.dumps(result, ensure_ascii=False))
        elif result["ok"]:
            header = f"===== {name} ({result['kind']}) =====\n" if len(args) > 1 else ""
            print(header + result["text"])
        else:
            print(f"doc-ingest: {name}: {result['error']}", file=sys.stderr)

    # Aggregate exit status must reflect the per-file results: any hard failure
    # dominates (1); otherwise any pointer-only result (image/audio, ok:false)
    # yields 3 — even in a mixed batch, so a caller never reads exit 0 while a
    # record is ok:false. All-extracted → 0.
    if had_failure:
        return 1
    return 3 if had_pointer else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
