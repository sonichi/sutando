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
  - a structural guard: no other package module reads a credential source or re-implements the
    masking contract (one registry, in diagnostics), and the vendored and generated copies match
    their canonical sources.
Hermetic: temp HOME and files, a faked vault, no network, no real credential.
"""
from __future__ import annotations

import ast
import contextlib
import functools
import importlib.util
import io
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow"
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO / "shared"))
from ag2_sparrow import gateway_credential_io, gateway_credential_sources as owner, gateway_credentials as contract, service_manager  # noqa: E402
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
    ("an env token file is the first token source and the rotation file", {"REMOTE_TASK_TOKEN_FILE": "TOKENFILE",
                                                                            "REMOTE_TASK_URL": GW},
     {"REMOTE_TASK_TOKEN": DEVICE_TOKEN}, {}, VAULT_TOKEN, FILE_TOKEN,
     (FILE_TOKEN, "token_file", GW, "TOKENFILE")),
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
            self.token_file.chmod(0o600)  # a token file is read only when it is private
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


# The name oracle, independent of the contract's constants: each stem, split by every separator
# the rule folds away, is a credential name; the controls are not.
ORACLE_STEMS = ("pass", "pwd", "secret", "token", "key", "auth", "cred", "bearer", "jwt", "session", "cookie",
                "priv", "sig", "otp", "apikey", "pw", "pat")
ORACLE_SEPARATORS = ("/", ".", "-", " ", "+", ":", "_", "\\\"", "\u2022", "\u00b7", "\u2192", "\u3000")
ORACLE_CONTROLS = ("name", "note", "colour", "ExecStart", "mode")


def unquoted_name_lines(name: str, sep: str) -> "tuple[str, ...]":
    """`name` as a plain assignment, colon key and flag; a flag word split at an ASCII blank is two
    shell words, so there the flag is only the quoted one."""
    lines = (f"{name}=synth-only-value", f"{name}: synth-only-value", f"run --{name}=synth-only-value",
             f'run --"{name}" synth-only-value', f"run '--{name}' synth-only-value")
    return lines + (() if sep == " " else (f"run --{name} synth-only-value",))


def name_sweep():
    """(key, separator, whether it names a credential) for every stem split at every position."""
    for stem in ORACLE_STEMS:
        for sep in ORACLE_SEPARATORS:
            for i in range(1, len(stem)):
                yield stem[:i] + sep + stem[i:], sep, True
    for control in ORACLE_CONTROLS:
        yield control, "", False


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
        # A setup revision has one spelling on both sides: an integer is its decimal text, then code_field.
        self.assertEqual(self.c.revision_field(41, registry), "41")
        self.assertEqual(self.c.revision_field("r41", registry), "r41")
        self.assertIsNone(self.c.revision_field(7, registry), "a digit of the secret is a piece of it")
        for value in (True, False, 41.0, None, "has space", "bearer-secret-value-77", "secret-value-77"):
            self.assertIsNone(self.c.revision_field(value, registry), value)
        self.assertIsNone(self.c.revision_field("query-secret-3", None, URL_SECRET_URL))

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

    def test_every_key_position_is_judged_whole_by_credential_name(self):
        for name, sep, shaped in name_sweep():
            for line in (f'{{"{name}":"synth-only-value"}}', f"{{'{name}': 'synth-only-value'}}",
                         f'Note={{"a": 1, "{name}" : "synth-only-value"}}'):
                with self.subTest(line=line):
                    self.assertEqual(self.c.credential_shaped(line), shaped, line)
                    shown = self.c.mask_env_assignments(line + "\n", self.c.SecretRegistry())
                    self.assertEqual("synth-only-value" not in shown, shaped, shown)
            for line in unquoted_name_lines(name, sep):
                with self.subTest(line=line):
                    self.assertEqual(self.c.credential_shaped(line), shaped, line)
                    self.assertEqual("synth-only-value" not in self.c.mask_env_assignments(
                        line + "\n", self.c.SecretRegistry()), shaped, line)

    def test_a_name_position_judges_every_suffix_of_its_line(self):
        rng, alphabet = random.Random(5698), "pPaAtTwWsSkKeEyYxX01 _-./\"'\u3000\uff50"
        for _ in range(4000):
            view = "".join(rng.choice(alphabet + ":=") for _ in range(rng.randint(1, 24)))
            seps = [m.start() for m in self.c._SEPARATOR_RE.finditer(view)]
            judged = self.c._name_positions(view)[:len(seps)]
            for j, (named, _value) in zip(seps, judged):
                start = max(view.rfind("\n", 0, j), view.rfind("\r", 0, j)) + 1
                if any(self.c.credential_name(view[i:j]) for i in range(start, j)):
                    self.assertTrue(named, (view, j))

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
        self.assertEqual(self.o.SOURCES, (("token", ("token_file", "env", "file", "vault")),
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
        self.assertEqual((found.token, found.token_source), ("overwritten-file-5530", "token_file"))
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
        self.assertEqual((found.token, found.token_source), ("", "none"), "no tier stands in for a refused one")
        self.assertEqual(found.notes, (self.o.refusal_note("/t", "not UTF-8"),))
        self.assertIn("REMOTE_TASK_TOKEN_FILE names /t", found.fatal)
        self.assertIn("not UTF-8", found.fatal)
        absent = self.o.discover({"REMOTE_TASK_TOKEN": ENV_TOKEN, "REMOTE_TASK_TOKEN_FILE": "/t"},
                                 read_token_file=lambda p: (None, ""), vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((absent.token, absent.notes, absent.fatal), (ENV_TOKEN, (), ""),
                         "a token file not yet written is no refusal: rotation waits for it, the env token runs")
        unwritten = self.o.discover({"REMOTE_TASK_TOKEN_FILE": "/t"}, [("/dev", {"REMOTE_TASK_TOKEN": DEVICE_TOKEN})],
                                    read_token_file=lambda p: (None, ""), vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((unwritten.token, unwritten.token_source), ("", "none"), "never the channel .env or the vault")

    def test_a_url_shaped_bearer_is_registered_whole(self):
        registry = self.o.contract.SecretRegistry()
        self.o.register(registry, [URL_SHAPED_BEARER])
        self.assertTrue(registry.contains(f"x {URL_SHAPED_BEARER} y"))

    def test_a_url_keys_plain_url_registers_by_its_parts_so_the_bare_gateway_renders(self):
        tf = f"REMOTE_TASK_URL={GW}\nREMOTE_TASK_TOKEN={FILE_TOKEN}\n"
        files = self.o.env_files({"AG2_DEVICE_ENV": "/dev"}, lambda p: f"REMOTE_TASK_URL={URL_SECRET_URL}\n")
        registry = self.o.contract.SecretRegistry()
        self.o.discover({"REMOTE_TASK_URL": GW, "AG2_DEVICE_ENV": "/dev", "REMOTE_TASK_TOKEN_FILE": "/t",
                         "REMOTE_TASK_TOKEN": URL_SHAPED_BEARER},
                        files, read_token_file=lambda p: tf, registry=registry)
        for line in (f"poll ok, gateway {GW}", f"gateway {self.o.contract.redact_url(URL_SECRET_URL)}"):
            self.assertEqual(registry.scrub(line), line, "a URL's redacted form is not a secret")
        for secret in ("pass-word-2", "query-secret-3", "frag-secret-4", URL_SHAPED_BEARER, FILE_TOKEN):
            self.assertTrue(registry.contains(f"x {secret} y"), secret)
        whole = self.o.contract.SecretRegistry()
        self.o.register_pairs(whole, [("REMOTE_TASK_URL", f"{GW}|{CHANNEL_TOKEN}"), ("REMOTE_TASK_URL", "bare-9911")])
        for secret in (CHANNEL_TOKEN, "bare-9911"):
            self.assertTrue(whole.contains(secret), "anything but a plain URL under a URL key is registered whole")

    def test_a_token_files_raw_line_is_registered_whichever_env_names_the_file(self):
        text = f"REMOTE_TASK_TOKEN={GW}|{FILE_TOKEN}\nraw-candidate-7781\n"
        for env, files in (({"REMOTE_TASK_TOKEN_FILE": "/t"}, []),
                           ({}, [("/chan", {"REMOTE_TASK_TOKEN_FILE": "/t"})])):
            with self.subTest(env=env):
                registry = self.o.contract.SecretRegistry()
                self.o.discover(env, files, read_token_file=lambda p: text, registry=registry)
                for secret in (FILE_TOKEN, "raw-candidate-7781"):
                    self.assertTrue(registry.contains(f"x {secret} y"), secret)

    def test_only_the_raw_line_that_could_become_the_token_is_registered(self):
        text = (f"# rotated\nREMOTE_TASK_TOKEN={GW}|{FILE_TOKEN}\nset -a\nok\n")
        registry = self.o.contract.SecretRegistry()
        self.o.discover({}, [("/chan", {"REMOTE_TASK_TOKEN_FILE": "/t"})],
                        read_token_file=lambda p: text, registry=registry)
        self.assertTrue(registry.contains(FILE_TOKEN))
        for line in ("poll ok -- backing off 4s", "set -a", "a broken token"):
            self.assertEqual(registry.scrub(line), line, "a short line beside an assignment is no candidate")
        later = self.o.contract.SecretRegistry()
        self.o.discover({"REMOTE_TASK_TOKEN_FILE": "/t"}, read_token_file=lambda p: (
            f"REMOTE_TASK_TOKEN={FILE_TOKEN}\nfirst-raw-line-1\nsecond-raw-line-2\n"), registry=later)
        self.assertTrue(later.contains("first-raw-line-1"))
        self.assertFalse(later.contains("second-raw-line-2"), "no later raw line is ever read as the token")
        alone = self.o.contract.SecretRegistry()
        self.o.discover({"REMOTE_TASK_TOKEN_FILE": "/t"}, read_token_file=lambda p: "# c\nok7\n", registry=alone)
        self.assertTrue(alone.contains("x ok7 y"), "with no assignment the raw line is the token, at any length")

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

    def test_a_readable_token_file_closes_the_file_and_vault_tiers(self):
        env = {"REMOTE_TASK_TOKEN_FILE": "/t", "AG2_DEVICE_ENV": "/dev"}
        files = [("/dev", {"REMOTE_TASK_TOKEN": f"{GW}|{DEVICE_TOKEN}"})]
        split = self.o.discover(env, files, read_token_file=lambda p: f"REMOTE_TASK_URL={GW}\n{FILE_TOKEN}\n",
                                vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((split.token, split.token_source, split.url, split.token_key),
                         (FILE_TOKEN, "token_file", GW, "REMOTE_TASK_TOKEN_FILE"), "the split layout's URL rides along")
        blank = self.o.discover(env, files, read_token_file=lambda p: "# rotating\n", vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((blank.token, blank.token_source), ("", "none"),
                         "a blank token file never falls through to the channel .env or the vault")
        self.assertIn("/t, which holds no token", blank.fatal)
        with_env = self.o.discover(dict(env, REMOTE_TASK_TOKEN=ENV_TOKEN), files, read_token_file=lambda p: "",
                                   vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((with_env.token, with_env.token_source), (ENV_TOKEN, "env"), "only the env tier may follow it")

    def host_token_from(self, env, token_file_text):
        """A host (non-seat) discovery with a token in the channel .env and in the vault; which tier won,
        and every vault key that was read."""
        vault_reads = []
        read = (lambda p: (None, "")) if token_file_text is None else (lambda p: token_file_text)
        found = self.o.discover(env, [("/chan", {"REMOTE_TASK_TOKEN": CHANNEL_TOKEN})], read_token_file=read,
                                vault_lookup=lambda k: vault_reads.append(k) or VAULT_TOKEN)
        return found, vault_reads

    def test_a_host_named_token_file_present_outranks_the_env_token(self):
        found, vault_reads = self.host_token_from(
            {"REMOTE_TASK_TOKEN_FILE": "/t", "REMOTE_TASK_TOKEN": ENV_TOKEN, "REMOTE_TASK_URL": GW},
            f"REMOTE_TASK_TOKEN={FILE_TOKEN}\n")
        self.assertFalse(self.o.is_seat({"REMOTE_TASK_TOKEN_FILE": "/t"}))
        self.assertEqual((found.token, found.token_source, found.token_key, found.fatal),
                         (FILE_TOKEN, "token_file", "REMOTE_TASK_TOKEN_FILE", ""), "the named file beats the env")
        self.assertEqual(vault_reads, [])

    def test_a_host_named_token_file_missing_lets_only_the_env_stand_in(self):
        env = {"REMOTE_TASK_TOKEN_FILE": "/t", "REMOTE_TASK_URL": GW}
        found, vault_reads = self.host_token_from(dict(env, REMOTE_TASK_TOKEN=ENV_TOKEN), None)
        self.assertEqual((found.token, found.token_source, found.fatal), (ENV_TOKEN, "env", ""),
                         "a named file not yet written: the env token runs")
        self.assertEqual((found.source_file, vault_reads), ("", []), "neither the channel .env nor the vault is read")
        self.assertEqual(found.token_file, "/t", "rotation still watches the named file")
        bare, vault_reads = self.host_token_from(env, None)
        self.assertEqual((bare.token, bare.token_source, bare.source_file), ("", "none", ""),
                         "the file and vault tiers are closed: no channel .env or vault token stands in")
        self.assertNotIn(CHANNEL_TOKEN, bare.raw + bare.token)
        self.assertEqual(vault_reads, [], "the vault is never consulted for a missing named token file")

    def test_a_host_without_a_named_token_file_walks_env_then_file_then_vault(self):
        base = {"REMOTE_TASK_URL": GW}
        for env, token_file_text, want in (
                (dict(base, REMOTE_TASK_TOKEN=ENV_TOKEN), None, (ENV_TOKEN, "env")),
                (base, None, (CHANNEL_TOKEN, "file")),
                (dict(base, REMOTE_TASK_TOKEN_FILE=""), None, (CHANNEL_TOKEN, "file"))):
            with self.subTest(env=env):
                found, _reads = self.host_token_from(env, token_file_text)
                self.assertEqual((found.token, found.token_source, found.fatal), want + ("",))
        vault_only = self.o.discover(base, [("/chan", {})], read_token_file=lambda p: (None, ""),
                                     vault_lookup=lambda k: VAULT_TOKEN if k == "REMOTE_TASK_TOKEN" else "")
        self.assertEqual((vault_only.token, vault_only.token_source), (VAULT_TOKEN, "vault"), "the vault is last")

    def test_a_seat_reads_its_own_token_file_and_nothing_else(self):
        seat = {"REMOTE_TASK_SEAT": "1", "AG2_DEVICE_ENV": "/seat/.env", "CLAUDE_CONFIG_DIR": "/core",
                "REMOTE_TASK_TOKEN_FILE": "/seat/token", "REMOTE_TASK_TOKEN": ENV_TOKEN}
        self.assertEqual(self.o.channel_env_paths(seat), ["/seat/.env"], "never the host's channel dir")
        read = lambda p: {"/seat/.env": "AGENT_MXID=@seat:example.org\n",
                          "/seat/token": f"{GW}|{FILE_TOKEN}\n"}.get(p, f"REMOTE_TASK_TOKEN={CHANNEL_TOKEN}\n")
        files = self.o.env_files(seat, read)
        self.assertEqual([p for p, _v in files], ["/seat/.env"])
        found = self.o.discover(seat, files, read_token_file=read, vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((found.token, found.token_source, found.url, found.fatal), (FILE_TOKEN, "token_file", GW, ""))
        for env in ({k: v for k, v in seat.items() if k != "REMOTE_TASK_TOKEN_FILE"}, seat):
            with self.subTest(token_file=env.get("REMOTE_TASK_TOKEN_FILE")):
                none = self.o.discover(env, files, read_token_file=lambda p: (None, ""), vault_lookup=lambda k: VAULT_TOKEN)
                self.assertEqual((none.token, none.token_source), ("", "none"),
                                 "a seat with no token file holds no token: not the env's, the core's file's or the vault's")
                self.assertTrue(none.fatal)
        homeless = self.o.discover({k: v for k, v in seat.items() if k != "AG2_DEVICE_ENV"}, [], read_token_file=read)
        self.assertIn("AG2_DEVICE_ENV does not name its .env", homeless.fatal)

    def test_a_32_hex_gateway_instance_is_a_seat_and_never_takes_the_envs_token_or_url(self):
        env = {"GATEWAY_INSTANCE": "e" * 32, "AG2_DEVICE_ENV": "/seat/.env", "REMOTE_TASK_TOKEN": ENV_TOKEN,
               "REMOTE_TASK_URL": "https://core-gw.example/relay", "REMOTE_TASK_TOKEN_FILE": "/seat/token"}
        self.assertTrue(self.o.is_seat(env))
        self.assertFalse(self.o.is_seat({"GATEWAY_INSTANCE": "dev"}), "a named instance is no seat")
        missing = self.o.discover(env, [], read_token_file=lambda p: (None, ""), vault_lookup=lambda k: VAULT_TOKEN)
        self.assertEqual((missing.token, missing.token_source), ("", "none"))
        self.assertIn("does not exist", missing.fatal)
        own = self.o.discover(env, [], read_token_file=lambda p: f"{GW}|{FILE_TOKEN}\n")
        self.assertEqual((own.token, own.url, own.url_key, own.fatal), (FILE_TOKEN, GW, "", ""),
                         "the seat's own URL, never the inherited REMOTE_TASK_URL")

    def test_an_empty_or_malformed_token_file_is_fatal_with_its_own_reason(self):
        seat = {"REMOTE_TASK_SEAT": "1", "AG2_DEVICE_ENV": "/seat/.env", "REMOTE_TASK_TOKEN_FILE": "/seat/token"}
        empty = self.o.discover(seat, [], read_token_file=lambda p: "\n")
        self.assertIn("/seat/token, which holds no token; a seat has no other credential source", empty.fatal)
        bare = self.o.discover(seat, [], read_token_file=lambda p: f"{FILE_TOKEN}\n")
        self.assertIn("carries no gateway URL", bare.fatal)
        split = self.o.discover(seat, [], read_token_file=lambda p: f"REMOTE_TASK_URL={GW}\n{FILE_TOKEN}\n")
        self.assertEqual((split.token, split.url, split.fatal), (FILE_TOKEN, GW, ""), "the split layout carries one")
        host = self.o.discover({"REMOTE_TASK_TOKEN_FILE": "/t"}, [("/dev", {"REMOTE_TASK_TOKEN": DEVICE_TOKEN})],
                               read_token_file=lambda p: "# rotating\n", vault_lookup=lambda k: VAULT_TOKEN)
        self.assertIn("/t, which holds no token and no REMOTE_TASK_TOKEN is set", host.fatal)

    def test_the_seat_writer_owns_the_layout_and_never_returns_the_bearer(self):
        bearer = "seat-bearer-HHHH8888"
        with tempfile.TemporaryDirectory() as td:
            seat = Path(td) / "agent.bridge"
            args = dict(agent_id="@a:example.org", instance="0" * 32, tier="owner", relay_url=GW, bearer=bearer)
            self.assertEqual(self.o.write_seat_credentials(seat, **args), "")
            chan = seat / "channels" / "ag2space"
            self.assertEqual(sorted(p.name for p in chan.iterdir()), [".env", "token"], "no temp file is left behind")
            self.assertEqual(stat.S_IMODE(chan.stat().st_mode), 0o700)
            for f in (".env", "token"):
                self.assertEqual(stat.S_IMODE((chan / f).stat().st_mode), 0o600, f)
            self.assertEqual((chan / "token").read_text(), f"{GW}|{bearer}\n")
            values = self.o.parse_env_text((chan / ".env").read_text())
            self.assertEqual(values, {"AGENT_MXID": "@a:example.org", "GATEWAY_INSTANCE": "0" * 32,
                                      "REMOTE_TASK_TOKEN_FILE": str(chan / "token"), "REMOTE_TASK_TIER": "owner"})
            self.assertNotIn(bearer, (chan / ".env").read_text(), "the token lives in the token file only")
            inode = (chan / "token").stat().st_ino
            self.assertEqual(self.o.write_seat_credentials(seat, **dict(args, bearer="rotated-IIII9999")), "")
            self.assertEqual((chan / "token").read_text(), f"{GW}|rotated-IIII9999\n")
            self.assertNotEqual((chan / "token").stat().st_ino, inode, "a rotation is a rename, never a rewrite in place")
            for bad in (dict(bearer=""), dict(bearer="a b"), dict(bearer="a|b"), dict(instance="short"),
                        dict(tier="admin"), dict(agent_id=""), dict(relay_url="ftp://x"), dict(agent_id="@a\nB=1")):
                with self.subTest(bad=bad):
                    why = self.o.write_seat_credentials(seat, **dict(args, **bad))
                    self.assertTrue(why)
                    self.assertNotIn(bearer, why)
            self.assertEqual((chan / "token").read_text(), f"{GW}|rotated-IIII9999\n", "a refused write changes nothing")
            with mock.patch.object(self.o.os, "replace", side_effect=OSError("disk full")), \
                    self.assertRaises(OSError):
                self.o.write_seat_credentials(seat, **dict(args, bearer="never-landed-PPPP6666"))
            self.assertEqual(sorted(p.name for p in chan.iterdir()), [".env", "token"], "a failed write leaves no temp file")
            self.assertEqual((chan / "token").read_text(), f"{GW}|rotated-IIII9999\n")
        self.assertRegex(self.o.mint_gateway_instance(), r"^[0-9a-f]{32}$")
        self.assertNotEqual(self.o.mint_gateway_instance(), self.o.mint_gateway_instance())

    def test_a_seat_bridge_environment_exports_the_seat_and_clears_what_it_inherits(self):
        values = {"AGENT_MXID": "@a:example.org", "GATEWAY_INSTANCE": "a" * 32,
                  "REMOTE_TASK_TOKEN_FILE": "/s/token", "REMOTE_TASK_TIER": "team"}
        text = lambda vals: (lambda p: "".join(f"{k}={v}\n" for k, v in vals.items()))
        env, why = self.o.seat_bridge_environ("/s/.env", text(values))
        self.assertEqual(why, "")
        self.assertEqual({k: env[k] for k in ("AG2_DEVICE_ENV", "REMOTE_TASK_SEAT", "AGENT_MXID", "GATEWAY_INSTANCE",
                                              "REMOTE_TASK_TOKEN_FILE", "REMOTE_TASK_TIER")},
                         {"AG2_DEVICE_ENV": "/s/.env", "REMOTE_TASK_SEAT": "1", "AGENT_MXID": "@a:example.org",
                          "GATEWAY_INSTANCE": "a" * 32, "REMOTE_TASK_TOKEN_FILE": "/s/token", "REMOTE_TASK_TIER": "team"})
        for key in ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "REMOTE_TASK_URL", "AGENT_ID", "GATEWAY_CREDENTIALS_INJECTED"):
            self.assertEqual(env[key], "", key)
        self.assertEqual(self.o.seat_bridge_environ("/s/.env", text(values), core_tier="guest")[0]["REMOTE_TASK_TIER"], "guest")
        self.assertEqual(self.o.seat_bridge_environ("/s/.env", text(values), core_tier="owner")[0]["REMOTE_TASK_TIER"], "team",
                         "the core's cap lowers a seat, never raises it")
        self.assertEqual(self.o.seat_bridge_environ("/d/.env", text({"REMOTE_TASK_TOKEN": "x"})), ({}, ""),
                         "no seat, no change")
        self.assertEqual(self.o.seat_bridge_environ("/d/.env", lambda p: (None, ""))[0], {}, "an absent file is no seat")
        refused = self.o.seat_bridge_environ("/s/.env", lambda p: (None, "not UTF-8"))
        self.assertEqual((refused[0], refused[1]), ({}, self.o.refusal_note("/s/.env", "not UTF-8")))
        for key in ("AGENT_MXID", "GATEWAY_INSTANCE", "REMOTE_TASK_TIER", "REMOTE_TASK_TOKEN_FILE"):
            with self.subTest(missing=key):
                env, why = self.o.seat_bridge_environ("/s/.env", text(dict(values, **{key: ""})))
                self.assertEqual(env, {})
                self.assertIn(key, why)

    def test_the_owner_touches_only_the_declaration(self):
        tree = ast.parse(Path(self.o.__file__).read_text())
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for io_call in ("os.environ.get", "os.getenv", "Path", "print"):
            self.assertNotIn(io_call, calls, io_call)
        self.assertNotIn("os.environ", ast.unparse(tree))
        def where(names):
            return {f.name for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for n in ast.walk(f) if isinstance(n, ast.Call) and ast.unparse(n.func) in names}
        self.assertEqual(where({"open"}), {"_declaration_owned"}, "the one read: the declaration's own check")
        self.assertEqual(where({"os.fdopen", "tempfile.mkstemp", "os.replace", "os.makedirs", "os.chmod"}),
                         {"write_bridge_declaration", "_replace_file", "write_seat_credentials"})
        self.assertEqual(where({"os.unlink"}), {"write_bridge_declaration", "remove_bridge_declaration", "_replace_file"})




class _Spy:
    """A file handle that records the size of each read."""

    def __init__(self, fh, reads):
        self._fh, self._reads = fh, reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def fileno(self):
        return self._fh.fileno()

    def read(self, n=-1):
        self._reads.append(n)
        return self._fh.read(n)


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


# A seat's refused startup, with the bridge's backoff recorded instead of slept; subprocess's own
# wait loop (a `ps` the bridge runs) still sleeps, since the time module is shared.
SEAT_MAIN = BRIDGE_MAIN.replace("try:\n    m.main()", (
    "slept = []\n_real_sleep = m.time.sleep\n"
    "def _sleep(s):\n"
    "    if __import__('sys')._getframe(1).f_globals.get('__name__') == 'subprocess':\n"
    "        return _real_sleep(s)\n"
    "    slept.append(s)\n"
    "m.time.sleep = _sleep\ntry:\n    m.main()")).replace(
    'print(json.dumps({"exited": exited, ', 'print(json.dumps({"exited": exited, "slept": slept, ')


class RealSinks(unittest.TestCase):
    """The same rows through the real bridge and the real Hatch install diff."""

    def setUp(self):
        # No case reaches the host's systemctl unless it supplies a fake.
        patcher = mock.patch.object(service_manager, "_run", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Nor the host's vault: a Hatch diff that reaches the vault tier reads a fake one.
        vault = mock.patch.object(gateway_credential_io, "_vault", return_value=None)
        vault.start()
        self.addCleanup(vault.stop)

    def bridge(self, fx, script=BRIDGE, timeout=60):
        state = fx.root / "state"
        # The helper owns the coverage bootstrap: only the coverage package reaches the child's path.
        env = hermetic_env(fx.root, **{"PATH": "/usr/bin:/bin", "PYTHONPATH": str(PKG),
                                       "AGENT_CONNECT_TASK_DIR": str(fx.root / "t"),
                                       "AGENT_CONNECT_RESULT_DIR": str(fx.root / "r"),
                                       "AGENT_CONNECT_STATE_DIR": str(state), **fx.env})
        child = fx.root / "child.py"
        child.write_text(script.replace("VAULT", json.dumps(fx.vault)).replace("CANDIDATES", json.dumps(fx.candidates)))
        cmd, cwd = [sys.executable, str(child)], fx.root
        if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":  # the gate measures the child too
            cmd = [sys.executable, "-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}", str(child)]
            cwd = REPO
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=str(cwd), timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr

    def plant_coverage(self, root: Path) -> "tuple[Path, Path]":
        """A synthetic coverage install that runs its target, with a sitecustomize.py beside it."""
        site = root / "site-packages"
        (site / "coverage").mkdir(parents=True)
        (site / "coverage" / "__init__.py").write_text("")
        (site / "coverage" / "__main__.py").write_text(
            "import runpy, sys\nsys.argv = sys.argv[3:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")
        witness = root / "sitecustomize-witness"
        (site / "sitecustomize.py").write_text(
            f"import os\nopen({str(witness)!r}, 'a').write(os.environ.get('REMOTE_TASK_TOKEN_FILE', '-') + '\\n')\n")
        return site, witness

    def under_synthetic_coverage(self, site: Path):
        fake = types.ModuleType("coverage")
        fake.__file__ = str(site / "coverage" / "__init__.py")
        return mock.patch.dict(sys.modules, {"coverage": fake}), mock.patch.dict(
            os.environ, {"SUTANDO_TEST_SUBPROCESS_COVERAGE": "1"})

    def test_a_sitecustomize_beside_coverage_never_runs_in_the_bridge_child(self):
        with tempfile.TemporaryDirectory() as td:
            site, witness = self.plant_coverage(Path(td))
            fx = Fixture(Path(td) / "row", ROWS[0])
            mods, env_patch = self.under_synthetic_coverage(site)
            with mods, env_patch:
                out, _err = self.bridge(fx)
            self.assertEqual(out["token"], fx.expected()["token"], "the child ran under the synthetic coverage")
            self.assertFalse(witness.exists(), "a sitecustomize beside coverage ran in the bridge child")

    def test_the_planted_sitecustomize_runs_when_its_directory_is_on_the_path(self):
        # Control: the containing directory, as the removed private bootstrap added it, runs the plant.
        with tempfile.TemporaryDirectory() as td:
            site, witness = self.plant_coverage(Path(td))
            fx = Fixture(Path(td) / "row", ROWS[0])
            fx.env["PYTHONPATH"] = os.pathsep.join([str(PKG), str(site)])
            mods, env_patch = self.under_synthetic_coverage(site)
            with mods, env_patch:
                self.bridge(fx)
            self.assertTrue(witness.exists(), "the planted sitecustomize never ran: the control proves nothing")

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

    def test_a_seat_with_the_token_file_never_connects_as_the_core(self):
        core = "core-token-JJJJ0000"
        seat_bearer = "seat-bearer-KKKK1111"
        seat_gw = "https://seat-gw.example/relay"
        for missing in (False, True):
            with self.subTest(missing_token_file=missing), tempfile.TemporaryDirectory() as td:
                row = ("seat", {"REMOTE_TASK_TOKEN": core, "REMOTE_TASK_URL": GW}, {}, {"REMOTE_TASK_TOKEN": core},
                       core, None, ("", "", "", ""))
                fx = Fixture(Path(td), row)
                seat = fx.root / "agent.bridge"
                self.assertEqual(owner.write_seat_credentials(seat, agent_id="@seat:example.org", instance="b" * 32,
                                                              tier="owner", relay_url=seat_gw, bearer=seat_bearer), "")
                seat_env = seat / "channels" / "ag2space" / ".env"
                if missing:
                    (seat / "channels" / "ag2space" / "token").unlink()
                launch, why = owner.seat_bridge_environ(str(seat_env), lambda p: Path(p).read_text())
                self.assertEqual(why, "")
                # The host's own token sits in its env, its channel .env and its vault; the seat launch clears the env.
                fx.env.update(launch)
                fx.candidates = [seat_bearer] if not missing else []
                # main() only where startup must refuse: a seat that holds a token would go on to poll.
                out, err = self.bridge(fx, SEAT_MAIN if missing else BRIDGE, timeout=20)
                self.assertNotEqual(out["token"], core)
                self.assertNotIn(core, (out["header"] or "") + err + (out.get("exited") or ""))
                if missing:
                    self.assertEqual((out["token"], out["source"]), ("", "none"))
                    self.assertIn("does not exist", out["exited"])
                    self.assertEqual(out["slept"], [60], "a seat that cannot start backs off before it exits")
                    status = json.loads(next((fx.root / "state").glob("gateway-status*.json")).read_text())
                    self.assertFalse(status["connected"])
                    self.assertIn("does not exist", status["error"], "the reason is in the status, not only stderr")
                else:
                    self.assertEqual((out["token"], out["source"], out["url"]), (seat_bearer, "token_file", seat_gw))
                    self.assertNotIn(seat_bearer, err, "the bearer is never logged")

    def test_a_host_reads_its_named_channel_env_as_before_and_only_a_seat_reads_it_private(self):
        core = f"{GW}|core-token-QQQQ7777"
        script = BRIDGE.replace("print(json.dumps({", 'print(json.dumps({"fatal": m._FOUND.fatal, ')
        for shape in ("0644", "symlink"):
            for seat in (False, True):
                with self.subTest(shape=shape, seat=seat), tempfile.TemporaryDirectory() as td:
                    fx = Fixture(Path(td), ("core", {}, {}, {}, "", None, ("", "", "", "")))
                    real = fx.root / "real.env"
                    real.write_text(f"REMOTE_TASK_TOKEN={core}\n")
                    real.chmod(0o644 if shape == "0644" else 0o600)
                    named = fx.channel
                    named.unlink()
                    if shape == "symlink":
                        named.symlink_to(real)
                    else:
                        named.write_text(real.read_text())
                        named.chmod(0o644)
                    # startup-runtime.sh: the core's channel .env is both the token file and the env token's source.
                    fx.env.update(REMOTE_TASK_TOKEN_FILE=str(named), REMOTE_TASK_TOKEN=core)
                    if seat:
                        fx.env.update(REMOTE_TASK_SEAT="1", AG2_DEVICE_ENV=str(fx.root / "seat.env"))
                    fx.candidates = []
                    out, _err = self.bridge(fx, script)
                    if seat:
                        self.assertEqual((out["token"], out["source"]), ("", "none"))
                        self.assertIn("mode 0644" if shape == "0644" else "a symlink", out["fatal"])
                    else:
                        self.assertEqual((out["token"], out["source"], out["fatal"]),
                                         ("core-token-QQQQ7777", "token_file", ""), "a live core starts as before")

    def test_a_host_named_token_file_missing_never_connects_with_the_channel_env_or_vault(self):
        for env_token in (ENV_TOKEN, None):
            with self.subTest(env_token=env_token), tempfile.TemporaryDirectory() as td:
                env = {"REMOTE_TASK_TOKEN_FILE": "TOKENFILE", "REMOTE_TASK_URL": GW}
                if env_token:
                    env["REMOTE_TASK_TOKEN"] = env_token
                fx = Fixture(Path(td), ("missing", env, {"REMOTE_TASK_TOKEN": DEVICE_TOKEN},
                                        {"REMOTE_TASK_TOKEN": CHANNEL_TOKEN}, VAULT_TOKEN, None, ("", "", "", "")))
                self.assertFalse(fx.token_file.exists())
                fx.candidates = []
                out, err = self.bridge(fx, BRIDGE if env_token else BRIDGE_MAIN, timeout=20)
                for other in (DEVICE_TOKEN, CHANNEL_TOKEN, VAULT_TOKEN):
                    self.assertNotIn(other, json.dumps(out) + err)
                if env_token:
                    self.assertEqual((out["token"], out["source"], out["token_file"]), (ENV_TOKEN, "env", str(fx.token_file)))
                else:
                    self.assertEqual((out["token"], out["source"]), ("", "none"))
                    self.assertIn("set REMOTE_TASK_TOKEN", out["exited"] or "", "it refuses to start, and says what to set")

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
            bad.chmod(0o600)
            fx.env["REMOTE_TASK_TOKEN_FILE"] = str(bad)
            out, _err = self.bridge(fx, BRIDGE.replace("print(json.dumps({",
                                                      'print(json.dumps({"rotated": m._read_token_file(m.TOKEN_FILE), '))
            self.assertEqual((out["rotated"], out["token"], out["token_file"]), ("", "", str(bad)),
                             "the import survives it, holds no token, and a rotation read is no rotation")

    def test_a_refused_named_token_file_is_fatal_at_startup(self):
        script = BRIDGE_MAIN
        for how, reason in (("oversize", "over 1048576 bytes"), ("non-utf8", "not UTF-8"),
                            ("fifo", "not a regular file")):
            with self.subTest(how=how), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), ROWS[0])
                self.refuse(fx.token_file, how)
                fx.env["REMOTE_TASK_TOKEN_FILE"] = str(fx.token_file)
                out, err = self.bridge(fx, script, timeout=20)
                self.assertEqual(out["token"], "", "the env token does not stand in for a refused token file")
                self.assertIn(f"REMOTE_TASK_TOKEN_FILE names {fx.token_file}", out["exited"] or "")
                self.assertIn(reason, out["exited"])
                self.assertIn(f"credential file {fx.token_file} was refused ({reason})", err)
                self.assertNotIn(DEVICE_TOKEN, err + out["exited"], "path and reason, never content")

    def test_a_token_file_refused_at_reload_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            fx.token_file.write_text(f"REMOTE_TASK_TOKEN={ENV_TOKEN}\n")
            fx.token_file.chmod(0o600)
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

    def test_a_malformed_rotated_gateway_url_is_logged_as_the_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(Path(td), ROWS[0])
            fx.token_file.write_text(f"REMOTE_TASK_TOKEN={ENV_TOKEN}\n")
            fx.token_file.chmod(0o600)
            fx.env["REMOTE_TASK_TOKEN_FILE"] = str(fx.token_file)
            path = json.dumps(str(fx.token_file))
            script = BRIDGE.replace("print(json.dumps({", (
                "import contextlib, io, pathlib\n"
                f"pathlib.Path({path}).write_text("
                f"'REMOTE_TASK_TOKEN={FILE_TOKEN}\\nREMOTE_TASK_URL=http://[::1?token=zq9rot\\n')\n"
                "logged = io.StringIO()\n"
                "with contextlib.redirect_stdout(logged), contextlib.redirect_stderr(logged):\n"
                "    reloaded = m._reload_rotated_token()\n"
                'print(json.dumps({"reloaded": reloaded, "logged": logged.getvalue(), '))
            out, _err = self.bridge(fx, script)
            self.assertFalse(out["reloaded"], "a URL change is never a rotation")
            self.assertIn(f"DIFFERENT gateway ({owner.contract.REDACTED_URL})", out["logged"])
            self.assertNotIn("zq9rot", out["logged"], "an unparseable URL fails closed, never the raw input")

    def hatch_diff(self, fx, installed: str, name: str = "ag2-sparrow.service",
                   ours: str = "[Service]\nExecStart=/y\n") -> str:
        unit = fx.root / name
        unit.write_text(installed)
        out = io.StringIO()
        env = hermetic_env(fx.root, **fx.env)
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

    def test_the_real_hatch_diff_judges_every_complete_key(self):
        for name, sep, shaped in name_sweep():
            old = f'Note={{"{name}":"synth-only-value"}}\nRestartSec=10\n'
            with self.subTest(key=name), mock.patch.object(hatch_cli, "_masking_registry",
                                                           side_effect=lambda: contract.SecretRegistry()):
                shown = hatch_cli._diff(old, "RestartSec=10\n", Path("unit"))
                self.assertEqual("synth-only-value" not in shown, shaped, shown)
                self.assertIn(" RestartSec=10\n", shown)
        for name, sep, shaped in name_sweep():
            for line in unquoted_name_lines(name, sep):
                with self.subTest(line=line), mock.patch.object(hatch_cli, "_masking_registry",
                                                                side_effect=lambda: contract.SecretRegistry()):
                    shown = hatch_cli._diff(f"{line}\nRestartSec=10\n", "RestartSec=10\n", Path("unit"))
                    self.assertEqual("synth-only-value" not in shown, shaped, shown)
        for line in ('{"pa/ss":"synth-only-value"}', "pa ss: synth-only-value", 'run --"pa ss" synth-only-value',
                     'run "--pa ss" synth-only-value', 'run --pa" "ss synth-only-value'):
            with self.subTest(line=line), mock.patch.object(hatch_cli, "_masking_registry",
                                                            side_effect=lambda: contract.SecretRegistry()):
                shown = hatch_cli._diff(line + "\n", "", Path("unit"))
            self.assertEqual(shown.splitlines()[2:], ["@@ -1 +0,0 @@", f"-{contract.WITHHELD_TEXT}"])

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
        env = hermetic_env(fx.root, **fx.env)
        ok = subprocess.CompletedProcess([], 0, "", "")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(hatch_cli.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(service_manager, "_run", return_value=ok), \
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
                    self.assertEqual(shown.splitlines().count(" Environment=<withheld>"), 5,
                                     "a line per old one, in context")
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
                env = hermetic_env(fx.root, **fx.env)
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
                        mock.patch.object(service_manager, "_run", return_value=ok), \
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

    def test_no_harness_reads_a_credential_path_the_outer_environment_names(self):
        with tempfile.TemporaryDirectory() as outer_td, tempfile.TemporaryDirectory() as td:
            outer = Path(outer_td)
            sentinels = (outer / "token", outer / "relay.env", outer / "claude" / "channels" / "sentinel-dir" / ".env")
            for path in sentinels:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("REMOTE_TASK_TOKEN=synth-outer-sentinel-5698\n")
            outer_env = {"REMOTE_TASK_TOKEN_FILE": str(sentinels[0]), "AG2_DEVICE_ENV": str(sentinels[1]),
                         "CLAUDE_CONFIG_DIR": str(outer / "claude"), "REMOTE_TASK_CHANNEL_DIR": "sentinel-dir"}
            opened, real_read, runs = [], gateway_credential_io.read_file, []
            real_run = subprocess.run
            with mock.patch.dict(os.environ, outer_env), \
                    mock.patch.object(gateway_credential_io, "read_file",
                                      side_effect=lambda path: opened.append(str(path)) or real_read(path)), \
                    mock.patch.object(subprocess, "run",
                                      side_effect=lambda cmd, **kw: runs.append(kw.get("env")) or real_run(cmd, **kw)):
                root = Path(td)
                self.hatch_diff(self.bare(root), "[Service]\nExecStart=/x\n")
                self.install_service(self.bare(root), "[Service]\nExecStart=/x\n")
                self.bridge(Fixture(root, ROWS[3]))
            self.assertTrue(opened, "the harnesses reached the readers")
            for path in sentinels:
                self.assertNotIn(str(path), opened)
            for env in runs:
                self.assertFalse(set(outer_env.values()) & set((env or {}).values()), "the bridge child's env")

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
        if path.is_file():
            path.chmod(0o600)  # refused for its content, not its mode

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
                # A refused token file fails closed: no tier stands in for it.
                want = ("", "none") if where == "REMOTE_TASK_TOKEN_FILE" else (ENV_TOKEN, "env")
                self.assertEqual((out["token"], out["source"]), want)


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

    def test_a_private_read_refuses_a_symlink_a_shared_mode_and_another_owner(self):
        for name, copy in self.COPIES.items():
            with self.subTest(copy=name), tempfile.TemporaryDirectory() as td:
                real = Path(td) / "token"
                real.write_text("https://gw.example/relay|s-1\n")
                real.chmod(0o600)
                self.assertEqual(copy.read_private(str(real)), ("https://gw.example/relay|s-1\n", ""))
                link = Path(td) / "link"
                link.symlink_to(real)
                self.assertIn("a symlink", copy.read_private(str(link))[1])
                self.assertEqual(copy.read_regular(str(link))[1], "", "only the private read refuses a link")
                real.chmod(0o644)
                self.assertEqual(copy.read_private(str(real)), (None, "mode 0644; a private credential file must be 0600"))
                real.chmod(0o600)
                with mock.patch.object(copy.os, "geteuid", return_value=os.geteuid() + 1):
                    self.assertIn("not this user", copy.read_private(str(real))[1])
                self.assertEqual(copy.read_private(str(Path(td) / "absent")), (None, ""))

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

    def test_a_dir_relative_no_follow_read_refuses_a_symlink(self):
        for name, m in self.COPIES.items():
            with self.subTest(copy=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "ok").write_text("{}")
                (root / "link").symlink_to(root / "ok")
                dfd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
                self.addCleanup(os.close, dfd)
                self.assertEqual(m.read_regular("ok", dir_fd=dfd, follow=False), ("{}", ""))
                self.assertEqual(m.read_regular("link", dir_fd=dfd, follow=False), (None, "a symlink"))
                self.assertEqual(m.read_regular("link", dir_fd=dfd), ("{}", ""), "followed by default")
                self.assertEqual(m.read_regular("missing", dir_fd=dfd, follow=False), (None, ""))
                os.chmod(root / "ok", 0)
                text, why = m.read_regular("ok", dir_fd=dfd, follow=False)
                os.chmod(root / "ok", 0o600)
                if os.geteuid() != 0:
                    self.assertEqual((text, why), (None, "unreadable (EACCES)"))
                self.assertTrue(m.unreadable("unreadable (EACCES)"))
                self.assertFalse(m.unreadable("not a regular file"))
                self.assertFalse(m.unreadable("a symlink"))


SOURCE_KEYS = set(owner.CREDENTIAL_KEYS) | {owner.TOKEN_FILE_KEY}
KEY_TUPLES = {"TOKEN_KEYS", "URL_KEYS", "CREDENTIAL_KEYS", "TOKEN_FILE_KEY", "MEDIA_TOKEN_KEY"}
# Only the owner builds, writes and reads the bridge record: outside it, its private names, its key
# tuples and the record's token key are never named, as an identifier or as a string.
OWNER_ONLY = frozenset({"token_env_name", "_bridge_declaration_text", "_RECORD_TOKEN_ENV"})
RECORD_KEY = "token_env"
# The owner's accessors for a supervisor that reads a declaration: only the one reader may name them.
READER_ONLY = frozenset({"bridge_declaration_fields", "bridge_token_environ", "bridge_strip_keys", "bridge_carried_config"})
# Outside the package, the one module that reads a bridge declaration, held to the owner-only rule.
RECORD_READERS = {"sparrow_agent_bridge.py": REPO / "src" / "sparrow_agent_bridge.py"}
# What names a credential file or store: a scope holding one of these and a read is credential I/O.
CREDENTIAL_MARKERS = {"AG2_DEVICE_ENV", "CLAUDE_CONFIG_DIR", "TOKEN_FILE", "device_env_path",
                      "channel_env_paths", "env_files", "channel_token", "token_from_vault"}
READS = {"open", "read_text", "read_bytes", "readlines"}
# The bridge declaration's file: a path built from these is opaque outside the owner. It may be
# formatted into a message or handed to one of DECLARATION_OWNER_CALLS, and nothing else.
DECLARATION_MARKERS = {"DECLARATION", "worker.json"}
DECLARATION_OWNER_CALLS = {"write_bridge_declaration", "remove_bridge_declaration"}
OWNER_MODULE = "gateway_credential_sources"
# The owner's entry points: reachable only as `<alias>.<name>(...)` on the module's one import of it.
OWNER_CALLS = frozenset(DECLARATION_OWNER_CALLS | READER_ONLY)
# Owner calls whose callable arguments read credentials: (positional index, keyword names).
OWNER_READERS = {"env_files": (1, ("read_text",)), "bridge_carried_config": (1, ("read_text",)),
                 "discover": (None, ("read_token_file", "vault_lookup"))}
# Where credential I/O may happen: the adapters' edge, as (module, function).
# The shared systemd reachability probe discards what show-environment prints.
IO_ALLOWED = {("remote_gateway_bridge.py", "_read_env_file"), ("remote_gateway_bridge.py", "_read_text"),
              ("remote_gateway_bridge.py", "_read_token_file_text"),
              ("remote_gateway_bridge.py", "_vault_lookup"), ("service_manager.py", "systemd_user_available"),
              ("gateway_credential_io.py", "read_file"), ("gateway_credential_io.py", "vault_lookup"),
              ("gateway_credential_io.py", "_vault"), ("gateway_credential_io.py", "_vault_once")}
# A command whose output is a credential store: the Keychain. The systemd user manager's environment
# is SYSTEMD_PROBE's closed-world rule below, which evaluates no constant.
STORE_READS = ("security", "find-generic-password")
SHOW_ENV = "show-environment"
SYSTEMD_PROBE = ("service_manager.py", "systemd_user_available")
# The one place systemctl is named and run; every other use calls it with a literal tuple.
SYSTEMCTL_HELPER = ("service_manager.py", "_systemctl")
# Keywords a helper call may pass besides the literal tuple: the unit, which the helper checks
# against its UNIT_RE at runtime, and the bound.
HELPER_KEYWORDS = ("unit", "timeout")
SYSTEMD_RUNNERS = frozenset({SYSTEMCTL_HELPER[1], "_systemd_run"})


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
    """A literal string node: the call-shape checks require one; detection reads _folded instead."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _folded(node, parents) -> "str | None":
    """_constant(node), except where the parent folds whole (that parent is read instead)."""
    parent = parents.get(node)
    if isinstance(parent, (ast.BinOp, ast.JoinedStr, ast.FormattedValue)) and _constant(parent) is not None:
        return None
    return _constant(node)


