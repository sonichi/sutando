#!/usr/bin/env python3
"""The gateway credential owner: one precedence table, every candidate registered, pure masking.

Pins, through the real sinks:
  - the pure contract (shared/ag2_gateway_credentials.py): the secret registry and its renderings,
    code_field, redact_text, safe_status, describe and the install-diff masking; I/O-free, with no
    module-global registry;
  - the discovery owner (src/gateway_credential_sources.py, vendored into ag2-sparrow): SOURCES,
    mixed and conflicting sources, every candidate registered before a winner is chosen;
  - the real bridge (imported in a child), driven by the table of rows, and its own startup
    check, which is what decides for a detached Sparrow Hatch falls back to;
  - the real Hatch install diff, which withholds every environment line whole and decides nothing
    about credentials;
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
from ag2_sparrow import gateway_credential_io, gateway_credential_sources as owner, gateway_credentials as contract  # noqa: E402
from ag2_sparrow.adapters.hatch import cli as hatch_cli, keepalive as hatch_keepalive  # noqa: E402


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

    def test_every_environment_line_is_withheld_whole(self):
        unit = ("[Service]\n"
                f'Environment="SAFE=keep-me" "REMOTE_TASK_TOKEN={RETIRED}"\n'
                f"Environment=REMOTE_TASK_URL={URL_SECRET_URL}\n"
                "EnvironmentFile=-/home/x/secret-dir-5698/relay.env\n"
                "UnsetEnvironment=REMOTE_TASK_TOKEN\nPassEnvironment=AG2_REMOTE_TOKEN\n"
                'Environment = "NOTE=spaced-5544"\n\tenvironment=NOTE=lower-6655\n'
                "# Environment=NOTE=commented-7766\n"
                'ExecStart=/opt/bin/x --flag\nREMOTE_TASK_TOKEN = "spaced-retired-7766"\nPLAIN=keep-too\n')
        shown = self.c.mask_env_assignments(unit)
        self.assertEqual(shown.splitlines(), ["[Service]"] + [self.c.ENVIRONMENT_WITHHELD] * 8 + [
            "ExecStart=/opt/bin/x --flag", "REMOTE_TASK_TOKEN = <withheld>", "PLAIN=keep-too"])
        self.assertEqual(self.c.ENVIRONMENT_WITHHELD, "Environment=<withheld>", "no key, value or path")

    def test_an_escaped_or_doubtful_directive_is_withheld(self):
        secret = "synth-hex-key-5698"
        for line in (f"Environment=REMOTE_TASK_\\x54OKEN={secret}", f"Environment=REMOTE_TASK_\\u0054OKEN={secret}",
                     f"Environment=REMOTE_TASK_\\124OKEN={secret}", f"\\x45nvironment=NOTE={secret}",
                     f"\\105nvironment=NOTE={secret}", f"Environ\\u006dent=NOTE={secret}",
                     f"\\U00000045nvironmentFile=/p/{secret}", f"Unset\\x45nvironment={secret}",
                     f"REMOTE_TASK_\\x54OKEN={secret}", f"REMOTE_TASK_\\124OKEN={secret}",
                     f"REMOTE_TASK_\\u0054OKEN={secret}", f"\uff25nvironment=NOTE={secret}"):
            with self.subTest(line=line):
                self.assertEqual(self.c.mask_env_assignments(f"[Service]\n{line}\nExecStart=/x\n"),
                                 f"[Service]\n{self.c.ENVIRONMENT_WITHHELD}\nExecStart=/x\n")

    def test_a_continued_environment_line_is_withheld_with_every_line_it_joins(self):
        for unit in ('Environment="REMOTE_TASK_TOKEN=first-9382\\\n# comment\ntail-8372"\n',
                     'Environment="REMOTE_TASK_TOKEN=first-9382\\\n  ; one\n# two \\\ntail-8372"\n',
                     "Environment=SAFE=first-9382\\\\\ntail-8372\n",
                     "ExecStart=/x first-9382 \\\nEnvironment=NOTE=tail-8372\n",
                     "# harmless first-9382 \\\nEnvironment=NOTE=tail-8372\n"):
            with self.subTest(unit=unit):
                shown = self.c.mask_env_assignments(unit + "After=y\n")
                self.assertEqual(shown, f"{self.c.ENVIRONMENT_WITHHELD}\nAfter=y\n")
        self.assertEqual(self.c.mask_env_assignments("# top\nA=b\n"), "# top\nA=b\n",
                         "a comment outside a continuation is its own line")

    def test_a_value_on_an_environment_line_is_withheld_wherever_it_recurs(self):
        old = ("[Service]\nEnvironment=REMOTE_TASK_TOKEN=tok-dup-5698\nEnvironment=NOTE=neutral-dup-4411\n"
               "Environment=OTHER=esc\\x2ddup-7722\nExecStart=/x --a tok-dup-5698\n")
        new = "[Service]\nNOTE2=neutral-dup-4411\nExecStartPre=/bin/echo esc-dup-7722\nExecStart=/x\n"
        registry = self.c.SecretRegistry()
        self.c.register_assignments(registry, old, new)
        shown = self.c.mask_env_assignments(old, registry) + self.c.mask_env_assignments(new, registry)
        for secret in ("tok-dup-5698", "neutral-dup-4411", "esc-dup-7722", "esc\\x2ddup-7722"):
            self.assertNotIn(secret, shown, secret)
        self.assertIn("ExecStart=/x\n", shown, "a line holding no registered value is shown")

    def test_a_systemd_value_is_registered_whole_whatever_its_quotes_or_escapes(self):
        exact = "Environment=REMOTE_TASK_TOKEN='abcdefgh'\nExecStart=/bin/echo --token='abcdefgh' --x\n"
        registry = self.c.SecretRegistry()
        self.c.register_assignments(registry, exact)
        self.assertEqual(self.c.mask_env_assignments(exact, registry), "Environment=<withheld>\nExecStart=<withheld>\n")
        for env_line, secret in (("Environment=REMOTE_TASK_TOKEN='abcdefgh'", "abcdefgh"),
                                 ('Environment="REMOTE_TASK_\\x54OKEN=abc"', "abc"),
                                 ('Environment="REMOTE_TASK_TOKEN=alpha\\somega"', "alpha omega")):
            with self.subTest(env_line=env_line):
                neutral = (f"NOTE={secret}\nExecStart=/bin/echo '{secret}' --x\nDescription=Sparrow \"{secret}\"\n"
                           f"ExecStartPre=/bin/echo {secret.replace(' ', chr(92) + 's')}\n")
                registry = self.c.SecretRegistry()
                self.c.register_assignments(registry, env_line + "\n", neutral)
                shown = self.c.mask_env_assignments(neutral, registry)
                self.assertNotIn(secret, shown)
                self.assertEqual(len(shown.splitlines()), 4)

    def test_a_credential_shape_is_withheld_whatever_the_registry_holds(self):
        tok = "synth-shape-5698"
        for line in (f"ExecStart=/opt/bin/sparrow --token {tok}", f"ExecStart=/opt/bin/sparrow --token={tok}",
                     f"execstart=/opt/bin/sparrow --token {tok}", f"ExecStart=/usr/bin/env -i REMOTE_TASK_TOKEN={tok} /x",
                     f"ExecStartPre=/bin/sh -c 'export REMOTE_TASK_TOKEN={tok}'",
                     f'curl -H "Authorization: Bearer {tok}" https://gw.example/', f"ExecStart=/x --secret {tok}",
                     f"ExecStart=/x --password={tok}", f"ExecStart=/x --api-key {tok}", f"ExecStart=/x --bearer {tok}",
                     f"ExecStart=/x -H Bearer\\s{tok}", f'{{"api_key": "{tok}"}}', f"run --auth-token {tok}"):
            for registry in (None, self.c.SecretRegistry()):
                with self.subTest(line=line, registry=registry is not None):
                    self.assertNotIn(tok, self.c.mask_env_assignments(line + "\n", registry))
        old = f"ExecStart=/usr/bin/env -i REMOTE_TASK_TOKEN={tok} /x\n"
        new = f'ExecStart=/usr/bin/env X={tok} /x\nDescription=Sparrow {tok}\nNOTE={tok}\n{{"note": "{tok}"}}\n'
        registry = self.c.SecretRegistry()
        self.c.register_assignments(registry, old, new)
        self.assertNotIn(tok, self.c.mask_env_assignments(old, registry) + self.c.mask_env_assignments(new, registry),
                         "a value found by its shape is withheld wherever it recurs")
        self.assertEqual(self.c.mask_env_assignments(f"Description=Sparrow {tok}\n", self.c.SecretRegistry()),
                         f"Description=Sparrow {tok}\n", "the stated limit: a bare token nothing names")

    BREAKS = ("\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\r", "\x00")

    def test_only_an_lf_ends_a_line_and_no_other_break_carries_a_tail(self):
        for ch in self.BREAKS:
            with self.subTest(ch=repr(ch)):
                self.assertEqual(self.c.physical_lines(f"a{ch}b\nc\r\nd"), [f"a{ch}b\n", "c\r\n", "d"])
                for text, shown in ((f'Environment="REMOTE_TASK_TOKEN=qa-first{ch}qa-tail-9988"\n',
                                     "Environment=<withheld>\n"),
                                    (f"Description=x{ch}Environment=NOTE=qa-tail-9988\n", "Environment=<withheld>\n"),
                                    (f"ExecStart=/x --flag qa-first{ch}qa-tail-9988\n", self.c.WITHHELD_TEXT + "\n"),
                                    (f"NOTE=qa-first{ch}qa-tail-9988\n", self.c.WITHHELD_TEXT + "\n")):
                    registry = self.c.SecretRegistry()
                    self.c.register_assignments(registry, text)
                    self.assertEqual(self.c.mask_env_assignments(text, registry), shown)

    def test_flag_header_and_yaml_credential_shapes(self):
        tok = "synth-n1-shape-5698"
        for line in (f"ExecStart=/x --key {tok}", f"ExecStart=/x --access-key={tok}", f"ExecStart=/x --credentials {tok}",
                     f"ExecStart=/x --pat {tok}", f"ExecStart=/x -H 'X-Api-Key: {tok}'", f"X-Auth-Token: {tok}",
                     f"token: {tok}", f"  api_key: {tok}", f"password: '{tok}'", f"ExecStart=/x -H X-Api-Key:{tok}"):
            with self.subTest(line=line):
                self.assertNotIn(tok, self.c.mask_env_assignments(line + "\n", self.c.SecretRegistry()))

    def test_a_credential_name_holds_a_stem_after_folding(self):
        for name in ("REMOTE_TASK_TOKEN", "MYTOKEN", "DBPASSWORD", "apiKey", "accessToken", "X-Api-Key", "pat",
                     "auth-mode", "SESSION_ID", "github_pat", "DB_PASSCODE", "AUTHKEY", "PRIVKEY", "SESSIONID", "JWT",
                     "pw", "pwd", "DB_PW", "dbpw", "auth.token", "access.token", "OAuthClientId", "oauth-client-id",
                     "AUTH_MODE", "author", "COMPASS", "bypass-proxy", "KillSignal", "\uff34\uff2f\uff2b\uff25\uff2e"):
            self.assertTrue(self.c.credential_name(name), name)
        for name in ("ExecStart", "Description", "WorkingDirectory", "RestartSec", "WantedBy", "StandardOutput",
                     "Environment", "tokn", "PWNAGE", "path", "patch", "PATH", "x"):
            self.assertFalse(self.c.credential_name(name), name)
        for line in ("ExecStart=/x --author someone", "X-AUTHOR=someone", "AUTH_MODE=basic", "Author: someone",
                     "ExecStart=/x --oauth-client-id abc", "Description=Bearer of tasks"):
            self.assertNotEqual(self.c.mask_env_assignments(line + "\n", self.c.SecretRegistry()), line + "\n",
                                "over-withholding is the accepted price of the closed-world rule")
        for line in ("ExecStart=/x --tokn v", "Description=A plain relay", "free text line", "see https://a.example/b:c"):
            self.assertEqual(self.c.mask_env_assignments(line + "\n", self.c.SecretRegistry()), line + "\n")

    def test_every_stem_separator_casing_and_shape_is_withheld(self):
        tok = "synth-sweep-5698"
        stems = self.c.CREDENTIAL_STEMS + self.c.SEGMENT_STEMS
        casings = (str.lower, str.upper, str.title, lambda n: n[:1] + n[1:].upper())
        shapes = ("{n}={v}", "export {n}={v}", "ExecStart=/x --{n} {v}", "ExecStart=/x --{n}={v}", "ExecStart=/x -{n} {v}",
                  "args: [--{n},{v}]", "args: [--{n}={v}]", '{{"{n}": "{v}"}}', "{{'{n}':'{v}'}}", "{n}: {v}",
                  "  - {n}: {v}", "ExecStart=/x -H '{n}: {v}'", "ExecStart=/usr/bin/env -i {n}={v} /x")
        for stem in stems:
            for sep in ("", "_", "-", ".", "__"):
                for case in casings:
                    name = case(f"db{sep}{stem}" if stem in self.c.SEGMENT_STEMS else f"db{sep}{stem}{sep}v2")
                    for shape in shapes:
                        line = shape.format(n=name, v=tok)
                        with self.subTest(line=line):
                            self.assertNotIn(tok, self.c.mask_env_assignments(line + "\n", self.c.SecretRegistry()))

    def test_adjacent_shapes_from_review_are_withheld(self):
        tok = "synth-adjacent-5698"
        for line in (f"DB_PASSCODE={tok}", f"args: [--token,{tok}]", f"args: [--access-key={tok}]",
                     f'{{"auth.token":"{tok}"}}', f"access.token: {tok}", f"ExecStart=/x --pw {tok}",
                     f"ExecStart=/x --pwd={tok}", f"AUTHKEY={tok}", f"PRIVKEY={tok}", f"SESSIONID={tok}",
                     f"JWT={tok}", f"ExecStart=/x -H Bearer\\s{tok[:6]}", f"Authorization: Bearer {tok[:5]}"):
            for registry in (None, self.c.SecretRegistry()):
                with self.subTest(line=line, registry=registry is not None):
                    self.assertNotIn(tok[:5], self.c.mask_env_assignments(line + "\n", registry))

    def test_a_short_or_path_shaped_value_hides_no_other_line(self):
        for shaped, harmless in (("ExecStart=/x --auth=1", "RestartSec=10\nDescription=Sparrow 1.0\n"),
                                 ("ExecStart=/x --auth-mode=basic", "Description=A basic relay\n"),
                                 ("ExecStart=/x --token-file /home/u/cfg/token", "ExecStartPre=/bin/ls /home/u/cfg/token\n")):
            with self.subTest(shaped=shaped):
                text = f"{shaped}\n{harmless}"
                registry = self.c.SecretRegistry()
                self.c.register_assignments(registry, text)
                self.assertEqual(self.c.mask_env_assignments(text, registry), f"ExecStart=<withheld>\n{harmless}")
        registry = self.c.SecretRegistry()
        self.c.register_assignments(registry, "ExecStart=/x --auth=long-enough-5698\n")
        self.assertTrue(registry.contains("NOTE=long-enough-5698"), "a shaped value of 8 or more is reused")

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
        self.c.register_assignments(self.c.SecretRegistry(), "Environment=NOTE=bad-escape\\x\n", "X_TOKEN=\\u12\n")
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
            self.assertNotIn(secret, self.o.contract.mask_env_assignments(f"NOTE={secret}\n", registry))

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

    def test_general_config_still_reaches_a_later_file_after_a_refused_one(self):
        env = {"AG2_DEVICE_ENV": "/dev", "CLAUDE_CONFIG_DIR": "/c"}
        files = self.o.env_files(env, lambda p: (None, "not UTF-8") if p == "/dev"
                                 else ("REMOTE_PROACTIVE_ROOM=!r:example.org\n", ""))
        self.assertTrue(files.blocked)
        self.assertEqual(self.o.config_from_files(files, "REMOTE_PROACTIVE_ROOM"), "!r:example.org")

    def test_a_refused_named_device_file_stops_the_file_and_vault_tiers(self):
        env = {"AG2_DEVICE_ENV": "/dev", "CLAUDE_CONFIG_DIR": "/c"}
        reads = {"/dev": (None, "over 1048576 bytes")}
        files = self.o.env_files(env, lambda p: reads.get(
            p, (f"REMOTE_TASK_TOKEN={GW}|{CHANNEL_TOKEN}\nREMOTE_TASK_URL={GW}\n", "")))
        self.assertEqual(files.refused, (("/dev", "over 1048576 bytes"),))
        found = self.o.discover(env, files, vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((found.token, found.token_source, found.url, found.token_file), ("", "none", "", ""),
                         "neither the later file nor the vault stands in, nor becomes the rotation source")
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

    def test_a_refused_named_token_file_is_named_and_fatal(self):
        found = self.o.discover({"REMOTE_TASK_TOKEN": ENV_TOKEN, "REMOTE_TASK_TOKEN_FILE": "/t"},
                                read_token_file=lambda p: (None, "not UTF-8"))
        self.assertEqual(found.token_source, "env")
        self.assertEqual(found.notes, (self.o.refusal_note("/t", "not UTF-8"),))
        self.assertIn("REMOTE_TASK_TOKEN_FILE names /t", found.fatal)
        self.assertIn("not UTF-8", found.fatal)
        absent = self.o.discover({"REMOTE_TASK_TOKEN": ENV_TOKEN, "REMOTE_TASK_TOKEN_FILE": "/t"},
                                 read_token_file=lambda p: (None, ""))
        self.assertEqual(absent.fatal, "", "an absent token file is not a refusal")

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


# The bridge's own startup check: main() exits FATAL before it polls or publishes anything.
BRIDGE_MAIN = BRIDGE.replace("print(json.dumps({", "try:\n    m.main()\n    exited = None\n"
                             "except SystemExit as e:\n    exited = str(e.code)\n"
                             'print(json.dumps({"exited": exited, ')


class RealSinks(unittest.TestCase):
    """The same rows through the real bridge and the real Hatch install diff."""

    def setUp(self):
        # No case reaches the host's systemctl unless it supplies a fake.
        patcher = mock.patch.object(hatch_cli, "_systemd_run", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Nor the host's vault: a Hatch diff that reaches the vault tier reads a fake one.
        vault = mock.patch.object(gateway_credential_io, "_vault", return_value=None)
        vault.start()
        self.addCleanup(vault.stop)

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

    def test_a_refused_named_token_file_is_fatal_at_startup(self):
        script = BRIDGE_MAIN
        for how, reason in (("oversize", "over 1048576 bytes"), ("non-utf8", "not UTF-8"),
                            ("fifo", "not a regular file")):
            with self.subTest(how=how), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[0])
                self.refuse(fx.token_file, how)
                fx.env["REMOTE_TASK_TOKEN_FILE"] = str(fx.token_file)
                out, err = self.bridge(fx, script, timeout=20)
                self.assertEqual(out["token"], ENV_TOKEN)
                self.assertIn(f"REMOTE_TASK_TOKEN_FILE names {fx.token_file}", out["exited"] or "")
                self.assertIn(reason, out["exited"])
                self.assertIn(f"credential file {fx.token_file} was refused ({reason})", err)
                self.assertNotIn(DEVICE_TOKEN, err + out["exited"], "path and reason, never content")

    def test_a_token_file_refused_at_reload_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            fx.token_file.write_text(f"REMOTE_TASK_TOKEN={ENV_TOKEN}\n")
            fx.env["REMOTE_TASK_TOKEN_FILE"] = str(fx.token_file)
            path = json.dumps(str(fx.token_file))
            script = BRIDGE.replace("print(json.dumps({", (
                "import os, pathlib\n"
                f"p = pathlib.Path({path})\n"
                f"p.write_bytes(b'REMOTE_TASK_TOKEN=' + {json.dumps(FILE_TOKEN)}.encode() + b'\\xff\\n')\n"
                "print('--reload--', file=sys.stderr, flush=True)\n"
                'print(json.dumps({"reloaded": m._reload_rotated_token(), '))
            out, err = self.bridge(fx, script)
            self.assertFalse(out["reloaded"])
            reload_err = err.split("--reload--", 1)[1]
            self.assertIn(f"credential file {fx.token_file} was refused (not UTF-8)", reload_err)
            self.assertNotIn(FILE_TOKEN, reload_err, "path and reason, never content")

    def hatch_diff(self, fx, installed: str, name: str = "ag2-sparrow.service",
                   ours: str = "[Service]\nExecStart=/y\n") -> str:
        unit = fx.root / name
        unit.write_text(installed)
        out = io.StringIO()
        host = owner.CREDENTIAL_KEYS + (owner.TOKEN_FILE_KEY, owner.CHANNEL_DIR_KEY, "AG2_DEVICE_ENV", "CLAUDE_CONFIG_DIR")
        env = {k: v for k, v in os.environ.items() if k not in host}
        env.update(fx.env, HOME=str(fx.root))
        vault, lookups = getattr(fx, "vault", ""), getattr(fx, "lookups", None)
        lookups = lookups if isinstance(lookups, list) else []
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(gateway_credential_io, "_vault", return_value=lambda key: (
                    lookups.append(key) or (vault if key == "REMOTE_TASK_TOKEN" else ""))):
            hatch_cli._install_file(unit, ours, 0o644, refuse_drift=True, force=False)
        self.assertIn("DIFF:", out.getvalue())
        return out.getvalue()

    def test_the_hatch_install_diff_withholds_every_candidate_of_every_row(self):
        for row in ROWS:
            with self.subTest(row=row[0]), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), row)
                # Each candidate only outside an assignment: discovery alone registers it.
                body = "".join(f"ExecStartPre=/x --token {c}\n" for c in fx.candidates)
                shown = self.hatch_diff(fx, f'[Service]\nEnvironment="SAFE=keep-me" "REMOTE_TASK_TOKEN={RETIRED}"\n'
                                            f"Environment=REMOTE_TASK_URL={URL_SECRET_URL}\n{body}"
                                            f"ExecStart=/x --a {fx.candidates[0]}\n")
                # The vault is read only when no earlier tier holds a token, as in the bridge.
                reached = [c for c in fx.candidates if c != fx.vault or fx.expected()["source"] == "vault"]
                for secret in reached + [RETIRED, "pass-word-2", "query-secret-3", "keep-me"]:
                    self.assertNotIn(secret.casefold(), shown.casefold(), secret)
                self.assertIn("-Environment=<withheld>\n", shown)
                self.assertNotIn("Environment=X", shown)

    def outside_assignments(self, live: str, env: dict, vault: str = ""):
        """The real Hatch diff of a unit and a hook script that hold `live` only outside any
        assignment, with `env` (paths under the temp root filled in) and a fake vault."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "relay.env").write_text(f"REMOTE_TASK_TOKEN={live}\n")
            (root / "token").write_text(f"REMOTE_TASK_TOKEN={live}\n")
            fx = mock.Mock(root=root, vault=vault, env={k: str(root / v) if v in ("relay.env", "token") else v
                                                        for k, v in env.items()})
            unit = self.hatch_diff(fx, f"[Service]\nExecStart=/x --token {live}\n")
            hook = self.hatch_diff(fx, f'#!/bin/sh\ncurl -H "Authorization: Bearer {live}" https://gw.example/\n',
                                   name=hatch_cli.SCRIPT_NAME, ours="#!/bin/sh\nexit 0\n")
        return unit, hook

    def test_a_token_only_outside_an_assignment_is_masked_from_the_env_or_the_device_file(self):
        live = "synth-live-outside-5698"
        for where, env in (("env", {"REMOTE_TASK_TOKEN": live}), ("device file", {"AG2_DEVICE_ENV": "relay.env"})):
            with self.subTest(where=where):
                unit, hook = self.outside_assignments(live, env)
                self.assertNotIn(live, unit + hook)
                self.assertIn("-ExecStart=<withheld>\n", unit)
                self.assertIn(f"-{contract.WITHHELD_TEXT}\n", hook)

    def test_a_token_only_outside_an_assignment_is_masked_from_the_token_file_or_the_vault(self):
        live = "synth-live-rotated-5698"
        for where, env, vault in (("token file", {"REMOTE_TASK_TOKEN_FILE": "token"}, ""), ("vault", {}, live)):
            with self.subTest(where=where):
                unit, hook = self.outside_assignments(live, env, vault)
                self.assertNotIn(live, unit + hook)
                self.assertIn("-ExecStart=<withheld>\n", unit)
                self.assertIn(f"-{contract.WITHHELD_TEXT}\n", hook)

    def test_the_hatch_install_diff_masks_past_a_comment_in_a_continuation(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            shown = self.hatch_diff(fx, '[Service]\nEnvironment="REMOTE_TASK_TOKEN=retired-first-9382\\\n'
                                        '# comment\nretired-last-8372"\nExecStart=/x\n')
            self.assertNotIn("retired-first-9382", shown)
            self.assertNotIn("retired-last-8372", shown)
            self.assertIn("-Environment=<withheld>\n", shown)

    def test_the_hatch_install_diff_follows_systemd_continuations(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            shown = self.hatch_diff(fx, "[Service]\n# harmless comment \\\n"
                                        "Environment=REMOTE_TASK_TOKEN=synth-after-comment-5698\n"
                                        "Environment=SAFE=C:\\\\\n"
                                        "Environment=REMOTE_TASK_TOKEN=synth-after-even-slashes-5698\nExecStart=/x\n")
            self.assertNotIn("synth-after-comment-5698", shown)
            self.assertNotIn("synth-after-even-slashes-5698", shown)

    def test_the_hatch_install_diff_withholds_escaped_directive_spellings(self):
        secret = "synth-hex-key-5698"
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            shown = self.hatch_diff(fx, "[Service]\n" + "".join(f"{line}\n" for line in (
                f"Environment=REMOTE_TASK_\\x54OKEN={secret}", f"Environment=REMOTE_TASK_\\u0054OKEN={secret}",
                f"Environment=REMOTE_TASK_\\124OKEN={secret}", f"\\x45nvironment=NOTE={secret}",
                f"\\105nvironmentFile=/p/{secret}", f"Environ\\u006dent=NOTE={secret}",
                f"REMOTE_TASK_\\x54OKEN={secret}", f"REMOTE_TASK_\\124OKEN={secret}"))
                + "ExecStart=/x\n")
            self.assertNotIn(secret, shown)
            self.assertNotIn("x54", shown)
            self.assertEqual(shown.count("-Environment=<withheld>\n"), 8)

    @staticmethod
    def body(shown: str) -> str:
        """The diff's own lines, without the headers that name the temp paths."""
        return "".join(line for line in shown.splitlines(True)
                       if line[:1] in "+- " and not line.startswith(("---", "+++")))

    def bare(self, root: Path, lookups: "list | None" = None):
        """A host with no credential anywhere: an empty environment, no files, a vault that holds
        nothing and records each key it is asked for."""
        return mock.Mock(root=root, env={}, vault="", lookups=lookups if lookups is not None else [])

    def test_the_hatch_install_diff_registers_a_systemd_value_whole(self):
        with tempfile.TemporaryDirectory() as td:
            exact = self.hatch_diff(self.bare(Path(td)), "[Service]\nEnvironment=REMOTE_TASK_TOKEN='abcdefgh'\n"
                                                         "ExecStart=/bin/echo --token='abcdefgh' --x\n")
            self.assertEqual(self.body(exact), " [Service]\n-Environment=<withheld>\n-ExecStart=<withheld>\n"
                                               "+ExecStart=/y\n")
        for env_line, secret in (("Environment=REMOTE_TASK_TOKEN='abcdefgh'", "abcdefgh"),
                                 ('Environment="REMOTE_TASK_\\x54OKEN=abc"', "abc"),
                                 ('Environment="REMOTE_TASK_TOKEN=alpha\\somega"', "alpha omega")):
            with self.subTest(env_line=env_line), tempfile.TemporaryDirectory() as td:
                shown = self.body(self.hatch_diff(self.bare(Path(td)), f"[Service]\n{env_line}\nNOTE={secret}\n"
                                                                       f"ExecStart=/bin/echo '{secret}' --x\n"))
                self.assertNotIn(secret, shown)
                self.assertIn("-NOTE=<withheld>\n-ExecStart=<withheld>\n", shown)

    def test_the_hatch_install_diff_withholds_every_credential_shape(self):
        tok = "synth-shape-sink-5698"
        lines = (f"ExecStart=/opt/bin/sparrow --token {tok}", f"ExecStart=/opt/bin/sparrow --token={tok}",
                 f"execstart=/opt/bin/sparrow --token {tok}", f"ExecStart=/usr/bin/env -i REMOTE_TASK_TOKEN={tok} /x",
                 f"ExecStartPre=/bin/sh -c 'export REMOTE_TASK_TOKEN={tok}'",
                 f'ExecStartPost=/usr/bin/curl -H "Authorization: Bearer {tok}" https://gw.example/')
        for line in lines:
            with self.subTest(line=line), tempfile.TemporaryDirectory() as td:
                lookups = []
                shown = self.body(self.hatch_diff(self.bare(Path(td), lookups), f"[Service]\n{line}\nExecStart=/x\n"))
                self.assertNotIn(tok, shown)
                self.assertIn("-ExecStart=/x\n", shown, "a line with no shape is shown")
        with tempfile.TemporaryDirectory() as td:
            recur = (f"ExecStart=/usr/bin/env X={tok} /x\nDescription=Sparrow {tok}\nNOTE={tok}\n"
                     f'Note={{"note": "{tok}"}}\n')
            shown = self.body(self.hatch_diff(self.bare(Path(td)), f"[Service]\n{lines[3]}\n{recur}"))
            self.assertNotIn(tok, shown, "a value found by its shape is withheld wherever it recurs")
        with tempfile.TemporaryDirectory() as td:
            limit = self.body(self.hatch_diff(self.bare(Path(td)), f"[Service]\nDescription=Sparrow {tok}\n"))
            self.assertIn(f"-Description=Sparrow {tok}\n", limit, "the stated limit: a bare token nothing names")

    def test_the_hatch_install_diff_shows_no_tail_past_a_unicode_line_break(self):
        for ch in ("\u2028", "\u2029", "\x85"):
            with self.subTest(ch=repr(ch)), tempfile.TemporaryDirectory() as td:
                shown = self.body(self.hatch_diff(self.bare(Path(td)), f'[Service]\nEnvironment="REMOTE_TASK_TOKEN='
                                                                       f'qa-first{ch}qa-tail-9988"\nExecStart=/x\n'))
                self.assertNotIn("qa-tail-9988", shown)
                self.assertNotIn("qa-first", shown)
                self.assertEqual(shown, " [Service]\n-Environment=<withheld>\n-ExecStart=/x\n+ExecStart=/y\n")

    def test_the_hatch_install_diff_keeps_harmless_lines_and_names(self):
        tok = "synth-n1-sink-5698"
        with tempfile.TemporaryDirectory() as td:
            shown = self.body(self.hatch_diff(self.bare(Path(td)), "[Service]\nExecStart=/x --auth=1\nRestartSec=10\n"
                                                                   "ExecStartPre=/x --author someone\n"
                                                                   f"ExecStartPost=/x -H 'X-Api-Key: {tok}'\n"))
            self.assertEqual(shown, " [Service]\n-ExecStart=<withheld>\n-RestartSec=10\n"
                                    "-ExecStartPre=<withheld>\n-ExecStartPost=<withheld>\n+ExecStart=/y\n",
                             "--author is withheld: the closed-world name rule over-withholds")

    def test_the_hatch_install_diff_withholds_the_adjacent_review_shapes(self):
        tok = "synth-adjacent-sink-5698"
        for line in (f"DB_PASSCODE={tok}", f"Note=args: [--token,{tok}]", f"Note=args: [--access-key={tok}]",
                     f'Note={{"auth.token":"{tok}"}}', f"Note=access.token: {tok}", f"ExecStartPre=/x --pw {tok}"):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as td:
                shown = self.body(self.hatch_diff(self.bare(Path(td)), f"[Service]\n{line}\nExecStart=/x\n"))
                self.assertNotIn(tok, shown)
                self.assertIn("-ExecStart=/x\n", shown, "a line with no shape is shown")

    def test_the_hatch_install_diff_seeds_from_the_default_device_file(self):
        live = "synth-default-device-5698"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            device = root / ".config" / "ag2-sparrow" / "relay.env"
            device.parent.mkdir(parents=True)
            device.write_text(f"REMOTE_TASK_TOKEN={live}\n")
            lookups = []
            shown = self.body(self.hatch_diff(self.bare(root, lookups), f"[Service]\nExecStart=/x --flag {live}\n"
                                                                        f"NOTE={live}\n"))
            self.assertNotIn(live, shown)
            self.assertIn("-ExecStart=<withheld>\n-NOTE=<withheld>\n", shown)
            self.assertEqual(lookups, [], "the default device file holds the token: the vault is not reached")

    def install_service(self, fx, installed: str, drop_in: str = "") -> str:
        """install_service over `installed` (and a drop-in beside it), with a reachable fake systemd."""
        unit = fx.root / ".config" / "systemd" / "user" / hatch_cli.SERVICE_NAME
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(installed)
        if drop_in:
            (unit.parent / f"{hatch_cli.SERVICE_NAME}.d").mkdir()
            (unit.parent / f"{hatch_cli.SERVICE_NAME}.d" / "override.conf").write_text(drop_in)
        env = {k: v for k, v in os.environ.items() if k not in owner.CREDENTIAL_KEYS}
        env.update(fx.env, HOME=str(fx.root), USER="hatch")
        ok = subprocess.CompletedProcess([], 0, "", "")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(hatch_cli.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(hatch_cli, "_systemd_run", return_value=ok), \
                mock.patch.object(hatch_cli, "_find_console_script", return_value="/opt/bin/ag2-sparrow"):
            hatch_cli.install_service(fx.root / "t", fx.root / "r", fx.root / "s", "", False)
        return out.getvalue() + err.getvalue()

    def test_an_owned_or_retained_unit_prints_no_environment_content(self):
        lines = ("Environment=REMOTE_TASK_TOKEN=synth-per-unit-duplicate-5698\n"
                 "Environment=NOTE=synth-per-unit-duplicate-5698\n"
                 "EnvironmentFile=/home/x/secret-dir-5698/relay.env\n"
                 "UnsetEnvironment=AG2_REMOTE_TOKEN\nPassEnvironment=REMOTE_TASK_URL\n"
                 "ExecStartPre=/bin/true synth-per-unit-duplicate-5698\nExecStart=/x\n")
        for kind, head in (("owned", hatch_cli.SERVICE_MARKER + "\n"), ("retained", "")):
            with self.subTest(kind), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[0])
                shown = self.install_service(fx, f"{head}[Service]\n{lines}")
                self.assertEqual("DIFF:" in shown, kind == "retained", shown)
                for secret in ("synth-per-unit-duplicate-5698", "secret-dir-5698", "AG2_REMOTE_TOKEN",
                               "REMOTE_TASK_URL", str(fx.root / "t")):
                    self.assertNotIn(secret, shown, secret)
                if kind == "retained":
                    self.assertEqual(shown.count("Environment=<withheld>\n"), 5, "a line per old one, in context")
                    self.assertIn("+Description=ag2-sparrow (AG2 Space listener)\n", shown, "the rest is shown")

    def test_a_drop_in_is_never_read_and_its_lines_would_be_withheld(self):
        drop_in = ("[Service]\nEnvironment=REMOTE_TASK_TOKEN=synth-drop-in-5698\n"
                   "EnvironmentFile=/home/x/drop-in-dir-5698/relay.env\n")
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            shown = self.install_service(fx, "[Service]\nEnvironment=NOTE=synth-drop-in-5698\nExecStart=/x\n",
                                         drop_in)
            self.assertIn("DIFF:", shown)
            for secret in ("synth-drop-in-5698", "drop-in-dir-5698"):
                self.assertNotIn(secret, shown, secret)
            self.assertNotIn("drop-in", hatch_cli._diff(drop_in, "", fx.root / "override.conf").split("\n", 2)[2])

    def test_after_a_failed_service_install_the_detached_bridge_checks_its_own_environment(self):
        for route in ("service install failed", "--no-service, no unit"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[0])  # an env token, and a named token file that is refused
                self.refuse(fx.token_file, "non-utf8")
                fx.env["REMOTE_TASK_TOKEN_FILE"] = str(fx.token_file)
                env = {k: v for k, v in os.environ.items() if k not in owner.CREDENTIAL_KEYS}
                env.update(fx.env, HOME=str(fx.root), USER="hatch")
                spawned = []

                def popen(cmd, env=None, **_kw):
                    spawned.append(env)
                    return mock.Mock(pid=4242, wait=mock.Mock(return_value=1))
                args = mock.Mock(mxid="@a:ag2.space", gateway_instance="", no_service=route.startswith("--"),
                                 no_keepalive=True, no_start=False, force=False,
                                 task_dir=str(fx.root / "task dir"), result_dir=str(fx.root / "r"),
                                 state_dir=str(fx.root / "s"), activity_file=str(fx.root / "act"),
                                 cron_dir=None, keepalive_log=None)
                ok = subprocess.CompletedProcess([], 0, "", "")
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err), \
                        mock.patch.object(hatch_cli.shutil, "which", return_value="/usr/bin/systemctl"), \
                        mock.patch.object(hatch_cli, "_systemd_run", return_value=ok), \
                        mock.patch.object(hatch_cli, "find_reply_command", return_value="/r"), \
                        mock.patch.object(hatch_cli, "reply_command_problem", return_value=""), \
                        mock.patch.object(hatch_cli, "install_hook", return_value=True), \
                        mock.patch.object(hatch_cli, "_find_console_script", return_value="/opt/bin/ag2-sparrow"), \
                        mock.patch.object(hatch_keepalive.subprocess, "Popen", side_effect=popen):
                    hatch_cli.install(args)
                said = out.getvalue() + err.getvalue()
                self.assertEqual(len(spawned), 1, said)
                if not args.no_service:
                    self.assertIn("no service was set up", said)
                self.assertNotIn("REFUSED", said, "Hatch decides nothing about a process it does not run")
                self.assertNotIn(str(fx.token_file), said)
                keys = owner.CREDENTIAL_KEYS + (owner.TOKEN_FILE_KEY, "AG2_DEVICE_ENV", "CLAUDE_CONFIG_DIR")
                fx.env = {k: v for k, v in spawned[0].items() if k in keys}
                result, stderr = self.bridge(fx, BRIDGE_MAIN, timeout=20)
                self.assertIn(f"REMOTE_TASK_TOKEN_FILE names {fx.token_file}", result["exited"] or "")
                self.assertNotIn(ENV_TOKEN, stderr + result["exited"])

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
        script = BRIDGE_MAIN
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

    def test_every_assignment_of_a_repeated_key_is_registered_by_the_bridge(self):
        for which in ("device", "channel"):
            with self.subTest(file=which), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[1])
                path = fx.device if which == "device" else fx.channel
                if which == "channel":
                    fx.device.unlink()
                path.write_text(f"REMOTE_TASK_TOKEN=early-dup-token-1111\nREMOTE_TASK_URL={GW}\n"
                                "REMOTE_TASK_TOKEN=late-dup-token-2222\n")
                fx.candidates = ["early-dup-token-1111", "late-dup-token-2222"]
                out, _err = self.bridge(fx)
                self.assertEqual(out["token"], "late-dup-token-2222", "the last assignment still wins")
                self.assertEqual(out["registered"], {c: True for c in fx.candidates})

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
# Hatch's systemd reachability probe discards what show-environment prints.
IO_ALLOWED = {("remote_gateway_bridge.py", "_read_env_file"), ("remote_gateway_bridge.py", "_read_text"),
              ("remote_gateway_bridge.py", "_vault_lookup"), ("adapters/hatch/cli.py", "systemd_user_available"),
              ("gateway_credential_io.py", "read_file"), ("gateway_credential_io.py", "vault_lookup"),
              ("gateway_credential_io.py", "_vault"), ("gateway_credential_io.py", "_vault_once")}
