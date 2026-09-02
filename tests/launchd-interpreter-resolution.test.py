#!/usr/bin/env python3
"""launchd bridge wrappers/installers must resolve the interpreter, not hardcode it.

qingyun's #2068 [P1] CR: the launch wrappers/installers reintroduced fixed
`/opt/homebrew`, `/usr/local`, `~/.claude` fallbacks — the repo's no-hardcoded-path
rule. The required shape (proven here):

  1. The INSTALLER resolves the interpreter's bin dir from its own PATH
     (`command -v python3` → dirname) — host-agnostic, no arch/user literal.
  2. That resolved dir is substituted into the plist PATH as __BREW_BIN__, so the
     launchd job runs with a working `python3` on PATH.
  3. The committed WRAPPERS carry NO clone-, arch-, or user-specific interpreter
     fallback probe (no `/opt/homebrew`, no `/usr/local/bin/python3`); they rely
     on the plist-provided PATH.

Run: python3 tests/launchd-interpreter-resolution.test.py   (exit 0/1)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GW_INSTALL = REPO / "src" / "install-gateway-bridge-launchd.sh"
CH_INSTALL = REPO / "src" / "install-channel-bridge-launchd.sh"
GW_WRAPPER = REPO / "src" / "launchd" / "gateway-bridge-wrapper.sh"
CH_WRAPPER = REPO / "src" / "launchd" / "channel-bridge-wrapper.sh"
GW_PLIST = REPO / "src" / "launchd" / "com.sutando.gateway-bridge.plist"

_fail = 0


def check(cond: bool, msg: str) -> None:
    global _fail
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fail += 1


# 1. Installer resolves the interpreter dir behaviorally = dirname(command -v python3).
#    Extract resolve_brew_bin from the gateway installer and run it in isolation.
_lines, _grab = [], False
for ln in GW_INSTALL.read_text().splitlines():
    if ln.startswith("resolve_brew_bin()"):
        _grab = True
    if _grab:
        _lines.append(ln)
    if _grab and ln == "}":
        break
with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as _tf:
    _tf.write("\n".join(_lines) + "\nresolve_brew_bin\n")
    _fn_path = _tf.name
got = subprocess.run(["bash", _fn_path], capture_output=True, text=True).stdout.strip()
Path(_fn_path).unlink(missing_ok=True)
want = str(Path(shutil.which("python3") or "/usr/bin/python3").parent)
check(got == want,
      f"install-gateway resolve_brew_bin() returns the real interpreter dir "
      f"(got {got!r}, want {want!r} = dirname of `command -v python3`)")

# 2. Both installers resolve via `command -v python3`, not a hardcoded dir probe.
for f in (GW_INSTALL, CH_INSTALL):
    src = f.read_text()
    check("command -v python3" in src,
          f"{f.name} resolves the interpreter via `command -v python3`")
    check("/opt/homebrew" not in src,
          f"{f.name} carries no /opt/homebrew literal")

# 3. The gateway plist template's PATH is built from the substituted __BREW_BIN__.
plist = GW_PLIST.read_text()
path_line = next((ln for ln in plist.splitlines()
                  if "<string>" in ln and "/usr/bin" in ln and ":" in ln), "")
check("__BREW_BIN__" in path_line,
      "gateway plist PATH uses the install-substituted __BREW_BIN__ (not a fixed literal)")
check("/opt/homebrew" not in plist,
      "gateway plist template carries no /opt/homebrew literal")

# 4. The committed wrappers carry no clone/arch/user-specific interpreter fallback.
for f in (GW_WRAPPER, CH_WRAPPER):
    src = f.read_text()
    check("/opt/homebrew" not in src,
          f"{f.name} has no /opt/homebrew interpreter literal")
    check(not re.search(r"/usr/local/bin/python3", src),
          f"{f.name} has no /usr/local/bin/python3 fallback literal")

if _fail:
    print(f"\nFAIL — {_fail} check(s)")
    sys.exit(1)
print("\nPASS — launchd interpreter resolution (no hardcoded fallback)")