def _literal_list(node) -> bool:
    return isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts) and all(_str(e) for e in node.elts)


def _callee(call) -> "str | None":
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _systemctl_literal(node, parents) -> bool:
    text = _folded(node, parents)
    return text is not None and os.path.basename(text.strip()) == "systemctl"


def _literal_args(call) -> bool:
    """The helper's call shape: one literal tuple of literal strings, then at most locate=True and
    the HELPER_KEYWORDS, each named."""
    return len(call.args) == 1 and isinstance(call.args[0], ast.Tuple) and all(_str(e) for e in call.args[0].elts) \
        and all(k.arg in HELPER_KEYWORDS or (k.arg == "locate" and isinstance(k.value, ast.Constant)
                                              and k.value.value is True) for k in call.keywords)


def _systemd_findings(tree, parents, rel: str) -> "list[str]":
    """Closed world, no dataflow: "systemctl" as a string (folded by _constant) anywhere
    but inside SYSTEMCTL_HELPER, the one top-level def of that name in its module; every use of a
    SYSTEMD_RUNNERS name a direct call by that name (the helper's with a literal tuple, _systemd_run's
    on a literal program), never imported under another name, rebound, redefined, or named in a
    string; and show-environment in any string (folded, template parts included) outside SYSTEMD_PROBE."""
    tops = [f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == SYSTEMCTL_HELPER[1]]
    canonical = {id(tops[0])} if rel == SYSTEMCTL_HELPER[0] and len(tops) == 1 else set()
    canonical |= {id(f) for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "_systemd_run"
                  and rel == SYSTEMCTL_HELPER[0]}
    helper = {id(n) for f in tree.body if id(f) in canonical and f.name == SYSTEMCTL_HELPER[1] for n in ast.walk(f)}
    out = []
    for n in ast.walk(tree):
        scope = parents.get(n)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents.get(scope)
        where = f"{rel}:{getattr(n, 'lineno', getattr(parents.get(n), 'lineno', '?'))}"
        if SHOW_ENV in (_folded(n, parents) or "") and (rel, getattr(scope, "name", None)) != SYSTEMD_PROBE:
            out.append(f"{where} {SHOW_ENV} outside the reachability probe")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in SYSTEMD_RUNNERS \
                and id(n) not in canonical:
            out.append(f"{where} a second definition of {n.name}")
        named = getattr(n, "name", None) if isinstance(n, ast.alias) else getattr(n, "arg", None) \
            if isinstance(n, (ast.arg, ast.keyword)) else None
        if isinstance(n, ast.alias) and (n.asname or n.name) != n.name and SYSTEMD_RUNNERS & {n.name, n.asname}:
            out.append(f"{where} {n.name} imported as {n.asname}")
        elif isinstance(n, (ast.arg, ast.keyword)) and named in SYSTEMD_RUNNERS:
            out.append(f"{where} {named} rebound as a parameter or keyword")
        text = _constant(n)
        if text is not None and any(w in text for w in SYSTEMD_RUNNERS):
            out.append(f"{where} {sorted(w for w in SYSTEMD_RUNNERS if w in text)[0]} named in a string")
        if id(n) in helper:
            continue
        if _systemctl_literal(n, parents):
            out.append(f"{where} a systemctl literal outside {SYSTEMCTL_HELPER[1]}")
        word = getattr(n, "id", None) if isinstance(n, ast.Name) else getattr(n, "attr", None)
        if word not in SYSTEMD_RUNNERS or not isinstance(n, (ast.Name, ast.Attribute)):
            continue
        call = parents.get(n)
        if not (isinstance(call, ast.Call) and call.func is n):
            out.append(f"{where} {word} used other than as a direct call")
        elif word == SYSTEMCTL_HELPER[1] and not _literal_args(call):
            out.append(f"{where} a {word} call whose arguments are not one literal tuple")
        elif word == "_systemd_run":
            arg = call.args[0] if len(call.args) == 1 and not call.keywords else None
            if not (isinstance(arg, (ast.List, ast.Tuple)) and arg.elts and _str(arg.elts[0])):
                out.append(f"{where} a _systemd_run call not on a literal program")
    return out