# A command whose output is a credential store: the Keychain. The systemd user manager's environment
# is SYSTEMD_PROBE's closed-world rule below, which evaluates no constant.
STORE_READS = ("security", "find-generic-password")
SHOW_ENV = "show-environment"
SYSTEMD_PROBE = ("adapters/hatch/cli.py", "systemd_user_available")


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
    try:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and _constant(node.left) is not None:
            elts = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            args = [_constant(e) for e in elts]
            return None if None in args else _constant(node.left) % tuple(args)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.keywords \
                and _constant(node.func.value) is not None:
            args = [_constant(a) for a in node.args]
            if node.func.attr == "format" and None not in args:
                return _constant(node.func.value).format(*args)
            if node.func.attr == "join" and len(node.args) == 1 and isinstance(node.args[0], (ast.Tuple, ast.List)):
                parts = [_constant(e) for e in node.args[0].elts]
                return None if None in parts else _constant(node.func.value).join(parts)
    except (TypeError, ValueError, IndexError, KeyError):
        pass
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


# What would keep the manager's environment: reading its text, printing, storing or yielding it.
PROBE_KEEPS = (ast.Global, ast.Nonlocal, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr, ast.Yield,
               ast.YieldFrom, ast.Lambda)


def _str(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _literal_list(node) -> bool:
    return isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts) and all(_str(e) for e in node.elts)


