#!/usr/bin/env python3
"""Python twin of tests/credential-resolver.test.ts — SAME vectors, one-for-one.

The twins share canaries so a latent defect can't survive in only one language
(the policy-twin lesson from #2516). Any change to the resolver contract must be
reflected in BOTH suites with identical inputs/outputs.

  1. No managed file -> resolution IDENTICAL to the legacy env chain
     (GEMINI_VOICE_API_KEY -> GEMINI_API_KEY -> ''), source 'env'/'none'.
  2. Managed tier wins over env; voice falls back to the managed TEXT credential
     BEFORE dropping to env (tier order beats slot order).
  3. Malformed / wrong-shape managed files skip the tier — never raise.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from credential_resolver import resolve_credential, ResolvedCredential  # noqa: E402

_ENV_KEYS = ("GEMINI_VOICE_API_KEY", "GEMINI_API_KEY")
_failures = []
_dir = Path(tempfile.mkdtemp(prefix="credresolver-"))


def _reset_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _missing() -> str:
    return str(_dir / "does-not-exist.json")


def _write_managed(caps: dict) -> str:
    p = _dir / "managed-credentials.json"
    p.write_text(json.dumps({"version": 1, "capabilities": caps}))
    return str(p)


def check(name, got, expected):
    exp = ResolvedCredential(key=expected["key"], source=expected["source"])
    if tuple(got) != tuple(exp):
        _failures.append(f"{name}: got {tuple(got)} expected {tuple(exp)}")
        print(f"  FAIL {name}: got {tuple(got)} expected {tuple(exp)}")
    else:
        print(f"  ok   {name}")


# 1. legacy equivalence: no managed file, VOICE key wins
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"; os.environ["GEMINI_API_KEY"] = "mk"
check("legacy: no managed, VOICE key wins", resolve_credential("gemini-voice", _missing()), {"key": "vk", "source": "env"})

# 2. legacy equivalence: no managed file, MAIN-key fallback for voice
_reset_env(); os.environ["GEMINI_API_KEY"] = "mk"
check("legacy: no managed, MAIN-key fallback for voice", resolve_credential("gemini-voice", _missing()), {"key": "mk", "source": "env"})

# 3. legacy equivalence: nothing set -> empty key, source none
_reset_env()
check("legacy: nothing set -> none", resolve_credential("gemini-voice", _missing()), {"key": "", "source": "none"})

# 4. text capability never reads the voice env var
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"
check("text never reads voice env", resolve_credential("gemini-text", _missing()), {"key": "", "source": "none"})

# 5. managed tier beats env
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"
check("managed tier beats env", resolve_credential("gemini-voice", _write_managed({"gemini-voice": {"key": "managed-v"}})), {"key": "managed-v", "source": "managed"})

# 6. tier order beats slot order: managed TEXT beats env VOICE
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"
check("tier order beats slot order (managed TEXT > env VOICE)", resolve_credential("gemini-voice", _write_managed({"gemini-text": {"key": "managed-t"}})), {"key": "managed-t", "source": "managed"})

# 7. empty managed key falls through to env
_reset_env(); os.environ["GEMINI_API_KEY"] = "mk"
check("empty managed key falls through to env", resolve_credential("gemini-voice", _write_managed({"gemini-voice": {"key": ""}})), {"key": "mk", "source": "env"})

# 8. malformed JSON skips managed tier, never raises
_reset_env(); os.environ["GEMINI_API_KEY"] = "mk"
_bad = _dir / "malformed.json"; _bad.write_text("{not json")
check("malformed JSON skips managed, never raises", resolve_credential("gemini-voice", str(_bad)), {"key": "mk", "source": "env"})

# 9. wrong-shape capabilities (array / non-string key) skip managed tier
_reset_env(); os.environ["GEMINI_API_KEY"] = "mk"
_arr = _dir / "arr.json"; _arr.write_text(json.dumps({"version": 1, "capabilities": []}))
check("wrong-shape (array capabilities) skips managed", resolve_credential("gemini-voice", str(_arr)), {"key": "mk", "source": "env"})
check("wrong-shape (non-string key) skips managed", resolve_credential("gemini-voice", _write_managed({"gemini-voice": {"key": 42}})), {"key": "mk", "source": "env"})

if _failures:
    print(f"\nFAIL — {len(_failures)} check(s) failed")
    sys.exit(1)
print("\nPASS — credential-resolver Python twin matches the TS canaries")