def _memo(scan):
    """Each scan is a pure function of (source, rel); tests repeat whole-package scans, so each module
    is scanned once per run. Callers get a fresh list."""
    cached = functools.lru_cache(maxsize=None)(lambda *args, **kw: tuple(scan(*args, **kw)))
    return functools.wraps(scan)(lambda *args, **kw: list(cached(*args, **kw)))


@_memo
def systemd_findings(source: str, rel: str) -> "list[str]":
    tree = ast.parse(source)
    return _systemd_findings(tree, {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}, rel)


@functools.lru_cache(maxsize=None)
def _named_defs(text: str) -> "tuple[tuple[str, bool], ...]":
    """(name, at the top level) for every def and class in a module's text."""
    tree = ast.parse(text)
    return tuple((n.name, n in tree.body) for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))


def package_systemd_findings(root: Path) -> "list[str]":
    """The systemd rule over every module under `root`, the credential owners included, and each
    runner's identity: exactly one def of that name in the package, top-level in the helper's module."""
    found, defs = [], {name: [] for name in sorted(SYSTEMD_RUNNERS)}
    for path in sorted(root.rglob("*.py")):
        rel, text = path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        found += systemd_findings(text, rel)
        for name, top in _named_defs(text):
            if name in defs:
                defs[name].append((rel, top))
    found += [f"{name} is defined at {where}, not once at the top level of {SYSTEMCTL_HELPER[0]}"
              for name, where in defs.items() if where != [(SYSTEMCTL_HELPER[0], True)]]
    return found


