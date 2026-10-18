#!/usr/bin/env python3
"""The gateway credential owner: one precedence table, every candidate registered, pure masking.

Pins, through the real sinks:
  - the pure contract (shared/ag2_gateway_credentials.py): the secret registry and its renderings,
    code_field, redact_text, safe_status, describe and the install-diff masking; I/O-free, with no
    module-global registry;
  - the discovery owner (src/gateway_credential_sources.py, vendored into ag2-sparrow): SOURCES,
    mixed and conflicting sources, every candidate registered before a winner is chosen;
  - the real bridge (imported in a child) and the real Hatch install diff, driven by the same
    table of rows, so both sinks follow one precedence;
  - a structural guard: no other package module reads a credential source, and the vendored and
    generated copies match their canonical sources.
Hermetic: temp HOME and files, a faked vault, no network, no real credential.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow"
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO / "shared"))
from ag2_sparrow import gateway_credential_sources as owner, gateway_credentials as contract  # noqa: E402
from ag2_sparrow.adapters.hatch import cli as hatch_cli  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass looks its module up while it is being defined
    spec.loader.exec_module(module)
    return module


# Every copy is tested, not only the one the package imports: the canonical sources and the
# generated skill copy are the same code, and drift between them is checked by the guard below.
CONTRACTS = {"package": contract,
             "shared": _load("ag2_gateway_credentials", REPO / "shared" / "ag2_gateway_credentials.py"),
             "skill": _load("room_ops_gateway_credentials", REPO / "skills" / "agent-room-ops" / "gateway_credentials.py")}
OWNERS = {"package": owner,
          "src": _load("gateway_credential_sources_src", REPO / "src" / "gateway_credential_sources.py")}

ENV_TOKEN, DEVICE_TOKEN, CHANNEL_TOKEN = "env-token-AAAA1111", "device-token-BBBB2222", "channel-token-CCCC3333"
VAULT_TOKEN, FILE_TOKEN, RETIRED = "vault-token-DDDD4444", "rotated-token-EEEE5555", "retired-token-FFFF6666"
GW = "https://gw.example/relay"
URL_SECRET_URL = "https://user-name-1:pass-word-2@gw.example/relay?k=query-secret-3#frag-secret-4"
URL_SHAPED_BEARER = "https://opaque.example/bearer-GGGG7777"

# (name, env, device file, channel file, vault, token file) -> (winner, source, url, token_file)
ROWS = [
    ("env wins over every file", {"REMOTE_TASK_TOKEN": ENV_TOKEN, "REMOTE_TASK_URL": GW},
     {"REMOTE_TASK_TOKEN": DEVICE_TOKEN}, {"REMOTE_TASK_TOKEN": CHANNEL_TOKEN}, VAULT_TOKEN, None,
     (ENV_TOKEN, "env", GW, "")),
    ("the device file wins over the channel file", {"REMOTE_TASK_URL": GW},
     {"REMOTE_TASK_TOKEN": DEVICE_TOKEN}, {"REMOTE_TASK_TOKEN": CHANNEL_TOKEN}, VAULT_TOKEN, None,
     (DEVICE_TOKEN, "file", GW, "DEVICE")),
    ("a blank device token falls through to the channel file", {},
     {"REMOTE_TASK_TOKEN": "", "REMOTE_TASK_URL": "https://ignored.example/relay"},
     {"REMOTE_TASK_TOKEN": f"{GW}|{CHANNEL_TOKEN}"}, VAULT_TOKEN, None,
     (CHANNEL_TOKEN, "file", GW, "CHANNEL")),
    ("the vault is last", {"REMOTE_TASK_URL": GW}, {}, {}, VAULT_TOKEN, None,
     (VAULT_TOKEN, "vault", GW, "")),
    ("an env token file is the token file whatever supplied the token", {"REMOTE_TASK_TOKEN_FILE": "TOKENFILE"},
     {"REMOTE_TASK_TOKEN": DEVICE_TOKEN, "REMOTE_TASK_URL": GW}, {}, "", FILE_TOKEN,
     (DEVICE_TOKEN, "file", GW, "TOKENFILE")),
    ("the legacy alias is honoured after the current name", {"AG2_REMOTE_TOKEN": ENV_TOKEN, "AG2_REMOTE_URL": GW},
     {"REMOTE_TASK_TOKEN": DEVICE_TOKEN}, {}, "", None, (ENV_TOKEN, "env", GW, "")),
    ("the channel dir is the one REMOTE_TASK_CHANNEL_DIR names", {"REMOTE_TASK_CHANNEL_DIR": "dev-ag2space"},
     {}, {"REMOTE_TASK_TOKEN": CHANNEL_TOKEN, "REMOTE_TASK_URL": GW}, "", None,
     (CHANNEL_TOKEN, "file", GW, "CHANNEL")),
    ("a URL-shaped opaque bearer is a secret whole", {"REMOTE_TASK_TOKEN": URL_SHAPED_BEARER, "REMOTE_TASK_URL": GW},
     {"REMOTE_TASK_TOKEN": DEVICE_TOKEN}, {}, "", None, (URL_SHAPED_BEARER, "env", GW, "")),
]


def _write_env(path: Path, values: dict) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


class Fixture:
    """One row's sources on disk, and the environment that names them."""

    def __init__(self, root: Path, row):
        _name, env, device, channel, self.vault, token_file, self.expect = row
        self.root = root
        self.device = root / "relay.env"
        self.config = root / "claude"
        self.channel = self.config / "channels" / env.get("REMOTE_TASK_CHANNEL_DIR", "ag2space") / ".env"
        self.channel.parent.mkdir(parents=True, exist_ok=True)
        self.token_file = root / "token"
        _write_env(self.device, device)
        _write_env(self.channel, channel)
        if token_file:
            self.token_file.write_text(f"REMOTE_TASK_TOKEN={token_file}\n")
        self.env = {k: (str(self.token_file) if v == "TOKENFILE" else v) for k, v in env.items()}
        self.env.update(AG2_DEVICE_ENV=str(self.device), CLAUDE_CONFIG_DIR=str(self.config))
        values = [v for d in (env, device, channel) for k, v in d.items()
                  if v and v != "TOKENFILE" and k != "REMOTE_TASK_CHANNEL_DIR"]
        values += ([self.vault] if self.vault else []) + ([token_file] if token_file else [])
        # The secrets: a bare token, or the secret of an onboarding string; a plain URL is none.
        self.candidates = [contract.parse_onboarding_token(v)[1] for v in values
                           if not (v.startswith("http") and "|" not in v) or v == URL_SHAPED_BEARER]

    def expected(self):
        token, source, url, token_file = self.expect
        token_file = {"DEVICE": str(self.device), "CHANNEL": str(self.channel),
                      "TOKENFILE": str(self.token_file)}.get(token_file, token_file)
        return {"token": token, "source": source, "url": url, "token_file": token_file}

    def owner_files(self, o=owner):
        return [(str(p), o.parse_env_text(p.read_text())) for p in (self.device, self.channel)]


