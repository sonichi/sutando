"""A strict TOML reader for interpreters older than 3.11, which have no tomllib.

`loads(text)` covers the TOML 1.0 a config file is written in: tables, arrays of
tables, dotted and quoted keys, all string forms, numbers, booleans, arrays and
inline tables. Dates and times come back as their source text. Anything it cannot
read with certainty raises TOMLDecodeError (a ValueError) instead of guessing,
so a caller that validates before writing refuses rather than corrupting a file.
"""
import datetime
import re

__all__ = ["TOMLDecodeError", "loads"]


class TOMLDecodeError(ValueError):
    pass


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ATOM = re.compile(r"[0-9A-Za-z_+\-.:]+")
_DATE_TIME_GAP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:")
_DEC = r"[+-]?(?:0|[1-9](?:_?\d)*)"
_INT = re.compile(rf"{_DEC}|0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*|0o[0-7](?:_?[0-7])*|0b[01](?:_?[01])*")
_FLOAT = re.compile(rf"{_DEC}(?:\.\d(?:_?\d)*)?(?:[eE][+-]?\d(?:_?\d)*)?|[+-]?(?:inf|nan)")
_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})?)?"
    r"|\d{2}:\d{2}:\d{2}(?:\.\d+)?")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def loads(text):
    return _Parser(text).parse()