def _callee(call) -> "str | None":
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _systemd_findings(tree, parents, rel: str) -> "list[str]":
    """Closed world, no constant evaluated: a string literal (an f-string, % or format template part
    included) holding show-environment outside SYSTEMD_PROBE, and a systemctl invocation (a list
    holding the literal "systemctl", any _systemd_run call, or "systemctl" anywhere else but
    shutil.which) whose arguments are not a plain literal list of literal strings."""
    out = []
    for n in ast.walk(tree):
        scope = parents.get(n)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents.get(scope)
        where = f"{rel}:{getattr(n, 'lineno', '?')}"
        if _str(n) and SHOW_ENV in n.value and (rel, getattr(scope, "name", None)) != SYSTEMD_PROBE:
            out.append(f"{where} {SHOW_ENV} outside the reachability probe")
        if _str(n) and n.value == "systemctl":
            parent = parents.get(n)
            ok = _literal_list(parent) or (isinstance(parent, ast.Call) and _callee(parent) == "which")
            if not ok:
                out.append(f"{where} a systemctl invocation whose arguments are not a literal list")
        if isinstance(n, ast.Call) and _callee(n) == "_systemd_run":
            arg = n.args[0] if len(n.args) == 1 and not n.keywords else None
            first = arg.elts[0] if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts else None
            other = _str(first) and first.value != "systemctl"
            if not other and not _literal_list(arg):
                out.append(f"{where} a _systemd_run call whose arguments are not a literal list")
    return out


