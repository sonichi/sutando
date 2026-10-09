#!/usr/bin/env python3
"""fallback-config — adjust the credential proxy's model-fallback ladder.

The shipped defaults are the `config` block of this skill's manifest.json; this
tool writes the owner's overrides to <workspace>/hosts/<host>/quota-fallback-config.json
(per host, beside crons.json), which the running proxy re-reads on change (no
restart). Precedence the proxy applies: env > that override file > manifest > built-in.

  fallback-config.py show
  fallback-config.py set 7d level1 0.90        # 7-day window: level-1 requests demote above 90%
  fallback-config.py set 5h level2 0.98        # 5-hour hard line to the level-3 model
  fallback-config.py set 5h projection on|off  # projection rule for the 5-hour window
  fallback-config.py set 5h projection-limit 0.99
  fallback-config.py set low level1 0.70       # low-priority ladder (both windows)
  fallback-config.py set low-priority on|off
  fallback-config.py set hysteresis 0.02
  fallback-config.py set enabled on|off
  fallback-config.py set dm-min-interval-sec 900  # tier-change DMs: at most one per window per 15 min
  fallback-config.py unset 7d level1           # back to the manifest default
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1]
_SRC = _SKILL.parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from util_paths import host_label  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

P = "SUTANDO_QUOTA_FALLBACK_"
OVERRIDE_BASENAME = "quota-fallback-config.json"
MANIFEST = _SKILL / "manifest.json"

# Friendly spelling → key suffix → value kind. Mirrors CONFIG_KEYS in
# quota-fallback-config.ts; tests/quota-fallback-config-cli.test.py pins the two.
SETTINGS: dict[tuple[str, ...], tuple[str, str]] = {
    ("enabled",): ("ENABLED", "flag"),
    ("5h", "level1"): ("5H_LEVEL1", "frac"),
    ("5h", "level2"): ("5H_LEVEL2", "frac"),
    ("7d", "level1"): ("7D_LEVEL1", "frac"),
    ("7d", "level2"): ("7D_LEVEL2", "frac"),
    ("hysteresis",): ("HYSTERESIS", "frac"),
    ("5h", "projection"): ("5H_PROJECTION", "flag"),
    ("5h", "projection-limit"): ("5H_PROJECTION_LIMIT", "limit"),
    ("5h", "projection-lookback-sec"): ("5H_PROJECTION_LOOKBACK_SEC", "count"),
    ("5h", "projection-min-span-sec"): ("5H_PROJECTION_MIN_SPAN_SEC", "count"),
    ("5h", "projection-clear-samples"): ("5H_PROJECTION_CLEAR_SAMPLES", "count"),
    ("5h", "projection-clear-after-sec"): ("5H_PROJECTION_CLEAR_AFTER_SEC", "count"),
    ("5h", "projection-escalate-after-sec"): ("5H_PROJECTION_ESCALATE_AFTER_SEC", "count"),
    ("low-priority",): ("LOW_PRIORITY", "flag"),
    ("low", "level1"): ("LOW_LEVEL1", "frac"),
    ("low", "level2"): ("LOW_LEVEL2", "frac"),
    ("level2-model",): ("LEVEL2_MODEL", "model"),
    ("level3-model",): ("LEVEL3_MODEL", "model"),
    ("family-levels",): ("FAMILY_LEVELS", "text"),
    ("dm-min-interval-sec",): ("DM_MIN_INTERVAL_SEC", "count"),
}
_MODEL_RE = re.compile(r"^claude-([a-z]+)-[0-9][0-9a-z.-]*$")
_MODEL_LEVEL = {"LEVEL2_MODEL": 2, "LEVEL3_MODEL": 3}
KEYS = [P + suffix for suffix, _ in SETTINGS.values()]
_LADDERS = (("5H_LEVEL1", "5H_LEVEL2"), ("7D_LEVEL1", "7D_LEVEL2"), ("LOW_LEVEL1", "LOW_LEVEL2"))


def override_path(workspace: Path | None = None) -> Path:
    ws = workspace if workspace is not None else resolve_workspace()
    return Path(ws) / "hosts" / host_label() / OVERRIDE_BASENAME


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def manifest_config() -> dict[str, str]:
    cfg = _read_json(MANIFEST).get("config") or {}
    return {k: str(v) for k, v in cfg.items() if k.startswith(P)}


def read_override(path: Path) -> dict[str, str]:
    return {k: str(v) for k, v in _read_json(path).items() if k.startswith(P)}


def write_override(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(values, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def effective(manifest: dict[str, str], override: dict[str, str], env: dict[str, str]) -> dict[str, tuple[str, str]]:
    """key → (value, source) under the proxy's precedence."""
    out: dict[str, tuple[str, str]] = {}
    for k in KEYS:
        if env.get(k):
            out[k] = (env[k], "env")
        elif k in override:
            out[k] = (override[k], "override")
        elif k in manifest:
            out[k] = (manifest[k], "manifest")
    return out