def _probe_keeps(inner, parents) -> "list[str]":
    """Why a scope that runs show-environment keeps its output: the run's result must be passed
    straight to _ok, a bool, and the scope may not read stdout/stderr, print, write or store."""
    probe = [n for n in inner if SHOW_ENV in (_folded(n, parents) or "")]
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


def _docstrings(tree) -> "set[int]":
    return {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}


def _checked_nodes(tree, parents):
    """(node, its folded string or None) for every node not inside a docstring or a folded string."""
    docstrings = _docstrings(tree)
    for node in ast.walk(tree):
        parent = parents.get(node)
        if id(node) in docstrings or (isinstance(parent, (ast.BinOp, ast.JoinedStr, ast.FormattedValue))
                                      and _constant(parent) is not None):
            continue  # the folded parent is checked whole
        yield node, _folded(node, parents)


def _binds(tree, name: str) -> int:
    """How many places in the module bind `name`: assignment targets, parameters, imports, defs,
    except-as, global or nonlocal declarations."""
    count = 0
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load):
            count += 1
        elif isinstance(n, ast.arg) and n.arg == name:
            count += 1
        elif isinstance(n, ast.alias) and (n.asname or n.name.split(".")[0]) == name:
            count += 1
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            count += 1
        elif isinstance(n, ast.ExceptHandler) and n.name == name:
            count += 1
        elif isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            count += 1
    return count


def _owner_call_node(call, alias) -> bool:
    """`call` is `<alias>.<declaration entry point>(...)` on the owner module's import."""
    return alias is not None and isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
        and call.func.attr in DECLARATION_OWNER_CALLS and isinstance(call.func.value, ast.Name) \
        and call.func.value.id == alias


def _owner_call(node, parents, alias) -> bool:
    """`node` is the function of a call `<alias>.<entry point>(...)` on the owner module's import."""
    call = parents.get(node)
    return alias is not None and isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
        and node.value.id == alias and isinstance(call, ast.Call) and call.func is node


def owner_alias(tree) -> "str | None":
    """The name the module's one top-level import of the owner module binds, if it binds it once and
    nothing rebinds it; None otherwise (then no call reaches the owner's entry points)."""
    imports = [(n, a) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names
               if a.name.rsplit(".", 1)[-1] == OWNER_MODULE and a.asname
               and (isinstance(n, ast.Import) or n.module in (None, "ag2_sparrow"))]
    if len(imports) != 1:
        return None
    alias = imports[0][1].asname
    return alias if _binds(tree, alias) == 1 else None


@_memo
def owner_only_findings(source: str, rel: str, reader: bool = False) -> "list[str]":
    """Every name the owner keeps to itself that `source` names: a key tuple, a private name or the
    record's token key, as an identifier, an import, an attribute or a string (a getattr argument);
    and an owner entry point reached other than as a direct call on the owner module's one import."""
    tree = ast.parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    alias = owner_alias(tree)
    out = []
    for node, text in _checked_nodes(tree, parents):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        if name in KEY_TUPLES or text in KEY_TUPLES:
            out.append(f"{rel}:{node.lineno} owner key tuple {name or text}")
        if name in OWNER_ONLY or text in OWNER_ONLY:
            out.append(f"{rel}:{node.lineno} {name or text} named outside the owner")
        if not reader and (name in READER_ONLY or text in READER_ONLY):
            out.append(f"{rel}:{node.lineno} {name or text} named outside the declaration's reader")
        if (name in OWNER_CALLS or text in OWNER_CALLS) and not _owner_call(node, parents, alias):
            out.append(f"{rel}:{node.lineno} {name or text} reached other than as a call on the owner module")
        if text == RECORD_KEY:
            out.append(f"{rel}:{node.lineno} the record key {RECORD_KEY!r} named outside the owner")
    return out


