"""Canonical loader for `sutando.config.json` / `sutando.config.local.json`.

The config file is the M0 contract for where Sutando's per-user runtime state
lives. This module is the SINGLE place that reads those files. All path-
resolution callers (`resolve_workspace`, vault sync engine, future services)
go through `load_config()` so that the contract is enforced in one place
rather than re-implemented per-service.

Resolution order (highest layer wins):
  1. `$SUTANDO_WORKSPACE` env var (legacy escape hatch; warn on use)
  2. `sutando.config.local.json` (per-clone override, gitignored)
  3. `sutando.config.json` (tracked defaults at repo root)
  4. Baked-in default (`{repo_root}/workspace`)

Deep-merge semantics: `.local.json` is merged over the tracked defaults.
Dicts deep-merge; arrays REPLACE wholesale (not unioned). This matches the
`.example` file pattern documented in the docstrings — users override only
the keys they want.

`${REPO_DIR}` in any string value expands to the directory containing the
config file (== git toplevel for a sane checkout). Other ${VAR}s are not
expanded; this is config, not shell.

Comment convention: any top- or nested-level key whose name starts with `_`
is treated as a comment and stripped before validation. This lets the
`.example` file carry inline documentation without polluting the runtime
schema.
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

# --------------------------------------------------------------------------- #
#  File discovery                                                             #
# --------------------------------------------------------------------------- #

_CONFIG_FILENAME = "sutando.config.json"
_LOCAL_FILENAME = "sutando.config.local.json"

# Known top-level keys the loader understands. The matching JSON Schema
# (docs/sutando-config.schema.json) declares `additionalProperties: false`
# to teach IDEs strictness for autocomplete; the loader itself stays
# lenient (warn-only) so users with experimental or scratch keys don't
# break. Per Mini's review #8 on PR #1395.
_KNOWN_TOP_LEVEL_KEYS = {"workspace", "vault"}


def _find_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from `start` (default: this module's parent) until we find
    a directory containing `sutando.config.json`. Returns None if not found
    within 6 hops — that's deep enough for any sane checkout layout.

    We anchor on the config file rather than `.git/` because:
      - app bundles + symlinked installs may lack `.git/` in the resolved path
      - users running outside a git checkout (CI, tarball install) still get
        a working loader as long as the config sits beside it

    Emits a one-line stderr diagnostic on miss (gated by `SUTANDO_DEBUG=1` to
    keep happy-path noise out of normal runs). Helps users diagnose "why is
    Sutando using the baked-in default" without strace, per Mini's review #3
    on PR #1395.
    """
    initial = (start or Path(__file__).resolve().parent).resolve()
    cur = initial
    for _ in range(6):
        if (cur / _CONFIG_FILENAME).is_file():
            return cur
        if cur == cur.parent:  # filesystem root
            break
        cur = cur.parent
    if os.environ.get("SUTANDO_DEBUG"):
        print(
            f"sutando config: _find_repo_root walked 6 hops from {initial} "
            f"and did not find {_CONFIG_FILENAME}; falling back to baked-in default.",
            file=sys.stderr,
        )
    return None


# --------------------------------------------------------------------------- #
#  JSON loading + comment stripping                                           #
# --------------------------------------------------------------------------- #


def _strip_comments(obj: Any) -> Any:
    """Recursively drop dict keys whose name starts with `_` (comment convention).

    Lists are walked element-wise; scalars pass through. Returns a new structure
    — the input is not mutated.
    """
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _load_json(path: Path) -> Dict[str, Any]:
    """Read + parse a JSON file, strip comment keys, return the resulting dict.

    Empty file is treated as `{}` (defensive: a freshly-touched `.local.json`
    is valid). Parse errors raise with a clear message.
    """
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"sutando config: failed to parse {path}: {e.msg} at line {e.lineno} col {e.colno}"
        ) from e
    if not isinstance(data, dict):
        raise RuntimeError(
            f"sutando config: {path} top-level must be a JSON object, got {type(data).__name__}"
        )
    return _strip_comments(data)


