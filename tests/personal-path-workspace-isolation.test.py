#!/usr/bin/env python3
"""`personal_path(name, workspace)` must not hand back a path outside `workspace`.

The failure this pins (#2452) is not a crash — it is a caller believing it is
isolated when it is not. `personal_path` resolved the not-yet-existing case from
`$SUTANDO_MEMORY_DIR`, which knows nothing about the `workspace` argument, so a
test that passed a fresh tmpdir got back a path in the operator's real,
**vault-synced** memory tree and wrote there. ALPHA/BRAVO fixtures reached Chi's
vault exactly that way.

Passing a tmpdir is not isolation. The property is that the RESOLVED path is
inside it, which is what these assert.

Deliberately NOT asserted: that an EXISTING legacy `machine-<host>/<file>` is
ignored when an explicit workspace is passed. The read probe is the migration
fallback and is still load-bearing — removing it would strand readers on files
that exist right now. See the PR body for that boundary.
"""
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from util_paths import personal_path, _host_label  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + label + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(label)


ws = pathlib.Path(tempfile.mkdtemp(prefix="pp-iso-"))
host = _host_label()

# --- the bug: a file that does not exist anywhere -------------------------
for name in ("brand-new-file.md", "pending-questions-unique-xyz.md", "stand-identity.json"):
    p = pathlib.Path(personal_path(name, ws))
    check(
        f"A {name} resolves INSIDE the passed workspace",
        str(p).startswith(str(ws)),
        f"escaped to {p}",
    )

# It should land in the documented canonical per-host home, not the workspace
# root — otherwise a later reader probing hosts/<host>/ would miss it.
p = pathlib.Path(personal_path("brand-new-file.md", ws))
check(
    "B and specifically under hosts/<host>/, the canonical per-host home",
    p == ws / "hosts" / host / "brand-new-file.md",
    str(p),
)

# --- controls: existing files still resolve, in preference order ----------
hd = ws / "hosts" / host
hd.mkdir(parents=True, exist_ok=True)
(hd / "exists-here.md").write_text("x")
check(
    "C1 CONTROL an existing hosts/<host>/ file is returned",
    pathlib.Path(personal_path("exists-here.md", ws)) == hd / "exists-here.md",
)

(ws / "at-root.md").write_text("x")
check(
    "C2 CONTROL an existing workspace-root file is still found",
    pathlib.Path(personal_path("at-root.md", ws)) == ws / "at-root.md",
)

assets = ws / "assets"
assets.mkdir(exist_ok=True)
(assets / "stand-avatar.png").write_bytes(b"x")
check(
    "C3 CONTROL the assets/ special case is preserved",
    pathlib.Path(personal_path("stand-avatar.png", ws)) == assets / "stand-avatar.png",
)

# --- ambient resolution must be UNCHANGED --------------------------------
# No workspace argument = the caller accepted ambient resolution, so the
# env-based private dir stays correct. Regressing this would relocate every
# existing per-host file on every host.
amb = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0,'src');"
     "from util_paths import personal_path, _private_machine_dir;"
     "p=personal_path('brand-new-ambient.md');"
     "d=_private_machine_dir();"
     "print('MATCH' if d is not None and str(p)==str(d/'brand-new-ambient.md') else f'DIFF {p}')"],
    capture_output=True, text=True, cwd=REPO,
)
check(
    "D ambient call (no workspace arg) still resolves to the private machine dir",
    "MATCH" in amb.stdout,
    (amb.stdout + amb.stderr).strip()[:160],
)

print()
if failures:
    print(f"FAIL — {len(failures)} check(s) failed")
    sys.exit(1)
print("PASS — personal_path workspace isolation")
