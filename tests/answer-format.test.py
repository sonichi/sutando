#!/usr/bin/env python3
"""answer-format normalizer tests (skills/answer-format/scripts/normalize.py).

Hermetic, no I/O. Run: python3 tests/answer-format.test.py
"""
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "normalize", _ROOT / "skills" / "answer-format" / "scripts" / "normalize.py")
nz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nz)

passed = []


def check(name, got, want):
    assert got == want, f"FAIL {name}: got {got!r} want {want!r}"
    passed.append(name)


# --- numbers ---
check("word-million", nz.normalize_number("100 million"), "100000000")
check("word-billion-decimal", nz.normalize_number("3.5 billion"), "3500000000")
check("word-thousand", nz.normalize_number("2 thousand"), "2000")
# Exact decimal arithmetic — binary float would emit 2009999.9999999998 etc.
check("decimal-magnitude-exact", nz.normalize_number("2.01 million"), "2010000")
check("decimal-magnitude-half", nz.normalize_number("1.5 million"), "1500000")
check("decimal-magnitude-quarter-billion", nz.normalize_number("0.25 billion"), "250000000")
check("decimal-magnitude-nonwhole", nz.normalize_number("1.2345 thousand"), "1234.5")
check("word-case-plural", nz.normalize_number("5 Millions"), "5000000")
check("commas-stripped", nz.normalize_number("1,234,567"), "1234567")
check("commas-decimal", nz.normalize_number("12,345.67"), "12345.67")
check("currency-strip", nz.normalize_number("$1234"), "1234")
check("percent-strip", nz.normalize_number("42%"), "42")
check("plain-number-untouched", nz.normalize_number("4192"), "4192")
check("negative-untouched", nz.normalize_number("-7.5"), "-7.5")
# non-numeric magnitude-like phrase must NOT be mangled
check("prose-untouched", nz.normalize_number("a million reasons"), "a million reasons")

# --- strings ---
check("quote-strip", nz.normalize_string('"Paris"'), "Paris")
check("single-quote-strip", nz.normalize_string("'mice'"), "mice")
check("whitespace-collapse", nz.normalize_string("  War   is  peace "), "War is peace")
check("article-kept-by-default", nz.normalize_string("The Wharvton"), "The Wharvton")
check("article-dropped-opt", nz.normalize_string("The Wharvton", drop_article=True), "Wharvton")
check("caps-preserved", nz.normalize_string("Claude Shannon"), "Claude Shannon")

# --- lists ---
check("list-spacing", nz.normalize_list("pears,bananas"), "pears, bananas")
check("list-trim", nz.normalize_list(" a , b ,c "), "a, b, c")
check("list-drop-empty", nz.normalize_list("a,,b,"), "a, b")
check("list-sort-opt", nz.normalize_list("mice, humans, cats", sort=True), "cats, humans, mice")
check("list-no-sort-default", nz.normalize_list("mice, humans"), "mice, humans")
check("list-number-items", nz.normalize_list("$5, 10%, 2 million", number_items=True), "5, 10, 2000000")

# --- auto inference ---
check("auto-number", nz.normalize_answer("100 million"), "100000000")
check("auto-string", nz.normalize_answer("  Claude Shannon "), "Claude Shannon")
check("auto-list", nz.normalize_answer("pears, bananas"), "pears, bananas")
check("auto-comma-number-not-list", nz.normalize_answer("1,234"), "1234")
check("auto-unknown-kind-passthru", nz.normalize_answer("whatever", kind="other"), "whatever")

# --- the exact GAIA L3 miss this recovers ---
check("gaia-100m-miss", nz.normalize_answer("100 million", kind="number"), "100000000")

# --- currency symbol × thousands separator must COMPOSE (qingyun CR on #2382:
# auto('$1,000') list-split to '$1, 000'; number('$1,000') passed through intact) ---
check("currency-grouped-auto", nz.normalize_answer("$1,000"), "1000")
check("currency-grouped-number", nz.normalize_answer("$1,000", kind="number"), "1000")
check("currency-grouped-euro-decimal", nz.normalize_answer("€1,234,567.89"), "1234567.89")
check("percent-grouped-compose", nz.normalize_answer("1,000%"), "1000")
check("currency-code-grouped-auto", nz.normalize_answer("USD 1,000"), "1000")
check("currency-code-decimal-number",
      nz.normalize_answer("USD 1,234.56", kind="number"), "1234.56")
check("currency-symbol-magnitude", nz.normalize_answer("$100 million"), "100000000")
check("currency-euro-magnitude-number",
      nz.normalize_answer("€100 million", kind="number"), "100000000")
check("currency-grouped-not-list", nz._infer_kind("$1,000"), "number")
check("currency-code-grouped-not-list", nz._infer_kind("USD 1,000"), "number")
check("unsupported-currency-wrapper-unchanged",
      nz.normalize_answer("AUD 1,000", kind="number"), "AUD 1,000")
check("unsupported-currency-wrapper-auto-unchanged",
      nz.normalize_answer("AUD 1,000"), "AUD 1,000")
check("real-list-still-list", nz._infer_kind("pears, bananas"), "list")

# --- CLI ---
def run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = nz.main(argv)
    return code, out.getvalue().strip()


code, out = run(["--kind", "number", "1,000,000"])
check("cli-number", (code, out), (0, "1000000"))
code, out = run(["--kind", "list", "--sort", "b, a"])
check("cli-list-sort", (code, out), (0, "a, b"))
code, out = run(["--kind", "string", "--drop-article", "the answer"])
check("cli-drop-article", (code, out), (0, "answer"))

print(f"OK — {len(passed)} checks passed: {', '.join(passed)}")