# --------------------------------------------------------------------------- #
#  Deep merge + variable expansion                                            #
# --------------------------------------------------------------------------- #


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `override` into `base`. Dicts merge; everything else
    (lists, scalars, None) is REPLACED by the override.

    Returns a new dict; inputs are not mutated.
    """
    out: Dict[str, Any] = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _expand_vars(obj: Any, repo_dir: Path) -> Any:
    """Expand `${REPO_DIR}` in every string value of the config tree.

    Only `${REPO_DIR}` is recognized — other variables pass through untouched
    (the loader is not a shell). Walks dicts + lists; scalars other than strings
    are returned as-is.
    """
    token = "${REPO_DIR}"
    if isinstance(obj, str):
        return obj.replace(token, str(repo_dir))
    if isinstance(obj, dict):
        return {k: _expand_vars(v, repo_dir) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_vars(v, repo_dir) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
#  Top-level loader                                                           #
# --------------------------------------------------------------------------- #


# Cache the resolved config per-process so repeated calls don't re-read disk
# or re-print warnings. None means "not yet loaded"; the cached value is the
# merged + expanded dict.
_CACHE: Optional[Dict[str, Any]] = None
_CACHE_REPO_ROOT: Optional[Path] = None
_LEGACY_ENV_WARN_PRINTED = False
_DOTENV_DRIFT_WARN_PRINTED = False
_UNKNOWN_KEYS_WARN_PRINTED = False


def _warn_unknown_top_level_keys(cfg: Dict[str, Any], path: Path) -> None:
    """Emit a one-time stderr warning if the resolved config has top-level
    keys the loader doesn't recognize. Helps catch typos like `workspce`
    while leaving experimental keys harmlessly ignored. Per Mini's #8.
    """
    global _UNKNOWN_KEYS_WARN_PRINTED
    if _UNKNOWN_KEYS_WARN_PRINTED:
        return
    extras = sorted(k for k in cfg if k not in _KNOWN_TOP_LEVEL_KEYS)
    if not extras:
        return
    _UNKNOWN_KEYS_WARN_PRINTED = True
    print(
        f"sutando config: {path} has top-level keys the loader does not read: "
        f"{', '.join(repr(k) for k in extras)}. Known keys: "
        f"{sorted(_KNOWN_TOP_LEVEL_KEYS)!r}. Typo? Or experimental key — "
        f"the loader will ignore it either way.",
        file=sys.stderr,
    )


def _reset_cache_for_tests() -> None:
    """Test-only: clear the per-process cache so unit tests can swap configs.

    Production code never calls this. Importing in tests is intentional —
    keeps the public surface honest.
    """
    global _CACHE, _CACHE_REPO_ROOT, _LEGACY_ENV_WARN_PRINTED, _DOTENV_DRIFT_WARN_PRINTED, _UNKNOWN_KEYS_WARN_PRINTED
    _CACHE = None
    _CACHE_REPO_ROOT = None
    _LEGACY_ENV_WARN_PRINTED = False
    _DOTENV_DRIFT_WARN_PRINTED = False
    _UNKNOWN_KEYS_WARN_PRINTED = False


def load_config(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Load + merge sutando config from disk. Memoized per-process.

    `repo_root` is the directory holding `sutando.config.json`; defaults to
    the result of `_find_repo_root()`. Pass an explicit Path in tests.

    Returns the deep-merged, ${REPO_DIR}-expanded config dict. Missing files
    are tolerated (defaults file optional too, in which case caller falls
    through to the hardcoded resolver default — see `resolve_workspace`).

    Raises `RuntimeError` only for parse errors (malformed JSON) or
    structurally-invalid top-level (non-object).
    """
    global _CACHE, _CACHE_REPO_ROOT
    if _CACHE is not None and (repo_root is None or repo_root == _CACHE_REPO_ROOT):
        return _CACHE

    root = repo_root or _find_repo_root()
    if root is None:
        # No config file anywhere in the search path; return an empty config.
        # Callers will fall back to baked-in defaults.
        _CACHE = {}
        _CACHE_REPO_ROOT = None
        return _CACHE

    defaults = _load_json(root / _CONFIG_FILENAME)
    overrides = _load_json(root / _LOCAL_FILENAME)
    merged = _deep_merge(defaults, overrides)
    expanded = _expand_vars(merged, root)

    _CACHE = expanded
    _CACHE_REPO_ROOT = root
    _warn_unknown_top_level_keys(expanded, root / _CONFIG_FILENAME)
    return expanded


# --------------------------------------------------------------------------- #
#  Public path resolvers                                                      #
# --------------------------------------------------------------------------- #


_HARDCODED_WORKSPACE_DEFAULT_REL = "workspace"  # relative to repo root


