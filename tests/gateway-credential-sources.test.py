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
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow"
sys.path.insert(0, str(PKG))
from ag2_sparrow import gateway_credential_sources as owner, gateway_credentials as contract  # noqa: E402
from ag2_sparrow.adapters.hatch import cli as hatch_cli  # noqa: E402

ENV_TOKEN, DEVICE_TOKEN, CHANNEL_TOKEN = "env-token-AAAA1111", "device-token-BBBB2222", "channel-token-CCCC3333"
VAULT_TOKEN, FILE_TOKEN, RETIRED = "vault-token-DDDD4444", "rotated-token-EEEE5555", "retired-token-FFFF6666"
GW = "https://gw.example/relay"
URL_SECRET_URL = "https://user-name-1:pass-word-2@gw.example/relay?k=query-secret-3#frag-secret-4"

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
        self.channel = self.config / "channels" / "ag2space" / ".env"
        self.channel.parent.mkdir(parents=True, exist_ok=True)
        self.token_file = root / "token"
        _write_env(self.device, device)
        _write_env(self.channel, channel)
        if token_file:
            self.token_file.write_text(f"REMOTE_TASK_TOKEN={token_file}\n")
        self.env = {k: (str(self.token_file) if v == "TOKENFILE" else v) for k, v in env.items()}
        self.env.update(AG2_DEVICE_ENV=str(self.device), CLAUDE_CONFIG_DIR=str(self.config))
        values = [v for d in (env, device, channel) for v in d.values() if v and v != "TOKENFILE"]
        values += ([self.vault] if self.vault else []) + ([token_file] if token_file else [])
        # The secrets: a bare token, or the secret of an onboarding string; a plain URL is none.
        self.candidates = [contract.parse_onboarding_token(v)[1] for v in values
                           if not (v.startswith("http") and "|" not in v)]

    def expected(self):
        token, source, url, token_file = self.expect
        token_file = {"DEVICE": str(self.device), "CHANNEL": str(self.channel),
                      "TOKENFILE": str(self.token_file)}.get(token_file, token_file)
        return {"token": token, "source": source, "url": url, "token_file": token_file}

    def owner_files(self):
        return [(str(p), owner.parse_env_text(p.read_text())) for p in (self.device, self.channel)]


class PureContract(unittest.TestCase):
    def test_every_rendering_and_case_of_a_registered_secret_is_withheld(self):
        registry = contract.SecretRegistry()
        registry.register("MixedToken/9+x")
        for text in ("MixedToken/9+x", "mixedtoken/9+x", "MIXEDTOKEN%2F9%2BX", "mixedtoken%2f9%2bx",
                     "MixedToken%252F9%252Bx", repr("MixedToken/9+x")):
            self.assertEqual(registry.scrub(f"a {text} b"), contract.WITHHELD_TEXT, text)
        self.assertEqual(registry.scrub("plain words"), "plain words")
        registry.register("\udcff")  # a non-UTF-8 environment byte: total, never raises
        self.assertTrue(registry.contains("x \udcff y"))

    def test_pieces_and_code_fields(self):
        registry = contract.SecretRegistry()
        registry.register("bearer-secret-value-77")
        for value in ("bearer-secret-value-77", "SECRET-VALUE", "secret-value-77"):
            self.assertIsNone(contract.code_field(value, registry), value)
        self.assertEqual(contract.code_field("reenroll_disabled", registry), "reenroll_disabled")
        self.assertIsNone(contract.code_field("has space", registry))
        self.assertIsNone(contract.code_field("query-secret-3", None, URL_SECRET_URL))

    def test_url_parts_are_registered_and_redacted(self):
        registry = contract.SecretRegistry()
        registry.register_gateway_url(URL_SECRET_URL)
        for piece in ("user-name-1", "pass-word-2", "query-secret-3", "frag-secret-4", "k=ab"[:0] + "=query-secret-3"):
            self.assertTrue(registry.contains(f"x {piece} y"), piece)
        self.assertEqual(contract.redact_text(f"poll {URL_SECRET_URL} failed", URL_SECRET_URL),
                         "poll https://gw.example/relay failed")
        self.assertEqual(contract.redact_text("leaked k=query-secret-3", URL_SECRET_URL), contract.REDACTED_TEXT)
        self.assertEqual(contract.redact_url("not a url"), contract.REDACTED_URL)

    def test_safe_status_and_describe(self):
        out = contract.safe_status({"a": float("nan"), "b": [1.5, "x"], "c": object()}, lambda t: t.upper())
        self.assertEqual(out, {"A": contract.WITHHELD, "B": [1.5, "X"], "C": contract.WITHHELD})
        try:
            {}["owner_dm"]
        except KeyError as e:
            self.assertRegex(contract.describe(e), r"^KeyError\['owner_dm'\] at ")
        self.assertEqual(contract.describe(FileNotFoundError(2, "SECRET", "/x")), "FileNotFoundError ENOENT")

    def test_install_diff_masking_covers_multi_assignment_and_url_lines(self):
        registry = contract.SecretRegistry()
        registry.register(ENV_TOKEN)
        unit = ("[Service]\n"
                f'Environment="SAFE=keep-me" "REMOTE_TASK_TOKEN={RETIRED}"\n'
                f"Environment=REMOTE_TASK_URL={URL_SECRET_URL}\n"
                f"Environment=OTHER={GW}|{RETIRED}\n"
                "Environment=AG2_DEVICE_ENV=/home/x/relay.env\n"
                f"ExecStart=/opt/bin/x --flag {ENV_TOKEN.upper()}\n")
        shown = contract.mask_env_assignments(unit, registry)
        for secret in (RETIRED, ENV_TOKEN, "user-name-1", "pass-word-2", "query-secret-3", "frag-secret-4"):
            self.assertNotIn(secret.casefold(), shown.casefold(), secret)
        self.assertIn("SAFE=keep-me", shown)
        self.assertIn("Environment=REMOTE_TASK_URL=https://gw.example/relay\n", shown)
        self.assertIn("AG2_DEVICE_ENV=/home/x/relay.env", shown)

    def test_the_contract_is_pure(self):
        tree = ast.parse((REPO / "shared" / "ag2_gateway_credentials.py").read_text())
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for io_call in ("open", "os.environ.get", "os.getenv", "subprocess.run", "print"):
            self.assertNotIn(io_call, calls, io_call)
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))])
        module_level = [ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)]
        self.assertFalse([v for v in module_level if "SecretRegistry(" in v], "no module-global registry")


