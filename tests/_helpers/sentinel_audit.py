"""Run a test file, or `-c` code, under an audit hook that records opens, child environments,
and this process's environment whenever a gateway, Hatch, room_ops, bee-actions or credential module
is imported.

    python3 sentinel_audit.py <out.json> <file.py | -c CODE> [args...]

The record is written to <out.json> at exit, even when the target exits non-zero."""
import json
import os
import re
import runpy
import sys

out_path, target, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
opens: "list[str]" = []
child_envs: "list[dict]" = []
import_envs: "list[dict]" = []
GATEWAY_MODULE = re.compile(
    r"remote_gateway_bridge|gateway_credential|adapters\.hatch|bee_actions|(^|\.)_gateway$|(^|\.)room_ops$")


def _hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        opens.append(os.path.abspath(os.fsdecode(args[0])))
    elif event == "subprocess.Popen" and len(args) > 3:
        child_envs.append(dict(args[3] if args[3] is not None else os.environ))
    elif event in ("os.exec", "os.posix_spawn") and len(args) > 2:
        child_envs.append(dict(args[2] if args[2] is not None else os.environ))
    elif event == "import" and isinstance(args[0], str) and GATEWAY_MODULE.search(args[0]):
        import_envs.append(dict(os.environ))


def _write():
    with open(out_path, "w") as f:
        json.dump({"opens": opens, "child_envs": child_envs, "import_envs": import_envs,
                   "final_env": dict(os.environ)}, f)


_real_exit, _pid = os._exit, os.getpid()


def _exit_after_write(code):
    if os.getpid() == _pid:  # a forked child's exit must not overwrite the parent's record
        _write()
    _real_exit(code)


os._exit = _exit_after_write
sys.addaudithook(_hook)
code = 0
try:
    if target == "-c":
        sys.argv = ["-c", *rest[1:]]
        exec(compile(rest[0], "<sentinel-audit>", "exec"), {"__name__": "__main__"})
    else:
        sys.argv = [target, *rest]
        sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
        runpy.run_path(target, run_name="__main__")
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
finally:
    _write()
sys.exit(code)
