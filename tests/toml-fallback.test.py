#!/usr/bin/env python3
"""The bundled TOML reader agrees with tomllib on what it reads and refuses what it cannot."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import toml_fallback  # noqa: E402

try:
    import tomllib
except ImportError:  # Python < 3.11: only the fallback's own expectations are checked
    tomllib = None

VALID = {
    "basic strings and escapes": 'a = "x\\ty\\n\\"q\\" \\\\ \\b\\f\\r \\u00e9 \\U0001F600"\n',
    "literal strings": "a = 'C:\\path\\x'\n'quoted key' = 'v'\n",
    "multiline basic": 'a = """\nline1\n  line2 \\\n   joined"""\nb = """x""""\nc = """y"""""\n',
    "multiline escape": 'a = """tab\\there"""\n',
    "multiline literal": "a = '''\nraw \\n text\n'''\nb = '''q''''\n",
    "integers": "a = 42\nb = -17\nc = +3\nd = 1_000\ne = 0xDEAD_beef\nf = 0o755\ng = 0b1101\nh = 0\n",
    "floats": "a = 3.14\nb = -0.5\nc = 5e+22\nd = 1E6\ne = 6.626e-34\nf = 9_224.6\n",
    "booleans": "a = true\nb = false\n",
    "arrays": "a = [1, 2, 3]\nb = [\n  \"x\", # note\n  'y',\n]\nc = [[1, 2], [\"a\"]]\nd = []\ne = [ ]\n",
    "inline tables": "a = {}\nb = { x = 1, y.z = \"w\" }\nc = {x={y=1}}\n",
    "dotted keys": "a.b.c = 1\na.b.d = 2\n\"q.k\".e = 3\n",
    "tables": "# top\n[server]\nhost = \"h\"\n\n[server.tls] # sub\non = true\n[ \"quoted\" . 'x' ]\nk = 1\n",
    "implicit super-table declared later": "[x.y]\na = 1\n[x]\nb = 2\n",
    "arrays of tables": "[[fruit]]\nname = \"apple\"\n[fruit.physical]\ncolor = \"red\"\n"
                       "[[fruit.variety]]\nname = \"red delicious\"\n[[fruit]]\nname = \"banana\"\n",
    "crlf and no trailing newline": "a = 1\r\nb = 2",
    "comment at end of file": "a = 1\n# done",
}

DATES = {
    "a = 1979-05-27T07:32:00Z\n": "1979-05-27T07:32:00Z",
    "a = 1979-05-27T00:32:00.999999-07:00\n": "1979-05-27T00:32:00.999999-07:00",
    "a = 1979-05-27 07:32:00\n": "1979-05-27 07:32:00",
    "a = 1979-05-27\n": "1979-05-27",
    "a = 07:32:00\n": "07:32:00",
    "a = 00:32:00.5\n": "00:32:00.5",
}

SPECIAL_FLOATS = "a = inf\nb = -inf\nc = +inf\nd = nan\n"

INVALID = {
    "duplicate key": "a = 1\na = 2\n",
    "duplicate table": "[a]\n[a]\n",
    "table over a value": "a = 1\n[a]\n",
    "table over a dotted key": "a.b = 1\n[a]\n",
    "dotted key over a value": "a = 1\na.b = 2\n",
    "dotted key into a header table": "[a.b]\nx = 1\n[a]\nb.y = 2\n",
    "extend an inline table": "a = {x = 1}\n[a.b]\n",
    "append to an inline array": "a = [1]\n[[a]]\n",
    "array of tables over a table": "[a]\n[[a]]\n",
    "header through a value": "a = 1\n[a.b]\n",
    "unclosed header": "[a\n",
    "unclosed array-of-tables header": "[[a]\n",
    "missing equals": "a 1\n",
    "invalid key": "= 1\n",
    "trailing garbage": "a = 1 2\n",
    "unsupported value": "a = hello\n",
    "invalid value": "a = \n",
    "leading zero": "a = 01\n",
    "bad date": "a = 2021-02-30\n",
    "bad time": "a = 25:00:00\n",
    "bad offset": "a = 1979-05-27T07:32:00+25:00\n",
    "bad escape": 'a = "\\x"\n',
    "short unicode escape": 'a = "\\u12"\n',
    "surrogate escape": 'a = "\\uD800"\n',
    "out of range escape": 'a = "\\U00110000"\n',
    "unterminated basic": 'a = "abc\n',
    "unterminated basic at eof": 'a = "abc',
    "unterminated literal": "a = 'abc\n",
    "unterminated multiline": 'a = """abc\n',
    "control char in string": 'a = "a\x01b"\n',
    "control char in literal": "a = 'a\x7fb'\n",
    "control char in multiline": 'a = """a\x00b"""\n',
    "control char in comment": "a = 1 # \x01\n",
    "array without comma": "a = [1 2]\n",
    "inline table without comma": "a = {x = 1 y = 2}\n",
}

# TOML 1.1 readers (tomllib on 3.15+) accept these; the 1.0 fallback refuses them.
NEWER_THAN_1_0 = {
    "inline table trailing comma": "a = {x = 1,}\n",
}


class Fallback(unittest.TestCase):
    def test_agrees_with_tomllib(self):
        for name, text in VALID.items():
            with self.subTest(name):
                got = toml_fallback.loads(text)
                if tomllib is not None:
                    self.assertEqual(got, tomllib.loads(text))

    def test_spot_values(self):
        doc = toml_fallback.loads(VALID["multiline basic"])
        self.assertEqual(doc, {"a": "line1\n  line2 joined", "b": 'x"', "c": 'y""'})
        self.assertEqual(toml_fallback.loads(VALID["integers"])["e"], 0xDEADBEEF)
        fruit = toml_fallback.loads(VALID["arrays of tables"])["fruit"]
        self.assertEqual([f["name"] for f in fruit], ["apple", "banana"])

    def test_dates_and_times_return_their_source_text(self):
        for text, want in DATES.items():
            with self.subTest(text):
                self.assertEqual(toml_fallback.loads(text)["a"], want)
                if tomllib is not None:
                    tomllib.loads(text)

    def test_special_floats(self):
        doc = toml_fallback.loads(SPECIAL_FLOATS)
        self.assertEqual((doc["a"], doc["b"], doc["c"]), (float("inf"), float("-inf"), float("inf")))
        self.assertNotEqual(doc["d"], doc["d"])

    def test_refuses_what_it_cannot_read(self):
        for name, text in INVALID.items():
            with self.subTest(name):
                with self.assertRaises(toml_fallback.TOMLDecodeError):
                    toml_fallback.loads(text)
                if tomllib is not None:
                    with self.assertRaises(tomllib.TOMLDecodeError):
                        tomllib.loads(text)

    def test_refuses_toml_1_1_additions(self):
        for name, text in NEWER_THAN_1_0.items():
            with self.subTest(name):
                with self.assertRaises(toml_fallback.TOMLDecodeError):
                    toml_fallback.loads(text)

    def test_decode_error_is_a_value_error_naming_the_line(self):
        with self.assertRaisesRegex(ValueError, r"line 2"):
            toml_fallback.loads("a = 1\na = 2\n")


if __name__ == "__main__":
    unittest.main()
