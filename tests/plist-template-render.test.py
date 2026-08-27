#!/usr/bin/env python3
"""Contract tests for src/render_plist_template.py plus wiring tests for the
four launchd installers that delegate to it."""

import importlib.util
import pathlib
import re
import plistlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rpt", REPO / "src" / "render_plist_template.py")
rpt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpt)

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>test.job</string>
    <key>ProgramArguments</key>
    <array>
        <string>__PYTHON__</string>
        <string>__REPO__/src/cron-runner.py</string>
    </array>
    <key>WorkingDirectory</key><string>__WORKSPACE__</string>
</dict>
</plist>
"""

HOSTILE = ["/tmp/a&b", "/tmp/a<b>c", "/tmp/a|b", "/tmp/a'b", '/tmp/a"b', "/tmp/a\\b", "/tmp/plain"]

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


def rendered_paths(repo):
    """Render with *repo* and return (program-arg, working-dir) as the OS sees them."""
    with tempfile.TemporaryDirectory() as td:
        tpl = pathlib.Path(td) / "t.plist"
        tpl.write_text(TEMPLATE)
        dest = pathlib.Path(td) / "out.plist"
        rpt.render_to_file(str(tpl), str(dest), {
            "REPO": repo, "WORKSPACE": repo + "/ws", "PYTHON": "/usr/bin/python3",
        })
        d = plistlib.loads(dest.read_bytes())
        return d["ProgramArguments"][1], d["WorkingDirectory"]


print("== renderer: hostile values survive substitution ==")
for repo in HOSTILE:
    try:
        arg, wd = rendered_paths(repo)
        check(f"value {repo!r} round-trips",
              arg == repo + "/src/cron-runner.py" and wd == repo + "/ws",
              f"got arg={arg!r} wd={wd!r}")
    except Exception as exc:
        check(f"value {repo!r} round-trips", False, f"raised {type(exc).__name__}: {exc}")

print("== renderer: the specific silent-corruption case ==")
arg, _ = rendered_paths("/tmp/a&b")
check("ampersand does not re-emit the token", "__REPO__" not in arg, f"got {arg!r}")

print("== SHIPPED plist: --recover-core must stay OUT of the default args ==")
# Asserts the REAL shipped file, not the inline TEMPLATE: a fixture assertion would
# certify nothing about what ships. Rationale in the template's own #2246 note.
SHIPPED = REPO / "src" / "launchd" / "com.sutando.health-check-fallback.plist"


def program_arguments(template):
    with tempfile.TemporaryDirectory() as td:
        dest = pathlib.Path(td) / "out.plist"
        rpt.render_to_file(str(template), str(dest), {
            "REPO": "/tmp/r", "WORKSPACE": "/tmp/r/ws", "PYTHON": "/usr/bin/python3",
            "CLAUDE_CONFIG_DIR": "/tmp/r/.claude", "HOMEBREW_BIN": "/opt/homebrew/bin",
        })
        # XML forbids `--` inside comments, so expat rejects this file while plutil
        # accepts it; strip comments rather than depend on Apple's parser.
        xml = re.sub(rb"<!--.*?-->", b"", dest.read_bytes(), flags=re.S)
        return plistlib.loads(xml)["ProgramArguments"]


_args = program_arguments(SHIPPED)
check("shipped fallback plist renders and yields args", bool(_args), f"got {_args!r}")
check("--recover-core absent from shipped ProgramArguments",
      "--recover-core" not in _args, f"got {_args!r}")

# Control: plant the flag in a real template and drive the SAME loader, so the
# absence assertion is proven able to go red rather than true by construction.
with tempfile.TemporaryDirectory() as _td:
    _planted = pathlib.Path(_td) / "planted.plist"
    _src = SHIPPED.read_text()
    _open = _src.index("<array>", _src.index("<key>ProgramArguments</key>")) + len("<array>")
    _planted.write_text(_src[:_open] + "\n        <string>--recover-core</string>" + _src[_open:])
    _planted_args = program_arguments(_planted)
check("control: the loader sees a planted --recover-core",
      "--recover-core" in _planted_args, f"got {_planted_args!r}")
check("control: the absence assertion would FAIL on the planted template",
      not ("--recover-core" not in _planted_args), "absence check did not go red")

print("== renderer: refuses to publish a bad render ==")
with tempfile.TemporaryDirectory() as td:
    tpl = pathlib.Path(td) / "t.plist"
    tpl.write_text(TEMPLATE)
    dest = pathlib.Path(td) / "out.plist"
    dest.write_text("PREEXISTING")

    # Missing value -> leftover placeholder -> raise, destination untouched.
    try:
        rpt.render_to_file(str(tpl), str(dest), {"REPO": "/tmp/r"})
        check("missing value raises", False, "no exception")
    except rpt.RenderError as exc:
        check("missing value raises", "PYTHON" in str(exc) and "WORKSPACE" in str(exc), str(exc))
    check("destination untouched after failure", dest.read_text() == "PREEXISTING")

    # A template that cannot parse even when fully substituted.
    bad = pathlib.Path(td) / "bad.plist"
    bad.write_text("<plist><dict><key>x</key><string>__REPO__</string>")
    try:
        rpt.render_to_file(str(bad), str(dest), {"REPO": "/tmp/r"})
        check("unparseable render raises", False, "no exception")
    except rpt.RenderError as exc:
        check("unparseable render raises", "not a valid plist" in str(exc), str(exc))
    check("destination untouched after parse failure", dest.read_text() == "PREEXISTING")

print("== renderer: tolerates what launchd tolerates ==")
# A shipped template has "--" in a comment: launchd accepts it, Expat does not,
# so validating raw text would break that installer.
with tempfile.TemporaryDirectory() as td:
    tpl = pathlib.Path(td) / "t.plist"
    tpl.write_text(TEMPLATE.replace("<dict>", "<dict>\n    <!-- runs with --emit-task --quiet -->", 1))
    dest = pathlib.Path(td) / "out.plist"
    try:
        rpt.render_to_file(str(tpl), str(dest), {
            "REPO": "/tmp/r", "WORKSPACE": "/tmp/w", "PYTHON": "/usr/bin/python3"})
        check("double-dash in a comment still renders", True)
    except rpt.RenderError as exc:
        check("double-dash in a comment still renders", False, str(exc))

# Escaping means a value can never terminate a comment, so stripping comments
# before the parse check cannot be used to smuggle in malformed output.
out = rpt.render(TEMPLATE.replace("<dict>", "<dict>\n    <!-- __REPO__ -->", 1), {"REPO": "x-->y"})
check("value cannot break out of a comment", "-->y" not in out.split("<key>")[0], out[:0] or "")

print("== renderer: CLI exit codes ==")
# main() is called in-process: a subprocess would not be measured by the
# coverage gate, so the CLI paths would look untested.
with tempfile.TemporaryDirectory() as td:
    tpl = pathlib.Path(td) / "t.plist"
    tpl.write_text(TEMPLATE)
    dest = pathlib.Path(td) / "out.plist"
    argv0 = "render_plist_template.py"

    rc = rpt.main([argv0, str(tpl), str(dest), "REPO=/tmp/a&b",
                   "WORKSPACE=/tmp/w", "PYTHON=/usr/bin/python3"])
    check("CLI exits 0 on success", rc == 0, f"rc={rc}")
    # On disk the value is escaped; what matters is the parsed value.
    check("CLI wrote the destination",
          dest.exists()
          and plistlib.loads(dest.read_bytes())["ProgramArguments"][1].startswith("/tmp/a&b"))

    rc = rpt.main([argv0, str(tpl), str(dest), "REPO=/tmp/r"])
    check("CLI exits 1 on missing value", rc == 1, f"rc={rc}")

    rc = rpt.main([argv0, str(tpl)])
    check("CLI exits 2 on too few arguments", rc == 2, f"rc={rc}")

    rc = rpt.main([argv0, str(tpl), str(dest), "NOEQUALS"])
    check("CLI exits 2 on a malformed pair", rc == 2, f"rc={rc}")

    rc = rpt.main([argv0, str(tpl / "nope"), str(dest), "REPO=/tmp/r"])
    check("CLI exits 1 when the template is unreadable", rc == 1, f"rc={rc}")

    # The CLI is still the real entry point, so check the process exit code too.
    proc = subprocess.run([sys.executable, str(REPO / "src" / "render_plist_template.py"),
                           str(tpl), str(dest), "REPO=/tmp/r"], capture_output=True, text=True)
    check("process exits non-zero on failure", proc.returncode != 0, f"rc={proc.returncode}")

print("== renderer: rejects an empty token name ==")
try:
    rpt.render("__X__", {"": "v"})
    check("empty token raises", False, "no exception")
except rpt.RenderError:
    check("empty token raises", True)

print("== renderer: leaves no temp file behind when publishing fails ==")
with tempfile.TemporaryDirectory() as td:
    tpl = pathlib.Path(td) / "t.plist"
    tpl.write_text(TEMPLATE)
    # A directory at the destination makes os.replace fail after the temp write.
    dest = pathlib.Path(td) / "adir"
    dest.mkdir()
    try:
        rpt.render_to_file(str(tpl), str(dest), {
            "REPO": "/tmp/r", "WORKSPACE": "/tmp/w", "PYTHON": "/usr/bin/python3"})
        check("publish failure propagates", False, "no exception")
    except OSError:
        check("publish failure propagates", True)
    leftovers = [f.name for f in pathlib.Path(td).iterdir()
                 if f.name.startswith(".plist-render-")]
    check("temp file cleaned up on failure", not leftovers, str(leftovers))

print("== every installer supplies exactly its template's placeholders ==")
# A missing token now fails the install loudly, so a typo here is a real
# breakage rather than a leftover literal in the rendered plist.
PAIRS = {
    "install-cron-runner-launchd.sh": "com.sutando.cron-runner.plist",
    "install-health-check-launchd.sh": "com.sutando.health-check-fallback.plist",
    "install-sutando-app-launchd.sh": "com.sutando.menubar.plist",
    "install-credential-proxy-launchd.sh": "com.sutando.credential-proxy.plist",
}
for inst, tplname in PAIRS.items():
    itext = (REPO / "src" / inst).read_text()
    blk = itext[itext.index("render_plist_template.py"):]
    blk = blk[:blk.index("|| exit 1")]
    supplied = set(re.findall(r'"([A-Z0-9_]+)=', blk))
    needed = set(re.findall(r"__([A-Z0-9_]+)__",
                            (REPO / "src" / "launchd" / tplname).read_text()))
    check(f"{inst} covers {tplname}", not (needed - supplied),
          f"missing {sorted(needed - supplied)}")
    # Render the real template with hostile values end to end.
    dest = pathlib.Path(tempfile.gettempdir()) / f"rt-{tplname}"
    try:
        rpt.render_to_file(str(REPO / "src" / "launchd" / tplname), str(dest),
                           {k: f"/tmp/a&b<x>|y/{k.lower()}" for k in supplied})
        check(f"{tplname} renders with hostile values", True)
    except rpt.RenderError as exc:
        check(f"{tplname} renders with hostile values", False, str(exc))
    finally:
        dest.unlink(missing_ok=True)

print("== no installer invokes the renderer through a bare python3 ==")
# A bare python3 can be the Xcode-CLT stub: it satisfies an existence check and
# raises the install dialog when run, so the interpreter must be resolved.
for inst in ["install-cron-runner-launchd.sh", "install-health-check-launchd.sh",
             "install-sutando-app-launchd.sh", "install-credential-proxy-launchd.sh"]:
    text = (REPO / "src" / inst).read_text()
    bare = [ln for ln in text.splitlines()
            if "render_plist_template.py" in ln and re.search(r':-python3\}|"python3"|^\s*python3\s', ln)]
    check(f"{inst} resolves the interpreter", not bare, str(bare[:1]))
    blk = text[text.index("render_plist_template.py"):]
    blk = text[max(0, text.index("render_plist_template.py") - 400):text.index("render_plist_template.py")]
    check(f"{inst} uses a resolved interpreter var",
          "require_python" in blk or "PYTHON_BIN=" in text or "$PYTHON_BIN" in text,
          "no resolver near the call site")

print("== installers delegate (no installer renders plists itself) ==")
INSTALLERS = ["install-cron-runner-launchd.sh", "install-health-check-launchd.sh",
              "install-sutando-app-launchd.sh", "install-credential-proxy-launchd.sh"]
for name in INSTALLERS:
    text = (REPO / "src" / name).read_text()
    check(f"{name} calls the shared renderer", "render_plist_template.py" in text)
    # Match the substitution expression wherever it sits, so a one-line sed
    # fails too. Other sed uses in these scripts are fine.
    templating = [ln for ln in text.splitlines()
                  if re.search(r"s\|__[A-Z0-9_]+__\|", ln)]
    check(f"{name} has no sed plist templating", not templating, str(templating[:2]))

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all plist-template-render checks passed")