class Discovery(unittest.TestCase):
    def test_the_table_is_stated_once(self):
        self.assertEqual(owner.SOURCES, (("token", ("env", "file", "vault")),
                                         ("url", ("env", "token", "file")),
                                         ("token_file", ("env", "file")),
                                         ("media_token", ("env",))))

    def test_every_row_picks_the_tabled_winner_and_registers_every_candidate(self):
        for row in ROWS:
            with self.subTest(row=row[0]), tempfile.TemporaryDirectory() as td:
                fx = Fixture(Path(td), row)
                registry = contract.SecretRegistry()
                found = owner.discover(fx.env, fx.owner_files(),
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

    def test_the_owner_opens_nothing_itself(self):
        tree = ast.parse((REPO / "src" / "gateway_credential_sources.py").read_text())
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for io_call in ("open", "os.environ.get", "os.getenv", "Path", "print"):
            self.assertNotIn(io_call, calls, io_call)
        self.assertNotIn("os.environ", ast.unparse(tree))


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

    def bridge(self, fx):
        state = fx.root / "state"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(fx.root), "PYTHONPATH": str(PKG),
               "AGENT_CONNECT_TASK_DIR": str(fx.root / "t"), "AGENT_CONNECT_RESULT_DIR": str(fx.root / "r"),
               "AGENT_CONNECT_STATE_DIR": str(state), **fx.env}
        script = BRIDGE.replace("VAULT", json.dumps(fx.vault)).replace("CANDIDATES", json.dumps(fx.candidates))
        r = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                           cwd=str(fx.root), timeout=60)
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
                shown = out.getvalue()
                self.assertIn("DIFF:", shown)
                self.assertIn("SAFE=keep-me", shown)
                # The vault is not a Hatch source; every other candidate, winner or not, is withheld.
                for secret in [c for c in fx.candidates if c != fx.vault] + [RETIRED, "pass-word-2", "query-secret-3"]:
                    self.assertNotIn(secret.casefold(), shown.casefold(), secret)


class Guard(unittest.TestCase):
    SOURCE_KEYS = set(owner.CREDENTIAL_KEYS) | {owner.TOKEN_FILE_KEY}
    OWNERS = {"gateway_credential_sources.py", "gateway_credentials.py"}

    def test_no_other_package_module_reads_a_credential_source(self):
        found = []
        for path in sorted((PKG / "ag2_sparrow").rglob("*.py")):
            if path.name in self.OWNERS:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # A read is a lookup of a credential key: env.get / mapping.get / getenv / [key].
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) \
                        in ("get", "getenv", "pop", "setdefault"):
                    keys = [a.value for a in node.args[:1] if isinstance(a, ast.Constant)]
                elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                    keys = [node.slice.value]
                else:
                    continue
                if any(k in self.SOURCE_KEYS for k in keys):
                    found.append(f"{path.relative_to(PKG)}:{node.lineno}")
        self.assertEqual(found, [], "a credential source is read outside the owner")

    def test_the_guard_sees_each_read_shape(self):
        for source in ('os.environ.get("REMOTE_TASK_TOKEN")', 'vals["AG2_REMOTE_URL"]',
                       'os.getenv("REMOTE_MEDIA_HS_TOKEN")', 'env.pop("REMOTE_TASK_TOKEN_FILE")'):
            tree = ast.parse(source)
            node = tree.body[0].value
            key = node.args[0].value if isinstance(node, ast.Call) else node.slice.value
            self.assertIn(key, self.SOURCE_KEYS, source)

    def test_the_vendored_and_generated_copies_match_their_sources(self):
        for cmd in ([sys.executable, str(PKG / "tools" / "sync_from_src.py"), "--check"],
                    [sys.executable, str(REPO / "tools" / "sync_gateway_credentials.py"), "--check"]):
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO), timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((PKG / "ag2_sparrow" / "gateway_credential_sources.py").read_text(),
                         (REPO / "src" / "gateway_credential_sources.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