class PureContractCases:
    c = contract

    def test_every_rendering_and_case_of_a_registered_secret_is_withheld(self):
        registry = self.c.SecretRegistry()
        registry.register("MixedToken/9+x")
        for text in ("MixedToken/9+x", "mixedtoken/9+x", "MIXEDTOKEN%2F9%2BX", "mixedtoken%2f9%2bx",
                     "MixedToken%252F9%252Bx", repr("MixedToken/9+x")):
            self.assertEqual(registry.scrub(f"a {text} b"), self.c.WITHHELD_TEXT, text)
        self.assertEqual(registry.scrub("plain words"), "plain words")
        registry.register("\udcff")  # a non-UTF-8 environment byte: total, never raises
        self.assertTrue(registry.contains("x \udcff y"))

    def test_pieces_and_code_fields(self):
        registry = self.c.SecretRegistry()
        registry.register("bearer-secret-value-77")
        for value in ("bearer-secret-value-77", "SECRET-VALUE", "secret-value-77"):
            self.assertIsNone(self.c.code_field(value, registry), value)
        self.assertEqual(self.c.code_field("reenroll_disabled", registry), "reenroll_disabled")
        self.assertIsNone(self.c.code_field("has space", registry))
        self.assertIsNone(self.c.code_field("query-secret-3", None, URL_SECRET_URL))

    def test_url_parts_are_registered_and_redacted(self):
        registry = self.c.SecretRegistry()
        registry.register_gateway_url(URL_SECRET_URL)
        for piece in ("user-name-1", "pass-word-2", "query-secret-3", "frag-secret-4", "k=ab"[:0] + "=query-secret-3"):
            self.assertTrue(registry.contains(f"x {piece} y"), piece)
        self.assertEqual(self.c.redact_text(f"poll {URL_SECRET_URL} failed", URL_SECRET_URL),
                         "poll https://gw.example/relay failed")
        self.assertEqual(self.c.redact_text("leaked k=query-secret-3", URL_SECRET_URL), self.c.REDACTED_TEXT)
        self.assertEqual(self.c.redact_url("not a url"), self.c.REDACTED_URL)

    def test_safe_status_and_describe(self):
        out = self.c.safe_status({"a": float("nan"), "b": [1.5, "x"], "c": object()}, lambda t: t.upper())
        self.assertEqual(out, {"A": self.c.WITHHELD, "B": [1.5, "X"], "C": self.c.WITHHELD})
        try:
            {}["owner_dm"]
        except KeyError as e:
            self.assertRegex(self.c.describe(e), r"^KeyError\['owner_dm'\] at ")
        self.assertEqual(self.c.describe(FileNotFoundError(2, "SECRET", "/x")), "FileNotFoundError ENOENT")

    def test_install_diff_masking_covers_multi_assignment_and_url_lines(self):
        registry = self.c.SecretRegistry()
        registry.register(ENV_TOKEN)
        unit = ("[Service]\n"
                f'Environment="SAFE=keep-me" "REMOTE_TASK_TOKEN={RETIRED}"\n'
                f"Environment=REMOTE_TASK_URL={URL_SECRET_URL}\n"
                f"Environment=OTHER={GW}|{RETIRED}\n"
                "Environment=AG2_DEVICE_ENV=/home/x/relay.env\n"
                f"ExecStart=/opt/bin/x --flag {ENV_TOKEN.upper()}\n")
        shown = self.c.mask_env_assignments(unit, registry)
        for secret in (RETIRED, ENV_TOKEN, "user-name-1", "pass-word-2", "query-secret-3", "frag-secret-4"):
            self.assertNotIn(secret.casefold(), shown.casefold(), secret)
        self.assertIn("SAFE=keep-me", shown)
        self.assertIn("Environment=REMOTE_TASK_URL=https://gw.example/relay\n", shown)
        self.assertIn("AG2_DEVICE_ENV=/home/x/relay.env", shown)
        registry.register("Synth9Tok-en-Q7zZ-live")
        for continued in ("Environment=REMOTE_TASK_TOKEN=Synth9Tok-\\\nen-Q7zZ-live\n",
                          "Environment=SAFE=keep-me \\\n  \"REMOTE_TASK_TOKEN=retired-cont-9988\"\n",
                          "REMOTE_TASK_TOKEN = \"spaced-retired-7766\"\n",
                          "Environment = \"REMOTE_TASK_TOKEN=spaced-retired-5544\"\n"):
            shown = self.c.mask_env_assignments(continued, registry)
            for piece in ("en-Q7zZ-live", "Synth9Tok", "retired-cont-9988", "spaced-retired-7766", "spaced-retired-5544"):
                self.assertNotIn(piece, shown, continued)

    def test_a_comment_inside_a_continuation_does_not_end_it(self):
        registry = self.c.SecretRegistry()
        registry.register("comment-held-4410")
        for unit in ('Environment="REMOTE_TASK_TOKEN=retired-first-9382\\\n# comment\nretired-last-8372"\n',
                     'Environment="REMOTE_TASK_TOKEN=retired-first-9382\\\n  ; one\n# two \\\nretired-last-8372"\n',
                     'Environment=SAFE=keep-me \\\n# comment-held-4410\n  OTHER=x\n'):
            shown = self.c.mask_env_assignments(unit, registry)
            for piece in ("retired-first-9382", "retired-last-8372", "comment-held-4410"):
                self.assertNotIn(piece, shown, unit)
        self.assertEqual(self.c.mask_env_assignments('Environment="REMOTE_TASK_TOKEN=a-1\\\n# c\nb-2"\nExecStart=/x\n'),
                         "Environment=REMOTE_TASK_TOKEN=<withheld>\nExecStart=/x\n")
        self.assertEqual(self.c.mask_env_assignments("# top\nA=b\n"), "# top\nA=b\n",
                         "a comment outside a continuation is its own line")

    def test_edge_paths_never_raise(self):
        registry = self.c.SecretRegistry()
        registry.register("\ud800lone")  # not even surrogateescape encodes a lone high surrogate
        self.assertTrue(registry.contains("x \ud800lone y"))
        self.assertEqual(self.c.redact_url("https://u:p@gw.example:8443/relay?k=v#f"), "https://gw.example:8443/relay")
        self.assertEqual(self.c.redact_url("http://[::1"), self.c.REDACTED_URL)
        self.assertIn("?k=secret-v", self.c.secret_parts("http://[::1?k=secret-v"))
        self.assertEqual(self.c.redact_text("plain", ""), "plain")
        self.assertEqual(self.c.redact_text("x bare-host y", "bare-host"), "x <unparseable gateway URL> y")
        self.assertEqual(self.c.safe_status(True, str.upper), True)
        self.assertEqual(self.c.mask_env_assignments('Environment="unclosed\n'), "Environment=<withheld>\n")
        self.assertEqual(self.c.mask_env_assignments("Environment=NOEQUALS\n"), "Environment=<withheld>\n")
        self.assertEqual(self.c.mask_env_assignments("free text line\n"), "free text line\n")
        import urllib.error
        import io as _io
        err = urllib.error.URLError(ConnectionRefusedError(61, "x"))
        self.assertTrue(self.c.describe(err).startswith("URLError(ConnectionRefusedError/"))
        http = urllib.error.HTTPError("u", 503, "SECRET", {}, _io.BytesIO())
        self.assertEqual(self.c.describe(http), "HTTPError (HTTP 503)")
        def deeper():
            raise ValueError("SECRET")
        try:
            deeper()
        except ValueError as e:
            self.assertRegex(self.c.describe(e), r"^ValueError at [\w.-]+:\d+$")
            self.assertEqual(int(self.c.describe(e).rsplit(":", 1)[1]), deeper.__code__.co_firstlineno + 1)

    def test_the_contract_is_pure(self):
        tree = ast.parse(Path(self.c.__file__).read_text())
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for io_call in ("open", "os.environ.get", "os.getenv", "subprocess.run", "print"):
            self.assertNotIn(io_call, calls, io_call)
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))])
        module_level = [ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)]
        self.assertFalse([v for v in module_level if "SecretRegistry(" in v], "no module-global registry")