def resolve_workspace(repo_root: Optional[Path] = None) -> Path:
    """Resolve the workspace directory per the canonical contract.

    Order:
      1. `$SUTANDO_WORKSPACE` env var (legacy escape hatch; warn once).
      2. `sutando.config.{json,local.json}` → `workspace.path` (deep-merged).
      3. `{repo_root}/workspace` baked-in default.

    Does NOT create the directory; the caller decides. Returns an absolute
    `Path`.

    The env-var warning fires at most once per process so log noise is
    bounded. The warning is informational — env still wins, so existing
    users don't break on the switch. Migration path: copy the env value
    into `sutando.config.local.json` → `workspace.path` and unset the env.
    """
    global _LEGACY_ENV_WARN_PRINTED, _DOTENV_DRIFT_WARN_PRINTED

    env_val = os.environ.get("SUTANDO_WORKSPACE", "").strip()
    if env_val:
        if not _LEGACY_ENV_WARN_PRINTED:
            _LEGACY_ENV_WARN_PRINTED = True
            print(
                f"sutando config: $SUTANDO_WORKSPACE is set ({env_val!r}); honoring "
                f"as legacy escape hatch. To silence, move the value into "
                f"`sutando.config.local.json` under `workspace.path` and unset the env.",
                file=sys.stderr,
            )
        # Preserve the pre-cutover semantic: only resolve when the env value
        # is RELATIVE (anchor to cwd, surface the misconfig). For absolute
        # paths, return as-is so symlinked paths like /tmp -> /private/tmp on
        # macOS don't get rewritten — that broke the workspace_default test
        # that compared paths string-equality.
        target = Path(env_val).expanduser()
        if not target.is_absolute():
            anchored = (Path.cwd() / target).resolve()
            print(
                f"sutando config: $SUTANDO_WORKSPACE={env_val!r} is relative — "
                f"anchored to {anchored} (CWD-dependent; set an absolute path to "
                f"avoid cross-process drift).",
                file=sys.stderr,
            )
            return anchored
        return target

    cfg = load_config(repo_root)
    root = repo_root or _CACHE_REPO_ROOT
    cfg_path = (cfg.get("workspace") or {}).get("path")
    if cfg_path:
        resolved = Path(cfg_path).expanduser().resolve()
    elif root is None:
        # No config and no repo root — fall back to the historic default so we
        # don't break ad-hoc invocations outside a checkout. workspace_default.py
        # has the same behavior; preserving it here avoids regressions on
        # services that import this loader before the cutover.
        resolved = Path.home().joinpath(".sutando", "workspace").resolve()
    else:
        resolved = (root / _HARDCODED_WORKSPACE_DEFAULT_REL).resolve()

    # M0 .env-drift warning: if the env var is UNSET but `.env` declares one,
    # the user has a stale `.env` line that the new contract bypasses. Surface
    # once per process so they migrate to `sutando.config.local.json` and
    # delete the `.env` line. Loader does NOT honor `.env` values — only the
    # process env. This is the "Detect SUTANDO_WORKSPACE from .env file and
    # give warning" bullet from Milestone 0.
    if not _DOTENV_DRIFT_WARN_PRINTED:
        _DOTENV_DRIFT_WARN_PRINTED = True
        dotenv_val = detect_env_workspace_in_dotenv(repo_root)
        if dotenv_val and Path(dotenv_val).resolve() != resolved:
            print(
                f"sutando config: .env declares SUTANDO_WORKSPACE={dotenv_val!r}, but "
                f"resolved workspace is {resolved} (config-driven). The .env line is "
                f"NOT honored by the new loader — move the value to "
                f"`sutando.config.local.json` under `workspace.path` and delete the .env "
                f"line, or `source .env` before launching processes that need the override.",
                file=sys.stderr,
            )

    return resolved


def resolve_vault(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Return the resolved vault subtree from config.

    Schema (after deep-merge + expansion):
      {
        "enabled": bool,
        "remote_url": str,
        "sync": {"include": [str, ...], "exclude": [str, ...]},
        "interval_seconds": int,
      }

    Missing config returns `{"enabled": False, ...}` with safe defaults so
    callers can branch on `cfg["enabled"]` without KeyError. The vault sync
    engine (M2) is the primary consumer.
    """
    cfg = load_config(repo_root)
    vault = dict(cfg.get("vault") or {})
    vault.setdefault("enabled", False)
    vault.setdefault("remote_url", "")
    sync = dict(vault.get("sync") or {})
    sync.setdefault("include", [])
    sync.setdefault("exclude", [])
    vault["sync"] = sync
    vault.setdefault("interval_seconds", 1800)
    return vault


def detect_env_workspace_in_dotenv(repo_root: Optional[Path] = None) -> Optional[str]:
    """Scan the repo's `.env` for `SUTANDO_WORKSPACE=` and return the value if
    found, else None. Used by the startup banner to warn users that their .env
    declares a workspace path that the loader is bypassing in favor of config.

    Best-effort: silent on file-not-found or read errors. Strips surrounding
    quotes and expands `~`.

    Triggers the .env warning bullet from Milestone 0.
    """
    root = repo_root or _find_repo_root()
    if root is None:
        return None
    env_file = root / ".env"
    if not env_file.is_file():
        return None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            key, _, val = s.partition("=")
            if key.strip() != "SUTANDO_WORKSPACE":
                continue
            v = val.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            return str(Path(v).expanduser()) if v else None
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------- #
#  CLI: `python3 -m src.sutando_config` prints the merged config for debug    #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover — operator-facing CLI
    # All diagnostic output goes to stdout so a caller can suppress it with
    # `>/dev/null` while still seeing the WARNINGS that resolve_workspace()
    # itself emits to stderr (legacy env, .env drift, parse errors). That
    # gives `bash src/startup.sh` a clean "warnings only" surface from this
    # call.
    cfg = load_config()
    workspace = resolve_workspace()  # emits stderr warnings if any
    env_val = detect_env_workspace_in_dotenv()
    print(json.dumps(cfg, indent=2, default=str))
    print(f"\n# resolved workspace: {workspace}")
    if env_val:
        print(f"# .env declares SUTANDO_WORKSPACE={env_val!r}")