def _probe_keeps(inner, parents) -> "list[str]":
    """Why a scope that runs show-environment keeps its output: the run's result must be passed
    straight to _ok, a bool, and the scope may not read stdout/stderr, print, write or store."""
    probe = [n for n in inner if _str(n) and SHOW_ENV in n.value]
    if not probe:
        return []
    why = []
    for n in inner:
        if n in probe:
            run = parents.get(n)
            while run is not None and not isinstance(run, ast.Call):
                run = parents.get(run)
            outer = parents.get(run)
            if not (isinstance(outer, ast.Call) and getattr(outer.func, "id", None) == "_ok" and run in outer.args):
                why.append("its result is not only tested by _ok")
        elif isinstance(n, ast.Attribute) and n.attr in ("stdout", "stderr", "output"):
            why.append(f"reads .{n.attr}")
        elif isinstance(n, ast.Call) and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) in (
                "print", "write", "append", "extend", "add", "update", "setattr", "setdefault"):
            why.append("prints or stores")
        elif isinstance(n, PROBE_KEEPS):
            why.append(type(n).__name__)
    return why


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
        store = any(_constant(n) in STORE_READS for n in inner) or \
            any(isinstance(n, (ast.Import, ast.ImportFrom)) and "channel_token" in ast.unparse(n) for n in inner)
        if (reads or store) and (rel, name) not in IO_ALLOWED:
            out.append(f"{rel}:{name} credential I/O outside the adapters' edge")
        out += [f"{rel}:{name} the show-environment probe keeps its output ({why})" for why in _probe_keeps(inner, parents)]
    out += _systemd_findings(tree, parents, rel)
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
            'def f():\n    subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True)',
            'def _manager_env():\n    return _systemd_run(["systemctl", "--user", "show-environment"]).stdout',
        ]
        for source in bypasses:
            self.assertTrue(credential_access(source, "probe.py"), source)
        allowed = ('def _read_text(path):\n    return open(path).read()\nfiles = env_files(env, _read_text)\n'
                   'files = env_files(env, read_text=_read_text)\n'
                   'discover(env, files, read_token_file=lambda p: _read_text(p) or "")\n'
                   'discover(env, files, vault_lookup=lambda k: (_read_text() or (lambda _k: ""))(k))\n')
        self.assertEqual(credential_access(allowed, "remote_gateway_bridge.py"), [])
        self.assertEqual(credential_access('"""Put REMOTE_TASK_TOKEN in the .env file."""\nx = 1\n', "probe.py"), [],
                         "a docstring naming a key is prose, not a read")

    def test_the_reachability_probe_discards_what_show_environment_prints(self):
        rel, run = "adapters/hatch/cli.py", '_systemd_run(["systemctl", "--user", "show-environment"])'
        shipped = (f'def systemd_user_available():\n    return shutil.which("systemctl") is not None and \\\n'
                   f"        _ok({run})\n")
        self.assertEqual(credential_access(shipped, rel), [])
        self.assertTrue(credential_access(shipped, "adapters/hatch/keepalive.py"), "only the one allowlisted probe")
        self.assertTrue(credential_access(shipped.replace("systemd_user_available", "probe"), rel))
        keeps = [f"    return {run}",
                 f"    return {run}.stdout",
                 f"    r = {run}\n    print(r.stdout)\n    return _ok(r)",
                 f"    global LAST\n    LAST = {run}\n    return _ok(LAST)",
                 f"    _ok({run})\n    CACHE.append({run})\n    return True",
                 f"    return _ok(r := {run}) and bool(r.stdout)",
                 f"    sys.stderr.write(str({run}))\n    return True",
                 f"    yield {run}"]
        for body in keeps:
            with self.subTest(body=body):
                self.assertTrue(credential_access(f"def systemd_user_available():\n{body}\n", rel), body)

    # Every runtime-equivalent spelling of the verb: none is a plain literal, so each is flagged
    # without evaluating it, even inside the probe and even when its output is discarded.
    BUILT = ('"show-" + "environment"', '"".join(("show-", "environment"))', 'f"show-{\'environment\'}"',
             '"show-{}".format("environment")', '"show-%s" % "environment"', '"show-%s%s" % ("environ", "ment")',
             '"-".join(["show", "environment"])', '("show-" "env") + "ironment"', 'f"show-{\'environment\'!s}"',
             '"show-{noun}".format(noun="environment")', '"%(verb)s-%(noun)s" % {"verb": "show", "noun": "environment"}',
             '"".join(("show-",) + ("environment",))', 'VERB', 'self.verb', 'verb()', '"show-environment"[0:16]',
             '"tnemnorivne-wohs"[::-1]', 'bytes.decode(b"show-environment")', 'str("show-environment")')

    def test_every_non_literal_systemctl_argument_is_flagged(self):
        rel, run = "adapters/hatch/cli.py", '_systemd_run(["systemctl", "--user", "show-environment"])'
        shipped = f"def systemd_user_available():\n    return _ok({run})\n"
        for cmd in self.BUILT:
            with self.subTest(cmd=cmd):
                folded = f'_systemd_run(["systemctl", "--user", {cmd}])'
                for source in (shipped.replace(run, folded),
                               f"def systemd_user_available():\n    return {folded}.stdout\n",
                               f"def f():\n    c = {cmd}\n    return _systemd_run(['systemctl', c]).stdout\n",
                               f"def f():\n    subprocess.run(['systemctl', '--user', {cmd}])\n",
                               f"def f():\n    subprocess.run(('systemctl', '--user', {cmd}), capture_output=True)\n"):
                    self.assertTrue(credential_access(source, rel), source)
        for source in ('def f():\n    _systemd_run(CMD)\n', 'def f():\n    _systemd_run(["systemctl", *ARGS])\n',
                       'SYSTEMCTL = "systemctl"\ndef f():\n    subprocess.run([SYSTEMCTL, "--user", v])\n',
                       'def f():\n    _systemd_run(["systemctl", "--user", "show", NAME])\n',
                       'def f():\n    _systemd_run(["systemctl"] + rest)\n',
                       'def f():\n    _systemd_run(["systemctl", "--user", "enable", *(["--now"] if s else [])])\n',
                       'def f():\n    subprocess.run("systemctl --user show-environment", shell=True)\n',
                       'X = "show-environment"\n', 'def f():\n    print(f"run systemctl --user show-environment")\n'):
            with self.subTest(source=source):
                self.assertTrue(credential_access(source, "adapters/hatch/cli.py"), source)
        for source in ('def f():\n    _systemd_run(["systemctl", "--user", "stop", "ag2-sparrow.service"])\n',
                       'def f():\n    _systemd_run(["loginctl", "enable-linger", user])\n',
                       'def f():\n    return shutil.which("systemctl")\n'):
            self.assertEqual(credential_access(source, "adapters/hatch/cli.py"), [], source)

    def test_every_shipped_service_name_literal_is_the_service_name(self):
        tree = ast.parse((PKG / "ag2_sparrow" / "adapters" / "hatch" / "cli.py").read_text(encoding="utf-8"))
        tree2 = ast.parse((PKG / "ag2_sparrow" / "adapters" / "hatch" / "keepalive.py").read_text(encoding="utf-8"))
        names = {e.value for t in (tree, tree2) for n in ast.walk(t) if _literal_list(n) and n.elts[0].value == "systemctl"
                 for e in n.elts if e.value.endswith(".service")}
        self.assertEqual(names, {"ag2-sparrow.service"})
        self.assertIn("SERVICE_NAME = 'ag2-sparrow.service'", ast.unparse(tree))

    def test_the_vendored_and_generated_copies_match_their_sources(self):
        for cmd in ([sys.executable, str(PKG / "tools" / "sync_from_src.py"), "--check"],
                    [sys.executable, str(REPO / "tools" / "sync_gateway_credentials.py"), "--check"]):
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO), timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((PKG / "ag2_sparrow" / "gateway_credential_sources.py").read_text(),
                         (REPO / "src" / "gateway_credential_sources.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