class PackageContract(PureContractCases, unittest.TestCase):
    c = CONTRACTS["package"]


class SharedContract(PureContractCases, unittest.TestCase):
    c = CONTRACTS["shared"]


class SkillContract(PureContractCases, unittest.TestCase):
    c = CONTRACTS["skill"]


class DiscoveryCases:
    o = owner

    def test_the_table_is_stated_once(self):
        self.assertEqual(self.o.SOURCES, (("token", ("env", "file", "vault")),
                                         ("url", ("env", "token", "file")),
                                         ("token_file", ("env", "file")),
                                         ("media_token", ("env",))))

    def test_every_row_picks_the_tabled_winner_and_registers_every_candidate(self):
        for row in ROWS:
            with self.subTest(row=row[0]), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), row)
                registry = self.o.contract.SecretRegistry()
                found = self.o.discover(fx.env, fx.owner_files(self.o),
                                       read_token_file=lambda p: Path(p).read_text() if Path(p).exists() else "",
                                       vault_lookup=lambda key, v=fx.vault: v if key == "REMOTE_TASK_TOKEN" else "",
                                       registry=registry)
                got = {"token": found.token, "source": found.token_source, "url": found.url,
                       "token_file": found.token_file}
                self.assertEqual(got, fx.expected())
                for candidate in fx.candidates:
                    if candidate == fx.vault and found.token_source != "vault":
                        continue  # the vault is read only when no earlier source holds a token
                    self.assertTrue(registry.contains(f"x {candidate} y"), candidate)

    def test_every_token_file_assignment_is_registered(self):
        text = ("REMOTE_TASK_TOKEN=current-file-9821\nAG2_REMOTE_TOKEN=legacy-file-7364\n"
                "REMOTE_TASK_TOKEN=overwritten-file-5530\nAG2_REMOTE_URL=https://legacy-u:legacy-p-6612@gw.example/r\n")
        registry = self.o.contract.SecretRegistry()
        found = self.o.discover({"REMOTE_TASK_TOKEN": ENV_TOKEN, "REMOTE_TASK_URL": GW, "REMOTE_TASK_TOKEN_FILE": "/t"},
                                read_token_file=lambda p: text, registry=registry)
        self.assertEqual((found.token, found.token_source), (ENV_TOKEN, "env"))
        for secret in ("current-file-9821", "legacy-file-7364", "overwritten-file-5530", "legacy-p-6612"):
            self.assertTrue(registry.contains(secret), secret)
            self.assertNotIn(secret, self.o.contract.mask_env_assignments(f"Environment=NOTE={secret}\n", registry))

    def test_every_assignment_of_a_repeated_key_in_a_channel_file_is_registered(self):
        text = {"/dev": "REMOTE_TASK_TOKEN=early-dup-1111\nREMOTE_TASK_URL=https://gw.example/r\n"
                        "REMOTE_TASK_TOKEN=late-dup-2222\n",
                "/chan": "export AG2_REMOTE_TOKEN=exported-dup-3333\nREMOTE_TASK_TOKEN=chan-early-4444\n"
                         "REMOTE_TASK_TOKEN=chan-late-5555\n"}
        env = {"AG2_DEVICE_ENV": "/dev", "CLAUDE_CONFIG_DIR": "/c"}
        files = self.o.env_files(env, lambda p: text.get(p) or text["/chan"])
        registry = self.o.contract.SecretRegistry()
        found = self.o.discover(env, files, registry=registry)
        self.assertEqual((found.token, found.token_source), ("late-dup-2222", "file"), "the last assignment still wins")
        for secret in ("early-dup-1111", "late-dup-2222", "exported-dup-3333", "chan-early-4444", "chan-late-5555"):
            self.assertTrue(registry.contains(secret), secret)

    def test_a_refused_named_device_file_stops_the_file_and_vault_tiers(self):
        env = {"AG2_DEVICE_ENV": "/dev", "CLAUDE_CONFIG_DIR": "/c"}
        reads = {"/dev": (None, "over 1048576 bytes")}
        files = self.o.env_files(env, lambda p: reads.get(p, (f"REMOTE_TASK_TOKEN={CHANNEL_TOKEN}\n", "")))
        self.assertEqual((list(files), files.refused), ([], (("/dev", "over 1048576 bytes"),)),
                         "the channel file after a refused device file is not read")
        found = self.o.discover(dict(env, REMOTE_TASK_URL=GW), files, vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((found.token, found.token_source), ("", "none"))
        self.assertIn("/dev", found.fatal)
        self.assertIn("over 1048576 bytes", found.fatal)
        self.assertEqual(found.notes, (self.o.refusal_note("/dev", "over 1048576 bytes"),))
        with_env = self.o.discover(dict(env, REMOTE_TASK_TOKEN=ENV_TOKEN), files)
        self.assertEqual((with_env.token_source, bool(with_env.fatal)), ("env", True),
                         "the env tier is still read, and the adapter still refuses")

    def test_a_refused_default_channel_file_is_named_and_skipped(self):
        env = {"CLAUDE_CONFIG_DIR": "/c"}
        files = self.o.env_files(env, lambda p: (None, "not a regular file"))
        found = self.o.discover(env, files, vault_lookup=lambda k: VAULT_TOKEN if k == "REMOTE_TASK_TOKEN" else "")
        self.assertEqual((found.token_source, found.fatal), ("vault", ""), "documented: the next tier, with a note")
        self.assertEqual(len(found.notes), 1)
        self.assertIn("not a regular file", found.notes[0])
        absent = self.o.discover(env, self.o.env_files(env, lambda p: None))
        self.assertEqual((absent.notes, absent.fatal), ((), ""), "an absent file is not a refusal")

    def test_a_refused_token_file_is_named(self):
        found = self.o.discover({"REMOTE_TASK_TOKEN": ENV_TOKEN, "REMOTE_TASK_TOKEN_FILE": "/t"},
                                read_token_file=lambda p: (None, "not UTF-8"))
        self.assertEqual((found.token_source, found.fatal), ("env", ""))
        self.assertEqual(found.notes, (self.o.refusal_note("/t", "not UTF-8"),))

    def test_a_url_shaped_bearer_is_registered_whole(self):
        registry = self.o.contract.SecretRegistry()
        self.o.register(registry, [URL_SHAPED_BEARER])
        self.assertTrue(registry.contains(f"x {URL_SHAPED_BEARER} y"))

    def test_the_table_drives_the_choice(self):
        row = ROWS[0]  # env wins over every file, as tabled
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(self.o, "SOURCES", (("token", ("file", "env", "vault")),) + self.o.SOURCES[1:]):
            fx = Fixture(Path(td), row)
            found = self.o.discover(fx.env, fx.owner_files(self.o))
            self.assertEqual((found.token, found.token_source), (DEVICE_TOKEN, "file"),
                             "reordering SOURCES reorders the choice: the table is the precedence")

    def test_the_parsers_and_tiers(self):
        self.assertEqual(self.o.parse_env_text("# c\n\nA = ' x '\nnoequals\n"), {"A": " x "}, "whitespace, then the quotes")
        self.assertEqual(self.o.channel_env_paths({"AG2_DEVICE_ENV": "/d", "CLAUDE_CONFIG_DIR": "/c"}, "chan"),
                         ["/d", os.path.join("/c", "channels", "chan", ".env")])
        self.assertEqual(self.o.channel_env_paths({}), [])
        files = [("/a", {"X": ""}), ("/b", {"X": "later", "Y": "y"})]
        self.assertEqual(self.o.config_from_files(files, "X"), "", "an explicit blank is a decision")
        self.assertEqual(self.o.config_from_files(files, "Y"), "y")
        self.assertEqual(self.o.config_from_files(files, "Z"), "")
        text = "export REMOTE_TASK_TOKEN='tok-1'\nAG2_REMOTE_TOKEN=legacy\nAG2_REMOTE_URL=https://u.example/relay\n"
        self.assertEqual((self.o.token_file_token(text), self.o.token_file_url(text)), ("tok-1", "https://u.example/relay"))
        self.assertEqual(self.o.token_file_token("# comment\nhttps://g.example/r|raw\n"), "https://g.example/r|raw")
        self.assertEqual(self.o.token_file_token("A=b\n"), "")
        self.assertEqual(self.o.token_from_vault(lambda key: ""), ("", ""))
        self.assertEqual(self.o.first_env({"REMOTE_TASK_TOKEN": "''", "AG2_REMOTE_TOKEN": "x"}, self.o.TOKEN_KEYS),
                         ("", "REMOTE_TASK_TOKEN"), "a set but quoted-empty env value still wins, as it always has")

    def test_a_file_value_is_unquoted_once(self):
        padded = 'REMOTE_TASK_TOKEN="  pad-tok-1  "\nREMOTE_TASK_URL="  https://p.example/r  "\n'
        vals = self.o.parse_env_text(padded)
        self.assertEqual(self.o.token_from_files([("/a", vals)])[:2], ("  pad-tok-1  ", "  https://p.example/r  "),
                         "the file tier uses the value parse_env_text gave, as main does")
        self.assertEqual(self.o.token_file_token(padded), "  pad-tok-1  ")
        self.assertEqual(self.o.token_file_url(padded), "  https://p.example/r  ")
        blank = [("/a", self.o.parse_env_text('REMOTE_TASK_TOKEN=" \'\'"\n')), ("/b", {"REMOTE_TASK_TOKEN": "later"})]
        self.assertEqual(self.o.token_from_files(blank)[0], " ", "a quoted space is not re-stripped into absence")
        registry = self.o.contract.SecretRegistry()
        found = self.o.discover({}, [("/a", vals)], registry=registry)
        self.assertEqual(found.raw, "  pad-tok-1  ")
        self.assertTrue(registry.contains(f"Bearer {found.raw}."), "the value used is the value registered")

    def test_the_owner_opens_nothing_itself(self):
        tree = ast.parse(Path(self.o.__file__).read_text())
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for io_call in ("open", "os.environ.get", "os.getenv", "Path", "print"):
            self.assertNotIn(io_call, calls, io_call)
        self.assertNotIn("os.environ", ast.unparse(tree))




class PackageDiscovery(DiscoveryCases, unittest.TestCase):
    o = OWNERS["package"]


class SrcDiscovery(DiscoveryCases, unittest.TestCase):
    o = OWNERS["src"]


BRIDGE = """
import json, sys, types
fake = types.ModuleType("channel_token")
fake.token_from_vault = lambda key, vault_get=None: (VAULT if key == "REMOTE_TASK_TOKEN" else "")
sys.modules["channel_token"] = fake
from ag2_sparrow import remote_gateway_bridge as m
print(json.dumps({"token": m.TOKEN, "source": m._FOUND.token_source, "url": m.URL,
                  "token_file": m.TOKEN_FILE, "header": m._AUTH_HEADERS.get("Authorization"),
                  "registered": {c: m._SECRETS.contains("x " + c + " y") for c in CANDIDATES}}))
"""


class RealSinks(unittest.TestCase):
    """The same rows through the real bridge and the real Hatch install diff."""

    def bridge(self, fx, script=BRIDGE, timeout=60):
        state = fx.root / "state"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(fx.root), "PYTHONPATH": str(PKG),
               "AGENT_CONNECT_TASK_DIR": str(fx.root / "t"), "AGENT_CONNECT_RESULT_DIR": str(fx.root / "r"),
               "AGENT_CONNECT_STATE_DIR": str(state), **fx.env}
        child = fx.root / "child.py"
        child.write_text(script.replace("VAULT", json.dumps(fx.vault)).replace("CANDIDATES", json.dumps(fx.candidates)))
        cmd, cwd = [sys.executable, str(child)], fx.root
        if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":  # the gate measures the child too
            import coverage
            env["PYTHONPATH"] += os.pathsep + str(Path(coverage.__file__).parent.parent)
            cmd = [sys.executable, "-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}", str(child)]
            cwd = REPO
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=str(cwd), timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr

    def test_the_bridge_uses_the_tabled_winner_and_registers_every_candidate(self):
        for row in ROWS:
            with self.subTest(row=row[0]), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), row)
                out, err = self.bridge(fx)
                want = fx.expected()
                self.assertEqual({k: out[k] for k in want}, want)
                self.assertEqual(out["header"], f"Bearer {want['token']}")
                for candidate, registered in out["registered"].items():
                    if candidate == fx.vault and want["source"] != "vault":
                        continue
                    self.assertTrue(registered, candidate)
                if want["source"] == "file":
                    self.assertIn("token not in env; loaded from", err)
                if row[1].get("AG2_REMOTE_TOKEN"):
                    self.assertIn("AG2_REMOTE_TOKEN is deprecated", err)

    def test_a_missing_device_file_is_skipped_and_the_media_marker_is_carried(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[2])
            fx.device.unlink()  # absent, not refused: the next candidate supplies the token
            _write_env(fx.channel, {"REMOTE_TASK_TOKEN": CHANNEL_TOKEN, "REMOTE_TASK_URL": GW,
                                    "REMOTE_MEDIA_MARKER": "media-tag"})
            out, err = self.bridge(fx, BRIDGE.replace("print(json.dumps({",
                                                      'import os\nprint(json.dumps({"marker": os.environ.get("REMOTE_MEDIA_MARKER"), '))
            self.assertEqual((out["token"], out["source"], out["marker"]), (CHANNEL_TOKEN, "file", "media-tag"))
            self.assertIn(f"loaded from {fx.channel}", err)

    def test_a_non_utf8_token_file_is_no_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            bad = fx.root / "bad-token"
            bad.write_bytes(b"REMOTE_TASK_TOKEN=\xff\xfe-not-utf8\n")
            fx.env["REMOTE_TASK_TOKEN_FILE"] = str(bad)
            out, _err = self.bridge(fx, BRIDGE.replace("print(json.dumps({",
                                                      'print(json.dumps({"rotated": m._read_token_file(m.TOKEN_FILE), '))
            self.assertEqual((out["rotated"], out["token"], out["token_file"]), ("", ENV_TOKEN, str(bad)),
                             "the import survives it and a rotation read is no rotation, never an exception")

    def test_the_hatch_install_diff_withholds_every_candidate_of_every_row(self):
        for row in ROWS:
            with self.subTest(row=row[0]), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), row)
                unit = fx.root / "ag2-sparrow.service"
                body = "".join(f"Environment=X{i}={c}\n" for i, c in enumerate(fx.candidates))
                unit.write_text(f'[Service]\nEnvironment="SAFE=keep-me" "REMOTE_TASK_TOKEN={RETIRED}"\n'
                                f"Environment=REMOTE_TASK_URL={URL_SECRET_URL}\n{body}ExecStart=/x\n")
                out = io.StringIO()
                env = {k: v for k, v in os.environ.items() if k not in owner.CREDENTIAL_KEYS}
                env.update(fx.env, HOME=str(fx.root))
                with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(io.StringIO()):
                    hatch_cli._install_file(unit, "[Service]\nExecStart=/y\n", 0o644, refuse_drift=True, force=False)
                with mock.patch.dict(os.environ, env, clear=True):
                    found, _registry = hatch_cli._credential_discovery()
                want = fx.expected()
                if want["source"] == "vault":
                    want = {"token": "", "source": "none", "url": GW, "token_file": ""}  # Hatch reads no vault
                self.assertEqual({"token": found.token, "source": found.token_source, "url": found.url,
                                  "token_file": found.token_file}, want, "Hatch resolves the bridge's row")
                shown = out.getvalue()
                self.assertIn("DIFF:", shown)
                self.assertIn("SAFE=keep-me", shown)
                # The vault is not a Hatch source; every other candidate, winner or not, is withheld.
                for secret in [c for c in fx.candidates if c != fx.vault] + [RETIRED, "pass-word-2", "query-secret-3"]:
                    self.assertNotIn(secret.casefold(), shown.casefold(), secret)


    def hatch_diff(self, fx, installed: str) -> str:
        unit = fx.root / "ag2-sparrow.service"
        unit.write_text(installed)
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k not in owner.CREDENTIAL_KEYS}
        env.update(fx.env, HOME=str(fx.root))
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            hatch_cli._install_file(unit, "[Service]\nExecStart=/y\n", 0o644, refuse_drift=True, force=False)
        self.assertIn("DIFF:", out.getvalue())
        return out.getvalue()

    def test_the_hatch_install_diff_masks_past_a_comment_in_a_continuation(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            shown = self.hatch_diff(fx, '[Service]\nEnvironment="REMOTE_TASK_TOKEN=retired-first-9382\\\n'
                                        '# comment\nretired-last-8372"\nExecStart=/x\n')
            self.assertNotIn("retired-first-9382", shown)
            self.assertNotIn("retired-last-8372", shown)
            self.assertIn("Environment=REMOTE_TASK_TOKEN=<withheld>", shown)

    def test_the_hatch_install_diff_withholds_a_losing_token_file_alias(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            fx.token_file.write_text("REMOTE_TASK_TOKEN=current-file-9821\nAG2_REMOTE_TOKEN=legacy-file-7364\n")
            fx.env["REMOTE_TASK_TOKEN_FILE"] = str(fx.token_file)
            shown = self.hatch_diff(fx, "[Service]\nEnvironment=NOTE=legacy-file-7364\n"
                                        "Environment=OTHER=current-file-9821\nExecStart=/x\n")
            self.assertNotIn("legacy-file-7364", shown)
            self.assertNotIn("current-file-9821", shown)

    def refuse(self, path: Path, how: str) -> None:
        if path.exists():
            path.unlink()
        if how == "fifo":
            os.mkfifo(path)
        elif how == "oversize":
            path.write_text(f"REMOTE_TASK_TOKEN={DEVICE_TOKEN}\n" + "#" * (1 << 20))
        elif how == "non-utf8":
            path.write_bytes(f"REMOTE_TASK_TOKEN={DEVICE_TOKEN}\n".encode() + b"\xff\xfe\n")
        else:
            path.mkdir()

    def test_a_refused_named_device_file_fails_closed(self):
        reasons = {"fifo": "not a regular file", "oversize": "over 1048576 bytes",
                   "non-utf8": "not UTF-8", "directory": "not a regular file"}
        script = BRIDGE.replace("print(json.dumps({", "try:\n    m.main()\n    exited = None\n"
                                "except SystemExit as e:\n    exited = str(e.code)\n"
                                'print(json.dumps({"exited": exited, ')
        for how, reason in reasons.items():
            with self.subTest(how=how), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[1])  # the device file would win over the channel file
                self.refuse(fx.device, how)
                out, err = self.bridge(fx, script, timeout=20)
                self.assertEqual((out["token"], out["source"]), ("", "none"), "no other file or the vault stands in")
                self.assertIn(f"AG2_DEVICE_ENV names {fx.device}", out["exited"])
                self.assertIn(reason, out["exited"])
                self.assertIn(f"credential file {fx.device} was refused ({reason})", err)
                self.assertNotIn("loaded from", err)
                for secret in (DEVICE_TOKEN, CHANNEL_TOKEN, VAULT_TOKEN):
                    self.assertNotIn(secret, err + out["exited"])

    def test_a_refused_default_channel_file_warns_and_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[1])
            del fx.env["AG2_DEVICE_ENV"]  # the channel .env under CLAUDE_CONFIG_DIR is the unnamed default
            self.refuse(fx.channel, "oversize")
            out, err = self.bridge(fx)
            self.assertEqual((out["token"], out["source"]), (VAULT_TOKEN, "vault"), "documented: the next tier")
            self.assertIn(f"credential file {fx.channel} was refused (over 1048576 bytes)", err)
            self.assertNotIn(DEVICE_TOKEN, err)

    def test_the_hatch_install_diff_withholds_a_repeated_key_in_a_channel_file(self):
        for which in ("device", "channel"):
            with self.subTest(file=which), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[1])
                path = fx.device if which == "device" else fx.channel
                if which == "channel":
                    fx.device.unlink()
                path.write_text(f"REMOTE_TASK_TOKEN=early-dup-token-1111\nREMOTE_TASK_URL={GW}\n"
                                "REMOTE_TASK_TOKEN=late-dup-token-2222\n")
                shown = self.hatch_diff(fx, "[Service]\nEnvironment=NOTE=early-dup-token-1111\n"
                                            "Environment=NOTE2=late-dup-token-2222\nExecStart=/x\n")
                self.assertNotIn("early-dup-token-1111", shown)
                self.assertNotIn("late-dup-token-2222", shown)
                fx.candidates = ["early-dup-token-1111", "late-dup-token-2222"]
                out, _err = self.bridge(fx)
                self.assertEqual(out["token"], "late-dup-token-2222", "the last assignment still wins")
                self.assertEqual(out["registered"], {c: True for c in fx.candidates})

    def test_the_hatch_install_refuses_a_refused_device_file(self):
        for how in ("oversize", "fifo"):
            with self.subTest(how=how), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[1])
                self.refuse(fx.device, how)
                env = {k: v for k, v in os.environ.items() if k not in owner.CREDENTIAL_KEYS}
                env.update(fx.env, HOME=str(fx.root))
                unit = fx.root / "ag2-sparrow.service"
                unit.write_text(f"[Service]\nEnvironment=NOTE={DEVICE_TOKEN}\nExecStart=/x\n")
                args = mock.Mock(mxid="@a:ag2.space", gateway_instance="")
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err), \
                        mock.patch.object(hatch_cli, "find_reply_command", return_value="/r"), \
                        mock.patch.object(hatch_cli, "reply_command_problem", return_value=""), \
                        mock.patch.object(hatch_cli, "install_hook", side_effect=AssertionError("installed")):
                    rc = hatch_cli.install(args)
                    hatch_cli._install_file(unit, "[Service]\nExecStart=/y\n", 0o644, refuse_drift=True, force=False)
                self.assertEqual(rc, 1)
                self.assertIn(f"NOTE credentials: credential file {fx.device} was refused", out.getvalue())
                self.assertIn(f"REFUSED: AG2_DEVICE_ENV names {fx.device}", err.getvalue())
                self.assertIn("diff withheld", out.getvalue())
                self.assertNotIn(DEVICE_TOKEN, out.getvalue() + err.getvalue())

    def test_a_fifo_credential_path_never_hangs_the_bridge_import(self):
        for where in ("REMOTE_TASK_TOKEN_FILE", "AG2_DEVICE_ENV"):
            with self.subTest(where=where), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[0])
                fifo = fx.root / "fifo"
                os.mkfifo(fifo)  # no writer: a blocking open or read never returns
                fx.env[where] = str(fifo)
                out, _err = self.bridge(fx, timeout=20)
                self.assertEqual((out["token"], out["source"]), (ENV_TOKEN, "env"))