class _Parser:
    def __init__(self, text):
        self.s = text.replace("\r\n", "\n")
        self.i = 0
        self.root = {}
        self.frozen = set()    # inline tables and arrays: never extended later
        self.declared = set()  # tables a header or a dotted key already defined

    def fail(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise TOMLDecodeError(f"{msg} (line {line})")

    def peek(self, n=1):
        return self.s[self.i:self.i + n]

    def skip_ws(self):
        while self.peek() in (" ", "\t"):
            self.i += 1

    def skip_comment(self):
        if self.peek() == "#":
            end = self.s.find("\n", self.i)
            body = self.s[self.i:] if end < 0 else self.s[self.i:end]
            if any(ord(c) < 0x20 and c != "\t" or c == "\x7f" for c in body):
                self.fail("control character in comment")
            self.i = len(self.s) if end < 0 else end

    def skip_blank(self):
        while True:
            self.skip_ws()
            self.skip_comment()
            if self.peek() != "\n":
                return
            self.i += 1

    def end_of_line(self):
        self.skip_ws()
        self.skip_comment()
        if self.peek() == "\n":
            self.i += 1
        elif self.peek():
            self.fail("expected end of line")

    def parse(self):
        current, section_dotted = self.root, set()
        while True:
            self.skip_blank()
            if not self.peek():
                return self.root
            if self.peek(2) == "[[":
                self.i += 2
                path = self.key_path()
                self.skip_ws()
                if self.peek(2) != "]]":
                    self.fail("expected ]]")
                self.i += 2
                parent = self.walk(path[:-1])
                arr = parent.setdefault(path[-1], [])
                if not isinstance(arr, list) or id(arr) in self.frozen:
                    self.fail(f"cannot append to {'.'.join(path)}")
                current = {}
                arr.append(current)
            elif self.peek() == "[":
                self.i += 1
                path = self.key_path()
                self.skip_ws()
                if self.peek() != "]":
                    self.fail("expected ]")
                self.i += 1
                parent = self.walk(path[:-1])
                current = parent.setdefault(path[-1], {})
                if not isinstance(current, dict) or id(current) in self.frozen | self.declared:
                    self.fail(f"cannot declare {'.'.join(path)} twice")
                self.declared.add(id(current))
            else:
                self.key_value(current, section_dotted)
                self.end_of_line()
                continue
            section_dotted = set()
            self.end_of_line()

    def walk(self, path):
        node = self.root
        for key in path:
            node = node.setdefault(key, {})
            if isinstance(node, list) and node and id(node) not in self.frozen:
                node = node[-1]
            if not isinstance(node, dict) or id(node) in self.frozen:
                self.fail(f"{key} is not an extendable table")
        return node

    def key_path(self):
        path = []
        while True:
            self.skip_ws()
            path.append(self.key())
            self.skip_ws()
            if self.peek() != ".":
                return path
            self.i += 1

    def key(self):
        if self.peek() == '"':
            return self.basic_string()
        if self.peek() == "'":
            return self.literal_string()
        m = _BARE_KEY.match(self.s, self.i)
        if not m:
            self.fail("invalid key")
        self.i = m.end()
        return m.group()

    def key_value(self, table, dotted):
        path = self.key_path()
        if self.peek() != "=":
            self.fail("expected =")
        self.i += 1
        self.skip_ws()
        value = self.value()
        for key in path[:-1]:
            if key not in table:
                table[key] = {}
                dotted.add(id(table[key]))
                self.declared.add(id(table[key]))
            table = table[key]
            if not isinstance(table, dict) or id(table) not in dotted:
                self.fail(f"cannot extend {key} with a dotted key")
        if path[-1] in table:
            self.fail(f"duplicate key {path[-1]}")
        table[path[-1]] = value

    def value(self):
        c = self.peek()
        if self.peek(3) == '"""':
            return self.multiline_string('"""')
        if self.peek(3) == "'''":
            return self.multiline_string("'''")
        if c == '"':
            return self.basic_string()
        if c == "'":
            return self.literal_string()
        if c == "[":
            return self.array()
        if c == "{":
            return self.inline_table()
        for word, val in (("true", True), ("false", False)):
            if self.s.startswith(word, self.i) and not _ATOM.match(self.s, self.i + len(word)):
                self.i += len(word)
                return val
        return self.atom()

    def atom(self):
        if _DATE_TIME_GAP.match(self.s, self.i):
            m = _DATETIME.match(self.s, self.i)
        else:
            m = _ATOM.match(self.s, self.i)
        if not m:
            self.fail("invalid value")
        token = m.group()
        self.i = m.end()
        if _INT.fullmatch(token):
            return int(token.replace("_", ""), 0) if token[:2] in ("0x", "0o", "0b") else int(token.replace("_", ""))
        if _FLOAT.fullmatch(token):
            return float(token.replace("_", ""))
        if _DATETIME.fullmatch(token):
            self.check_datetime(token)
            return token
        self.fail(f"unsupported value {token!r}")

    def check_datetime(self, token):
        date = re.match(r"(\d{4})-(\d{2})-(\d{2})", token)
        clock = re.search(r"(\d{2}):(\d{2}):(\d{2})", token)
        offset = re.search(r"[+-](\d{2}):(\d{2})$", token[10:])
        try:
            if date:
                datetime.date(*map(int, date.groups()))
            if clock:
                datetime.time(*map(int, clock.groups()))
            if offset:
                datetime.time(*map(int, offset.groups()))
        except ValueError:
            self.fail(f"invalid date or time {token!r}")

    def array(self):
        self.i += 1
        out = []
        while True:
            self.skip_blank()
            if self.peek() == "]":
                self.i += 1
                break
            out.append(self.value())
            self.skip_blank()
            if self.peek() == ",":
                self.i += 1
            elif self.peek() == "]":
                self.i += 1
                break
            else:
                self.fail("expected , or ] in array")
        self.frozen.add(id(out))
        return out

    def inline_table(self):
        self.i += 1
        out, dotted = {}, set()
        self.skip_ws()
        if self.peek() == "}":
            self.i += 1
        else:
            while True:
                self.key_value(out, dotted)
                self.skip_ws()
                if self.peek() == ",":
                    self.i += 1
                    self.skip_ws()
                elif self.peek() == "}":
                    self.i += 1
                    break
                else:
                    self.fail("expected , or } in inline table")
        self.frozen.add(id(out))
        self.frozen.update(dotted)
        return out

    def check_char(self, c, multiline):
        if (ord(c) < 0x20 and c != "\t" and not (multiline and c == "\n")) or c == "\x7f":
            self.fail("control character in string")

    def escape(self):
        c = self.peek()
        if c in _ESCAPES:
            self.i += 1
            return _ESCAPES[c]
        if c in ("u", "U"):
            n = 4 if c == "u" else 8
            digits = self.s[self.i + 1:self.i + 1 + n]
            if not re.fullmatch(f"[0-9A-Fa-f]{{{n}}}", digits):
                self.fail("invalid unicode escape")
            cp = int(digits, 16)
            if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                self.fail("invalid unicode scalar")
            self.i += 1 + n
            return chr(cp)
        self.fail(f"unsupported escape \\{c}")

    def basic_string(self):
        self.i += 1
        out = []
        while True:
            c = self.peek()
            if not c or c == "\n":
                self.fail("unterminated string")
            self.i += 1
            if c == '"':
                return "".join(out)
            if c == "\\":
                out.append(self.escape())
            else:
                self.check_char(c, False)
                out.append(c)

    def literal_string(self):
        self.i += 1
        end = self.s.find("'", self.i)
        nl = self.s.find("\n", self.i)
        if end < 0 or 0 <= nl < end:
            self.fail("unterminated string")
        body = self.s[self.i:end]
        for c in body:
            self.check_char(c, False)
        self.i = end + 1
        return body

    def multiline_string(self, delim):
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        out = []
        while True:
            if not self.peek():
                self.fail("unterminated string")
            if self.s.startswith(delim, self.i):
                extra = 0
                while extra < 2 and self.s.startswith(delim + delim[0] * (extra + 1), self.i):
                    extra += 1
                out.append(delim[0] * extra)
                self.i += 3 + extra
                return "".join(out)
            c = self.peek()
            self.i += 1
            if c == "\\" and delim == '"""':
                rest = re.match(r"[ \t]*\n", self.s[self.i:])
                if rest:
                    self.i += rest.end()
                    while self.peek() in (" ", "\t", "\n"):
                        self.i += 1
                else:
                    out.append(self.escape())
            else:
                self.check_char(c, True)
                out.append(c)
