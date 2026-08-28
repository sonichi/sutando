"""The env tier must strip quotes, like the three file tiers already do.

A user who writes the credential quoted —

    REMOTE_TASK_TOKEN='https://gw.example/relay|<secret>'

— gets a working bridge when the value is read from the FILE (three readers
call `.strip().strip("'\\"")`) and a permanently unauthorized one when a launcher
hands the same value over in the process environment, because `_env_compat` used
`os.environ.get` raw. The bearer then travels as `<secret>'`, the gateway's
`Authorization` parse strips whitespace but not quotes, no registry entry
matches, and every poll answers 401 — which reads as a revoked key rather than a
parsing bug. Live production diagnosis 2026-08-20 spent a session on it.

Env wins over file (`_RAW = _env_compat(...)` runs first, and only `if not _RAW`
does the quote-stripping file reader get a turn), so the one tier that did not
strip was also the tier that decided.

Pins the SHIPPED readers by importing the real bridge module, not a copy.

Run: python3 packages/ag2-sparrow/tests/test_env_token_quotes.py
"""
import importlib
import os
import pathlib
import sys
import tempfile
import types
import unittest

# Module init resolves the token during exec_module, so both boundaries must be
# hermetic before the bridge is imported.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-env-token-quotes-")

_FAKE_VI = types.ModuleType("vault_intercept")


def _no_vault(var):
    raise KeyError(var)             # empty vault: never resolves, never touches Keychain


_FAKE_VI.get_vault_key = _no_vault
sys.modules["vault_intercept"] = _FAKE_VI

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

URL = "https://gw.example/relay"
SECRET = "s3cr3t-value"
COMBINED = f"{URL}|{SECRET}"
TOKEN_VARS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "REMOTE_TASK_URL",
              "AG2_REMOTE_URL", "AG2_DEVICE_ENV", "GATEWAY_INSTANCE",
              "REMOTE_TASK_TOKEN_FILE")


def _load(**env):
    """Reload the bridge with exactly `env` as its credential environment."""
    for k in TOKEN_VARS:
        os.environ.pop(k, None)
    os.environ.update(env)
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


class UnquoteEnvTests(unittest.TestCase):
    """The shared helper itself."""

    def setUp(self):
        self.m = _load()

    def test_strips_both_quote_styles_and_whitespace(self):
        for raw in (f"'{COMBINED}'", f'"{COMBINED}"', f"  {COMBINED}  ",
                    f"  '{COMBINED}'  "):
            self.assertEqual(self.m._unquote_env(raw), COMBINED, raw)

    def test_leaves_an_unquoted_value_alone(self):
        self.assertEqual(self.m._unquote_env(COMBINED), COMBINED)

    def test_preserves_quotes_inside_the_value(self):
        # Only the surrounding pair is a quoting artifact; a secret that
        # contains a quote keeps it.
        self.assertEqual(self.m._unquote_env("ab'cd"), "ab'cd")

    def test_empty_and_none_pass_through(self):
        self.assertEqual(self.m._unquote_env(""), "")
        self.assertIsNone(self.m._unquote_env(None))


class EnvTierTests(unittest.TestCase):
    """The regression: a quoted credential in the process environment."""

    def test_quoted_combined_token_yields_a_clean_bearer(self):
        m = _load(REMOTE_TASK_TOKEN=f"'{COMBINED}'")
        self.assertEqual(m.TOKEN, SECRET)
        self.assertEqual(m.URL, URL)

    def test_double_quoted_too(self):
        m = _load(REMOTE_TASK_TOKEN=f'"{COMBINED}"')
        self.assertEqual(m.TOKEN, SECRET)
        self.assertEqual(m.URL, URL)

    def test_unquoted_is_unchanged(self):
        m = _load(REMOTE_TASK_TOKEN=COMBINED)
        self.assertEqual(m.TOKEN, SECRET)
        self.assertEqual(m.URL, URL)

    def test_quoted_legacy_alias_is_cleaned_too(self):
        # The deprecated name is the one the 2026-08-20 report carried.
        m = _load(AG2_REMOTE_TOKEN=f"'{COMBINED}'")
        self.assertEqual(m.TOKEN, SECRET)
        self.assertEqual(m.URL, URL)

    def test_trailing_quote_only(self):
        # Leading quote already eaten upstream: the URL parses, so the request reaches
        # the gateway and 401s on the bearer alone.
        m = _load(REMOTE_TASK_TOKEN=f"{COMBINED}'")
        self.assertEqual(m.TOKEN, SECRET)
        self.assertEqual(m.URL, URL)

    def test_quoted_split_layout_url(self):
        m = _load(REMOTE_TASK_TOKEN=f"'{SECRET}'", REMOTE_TASK_URL=f"'{URL}'")
        self.assertEqual(m.TOKEN, SECRET)
        self.assertEqual(m.URL, URL)

    def test_a_bare_secret_never_gains_a_url(self):
        # Guard against over-stripping turning an opaque secret into a URL split.
        m = _load(REMOTE_TASK_TOKEN=f"'{SECRET}'")
        self.assertEqual(m.TOKEN, SECRET)
        self.assertEqual(m.URL, "")


class TierAgreementTests(unittest.TestCase):
    """The invariant this change exists to hold: same file, same answer,
    whichever tier reads it."""

    def _via_file(self, line: str) -> "tuple[str, str]":
        with tempfile.TemporaryDirectory() as d:
            env_file = pathlib.Path(d) / ".env"
            env_file.write_text(line + "\n", encoding="utf-8")
            m = _load(AG2_DEVICE_ENV=str(env_file))
            return m.TOKEN, m.URL

    def _via_env(self, value: str) -> "tuple[str, str]":
        m = _load(REMOTE_TASK_TOKEN=value)
        return m.TOKEN, m.URL

    def test_quoted_line_reads_the_same_through_both_tiers(self):
        quoted = f"'{COMBINED}'"
        self.assertEqual(self._via_file(f"REMOTE_TASK_TOKEN={quoted}"),
                         self._via_env(quoted))

    def test_and_that_shared_answer_is_the_clean_one(self):
        self.assertEqual(self._via_file(f"REMOTE_TASK_TOKEN='{COMBINED}'"),
                         (SECRET, URL))

    def test_unquoted_line_reads_the_same_through_both_tiers(self):
        self.assertEqual(self._via_file(f"REMOTE_TASK_TOKEN={COMBINED}"),
                         self._via_env(COMBINED))


class BearerShapeTests(unittest.TestCase):
    """What the gateway actually receives."""

    def test_auth_header_carries_no_quote(self):
        m = _load(REMOTE_TASK_TOKEN=f"'{COMBINED}'")
        self.assertEqual(m._AUTH_HEADERS["Authorization"], f"Bearer {SECRET}")
        self.assertNotIn("'", m._AUTH_HEADERS["Authorization"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
