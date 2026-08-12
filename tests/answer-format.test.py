#!/usr/bin/env python3
"""Tests for skills/answer-format/scripts/normalize.py. Hermetic, no I/O."""
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

# Grouping commas inside an element are not element separators; auto mode
# keeps them, --number-items strips them.
check("list-grouped-numbers-preserved",
      nz.normalize_list("1,000,000, 500,000, 250,000"),
      "1,000,000, 500,000, 250,000")
check("list-grouped-numbers-number-items",
      nz.normalize_list("1,000,000, 500,000, 250,000", number_items=True),
      "1000000, 500000, 250000")
check("list-grouped-mixed-width",
      nz.normalize_list("1,234, 5,678"), "1,234, 5,678")
check("list-grouped-then-word",
      nz.normalize_list("$1,000, Paris"), "$1,000, Paris")
check("list-grouped-decimals",
      nz.normalize_list("1,234.5, 6,789.0"), "1,234.5, 6,789.0")
# No-space numeric list still splits correctly — the comma after "1000" is
# followed by "2000" (4 digits, not a valid 3-digit grouping) → a separator.
check("list-nospace-plain-numbers",
      nz.normalize_list("1000,2000"), "1000, 2000")
# No grouped number has a four-digit lead group, so "1000,200" is two
# elements — the whole token decides, not the individual comma.
check("list-three-digit-second-item",
      nz.normalize_list("1000,200"), "1000, 200")
check("list-three-digit-second-item-number-items",
      nz.normalize_list("1000,200", number_items=True), "1000, 200")
# One token must split consistently: every comma in it gets the same answer.
check("list-three-digit-middle-item",
      nz.normalize_list("1000,200,30"), "1000, 200, 30")
# The lead group governs, not the element count: "1,0000" is not grouped.
check("list-four-digit-tail-not-grouped",
      nz.normalize_list("1,0000"), "1, 0000")
# A grouped number may be followed by a separator comma, so the trailing
# guard rejects a digit only.
check("list-grouped-then-grouped",
      nz.normalize_list("1,000,000, 500,000"), "1,000,000, 500,000")
check("list-grouped-between-words",
      nz.normalize_list("a, 1,000, b"), "a, 1,000, b")
# Auto mode routes a multi-grouped-number answer to list and keeps 3 elements
# (previously 7 garbage fragments).
check("auto-list-grouped-numbers",
      nz.normalize_answer("1,000,000, 500,000, 250,000"),
      "1,000,000, 500,000, 250,000")

# --- auto inference ---
check("auto-number", nz.normalize_answer("100 million"), "100000000")
check("auto-string", nz.normalize_answer("  Claude Shannon "), "Claude Shannon")
check("auto-list", nz.normalize_answer("pears, bananas"), "pears, bananas")
check("auto-comma-number-not-list", nz.normalize_answer("1,234"), "1234")
check("auto-unknown-kind-passthru", nz.normalize_answer("whatever", kind="other"), "whatever")

# --- the exact GAIA L3 miss this recovers ---
check("gaia-100m-miss", nz.normalize_answer("100 million", kind="number"), "100000000")

# --- currency symbol x thousands separator must compose ---
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
# Signed currency: the minus may precede the symbol.
check("currency-negative-before-symbol", nz.normalize_answer("-$1,000"), "-1000")
check("currency-negative-before-symbol-number",
      nz.normalize_answer("-$1,000", kind="number"), "-1000")
check("currency-negative-before-code", nz.normalize_answer("-USD 1,000"), "-1000")
check("currency-negative-magnitude", nz.normalize_answer("-$100 million"), "-100000000")
check("currency-negative-after-symbol", nz.normalize_answer("$-1,000"), "-1000")
check("currency-negative-not-list", nz._infer_kind("-$1,000"), "number")
check("currency-negative-unknown-code-untouched",
      nz.normalize_answer("-AUD 1,000"), "-AUD 1,000")
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