def normalize(kind: str, raw: str) -> str:
    """Canonical string for one setting, or raise ValueError."""
    v = raw.strip().lower()
    if kind == "flag":
        if v in ("1", "true", "on", "yes"):
            return "1"
        if v in ("0", "false", "off", "no"):
            return "0"
        raise ValueError(f"expected on/off, got {raw!r}")
    if kind in ("frac", "limit"):
        try:
            x = float(v)
        except ValueError:
            raise ValueError(f"expected a number, got {raw!r}") from None
        hi = 1.5 if kind == "limit" else 1.0
        if not (0 < x < hi):
            raise ValueError(f"expected 0 < value < {hi}, got {raw!r}")
        return f"{x:.4g}"
    if kind == "count":
        if not v.isdigit():
            raise ValueError(f"expected a whole number of seconds/samples, got {raw!r}")
        return str(int(v))
    if kind == "model":
        if not _MODEL_RE.match(raw.strip()):
            raise ValueError(f"expected a Claude model id like claude-opus-5-5, got {raw!r}")
        return raw.strip()
    if not raw.strip():
        raise ValueError("expected a non-empty value")
    return raw.strip()


def family_levels(values: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (values.get(P + "FAMILY_LEVELS") or "").split(","):
        fam, _, lvl = part.strip().partition(":")
        if fam and lvl in ("1", "2", "3"):
            out[fam] = int(lvl)
    return out


def validate_merged(values: dict[str, str]) -> None:
    """After the merge: every ladder keeps level1 < level2, and each target model is a
    Claude id whose family sits at exactly its level (a typo is refused, never routed)."""
    for lo, hi in _LADDERS:
        a, b = values.get(P + lo), values.get(P + hi)
        if a is None or b is None:
            continue
        if float(a) >= float(b):
            raise ValueError(f"{lo.lower().replace('_', ' ')} ({a}) must be below {hi.lower().replace('_', ' ')} ({b})")
    fams = family_levels(values)
    for suffix, level in _MODEL_LEVEL.items():
        m = values.get(P + suffix)
        if m is None:
            continue
        match = _MODEL_RE.match(m)
        if not match or fams.get(match.group(1)) != level:
            raise ValueError(f"{suffix.lower().replace('_', ' ')} {m!r} is not a known level-{level} Claude model "
                             f"(families at level {level}: {', '.join(f for f, l in fams.items() if l == level) or 'none'})")


def resolve_setting(words: list[str]) -> tuple[str, str]:
    key = tuple(w.lower() for w in words)
    if key not in SETTINGS:
        choices = ", ".join(" ".join(k) for k in SETTINGS)
        raise ValueError(f"unknown setting {' '.join(words)!r}; one of: {choices}")
    suffix, kind = SETTINGS[key]
    return P + suffix, kind


def render(manifest: dict[str, str], override: dict[str, str], env: dict[str, str]) -> str:
    eff = effective(manifest, override, env)

    def g(suffix: str) -> tuple[str, str]:
        return eff.get(P + suffix, ("?", "-"))

    def pct(suffix: str) -> str:
        return f"{float(g(suffix)[0]) * 100:.0f}%" if g(suffix)[0] != "?" else "?"

    def on(suffix: str) -> str:
        return "on" if g(suffix)[0] == "1" else "off"

    lines = [
        f"fallback: {on('ENABLED')}   level2 model: {g('LEVEL2_MODEL')[0]}   level3 model: {g('LEVEL3_MODEL')[0]}",
        f"5h window: level1 > {pct('5H_LEVEL1')} → level 2 (used when projection is off/unavailable), "
        f"level2 > {pct('5H_LEVEL2')} → level 3 (hard line); projection {on('5H_PROJECTION')}, "
        f"limit {pct('5H_PROJECTION_LIMIT')} at reset",
        f"7d window: level1 > {pct('7D_LEVEL1')} → level 2, level2 > {pct('7D_LEVEL2')} → level 3",
        f"hysteresis: {pct('HYSTERESIS')}   low-priority ladder: {on('LOW_PRIORITY')} "
        f"(level1 > {pct('LOW_LEVEL1')}, level2 > {pct('LOW_LEVEL2')})",
        f"family levels: {g('FAMILY_LEVELS')[0]}   tier-change DMs: at most one per window per {g('DM_MIN_INTERVAL_SEC')[0]}s",
        "",
        "sources:",
    ]
    for k in KEYS:
        if k in eff:
            lines.append(f"  {k[len(P):]:<34} {eff[k][0]:<40} ({eff[k][1]})")
    return "\n".join(lines)


def main(argv: list[str], workspace: Path | None = None, env: dict[str, str] | None = None) -> int:
    env = dict(os.environ if env is None else env)
    path = override_path(workspace)
    manifest = manifest_config()
    override = read_override(path)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "show":
            print(render(manifest, override, env))
            print(f"\noverride file: {path}")
            return 0
        if cmd == "set":
            if len(rest) < 2:
                raise ValueError("usage: set <setting...> <value>")
            key, kind = resolve_setting(rest[:-1])
            new = dict(override)
            new[key] = normalize(kind, rest[-1])
            validate_merged({**manifest, **new})
            write_override(path, new)
            print(f"set {key} = {new[key]} (override file: {path})\n")
            print(render(manifest, new, env))
            return 0
        if cmd == "unset":
            key, _ = resolve_setting(rest)
            new = {k: v for k, v in override.items() if k != key}
            write_override(path, new)
            print(f"unset {key} — back to {manifest.get(key, 'the built-in default')}\n")
            print(render(manifest, new, env))
            return 0
        raise ValueError(f"unknown command {cmd!r}; one of: show, set, unset")
    except ValueError as e:
        print(f"fallback-config: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
