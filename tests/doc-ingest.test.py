#!/usr/bin/env python3
"""doc-ingest extraction tests (skills/doc-ingest/scripts/ingest.py).

Hermetic: every fixture is generated into a tempdir at run time; formats whose
optional library is missing on this host are exercised through their
dependency-free fallback (pptx/xlsx zip-XML paths) or skipped with a notice.
Run: python3 tests/doc-ingest.test.py
"""
import importlib.util
import io
import json
import tempfile
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "ingest", _ROOT / "skills" / "doc-ingest" / "scripts" / "ingest.py")
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)

passed = []


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ingest.main(argv)
    return code, out.getvalue(), err.getvalue()


def check(name, condition, detail=""):
    assert condition, f"FAIL {name}: {detail}"
    passed.append(name)


def make_pptx(path: Path):
    # Minimal OOXML shell — just enough structure for the slide-XML scrape.
    slide = (b'<?xml version="1.0"?><p:sld xmlns:p="x" xmlns:a="y">'
             b"<p:txBody><a:p><a:r><a:t>Hello from slide one</a:t></a:r></a:p></p:txBody></p:sld>")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide)


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # 1. Plain text passes through, and --max-chars truncates with a notice.
    txt = tmp / "note.txt"
    txt.write_text("alpha beta gamma " * 100)
    code, out, _ = run_cli([str(txt)])
    check("text-read", code == 0 and "alpha beta" in out)
    code, out, _ = run_cli([str(txt), "--max-chars", "50"])
    check("truncation-notice", "truncated at 50 chars" in out, out[-120:])

    # 2. CSV → markdown table, row cap appends a visible notice.
    csvf = tmp / "data.csv"
    csvf.write_text("name,qty\nwidget,3\ngadget,5\nsprocket,7\n")
    code, out, _ = run_cli([str(csvf)])
    check("csv-table", code == 0 and "| name | qty |" in out and "| gadget | 5 |" in out)
    code, out, _ = run_cli([str(csvf), "--max-rows", "2"])
    check("csv-row-cap", "showing 2 of 4 rows" in out, out[-120:])

    # 3. PPTX via the dependency-free zip-XML path (always runs).
    pptx = tmp / "deck.pptx"
    make_pptx(pptx)
    code, out, _ = run_cli([str(pptx)])
    check("pptx-slide-text", code == 0 and "Hello from slide one" in out and "Slide 1" in out)

    # 4. XLSX when openpyxl is available (else skip — fallback covered by pptx path).
    try:
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Inventory"
        ws.append(["part", "count"])
        ws.append(["bolt", 42])
        xlsx = tmp / "inv.xlsx"
        wb.save(str(xlsx))
        code, out, _ = run_cli([str(xlsx)])
        check("xlsx-sheet", code == 0 and "Sheet: Inventory" in out and "| bolt | 42 |" in out)
    except ImportError:
        print("SKIP xlsx-sheet (openpyxl not installed)")

    # 5. DOCX when python-docx is available (else skip).
    try:
        import docx

        d = docx.Document()
        d.add_paragraph("Quarterly summary paragraph.")
        docxf = tmp / "report.docx"
        d.save(str(docxf))
        code, out, _ = run_cli([str(docxf)])
        check("docx-paragraph", code == 0 and "Quarterly summary paragraph." in out)
    except ImportError:
        print("SKIP docx-paragraph (python-docx not installed)")

    # 6. Image → pointer to native reading, exit 3, nothing on stdout.
    img = tmp / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    code, out, err = run_cli([str(img)])
    check("image-pointer", code == 3 and not out.strip() and "Read tool" in err, f"code={code}")

    # 7. Audio → pointer to audio-transcribe.
    audio = tmp / "memo.mp3"
    audio.write_bytes(b"ID3")
    code, out, err = run_cli([str(audio)])
    check("audio-pointer", code == 3 and "audio-transcribe" in err, f"code={code}")

    # 8. Missing file → exit 1 with a stderr report.
    code, out, err = run_cli([str(tmp / "ghost.pdf")])
    check("missing-file", code == 1 and "not a file" in err, f"code={code}")

    # 9. Corrupt PDF → reported as a per-file error, never a traceback.
    bad = tmp / "bad.pdf"
    bad.write_text("this is not a pdf")
    code, out, err = run_cli([str(bad)])
    check("corrupt-pdf-reported", code == 1 and "bad.pdf" in err, f"code={code} err={err[:120]}")

    # 10. --json mode emits one machine-readable line per input, mixed statuses.
    code, out, err = run_cli([str(txt), str(img), "--json"])
    lines = [json.loads(line) for line in out.strip().splitlines()]
    check("json-lines",
          len(lines) == 2 and lines[0]["ok"] is True and lines[1]["ok"] is False
          and lines[1]["kind"] == "image",
          out[:200])

    # 11. Bad invocation (no files) → usage + exit 2.
    code, out, err = run_cli([])
    check("usage-exit-2", code == 2 and "usage:" in err)

    # 12. ZIP → manifest + recursive member extraction; image members deferred.
    zipf = tmp / "bundle.zip"
    with zipfile.ZipFile(zipf, "w") as zf:
        zf.writestr("inner/data.csv", "a,b\n1,2\n")
        zf.writestr("readme.txt", "hello archive")
        zf.writestr("pic.png", "\x89PNG")
    code, out, _ = run_cli([str(zipf)])
    check("zip-recursive",
          code == 0 and "Archive contents" in out and "| a | b |" in out
          and "hello archive" in out and "handled natively" in out,
          out[:300])

print(f"OK — {len(passed)} checks passed: {', '.join(passed)}")