def _scope_nodes(root):
    """The nodes of one scope: `root` and what it holds, not the bodies of nested defs or classes."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    stack, out = [root], []
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(c for c in ast.iter_child_nodes(n) if not isinstance(c, nested))
    return out


@_memo
def declaration_reads(source: str, rel: str) -> "list[str]":
    """Every call, in `source`, that takes a path to the declaration (as an argument or as the object of
    a method: open, read_text, a helper, str()) other than an owner call on the owner module's import.
    A path is built from DECLARATION or a string folding to the file name, or comes from a name, module
    global, function or lambda that yields one. A message about the path (an f-string, a name bound from
    one, a function returning only those) is exempt only as the argument itself; any other use is a path."""
    tree = ast.parse(source)
    alias, out = owner_alias(tree), []
    names = {m for m in DECLARATION_MARKERS if "." not in m}
    files = {m for m in DECLARATION_MARKERS if "." in m}
    funcs = [f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
    module = [n for root in tree.body if not isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              for n in _scope_nodes(root)]
    producers, messages = set(), set()

    def marked(expr, bound):
        """Whether `expr` reaches the path at all: pure reachability, opaque only at an owner call."""
        stack = [expr]
        while stack:
            n = stack.pop()
            if any(m in (_constant(n) or "") for m in files):
                return True
            if _owner_call_node(n, alias):
                continue  # what the owner returns is not the path
            word = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
            if word in names | producers | messages or (isinstance(n, ast.Name) and n.id in bound):
                return True
            stack.extend(ast.iter_child_nodes(n))
        return False

    def message(expr, said, bound):
        """`expr` itself is a message or a truth value: an f-string, a name bound from one, a call to a
        function whose every path-reaching return is one, a `not` or a comparison, or an `and`/`or`/`if`
        whose path-reaching parts all are."""
        word = getattr(getattr(expr, "func", None), "id", None) or getattr(getattr(expr, "func", None), "attr", None)
        if isinstance(expr, ast.BoolOp):
            return all(message(v, said, bound) for v in expr.values if marked(v, bound))
        if isinstance(expr, ast.IfExp):
            return all(message(v, said, bound) for v in (expr.body, expr.orelse) if marked(v, bound))
        return isinstance(expr, (ast.JoinedStr, ast.Compare)) or (isinstance(expr, ast.Name) and expr.id in said) \
            or (isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not)) \
            or (isinstance(expr, ast.Call) and word in messages - producers)

    def taint(inner, seed):
        tainted, said = set(seed[0]), set(seed[1])
        for _ in range(4):
            before = (len(tainted), len(said))
            for n in inner:
                bound = tainted | said
                if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign)) and n.value is not None \
                        and marked(n.value, bound):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    hit = {t.id for tg in targets for t in ast.walk(tg) if isinstance(t, ast.Name)}
                    if message(n.value, said, bound) and not isinstance(n, ast.AugAssign):
                        said |= hit - tainted
                    else:
                        tainted |= hit
                        said -= hit
                elif isinstance(n, ast.withitem) and n.optional_vars is not None and marked(n.context_expr, bound):
                    tainted |= {t.id for t in ast.walk(n.optional_vars) if isinstance(t, ast.Name)}
                elif isinstance(n, (ast.For, ast.comprehension)) and marked(n.iter, bound):
                    tainted |= {t.id for t in ast.walk(n.target) if isinstance(t, ast.Name)}
            if (len(tainted), len(said)) == before:
                break
        return tainted, said

    scopes = []
    for _ in range(4):  # producers, messages and module globals, to a fixpoint
        known = (len(producers), len(messages))
        glob = taint(module, (set(), set()))
        scopes = [(module, glob)]
        for f in funcs:
            inner = [n for c in ast.iter_child_nodes(f) for n in _scope_nodes(c)]
            tainted, said = taint(inner, glob)
            scopes.append((inner, (tainted, said)))
            returns = [f.body] if isinstance(f, ast.Lambda) else [n.value for n in inner if isinstance(n, ast.Return) and n.value]
            reached = [v for v in returns if marked(v, tainted | said)]
            if not isinstance(f, ast.Lambda) and reached:
                (messages if all(message(v, said, tainted | said) for v in reached) else producers).add(f.name)
        if (len(producers), len(messages)) == known:
            break
    for inner, (tainted, said) in scopes:
        for n in inner:
            if not isinstance(n, ast.Call) or _owner_call_node(n, alias):
                continue
            taken = n.args + [k.value for k in n.keywords] + ([n.func.value] if isinstance(n.func, ast.Attribute) else [])
            receiver = [n.func.value] if isinstance(n.func, ast.Attribute) else []
            if any(marked(a, tainted | said) and (a in receiver or not message(a, said, tainted | said)) for a in taken):
                out.append(f"{rel}:{n.lineno} hands the bridge declaration's path to {ast.unparse(n.func)}")
    return sorted(set(out))


# Closed world for the declaration's reader: bridge_environ is exactly this (its docstring aside), and
# these are the module's only mapping writes. A change to either is a reviewed edit of this test.
READER_ENVIRON = """
strip = _sources.bridge_strip_keys() + _sources.BRIDGE_CARRIED_CONFIG
host = os.environ if base is None else base
env = {k: v for k, v in host.items() if k not in HOST_ONLY and k not in strip}
env.update(decl["env"], REMOTE_PROACTIVE_ROOM="")
if is_seat(decl):
    return env
token_env = _sources.bridge_token_environ(token, url)
env.update(_sources.bridge_carried_config(host, read_regular))
env.update(token_env)
return env
"""
READER_WRITES = {("bridge_environ", "env.update(decl['env'], REMOTE_PROACTIVE_ROOM='')"),
                 ("bridge_environ", "env.update(_sources.bridge_carried_config(host, read_regular))"),
                 ("bridge_environ", "env.update(token_env)"),
                 ("main", "os.environ.clear()"), ("main", "os.environ.update(environ)")}
MAPPING_WRITES = {"update", "setdefault", "__setitem__", "__delitem__", "pop", "popitem", "clear", "putenv", "unsetenv",
                  "__ior__", "__or__", "__ror__"}
# Every reach of a process environment in the reader, by (function, the expression holding it).
READER_ENVIRON_USES = {("bridge_environ", "os.environ if base is None else base"),
                       ("main", "os.environ.clear"), ("main", "os.environ.update")}


def token_injection_findings(source: str, rel: str) -> "list[str]":
    """The reader hands the bridge its token only through the owner: bridge_environ is READER_ENVIRON,
    the module's mapping writes are READER_WRITES, main's environment is bridge_environ's result, and
    no string in the module is one of the owner's token variable names."""
    tree = ast.parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    alias = owner_alias(tree)
    out = [f"{rel}:{n.lineno} names the token variable {_constant(n)!r}" for n in ast.walk(tree)
           if isinstance(_constant(n), str) and any(_constant(n) == k or (len(_constant(n)) >= 8 and _constant(n) in k)
                                                    for k in owner.TOKEN_KEYS)]
    fn = [f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "bridge_environ"]
    body = fn[0].body if fn else []
    if body and isinstance(body[0], ast.Expr) and isinstance(_constant(body[0].value), str):
        body = body[1:]
    if len(fn) != 1 or [ast.dump(n) for n in body] != [ast.dump(n) for n in ast.parse(READER_ENVIRON).body]:
        out.append(f"{rel}: bridge_environ is not the pinned owner merge")

    def scope(n):
        while n is not None and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = parents.get(n)
        return getattr(n, "name", "<module>")
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and not isinstance(n.ctx, ast.Load):
            out.append(f"{rel}:{n.lineno} a mapping item written directly ({ast.unparse(n)})")
        elif isinstance(n, ast.Call) and getattr(n.func, "attr", None) in MAPPING_WRITES \
                and (scope(n), ast.unparse(n)) not in READER_WRITES:
            out.append(f"{rel}:{n.lineno} a mapping write outside the pinned ones ({ast.unparse(n)})")
        elif isinstance(n, ast.AugAssign) or (isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)):
            out.append(f"{rel}:{n.lineno} an in-place or | merge ({ast.unparse(n)})")
        elif isinstance(n, ast.Attribute) and n.attr == "environ" \
                and (scope(n), ast.unparse(parents[n])) not in READER_ENVIRON_USES:
            out.append(f"{rel}:{n.lineno} the process environment reached outside the pinned uses ({ast.unparse(parents[n])})")
        elif (isinstance(n, ast.alias) and n.name == "environ") or _constant(n) == "environ":
            out.append(f"{rel}:{getattr(n, 'lineno', '?')} the process environment named another way")
        elif isinstance(n, ast.Dict) and any(k is None and ast.unparse(v) != f"{alias}.bridge_declaration_fields()"
                                             for k, v in zip(n.keys, n.values)):
            out.append(f"{rel}:{n.lineno} a ** merge of something other than the owner's fields")
    main = [f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "main"]
    binds = [n for f in main for n in ast.walk(f) if isinstance(n, ast.Name) and n.id == "environ"
             and not isinstance(n.ctx, ast.Load)]
    if len(binds) != 1 or ast.unparse(parents[binds[0]].value) != "bridge_environ(decl, token, url=url)":
        out.append(f"{rel}: main's environment is not bridge_environ(decl, token, url=url) alone")
    return out


@_memo
def credential_access(source: str, rel: str) -> "list[str]":
    """Every way `source` (module `rel`) touches a credential source on its own: a credential key
    or a piece of one as a string, the owner's key tuples, credential I/O outside IO_ALLOWED, and
    a function handed to the owner as a reader that is not in IO_ALLOWED."""
    tree = ast.parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    out = owner_only_findings(source, rel) + declaration_reads(source, rel)
    for node, text in _checked_nodes(tree, parents):
        if text is not None and any(text == k or (len(text) >= 8 and text in k) for k in SOURCE_KEYS):
            out.append(f"{rel}:{node.lineno} key {text!r}")
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
                text = _constant(n)
                if text is not None and (".env" in text or text in CREDENTIAL_MARKERS):
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


# The masking contract has one implementation, the owner's. Elsewhere in the package a function by one of
# its names may only delegate to it; no function may repeat one of its bodies under another name.
_OWNER_TREE = ast.parse((REPO / "shared" / "ag2_gateway_credentials.py").read_text(encoding="utf-8"))
_MASKING_START = next(n.lineno for n in _OWNER_TREE.body if isinstance(n, ast.Assign)
                      and any(getattr(t, "id", "") == "WITHHELD" for t in n.targets))
_REGISTRY_CLASS = next(n for n in _OWNER_TREE.body if isinstance(n, ast.ClassDef) and n.name == "SecretRegistry")
MASKING_NAMES = ({n.name.lstrip("_") for n in _OWNER_TREE.body
                  if isinstance(n, ast.FunctionDef) and n.lineno > _MASKING_START}
                 | {n.name.lstrip("_") for n in _REGISTRY_CLASS.body if isinstance(n, ast.FunctionDef)
                    and not n.name.startswith("__") and n.name not in ("register", "contains")})
DELEGATES_TO = ("gateway_credentials", "REGISTRY", "contract")
UNRELATED_NAMES = {("adapters/claude_code/hmac_policy.py", "describe")}  # an HMAC policy's mode line
REGISTRY_HOLDER = "diagnostics.py"


def _body(fn) -> list:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def _dump(stmts) -> str:
    return ast.dump(ast.Module(body=stmts, type_ignores=[]), annotate_fields=False)


_OWNER_BODIES = {_dump(_body(f)) for f in ast.walk(_OWNER_TREE) if isinstance(f, ast.FunctionDef)
                 and (len(_body(f)) > 1 or len(_dump(_body(f))) > 160)}


def _delegates(fn) -> bool:
    body = _body(fn)
    if len(body) != 1 or not isinstance(body[0], ast.Return) or not isinstance(body[0].value, ast.Call):
        return False
    root = body[0].value.func
    while isinstance(root, ast.Attribute):
        root = root.value
    return isinstance(body[0].value.func, ast.Attribute) and isinstance(root, ast.Name) and root.id in DELEGATES_TO


def masking_copies(source: str, rel: str) -> "list[str]":
    """Each place `source` (package path `rel`) re-implements the owner's masking contract."""
    out = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.name.lstrip("_") in MASKING_NAMES and (rel, n.name) not in UNRELATED_NAMES and not _delegates(n):
                out.append(f"{rel}:{n.lineno}: {n.name} re-implements the owner's {n.name.lstrip('_')}")
            elif _dump(_body(n)) in _OWNER_BODIES:
                out.append(f"{rel}:{n.lineno}: {n.name} repeats an owner function's body")
        elif isinstance(n, ast.ClassDef) and any(ast.unparse(b).endswith("SecretRegistry") for b in n.bases):
            out.append(f"{rel}:{n.lineno}: {n.name} subclasses the owner's registry")
    module_registries = [n for n in ast.parse(source).body if isinstance(n, (ast.Assign, ast.AnnAssign))
                         and n.value is not None and "SecretRegistry(" in ast.unparse(n.value)]
    if rel != REGISTRY_HOLDER:
        out += [f"{rel}:{n.lineno}: a second process-wide registry" for n in module_registries]
    return out