class BoundedRead(unittest.TestCase):
    COPIES = {"package": _load("bounded_file_read_pkg", PKG / "ag2_sparrow" / "bounded_file_read.py"),
              "src": _load("bounded_file_read_src", REPO / "src" / "bounded_file_read.py")}

    def read(self, m, path, **kw):
        box = []
        t = threading.Thread(target=lambda: box.append(m.read_regular_text(str(path), **kw)), daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), f"{path} blocked the read")
        return box[0]

    def test_only_a_bounded_regular_file_is_read(self):
        for name, m in self.COPIES.items():
            with self.subTest(copy=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                os.mkfifo(root / "fifo")
                (root / "ok").write_bytes(b"A=b\r\nC=d\n")
                (root / "big").write_bytes(b"x" * 33)
                (root / "bad").write_bytes(b"\xff\xfe")
                self.assertEqual(self.read(m, root / "ok"), "A=b\nC=d\n")
                self.assertIsNone(self.read(m, root / "fifo"))
                self.assertIsNone(self.read(m, root))
                self.assertIsNone(self.read(m, root / "missing"))
                self.assertIsNone(self.read(m, root / "bad"))
                self.assertIsNone(self.read(m, root / "big", limit=32))
                self.assertEqual(self.read(m, root / "big", limit=33), "x" * 33)
                self.assertEqual(m.read_regular(str(root / "ok")), ("A=b\nC=d\n", ""))
                self.assertEqual(m.read_regular(str(root / "missing")), (None, ""), "absent is not a refusal")
                self.assertEqual(m.read_regular(str(root / "fifo")), (None, "not a regular file"))
                self.assertEqual(m.read_regular(str(root)), (None, "not a regular file"))
                self.assertEqual(m.read_regular(str(root / "bad")), (None, "not UTF-8"))
                self.assertEqual(m.read_regular(str(root / "big"), limit=32), (None, "over 32 bytes"))


SOURCE_KEYS = set(owner.CREDENTIAL_KEYS) | {owner.TOKEN_FILE_KEY}
KEY_TUPLES = {"TOKEN_KEYS", "URL_KEYS", "CREDENTIAL_KEYS", "TOKEN_FILE_KEY", "MEDIA_TOKEN_KEY"}
# What names a credential file or store: a scope holding one of these and a read is credential I/O.
CREDENTIAL_MARKERS = {"AG2_DEVICE_ENV", "CLAUDE_CONFIG_DIR", "TOKEN_FILE", "device_env_path",
                      "channel_env_paths", "env_files", "channel_token", "token_from_vault"}
READS = {"open", "read_text", "read_bytes", "readlines"}
# Owner calls whose callable arguments read credentials: (positional index, keyword names).
OWNER_READERS = {"env_files": (1, ("read_text",)), "discover": (None, ("read_token_file", "vault_lookup"))}
# Where credential I/O may happen: the adapters' edge, as (module, function).
IO_ALLOWED = {("remote_gateway_bridge.py", "_read_env_file"), ("remote_gateway_bridge.py", "_read_text"),
              ("remote_gateway_bridge.py", "_vault_lookup"), ("adapters/hatch/cli.py", "_read_text")}


def _constant(node):
    """A string the expression always evaluates to, when it is built from constants only."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _constant(node.left), _constant(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_constant(v) for v in node.values]
        return None if None in parts else "".join(parts)
    if isinstance(node, ast.FormattedValue) and node.conversion == -1 and node.format_spec is None:
        return _constant(node.value)
    return None


def _free_refs(node, bound=frozenset()):
    """Names and attributes `node` reads, minus names bound as a parameter of a lambda enclosing
    the use; a default is evaluated outside its lambda, so the names it reads stay free."""
    if isinstance(node, ast.Lambda):
        a = node.args
        for default in a.defaults + [d for d in a.kw_defaults if d is not None]:
            yield from _free_refs(default, bound)
        params = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if x}
        yield from _free_refs(node.body, bound | params)
        return
    if isinstance(node, ast.Name) and node.id not in bound:
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    for child in ast.iter_child_nodes(node):
        yield from _free_refs(child, bound)


def credential_access(source: str, rel: str) -> "list[str]":
    """Every way `source` (module `rel`) touches a credential source on its own: a credential key
    or a piece of one as a string, the owner's key tuples, credential I/O outside IO_ALLOWED, and
    a function handed to the owner as a reader that is not in IO_ALLOWED."""
    tree = ast.parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for node in ast.walk(tree):
        parent = parents.get(node)
        if id(node) in docstrings or (isinstance(parent, (ast.BinOp, ast.JoinedStr, ast.FormattedValue))
                                      and _constant(parent) is not None):
            continue  # the folded parent is checked whole
        text = _constant(node)
        if text is not None and any(text == k or (len(text) >= 8 and text in k) for k in SOURCE_KEYS):
            out.append(f"{rel}:{node.lineno} key {text!r}")
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name in KEY_TUPLES:
            out.append(f"{rel}:{node.lineno} owner key tuple {name}")
    scopes = [("<module>", [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))])]
    scopes += [(f.name, [f]) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for name, nodes in scopes:
        inner = [n for root in nodes for n in ast.walk(root)
                 if not (root is not n and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))]
        def marked(expr, tainted=frozenset()):
            for n in ast.walk(expr):
                word = getattr(n, "id", None) or getattr(n, "attr", None)
                if word in CREDENTIAL_MARKERS or word in tainted:
                    return True
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and (
                        ".env" in n.value or n.value in CREDENTIAL_MARKERS):
                    return True
            return False
        tainted = set()
        for _ in range(3):  # names bound from a credential path, to a fixpoint
            for n in inner:
                if isinstance(n, ast.Assign) and marked(n.value, tainted):
                    tainted |= {t.id for t in n.targets if isinstance(t, ast.Name)}
                elif isinstance(n, ast.For) and marked(n.iter, tainted) and isinstance(n.target, ast.Name):
                    tainted.add(n.target.id)
                elif isinstance(n, ast.withitem) and n.optional_vars is not None and marked(n.context_expr, tainted):
                    tainted |= {t.id for t in ast.walk(n.optional_vars) if isinstance(t, ast.Name)}
        reads = any(isinstance(n, ast.Call) and (getattr(n.func, "id", None) in READS or getattr(n.func, "attr", None) in READS)
                    and marked(n, tainted) for n in inner)
        store = any(isinstance(n, ast.Constant) and n.value in ("security", "find-generic-password") for n in inner) or \
            any(isinstance(n, (ast.Import, ast.ImportFrom)) and "channel_token" in ast.unparse(n) for n in inner)
        if (reads or store) and (rel, name) not in IO_ALLOWED:
            out.append(f"{rel}:{name} credential I/O outside the adapters' edge")
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        callee = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
        if callee not in OWNER_READERS:
            continue
        index, keywords = OWNER_READERS[callee]
        handed = [k.value for k in call.keywords if k.arg in keywords]
        handed += call.args[index:index + 1] if index is not None else []
        for value in handed:
            # A reader handed in, a lambda's body and defaults included, may only name allowlisted readers.
            for word in _free_refs(value):
                if (rel, word) not in IO_ALLOWED:
                    out.append(f"{rel}:{call.lineno} reader {word} handed to the owner")
    return out


class Guard(unittest.TestCase):
    OWNERS = {"gateway_credential_sources.py", "gateway_credentials.py"}

    def test_no_other_package_module_touches_a_credential_source(self):
        found = []
        for path in sorted((PKG / "ag2_sparrow").rglob("*.py")):
            if path.name not in self.OWNERS:
                rel = str(path.relative_to(PKG / "ag2_sparrow"))
                found += credential_access(path.read_text(encoding="utf-8"), rel)
        self.assertEqual(found, [], "a credential source is touched outside the owner and the adapters' edge")

    def test_the_detector_catches_every_bypass(self):
        bypasses = [
            'os.environ.get("REMOTE_TASK_TOKEN")',
            'e = os.environ\ne.get("REMOTE_TASK_TOKEN")',
            'K = "REMOTE_TASK_TOKEN"\nos.environ.get(K)',
            'os.environ.get("REMOTE_TASK_" + "TOKEN")',
            'os.environ.get(f"REMOTE_TASK_{\'TOKEN\'}")',
            'os.environ.get(g.TOKEN_KEYS[0])',
            '[v for k, v in os.environ.items() if k.endswith("TASK_TOKEN")]',
            'os.environ.__getitem__("REMOTE_TASK_TOKEN")',
            'Path("~/.config/ag2-sparrow/relay.env").expanduser().read_text()',
            'open(os.environ.get("AG2_DEVICE_ENV")).read()',
            'def f():\n    subprocess.run(["security", "find-generic-password", "-s", "x"])',
            'from channel_token import token_from_vault\ntoken_from_vault(name)',
            'def g():\n    return discover(env, files, read_token_file=_read_anything)',
            'n = "TOKEN"\nos.environ.get(f"REMOTE_TASK_{n}")',
            'n = "TOKEN"\nos.environ.get("REMOTE_TASK_" + n)',
            'discover(env, files, read_token_file=lambda p: open(p).read())',
            'discover(env, files, vault_lookup=lambda k: Path(k).read_text())',
            'env_files(env, lambda p: open(p).read())',
            'env_files(env, read_text=lambda p: open(p).read())',
            'discover(env, files, read_token_file=lambda p, open=open, read=None: open(p).read())',
            'discover(env, files, read_token_file=lambda p, r=_read_anything: r(p))',
        ]
        for source in bypasses:
            self.assertTrue(credential_access(source, "probe.py"), source)
        allowed = ('def _read_text(path):\n    return open(path).read()\nfiles = env_files(env, _read_text)\n'
                   'files = env_files(env, read_text=_read_text)\n'
                   'discover(env, files, read_token_file=lambda p: _read_text(p) or "")\n'
                   'discover(env, files, vault_lookup=lambda k: (_read_text() or (lambda _k: ""))(k))\n')
        self.assertEqual(credential_access(allowed, "adapters/hatch/cli.py"), [])
        self.assertEqual(credential_access('"""Put REMOTE_TASK_TOKEN in the .env file."""\nx = 1\n', "probe.py"), [],
                         "a docstring naming a key is prose, not a read")

    def test_the_vendored_and_generated_copies_match_their_sources(self):
        for cmd in ([sys.executable, str(PKG / "tools" / "sync_from_src.py"), "--check"],
                    [sys.executable, str(REPO / "tools" / "sync_gateway_credentials.py"), "--check"]):
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO), timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((PKG / "ag2_sparrow" / "gateway_credential_sources.py").read_text(),
                         (REPO / "src" / "gateway_credential_sources.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
