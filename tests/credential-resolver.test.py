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
  4. The S1 voicePreference/quarantined truth table (design 2b): unset =>
     legacy managed->env; 'managed' => ONLY a non-quarantined managed entry
     satisfies (env keys never silently satisfy it); 'byok' => env only;
     quarantined entries are absent in EVERY mode.
  5. S3/R15 read side: opaque generations REPORTED (managed `generation` field /
     SUTANDO_VOICE_CREDENTIAL_GENERATION), never minted; top-level
     preferenceRevision/sessionRevision tolerated and ignored.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from credential_resolver import (  # noqa: E402
    ResolvedCredential,
    credential_source_label,
    resolve_credential,
)

_ENV_KEYS = (
    "GEMINI_VOICE_API_KEY",
    "GEMINI_API_KEY",
    "SUTANDO_VOICE_CREDENTIAL_GENERATION",
)
_failures = []
_dir = Path(tempfile.mkdtemp(prefix="credresolver-"))


def _reset_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _missing() -> str:
    return str(_dir / "does-not-exist.json")


def _write_managed(caps, top_level=None) -> str:
    doc = {"version": 1, "capabilities": caps}
    doc.update(top_level or {})
    p = _dir / "managed-credentials.json"
    p.write_text(json.dumps(doc))
    return str(p)


# Both managed slots filled — the design's canonical S1 fixture shape.
_BOTH_SLOTS = {
    "gemini-voice": {"key": "managed-v"},
    "gemini-text": {"key": "managed-t"},
}


def check(name, got, expected):
    exp = ResolvedCredential(
        key=expected["key"],
        source=expected["source"],
        credential_generation=expected.get("credential_generation"),
    )
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

# 9b. non-object ROOT document: caps empty, preference unset, not quarantined
_reset_env(); os.environ["GEMINI_API_KEY"] = "mk"
_rootarr = _dir / "root-arr.json"; _rootarr.write_text(json.dumps([1, 2]))
check("non-object root document skips managed", resolve_credential("gemini-voice", str(_rootarr)), {"key": "mk", "source": "env"})

# --- S1 truth table: voicePreference x quarantined (design 2b) ---------------

# 10. byok preference: managed voice+text entries + env key -> env wins
_reset_env(); os.environ["GEMINI_API_KEY"] = "byo-mk"
check("byok pref: managed entries + env key -> env wins", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"voicePreference": "byok"})), {"key": "byo-mk", "source": "env"})

# 11. byok preference + NO env key -> none (the "fail actionably" input)
_reset_env()
check("byok pref + no env key -> none", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"voicePreference": "byok"})), {"key": "", "source": "none"})

# 12. managed preference: non-quarantined managed entry satisfies (env irrelevant)
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"
check("managed pref: managed entry satisfies", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"voicePreference": "managed"})), {"key": "managed-v", "source": "managed"})

# 13. S1: managed preference + env key + managed entries MISSING -> none, never env
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"; os.environ["GEMINI_API_KEY"] = "mk"
check("S1: managed pref + env key + managed missing -> none", resolve_credential("gemini-voice", _write_managed({}, {"voicePreference": "managed"})), {"key": "", "source": "none"})

# 14. S1: managed preference + env key + QUARANTINED entries -> none, never env
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"
check("S1: managed pref + env key + quarantined -> none", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"voicePreference": "managed", "quarantined": True})), {"key": "", "source": "none"})

# 15. quarantined (unset preference): managed entries absent -> env fallback
_reset_env(); os.environ["GEMINI_API_KEY"] = "mk"
check("quarantined (unset pref) -> env fallback", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"quarantined": True})), {"key": "mk", "source": "env"})

# 16. quarantined (unset preference) + no env key -> none
_reset_env()
check("quarantined (unset pref) + no env -> none", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"quarantined": True})), {"key": "", "source": "none"})

# 17. quarantined only as strict JSON true; false/absent keep the tier
_reset_env()
check("quarantined=false keeps the tier", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"quarantined": False})), {"key": "managed-v", "source": "managed"})

# 18. R15 read side: revisions + unset preference -> byte-identical legacy behavior
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"
check("R15: revisions tolerated, legacy walk", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"preferenceRevision": 7, "sessionRevision": 3})), {"key": "managed-v", "source": "managed"})

# 19. out-of-vocabulary voicePreference reads as unset (legacy walk)
_reset_env()
for _bad in ("MANAGED", "Byok", 42, None, {}):
    check(f"out-of-vocabulary voicePreference {_bad!r} -> unset", resolve_credential("gemini-voice", _write_managed(_BOTH_SLOTS, {"voicePreference": _bad})), {"key": "managed-v", "source": "managed"})

# 20. voicePreference scopes VOICE: gemini-text ignores byok; quarantine still hides it
_reset_env()
check("text ignores byok preference", resolve_credential("gemini-text", _write_managed(_BOTH_SLOTS, {"voicePreference": "byok"})), {"key": "managed-t", "source": "managed"})
check("text still honors quarantine", resolve_credential("gemini-text", _write_managed(_BOTH_SLOTS, {"voicePreference": "byok", "quarantined": True})), {"key": "", "source": "none"})

# --- S3/Y4 read side: opaque generation reporting ----------------------------

# 21. managed entry generation is reported verbatim; legacy entries omit it
_reset_env()
check("managed generation reported verbatim", resolve_credential("gemini-voice", _write_managed({"gemini-voice": {"key": "managed-v", "generation": "cg1-abc"}})), {"key": "managed-v", "source": "managed", "credential_generation": "cg1-abc"})
check("legacy managed entry stays generationless", resolve_credential("gemini-voice", _write_managed({"gemini-voice": {"key": "managed-v"}})), {"key": "managed-v", "source": "managed"})

# 22. env voice key reports SUTANDO_VOICE_CREDENTIAL_GENERATION only when injected
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"; os.environ["SUTANDO_VOICE_CREDENTIAL_GENERATION"] = "cg1-injected"
check("env + injected generation reported", resolve_credential("gemini-voice", _missing()), {"key": "vk", "source": "env", "credential_generation": "cg1-injected"})
_reset_env(); os.environ["GEMINI_VOICE_API_KEY"] = "vk"
check("manual/legacy env key stays generationless (Y4/Z4)", resolve_credential("gemini-voice", _missing()), {"key": "vk", "source": "env"})

# 23. gemini-text env key never picks up the VOICE generation env var
_reset_env(); os.environ["GEMINI_API_KEY"] = "mk"; os.environ["SUTANDO_VOICE_CREDENTIAL_GENERATION"] = "cg1-injected"
check("text env key never carries the voice generation", resolve_credential("gemini-text", _missing()), {"key": "mk", "source": "env"})

# 24. credential_source_label: the design's user-facing vocabulary
_reset_env()
for _src, _label in (("managed", "managed"), ("env", "byok"), ("none", "none")):
    got_label = credential_source_label(_src)
    if got_label != _label:
        _failures.append(f"label({_src}): got {got_label} expected {_label}")
        print(f"  FAIL label({_src}): got {got_label} expected {_label}")
    else:
        print(f"  ok   label({_src}) == {_label}")

if _failures:
    print(f"\nFAIL — {len(_failures)} check(s) failed")
    sys.exit(1)
print("\nPASS — credential-resolver Python twin matches the TS canaries")