class Guard(unittest.TestCase):
    # The scan's only exclusions: the two owner modules, by exact path under the package.
    OWNERS = frozenset({"gateway_credential_sources.py", "gateway_credentials.py"})

    def package_findings(self, replace=None, extra=None):
        """The scan over every package module, with `replace` editing one's text and `extra` adding modules."""
        root, found = PKG / "ag2_sparrow", []
        modules = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*.py"))}
        for rel, text in {**modules, **(extra or {})}.items():
            if rel not in self.OWNERS:
                found += credential_access((replace or {}).get(rel, lambda t: t)(text), rel)
        return found

    def test_no_other_package_module_touches_a_credential_source(self):
        self.assertEqual(self.package_findings(), [], "a credential source is touched outside the owner and the adapters' edge")

    def test_the_exclusions_are_exactly_the_owner_modules(self):
        self.assertEqual({p for p in self.OWNERS if (PKG / "ag2_sparrow" / p).is_file()}, self.OWNERS, "no stale exclusion")
        twin = "adapters/claude_code/gateway_credential_sources.py"
        found = self.package_findings(extra={twin: "x = _sources.token_env_name()\n"})
        self.assertEqual([f for f in found if f.startswith(twin + ":")],
                         [f"{twin}:1 token_env_name named outside the owner"], "a same-named module elsewhere is scanned")

    def test_the_package_scan_fires_on_one_reference_in_the_adapter(self):
        rel = "adapters/claude_code/cli.py"
        for planted in ("\n_N = _sources.token_env_name()\n", "\n_N = _sources.TOKEN_KEYS[0]\n",
                        '\n_N = {"token_env": None}\n'):
            found = self.package_findings({rel: lambda t, planted=planted: t + planted})
            self.assertTrue(found and all(f.startswith(rel + ":") for f in found), (planted, found))

    def test_the_package_has_one_masking_implementation(self):
        found = []
        for path in sorted((PKG / "ag2_sparrow").rglob("*.py")):
            if path.name not in self.OWNERS:
                rel = str(path.relative_to(PKG / "ag2_sparrow"))
                found += masking_copies(path.read_text(encoding="utf-8"), rel)
        self.assertEqual(found, [], "the masking contract is gateway_credentials'; call it, never copy it")

    def test_the_masking_detector_catches_a_copy(self):
        import inspect
        renamed = inspect.getsource(contract._split_by_hand).replace("def _split_by_hand", "def _hand_split")
        copies = ['def fold(text):\n    return str(text).casefold()\n',
                  'def _views(text):\n    return {text}\n',
                  'def describe(exc):\n    return type(exc).__name__\n',
                  'def redact_text(text, url):\n    return gateway_credentials.redact_url(text) or text\n',
                  'def scrub(text):\n    return "<withheld>" if "x" in text else text\n',
                  renamed,
                  'class SinkRegistry(gateway_credentials.SecretRegistry):\n    pass\n',
                  '_SECRETS = gateway_credentials.SecretRegistry()\n']
        for source in copies:
            self.assertTrue(masking_copies(source, "probe.py"), source)
        delegations = ('from . import gateway_credentials\nREGISTRY = gateway_credentials.SecretRegistry()\n'
                       'def describe(exc):\n    """Doc."""\n    return gateway_credentials.describe(exc, REGISTRY)\n'
                       'def scrub(text):\n    return REGISTRY.scrub(text)\n'
                       'redact_url = gateway_credentials.redact_url\n')
        self.assertEqual(masking_copies(delegations, REGISTRY_HOLDER), [])
        self.assertTrue(masking_copies(delegations, "gateway_instance.py"), "one registry, in one module")

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
            'body = {"token_env": _sources.TOKEN_KEYS[0]}',
            'from .gateway_credential_sources import TOKEN_KEYS\nbody = {"token_env": TOKEN_KEYS[0]}',
            'body = {"token_env": "REMOTE_TASK_TOKEN"}',
            'os.environ.get(_sources.token_env_name())',
            'os.environ[_sources.token_env_name()]',
            'os.getenv(token_env_name())',
            'k = _sources.token_env_name()\nos.environ.get(k)',
            'body = {"token_env": _sources.token_env_name}',
            'name = _sources.token_env_name\nos.environ.get(name())',
            'body = {_sources.token_env_name(): 1}',
            'body = {"token_env": _sources.token_env_name(os.environ)}',
            'body = {"token_env": os.environ.get(_sources.token_env_name())}',
            'def token_env_name():\n    return "TOKEN"',
            'def f():\n    p = "relay" + ".en" + "v"\n    open(p).read()',
            'def f():\n    open(f"relay{\'.env\'}").read()',
            'def f():\n    subprocess.run(["system" + "ctl", "--user", "show-" + "environment"], capture_output=True).stdout',
            'def f():\n    subprocess.run([f"{\'system\'}ctl", "--user", "show-environ" + "ment"]).stdout',
            'def f():\n    subprocess.run(["{}ctl".format("system"), "status"])',
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
        self.assertEqual(credential_access(
            'from ... import gateway_credential_sources as _sources\n'
            'def wire_bridge(cfg):\n    _sources.write_bridge_declaration(path, name=n, env=env)\n',
            "adapters/claude_code/cli.py"), [], "the owner builds the record; the adapter writes its text")

    def assert_flagged(self, *sources):
        for source in sources:
            self.assertTrue(credential_access(source, "adapters/claude_code/cli.py"), source)
            self.assertTrue(credential_access(source, "probe.py"), source)

    def test_the_accessor_as_a_keyword_value_is_flagged(self):
        self.assert_flagged('os.getenv(key=_sources.token_env_name())',
                            'def wire_bridge(cfg):\n    return os.getenv(key=_sources.token_env_name())',
                            'record(path, token_env=_sources.token_env_name())')

    def test_the_accessor_as_a_value_of_a_dict_that_reads_is_flagged(self):
        self.assert_flagged('os.environ.get({"token_env": _sources.token_env_name()}["token_env"])',
                            'def wire_bridge(cfg):\n    return os.environ[{"k": _sources.token_env_name()}["k"]]',
                            'body = {"token_env": _sources.token_env_name()}')

    def test_the_accessor_under_an_alias_is_flagged(self):
        self.assert_flagged('from .gateway_credential_sources import token_env_name as key_name\nos.getenv(key_name())',
                            'from .gateway_credential_sources import token_env_name as key_name\n'
                            'def wire_bridge(cfg):\n    body = {"token_env": key_name()}',
                            'te = _sources.token_env_name\nos.getenv(te())')

    def test_every_record_use_from_review_is_flagged_outside_the_owner(self):
        record = 'def wire_bridge(cfg):\n    body = {"token_env": _sources.token_env_name(), "name": "w"}\n'
        uses = ('    return json.dumps(body)\n', '    return os.getenv(body["token_env"])\n',
                '    return dict(body).popitem()[1]\n', '    d = {}\n    d.update(body)\n', '    return list(body)\n',
                '    return {**body}\n', '    return os.getenv(**body)\n', '    b2 = body\n')
        for use in uses:
            self.assert_flagged(record + use)
        for scope in ('def outer():\n    def wire_bridge(cfg):\n        return {"token_env": _sources.token_env_name()}\n',
                      'class C:\n    def wire_bridge(self):\n        return {"token_env": _sources.token_env_name()}\n',
                      'async def wire_bridge(cfg):\n    return {"token_env": _sources.token_env_name()}\n'):
            self.assert_flagged(scope)
        for name in ('from .gateway_credential_sources import token_env_name\n',
                     'from .gateway_credential_sources import TOKEN_KEYS\n',
                     'import json as j\nj.dumps(getattr(_sources, "token_env_name")())\n',
                     'def f(d):\n    return os.getenv(d["token_env"])\n',
                     'def f(d):\n    return os.getenv(d.get("token_" + "env"))\n'):
            self.assert_flagged(name)

    def test_the_cli_names_neither_the_accessor_nor_the_record_key(self):
        rel = "adapters/claude_code/cli.py"
        shipped = (PKG / "ag2_sparrow" / rel).read_text(encoding="utf-8")
        words = {getattr(n, "id", None) or getattr(n, "attr", None) or getattr(n, "name", None) or _constant(n)
                 for n in ast.walk(ast.parse(shipped))}
        self.assertEqual(words & (OWNER_ONLY | KEY_TUPLES | {RECORD_KEY}), set())
        self.assertEqual(credential_access(shipped, rel), [])

    def test_the_adapter_hands_the_owner_a_path_and_holds_no_record(self):
        text = (PKG / "ag2_sparrow" / "adapters" / "claude_code" / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        uses = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and ast.unparse(n.value) == "_sources"]
        self.assertEqual(sorted(n.attr for n in uses), ["remove_bridge_declaration", "seat_bridge_environ",
                                                        "write_bridge_declaration"],
                         "the owner checks, writes and removes the declaration and reads a seat; the adapter only "
                         "names its path")
        for use in uses:
            call = parents[use]
            self.assertTrue(isinstance(call, ast.Call) and call.func is use)
            self.assertEqual([k.arg for k in call.keywords if k.arg is None], [], "no ** spread into the owner")
        self.assertEqual(declaration_reads(text, "adapters/claude_code/cli.py"), [])
        self.assertNotIn("owned_declaration", text)

    def test_a_read_of_the_declaration_path_outside_the_owner_is_flagged(self):
        head = 'from ... import gateway_credential_sources as _sources\ndef f(root):\n    path = bridge_home(root) / DECLARATION\n'
        for read in ('    text = path.read_text()\n', '    text, why = cm.file_text(path, "declaration")\n',
                     '    with open(path) as fh:\n        json.load(fh)\n', '    p2 = path\n    p2.read_bytes()\n',
                     '    json.load(open(bridge_home(root) / "worker.json"))\n', '    owned_declaration(path)\n',
                     '    s = str(path)\n', '    helper(where=path)\n', '    path.unlink()\n'):
            self.assertTrue(declaration_reads(head + read, "adapters/claude_code/cli.py"), read)
            self.assertTrue(credential_access(head + read, "adapters/claude_code/cli.py"), read)
        for fine in ('    print(f"declared: {path}")\n', '    _sources.write_bridge_declaration(path, written_by="x")\n'):
            self.assertEqual(declaration_reads(head + fine, "adapters/claude_code/cli.py"), [], fine)

    def test_a_declaration_name_built_from_constants_is_the_declaration(self):
        self.assertEqual(declaration_reads('def f(base, path):\n    open(os.path.join(base, "x.json")).read()\n', "a.py"), [],
                         "the control: another file, beside a local named path, is not the declaration")
        for built in ('"worker.json"', '"worker" + ".json"', '"work" + "er.json"', 'f"{\'worker\'}.json"',
                      '"%s.json" % "worker"', '"{}.json".format("worker")'):
            for source in (f'def f(base):\n    name = {built}\n    open(os.path.join(base, name)).read()\n',
                           f'def f(base):\n    json.load(open(os.path.join(base, {built})))\n',
                           f'def f(base):\n    other = {built}\n    where = os.path.join(base, other)\n    where.read_text()\n'):
                self.assertTrue(declaration_reads(source, "adapters/claude_code/cli.py"), source)
                self.assertTrue(credential_access(source, "adapters/claude_code/cli.py"), source)

    def test_the_owner_writes_the_record_install_wrote_before(self):
        env = {"AGENT_CONNECT_TASK_DIR": "/w/a/tasks", "AGENT_MXID": "@a:ag2.space"}
        before = {"written_by": "x install", "schema": 1, "host": "sparrowd", "name": "bridge-a", "module": "m.bridge",
                  "env": env, "token_vault_key": "AG2_RELAY_TOKEN_A", "token_env": "REMOTE_TASK_TOKEN"}
        for label, o in OWNERS.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "a.bridge" / "worker.json"
                self.assertEqual("", o.write_bridge_declaration(path, written_by="x install", host="sparrowd", name="bridge-a",
                                                             module="m.bridge", env=env, token_vault_key="AG2_RELAY_TOKEN_A"))
                self.assertEqual(path.read_text(encoding="utf-8"), json.dumps(before, indent=1, sort_keys=False) + "\n")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["worker.json"], "no temp file left")
                self.assertEqual(o.bridge_declaration_fields(), {"token_env": "REMOTE_TASK_TOKEN"})
                self.assertEqual(o.bridge_token_environ("t"), {"REMOTE_TASK_TOKEN": "t", "GATEWAY_CREDENTIALS_INJECTED": "1"})
                self.assertEqual(o.BRIDGE_CARRIED_CONFIG, ("REMOTE_PROACTIVE_TRUST_OK",), "the one carried host setting")
                self.assertEqual(o.bridge_strip_keys(), ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "GATEWAY_TOKEN",
                                                         "RELAY_TOKEN", "REMOTE_MEDIA_HS_TOKEN", "REMOTE_TASK_URL",
                                                         "AG2_REMOTE_URL", "GATEWAY_URL", "RELAY_URL"),
                                 "every name a host's gateway token or endpoint may sit under: each token key, each "
                                 "contract alias, the media token and each URL name and alias, each once")
                self.assertEqual(set(o.bridge_strip_keys()),
                                 set(o.TOKEN_KEYS) | set(o.contract.TOKEN_ALIAS_PRECEDENCE) | {o.MEDIA_TOKEN_KEY}
                                 | set(o.URL_KEYS) | set(o.contract.URL_ALIAS_PRECEDENCE))
                self.assertTrue(set(o.bridge_token_environ("t")) - {o.INJECTED_KEY} <= set(o.bridge_strip_keys()),
                                "the name the agent's token goes under is stripped of the host's first")

    def test_the_agents_gateway_rides_beside_a_bare_token_only_and_the_wake_child_acts_as_the_agent(self):
        url, combined = "https://agent-gw.example/relay", "https://agent-gw.example/relay|s"
        for label, o in OWNERS.items():
            with self.subTest(label):
                self.assertEqual(o.bridge_token_environ("s", url),
                                 {"REMOTE_TASK_TOKEN": "s", o.INJECTED_KEY: "1", "REMOTE_TASK_URL": url})
                self.assertEqual(o.bridge_token_environ(combined, "https://other/relay"),
                                 {"REMOTE_TASK_TOKEN": combined, o.INJECTED_KEY: "1"}, "the token's own URL wins")
                self.assertEqual((o.token_embeds_url(combined), o.token_embeds_url("s"), o.token_embeds_url("")),
                                 (True, False, False))
                self.assertEqual([o.token_url_shaped(t) for t in (URL_SHAPED_BEARER, "HTTP://gw/relay",
                                                                  combined, "https://gw/relay%7Cs", "s", "")],
                                 [True, True, False, False, False, False], "a scheme with no separator is URL-shaped")
                self.assertEqual((o.token_gateway(combined), o.token_gateway(URL_SHAPED_BEARER), o.token_gateway("s")),
                                 (url, "", ""))
                self.assertEqual([o.token_secret_empty(t) for t in ("https://gw/relay|", "https://gw/relay| ", "", "  ",
                                                                    combined, "s", "httpfoo")],
                                 [True, True, True, True, False, False, False], "no secret on either side of '|'")
                self.assertEqual(o.gateway_url_problem(url), "")
                self.assertEqual(o.gateway_url_problem("http://127.0.0.1:9/relay"), "")
                self.assertEqual(o.gateway_url_problem("https://gw.example:8443/relay"), "")
                for credential_shaped in ("https://user:pass@gw.example:8443/relay", "https://user@gw.example/relay",
                                          "https://@gw.example/relay", "https://gw.example/relay?q=1",
                                          "https://gw.example/relay?", "https://gw.example/relay#f",
                                          "https://gw.example/relay#"):
                    problem = o.gateway_url_problem(credential_shaped)
                    self.assertIn("userinfo, a query or a fragment", problem, credential_shaped)
                    self.assertNotIn("gw.example", problem, "why, never the value")
                for bad in ("", "ftp://x/relay", "https://", "x.example/relay", "https://x/relay|s", "https://x /r",
                            "https://x/r\x7f", "https://?missing-host", "https://#missing-host", "https://:443/relay",
                            "https://user@/relay", "https://gw.example:notaport/relay", "https://gw.example:99999/r"):
                    self.assertTrue(o.gateway_url_problem(bad), bad)
                host = {k: "host" for k in o.bridge_strip_keys()}
                marked = dict(host, KEEP="1", **{o.INJECTED_KEY: "1"})
                self.assertEqual(o.wake_environ(marked, "s", url),
                                 {"KEEP": "1", o.INJECTED_KEY: "1", "REMOTE_TASK_TOKEN": "s", "REMOTE_TASK_URL": url})
                self.assertEqual(o.wake_environ(marked, combined, ""),
                                 {"KEEP": "1", o.INJECTED_KEY: "1", "REMOTE_TASK_TOKEN": combined})
                self.assertEqual(o.wake_environ(marked, "", url), {"KEEP": "1", o.INJECTED_KEY: "1"})
                self.assertEqual(o.wake_environ(dict(host, KEEP="1"), "s", url), dict(host, KEEP="1"), "unmarked: unchanged")

    def test_a_declared_bridge_reads_no_credential_file_and_the_host_resolves_what_it_carries(self):
        canary = "canary-host-file-token-0123456789"
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "channels" / "ag2space"
            cfg.mkdir(parents=True)
            opened = []

            def read_text(path):
                opened.append(path)
                return Path(path).read_text(encoding="utf-8")
            host = {"CLAUDE_CONFIG_DIR": d, "REMOTE_TASK_TOKEN": "host-env-token"}
            for label, o in OWNERS.items():
                with self.subTest(label):
                    opened.clear()
                    (cfg / ".env").write_text(f"REMOTE_TASK_TOKEN={canary}\nREMOTE_PROACTIVE_TRUST_OK=1\n", encoding="utf-8")
                    child = {**host, **o.bridge_token_environ("agent-token")}
                    self.assertTrue(o.credentials_injected(child))
                    self.assertFalse(o.credentials_injected(host))
                    files = o.env_files(child, read_text)
                    self.assertEqual((list(files), files.credential_pairs, files.refused, files.blocked, opened),
                                     ([], (), (), "", []), "injected: no channel file is opened, nothing is read")
                    self.assertEqual(o.bridge_carried_config(child, read_text), {}, "the child resolves nothing itself")
                    registry = o.contract.SecretRegistry()
                    found = o.discover(child, files, registry=registry)
                    self.assertEqual((found.token, found.token_source), ("agent-token", "env"))
                    self.assertFalse(registry.contains(canary), "the host's file token is never registered")
                    self.assertEqual(opened, [])
                    # The host (no mark) resolves the carried setting from its own file; env wins when set.
                    self.assertEqual(o.bridge_carried_config(host, read_text), {"REMOTE_PROACTIVE_TRUST_OK": "1"})
                    self.assertEqual(opened, [str(cfg / ".env")])
                    self.assertEqual(o.bridge_carried_config({**host, "REMOTE_PROACTIVE_TRUST_OK": "0"}, read_text),
                                     {"REMOTE_PROACTIVE_TRUST_OK": "0"})
                    self.assertEqual(opened, [str(cfg / ".env")], "set in the environment: the file is not opened")
                    (cfg / ".env").write_text(f"REMOTE_TASK_TOKEN={canary}\n", encoding="utf-8")
                    self.assertEqual(o.bridge_carried_config(host, read_text), {}, "set nowhere: absent, not blank")
                    self.assertEqual(o.bridge_carried_config({}, read_text), {}, "no pointer: nothing to read")
                    for value in o.bridge_carried_config(host, read_text).values():
                        self.assertNotIn(canary, value)

    def test_the_owner_checks_and_removes_only_its_own_declaration(self):
        args = dict(written_by="x install", host="h", name="n", module="m", env={}, token_vault_key="k")
        for label, o in OWNERS.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "worker.json"
                self.assertEqual(o.remove_bridge_declaration(path, written_by="x install"), (False, ""), "nothing there")
                self.assertEqual(o.write_bridge_declaration(path, **args), "")
                self.assertEqual(o.write_bridge_declaration(path, **args), "", "its own declaration is rewritten")
                self.assertEqual(o.remove_bridge_declaration(path, written_by="x install"), (True, ""))
                self.assertFalse(path.exists())
                foreign = f"{path} was not written by install; remove it by hand (rm {path}) if it is yours to remove"
                for text in ('{"written_by": "someone else"}', "not json", "[1]"):
                    path.write_text(text)
                    self.assertEqual(o.write_bridge_declaration(path, **args), foreign)
                    self.assertEqual(o.remove_bridge_declaration(path, written_by="x install"), (False, foreign))
                    self.assertEqual(path.read_text(), text, "a foreign file is left as it was")
                path.unlink()
                path.mkdir()
                self.assertEqual(o.write_bridge_declaration(path, **args),
                                 f"{path} is a directory, not a declaration; remove it yourself")
                path.rmdir()
                path.symlink_to(Path(d) / "gone")
                self.assertEqual(o.remove_bridge_declaration(path, written_by="x install"),
                                 (False, f"{path} is a dangling symlink; remove it by hand (rm {path})"))
                path.unlink()
                path.write_text('{"written_by": "x install"}')
                with mock.patch.object(o, "open", side_effect=PermissionError(13, "Permission denied"), create=True):
                    self.assertEqual(o.write_bridge_declaration(path, **args), f"{path} unreadable (Permission denied)")

    def test_a_declaration_past_the_cap_is_refused_unread_and_left_untouched(self):
        args = dict(written_by="x install", host="h", name="n", module="m", env={}, token_vault_key="k")
        for label, o in OWNERS.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "worker.json"
                body = '{"written_by": "x install", "pad": "'
                at_cap = body + "a" * (o.MAX_DECLARATION_BYTES - len(body) - 2) + '"}'
                self.assertEqual(len(at_cap), o.MAX_DECLARATION_BYTES)
                path.write_text(at_cap)
                self.assertEqual(o.remove_bridge_declaration(path, written_by="x install"), (True, ""), "at the cap: ours")
                over = at_cap[:-2] + 'a"}'
                path.write_text(over)
                refused = (f"{path} is over the {o.MAX_DECLARATION_BYTES}-byte cap for a declaration; "
                           f"remove it by hand (rm {path}) if it is yours to remove")
                self.assertEqual(o.write_bridge_declaration(path, **args), refused)
                self.assertEqual(o.remove_bridge_declaration(path, written_by="x install"), (False, refused))
                self.assertEqual(path.read_text(), over, "left as it was")
                reads, real_open = [], open
                with mock.patch.object(o, "open", side_effect=lambda *a, **k: _Spy(real_open(*a, **k), reads),
                                       create=True):
                    o.remove_bridge_declaration(path, written_by="x install")
                self.assertEqual(reads, [], "an oversized file is never read")
                path.write_text(at_cap)
                with mock.patch.object(o, "open", side_effect=lambda *a, **k: _Spy(real_open(*a, **k), reads),
                                       create=True):
                    o.remove_bridge_declaration(path, written_by="x install")
                self.assertEqual(reads, [o.MAX_DECLARATION_BYTES + 1], "a read is bounded at the cap")

    def test_a_failed_write_leaves_no_declaration_and_no_temp_file(self):
        for label, o in OWNERS.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "worker.json"
                with mock.patch.object(o.os, "replace", side_effect=OSError("disk")), self.assertRaises(OSError):
                    o.write_bridge_declaration(path, written_by="x", host="h", name="n", module="m", env={},
                                               token_vault_key="k")
                self.assertEqual(list(Path(d).iterdir()), [])

    def test_a_string_or_getattr_reference_to_an_owner_private_name_is_flagged(self):
        for source in ('getattr(_sources, "TOKEN_KEYS")[0]', 'k = "TOKEN_KEYS"', 'getattr(_sources, "TOKEN_" + "KEYS")',
                       'getattr(_sources, "token_env_name")()', 'getattr(_sources, "_bridge_declaration_text")',
                       'getattr(_sources, "_RECORD_TOKEN_ENV")', 'getattr(_sources, "CREDENTIAL_KEYS")',
                       '_sources.bridge_token_environ("")', 'getattr(_sources, "bridge_declaration_fields")()',
                       '_sources.bridge_strip_keys()', 'getattr(_sources, "bridge_strip_keys")()',
                       '_sources.bridge_carried_config({}, f)', 'getattr(_sources, "bridge_carried_config")',
                       'from .gateway_credential_sources import bridge_token_environ',
                       'from .gateway_credential_sources import bridge_strip_keys'):
            self.assert_flagged(source)
        helper = "adapters/claude_code/probe_helper.py"
        found = self.package_findings(extra={helper: 'import os\nos.getenv(getattr(_sources, "TOKEN_KEYS")[0])\n'})
        self.assertEqual([f for f in found if f.startswith(helper + ":")], [f"{helper}:2 owner key tuple TOKEN_KEYS"])

    def test_the_declarations_reader_names_only_the_owners_accessors(self):
        for rel, path in RECORD_READERS.items():
            text = path.read_text(encoding="utf-8")
            self.assertEqual(owner_only_findings(text, rel, reader=True), [])
            self.assertIn("bridge_declaration_fields", text)
            for planted in ('\nX = "token_env"\n', "\nX = _sources.token_env_name()\n", '\nX = getattr(_sources, "TOKEN_KEYS")\n'):
                self.assertTrue(owner_only_findings(text + planted, rel, reader=True), planted)
            self.assertTrue(owner_only_findings("_sources.bridge_token_environ('')", "adapters/x.py"),
                            "the reader's accessors stay the reader's")
            self.assertEqual(token_injection_findings(text, rel), [])
            for mutant in (('    token_env = _sources.bridge_token_environ(token, url)\n', '    token_env = {"REMOTE_TASK_TOKEN": token}\n'),
                           ('    strip = _sources.bridge_strip_keys() + _sources.BRIDGE_CARRIED_CONFIG\n', '    strip = token_env + _sources.BRIDGE_CARRIED_CONFIG\n'),
                           ('    strip = _sources.bridge_strip_keys() + _sources.BRIDGE_CARRIED_CONFIG\n', '    strip = HOST_ONLY + tuple(token_env)\n'),
                           ('    strip = _sources.bridge_strip_keys() + _sources.BRIDGE_CARRIED_CONFIG\n', '    strip = _sources.bridge_strip_keys()[:1] + _sources.BRIDGE_CARRIED_CONFIG\n'),
                           ('    strip = _sources.bridge_strip_keys() + _sources.BRIDGE_CARRIED_CONFIG\n', '    strip = _sources.bridge_strip_keys()\n'),
                           ('    env.update(_sources.bridge_carried_config(host, read_regular))\n',
                            '    env.update(REMOTE_PROACTIVE_TRUST_OK=host.get("REMOTE_PROACTIVE_TRUST_OK", ""))\n'),
                           ('    env.update(_sources.bridge_carried_config(host, read_regular))\n', ''),
                           ('    env.update(token_env)\n', '    env.update(token_env)\n    env.pop(next(k for k in token_env if k != "REMOTE_TASK_TOKEN"))\n'),
                           ('    env.update(token_env)\n', '    env.update(token_env)\n    env["AG2_REMOTE_" + "TOKEN"] = token\n'),
                           ('    env.update(token_env)\n', '    env.update(token_env, X=os.environ.get("REMOTE_TASK_TOKEN"))\n')):
                self.assertIn(mutant[0], text)
                self.assertTrue(token_injection_findings(text.replace(mutant[0], mutant[1]), rel), mutant[1])

    READER_SELF_WRITES = (
        ('    env.update(token_env)\n', '    env.update(token_env)\n    env.update(REMOTE_TASK_TOKEN=token)\n'),
        ('    env.update(token_env)\n', '    if False:\n        env.update(token_env)\n    env.update({k: token for k in token_env})\n'),
        ('    env.update(token_env)\n', '    env.update(token_env)\n    env[suffix_key(decl)] = token\n'),
        ('    env.update(token_env)\n', '    env.update(token_env)\n    env.setdefault(next(iter(token_env)), token)\n'),
        ('    os.environ.update(environ)\n', '    os.environ.update(environ)\n    os.environ.update(**{next(iter(environ)): token})\n'),
        ('    environ = bridge_environ(decl, token, url=url)\n', '    environ = {**bridge_environ(decl, token, url=url), "X": token}\n'),
        ('    os.environ.update(environ)\n', '    os.environ.update(environ)\n    os.putenv(name_of(decl), token)\n'),
        ('    os.environ.update(environ)\n', '    os.environ.update(environ)\n    os.environ |= {next(iter(environ)): token}\n'),
        ('    os.environ.update(environ)\n', '    os.environ.update(environ)\n    e = os.environ\n    e |= {next(iter(environ)): token}\n'),
        ('    os.environ.update(environ)\n', '    os.environ.update(environ)\n    os.environ.__ior__({next(iter(environ)): token})\n'),
        ('    os.environ.update(environ)\n', '    os.environ.update(environ | {next(iter(environ)): token})\n'),
        ('    os.environ.update(environ)\n', '    os.environ.update(environ)\n    del os.environ[next(iter(environ))]\n'))

    def test_the_reader_writes_no_environment_entry_itself(self):
        for rel, path in RECORD_READERS.items():
            text = path.read_text(encoding="utf-8")
            self.assertEqual(token_injection_findings(text, rel), [])
            for before, after in self.READER_SELF_WRITES:
                self.assertIn(before, text)
                self.assertTrue(token_injection_findings(text.replace(before, after, 1), rel), after)

    def test_the_owner_entry_points_are_reached_only_on_the_owner_import(self):
        imp = "from ... import gateway_credential_sources as _sources\n"
        head = imp + "def f(root):\n    path = bridge_home(root) / DECLARATION\n"
        self.assertEqual(credential_access(head + '    _sources.write_bridge_declaration(path, written_by="x")\n', "a.py"), [],
                         "the control: the owner's import, called directly")
        for source in (
                head + "    class H:\n        def write_bridge_declaration(self, p):\n            return p.read_text()\n"
                       "    H().write_bridge_declaration(path)\n",
                head + "    write_bridge_declaration(path)\n",
                imp + "def write_bridge_declaration(p):\n    return open(p).read()\n"
                      "def f(root):\n    write_bridge_declaration(bridge_home(root) / DECLARATION)\n",
                head + "    _sources = Spy()\n    _sources.write_bridge_declaration(path)\n",
                imp + "import json as _sources\n" + head.split("\n", 1)[1] + "    _sources.remove_bridge_declaration(path)\n",
                "import json as _sources\n" + head.split("\n", 1)[1] + "    _sources.write_bridge_declaration(path)\n",
                head + '    getattr(_sources, "write_bridge_declaration")(path)\n',
                head + "    w = _sources.write_bridge_declaration\n    w(path)\n",
                head + "    def g(_sources):\n        _sources.write_bridge_declaration(path)\n"):
            self.assertTrue(credential_access(source, "adapters/claude_code/cli.py"), source)

    def test_a_declaration_path_in_an_f_string_is_still_the_path_once_read(self):
        rel = "adapters/claude_code/cli.py"
        self.assertTrue(declaration_reads("def f():\n    open(DECLARATION).read()\n", rel), "the positive control")
        for source in ('def f():\n    fh = open(f"{DECLARATION}")\n    fh.read()\n',
                       'def f(root):\n    path = bridge_home(root) / DECLARATION\n    text = open(f"{path}").read()\n',
                       'def f():\n    open(f"{DECLARATION}").read()\n',
                       'def f(root):\n    name = f"{bridge_home(root) / DECLARATION}"\n    open(name).read()\n',
                       'def where(root):\n    return f"{bridge_home(root) / DECLARATION}"\n'
                       'def f(root):\n    open(where(root)).read()\n',
                       'def f(root):\n    path = bridge_home(root) / DECLARATION\n    print(path or "none")\n'):
            self.assertTrue(declaration_reads(source, rel), source)
        for source in ('def f(root):\n    path = bridge_home(root) / DECLARATION\n    msg = f"see {path}"\n    print(msg)\n',
                       'def f(root):\n    path = bridge_home(root) / DECLARATION\n    print(f"declared: {path}")\n',
                       'def why(root):\n    return f"see {bridge_home(root) / DECLARATION}"\n'
                       'def f(root, r):\n    problem = why(root)\n    check(r, not problem, "x", problem or "fine")\n'):
            self.assertEqual(declaration_reads(source, rel), [], "the control: a message is handed on, never read")

    def test_a_declaration_path_from_a_helper_is_the_declaration(self):
        for source in (
                "def decl_path(root):\n    return bridge_home(root) / DECLARATION\n"
                "def f(root):\n    p = decl_path(root)\n    if p.exists():\n        json.loads(p.read_text())\n",
                "def decl_path(root):\n    p = bridge_home(root) / DECLARATION\n    return p\n"
                "def outer(root):\n    return decl_path(root)\n"
                "def f(root):\n    outer(root).read_text()\n",
                "DECL = lambda root: bridge_home(root) / DECLARATION\ndef f(root):\n    DECL(root).read_text()\n",
                "HOME_DECL = Path('~') / ('work' + 'er.json')\ndef f():\n    HOME_DECL.read_text()\n",
                "class C:\n    def where(self, root):\n        return bridge_home(root) / DECLARATION\n"
                "    def f(self, root):\n        self.where(root).read_text()\n",
                "def f(root):\n    for p in [bridge_home(root) / DECLARATION]:\n        p.read_text()\n"):
            self.assertTrue(declaration_reads(source, "adapters/claude_code/cli.py"), source)
        self.assertEqual(declaration_reads('def msg(root):\n    return f"see {bridge_home(root) / DECLARATION}"\n'
                                           'def f(root):\n    print(msg(root))\n', "a.py"), [],
                         "the control: a function returning a message about the path is not a path")

    def test_the_name_only_route_is_the_owners_canonical_token_key(self):
        self.assertEqual(owner.token_env_name(), owner.TOKEN_KEYS[0])
        self.assertEqual(owner.token_env_name(), "REMOTE_TASK_TOKEN")
        self.assertIn(owner.token_env_name(), SOURCE_KEYS)

    def test_the_reachability_probe_discards_what_show_environment_prints(self):
        rel, run = SYSTEMD_PROBE[0], '_systemctl(("--user", "show-environment"))'
        shipped = f"def systemd_user_available():\n    return _ok({run})\n"
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

    HELPER = ('def _systemctl(args, *, locate=False):\n    if locate:\n        return shutil.which("systemctl")\n'
              '    return _systemd_run(["systemctl", *args])\n')

    def test_every_non_literal_systemctl_argument_is_flagged(self):
        rel = SYSTEMCTL_HELPER[0]
        for cmd in self.BUILT:
            with self.subTest(cmd=cmd):
                for source in (f"def systemd_user_available():\n    return _ok(_systemctl(('--user', {cmd})))\n",
                               f"def f():\n    c = {cmd}\n    return _systemctl(('--user', c)).stdout\n",
                               f"def f():\n    return _systemctl(('--user',) + ({cmd},)).stdout\n",
                               f"def f():\n    return _systemctl(tuple(['--user', {cmd}])).stdout\n",
                               f"def f():\n    _systemd_run(['systemctl', '--user', {cmd}])\n",
                               f"def f():\n    subprocess.run(['systemctl', '--user', {cmd}])\n",
                               f"def f():\n    subprocess.run(['systemctl'] + ['--user', {cmd}], capture_output=True).stdout\n",
                               f"def f():\n    c = ['systemctl']\n    c += ['--user', {cmd}]\n    return subprocess.run(c).stdout\n",
                               f"def f():\n    return subprocess.run([*['systemctl'], '--user', {cmd}]).stdout\n",
                               f"def f():\n    c = ['systemctl']\n    c.append({cmd})\n    return _systemd_run(c).stdout\n"):
                    self.assertTrue(self.appended(source, rel), source)
        for source in (
                # A literal "systemctl" staged into a non-literal argv, in each form.
                'def manager_env(rest):\n    return subprocess.run(\n        ["systemctl"] + rest, capture_output=True, text=True\n    ).stdout\n',
                'def f(rest):\n    cmd = ["systemctl"]\n    cmd += rest\n    return subprocess.run(cmd).stdout\n',
                'def f(rest):\n    return subprocess.run([*["systemctl"], *rest]).stdout\n',
                'def f(rest):\n    return subprocess.run(["systemctl", *rest]).stdout\n',
                'def f(rest):\n    return _systemd_run(["systemctl"] + rest).stdout\n',
                'def f():\n    return subprocess.run(["/usr/bin/systemctl", "--user", "show"]).stdout\n',
                'SYSTEMCTL = "systemctl"\ndef f():\n    subprocess.run([SYSTEMCTL, "--user", v])\n',
                'def f():\n    return shutil.which("systemctl")\n',
                # The helper with anything but one literal tuple, or not called directly.
                'def f(rest):\n    return _systemctl(rest).stdout\n',
                'def f(rest):\n    return _systemctl(("--user", *rest)).stdout\n',
                'def f(rest):\n    return _systemctl(["--user", "show"]).stdout\n',
                'def f(rest):\n    return _cli._systemctl(rest).stdout\n',
                'def f(rest):\n    run = _systemctl\n    return run(rest).stdout\n',
                'def f(flag):\n    return _systemctl((), locate=flag)\n',
                'def f(**kw):\n    return _systemctl(("--user",), **kw)\n',
                'def f(u):\n    return _systemctl(("--user", "stop"), unit=u, env={"A": "b"})\n',
                'def f(u, rest):\n    return _systemctl(rest, unit=u)\n',
                'def f(u):\n    return _systemctl(("--user", "stop", u), unit=u)\n',
                'def f(u):\n    return _systemctl(("--user", "stop"), u)\n',
                'def f():\n    _systemd_run(CMD)\n', 'def f(rest):\n    run = _systemd_run\n    run(rest)\n',
                'def f():\n    subprocess.run("systemctl --user show-environment", shell=True)\n',
                'X = "show-environment"\n', 'def f():\n    print(f"run systemctl --user show-environment")\n',
                # Another name for a runner, a redefinition of one, or one named in a string.
                'from .cli import _systemctl as run\ndef f(rest):\n    return run(rest).stdout\n',
                'from .cli import _systemd_run as run\ndef f(rest):\n    return run(rest).stdout\n',
                'from . import cli\nrun = cli._systemctl\n', 'run = _systemctl\n',
                'def f():\n    return getattr(_cli, "_systemctl")(("--user",)).stdout\n',
                'def f():\n    return getattr(_cli, "_system" + "ctl")(("--user",)).stdout\n',
                'def f(_systemctl=print):\n    return _systemctl(("--user",))\n',
                'class Unit:\n    def _systemctl(self, args):\n        return subprocess.run(["systemctl", *args])\n',
                'def f():\n    def _systemctl(args):\n        return args\n    return _systemctl(("--user",))\n',
                'def _systemctl(args):\n    return _systemd_run(["systemctl", *args])\n',
                'def f():\n    def _systemd_run(cmd):\n        return subprocess.run(cmd)\n    return _systemd_run(["x"])\n'):
            with self.subTest(source=source):
                self.assertTrue(self.appended(source, rel), source)
        self.assertTrue(credential_access(self.HELPER, "adapters/hatch/keepalive.py"),
                        "only the one helper, in its own module, may name systemctl")
        self.assertEqual(credential_access(self.HELPER, rel), [], "the helper alone, in its module")
        for source in ('def f():\n    _systemctl(("--user", "stop", "ag2-sparrow.service"))\n',
                       'def f():\n    _cli._systemctl(("--user", "restart", "ag2-sparrow.service"))\n',
                       'def f():\n    _systemd_run(["loginctl", "enable-linger", user])\n',
                       'def f():\n    return _systemctl((), locate=True)\n',
                       'def f(shape):\n    _sm._systemctl(("--user", "stop"), unit=shape.service_name, timeout=shape.manager_timeout_s)\n',
                       'def f(shape):\n    _systemctl(("--user", "daemon-reload"), timeout=shape.manager_timeout_s)\n',
                       'def f():\n    note(f"run systemctl --user restart {SERVICE_NAME}")\n'):
            self.assertEqual(credential_access(self.HELPER + source, rel), [], source)

    def appended(self, source: str, rel: str) -> "list[str]":
        """The findings credential_access reports in `source` itself, after HELPER is prepended."""
        offset, out = self.HELPER.count("\n"), []
        for found in credential_access(self.HELPER + source, rel):
            where = found.split(" ", 1)[0].split(":", 1)[1]
            if (int(where) > offset) if where.isdigit() else where != SYSTEMCTL_HELPER[1]:
                out.append(found)
        return out

    def test_the_systemd_rule_covers_every_package_module(self):
        root = PKG / "ag2_sparrow"
        self.assertEqual(package_systemd_findings(root), [])
        probe = '\ndef manager(rest):\n    return subprocess.run(["systemctl", *rest]).stdout\n'
        for module in ("gateway_credentials.py", "gateway_credential_sources.py", "remote_gateway_bridge.py",
                       "adapters/hatch/keepalive.py", "adapters/hatch/cli.py", "connect_common.py",
                       "adapters/claude_code/cli.py"):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as td:
                copy = Path(td) / "ag2_sparrow"
                shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__"))
                with open(copy / module, "a", encoding="utf-8") as f:
                    f.write(probe)
                found = package_systemd_findings(copy)
                self.assertTrue(found and all(x.startswith(f"{module}:") for x in found), found)
        for module, extra in (("adapters/hatch/keepalive.py", "\ndef _systemctl(args):\n    return args\n"),
                              ("connect_common.py", "\ndef _systemctl(*args, timeout=30):\n    return args\n"),
                              ("adapters/hatch/cli.py", "\nclass Unit:\n    def _systemctl(self, args):\n"
                                                        "        return args\n")):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as td:
                copy = Path(td) / "ag2_sparrow"
                shutil.copytree(root, copy, ignore=shutil.ignore_patterns("__pycache__"))
                with open(copy / module, "a", encoding="utf-8") as f:
                    f.write(extra)
                found = package_systemd_findings(copy)
                self.assertTrue(any(x.startswith(f"{SYSTEMCTL_HELPER[1]} is defined at") for x in found), found)
                self.assertTrue(any(x.startswith(f"{module}:") and "a second definition" in x for x in found), found)

    def test_the_helper_runs_only_an_ag2_sparrow_unit(self):
        seen = []
        with mock.patch.object(service_manager, "_run", side_effect=lambda cmd, timeout: seen.append((cmd, timeout))):
            for unit in ("evil.service", "show-environment", "ag2-sparrow.service --all", "ag2-sparrow.socket",
                         "AG2-SPARROW.service", "", b"ag2-sparrow.service", "-ag2-sparrow.service"):
                with self.subTest(unit=unit), self.assertRaises(ValueError):
                    service_manager._systemctl(("--user", "stop"), unit=unit)
            self.assertEqual(seen, [], "a refused unit runs nothing")
            service_manager._systemctl(("--user", "stop"), unit="ag2-sparrow-claude-local.service", timeout=7)
            service_manager._systemctl(("--user", "restart", "ag2-sparrow.service"))
        self.assertEqual(seen, [(["systemctl", "--no-ask-password", "--user", "stop", "ag2-sparrow-claude-local.service"], 7),
                                (["systemctl", "--no-ask-password", "--user", "restart", "ag2-sparrow.service"],
                                 service_manager.TIMEOUT_S)])

    def test_every_shipped_service_name_literal_is_the_service_name(self):
        tree = ast.parse((PKG / "ag2_sparrow" / "adapters" / "hatch" / "cli.py").read_text(encoding="utf-8"))
        tree2 = ast.parse((PKG / "ag2_sparrow" / "adapters" / "hatch" / "keepalive.py").read_text(encoding="utf-8"))
        calls = [n for t in (tree, tree2) for n in ast.walk(t) if isinstance(n, ast.Call) and _callee(n) == "_systemctl"]
        names = {e.value for n in calls for e in n.args[0].elts if e.value.endswith(".service")}
        self.assertEqual(names, {"ag2-sparrow.service"})
        self.assertGreaterEqual(len(calls), 10)
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
