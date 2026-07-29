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
            return call(extractor)
        except Exception:  # noqa: BLE001 — fall through to the next extractor
            continue
    if tried:
        raise RuntimeError(f"PDF extraction failed (tried: {', '.join(tried)}) — file may be corrupt or image-only")
    raise RuntimeError("no PDF extractor available (need poppler's pdftotext, pypdf, or PyMuPDF)")


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
        with zipfile.ZipFile(path) as zf:
            shared = "".join(_xml_text(zf.read(n)) for n in zf.namelist() if n == "xl/sharedStrings.xml")
            return shared or "(xlsx: openpyxl not installed; only shared strings extracted)"


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


def extract_textutil(path: Path) -> str:
    if not shutil.which("textutil"):
        raise RuntimeError(f"no extractor for {path.suffix} (textutil unavailable on this host)")
    proc = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"textutil failed on {path.name}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def extract(path: Path, max_rows: int) -> tuple[str, str]:
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

    if had_failure:
        return 1
    return 3 if had_pointer and len(args) == 1 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
