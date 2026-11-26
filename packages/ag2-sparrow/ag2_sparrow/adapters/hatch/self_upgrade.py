"""Self-upgrade for a Hatch install: move ag2-sparrow up to the backend's target_version.

The keepawake run decides (`maybe_start`) and logs the decision on its line. First, under
the upgrade lock, it settles an attempt still marked started after 30 minutes: crashed,
with the snapshot restored if another version was left installed. Then at most one
attempt per target version, and only up; never when min_version is above the target,
without the target's sha256 map (wheel filename -> sha256), with AUTO_UPGRADE=0, or for
an editable or URL install. The upgrade runs detached (`run_upgrade`) under
STATE_DIR/upgrade.lock, which keepawake also holds across its probe and restart, and
first re-reads the installed version: a target not above it is skipped. Its order is
fetch, snapshot, stop, install, start, verify: the pinned wheel is downloaded and its
own RECORD and entry points name every path the target installs (wheel_inventory);
ownership is the old RECORD plus that inventory, never a name that merely appeared.
Every file of the package's (those, and everything in an ag2_sparrow* entry of the
site dir) is copied with its sha256 to STATE_DIR/rollback/<version>/ before anything
changes; Sparrow is stopped through the manager that owns it and proven gone
(keepalive.quiesce_sparrow; a hand-made unit is never touched); `pip install --no-deps
--only-binary=:all: --require-hashes --no-index` puts the fetched wheel into this
interpreter; `pip check` must run and report no new problem naming ag2-sparrow; install is re-run with --no-start (which never
starts the service); and Sparrow is started through the same manager and must import the
target and reconnect. A reply posted while Sparrow is stopped waits in the results dir
and its outbox, which the upgrade never touches, and is delivered once Sparrow starts.
Any failure restores the snapshot (no index, no network): every file of the package's
the snapshot does not list is removed, the copies are put back, and the namespace must
hash equal to the snapshot, whatever RECORD says afterwards (an unknown file in an
ag2_sparrow* entry is kept and fails that check); then install is re-run and Sparrow
started. The outcome is "rolled-back" only when all of that held and Sparrow reconnects,
else "restore-failed" with what is installed; a stop that cannot be proven aborts the
restore before any file changes. Either leaves the health reason upgrade-failed. The
attempt record is a journal: its phase (snapshot, quiesced, installing, installed,
restoring) is written before each step, so an attempt found dead (the lock free) is
settled from it: nothing installed means crashed (Sparrow restarted if it was stopped), a
namespace that differs from the snapshot or an interrupted restore is restored, and an
interrupted restore resumes on the next run. While an attempt is held (recovery_blocked:
restore-failed, a mutating phase, or a journal that exists but cannot be read) nothing
starts Sparrow or an upgrade, and a failed version is never imported; the owner's install
lifts a restore-failed hold (release_hold) only once every installed file verifies
against its RECORD (verify_installed). A changed hook definition (install's REQUIRED
hook: line) is never applied: the hook wakes the agent once with it."""
from __future__ import annotations

import argparse
import base64
import configparser
import contextlib
import csv
import glob
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from ... import __version__, _dirs, file_lock
from . import cli as _cli, keepalive as _keepalive

PACKAGE = "ag2-sparrow"
STATE_NAME = "hatch-upgrade.json"
NOTICE_NAME = "hatch-wake-notice"
LOG_NAME = "hatch-upgrade.log"
LOCK_NAME = "upgrade.lock"
ROLLBACK_DIR = "rollback"
WHEELS_DIR = "wheels"
UPGRADE_FAILED = _keepalive.UPGRADE_FAILED
# A "started" attempt this old, with no upgrade holding the lock, crashed.
STALE_START_S = 30 * 60
# Phases written under the lock; a dead attempt in one of these may have changed files.
MUTATING_PHASES = ("installing", "installed", "restoring")
SUMMARY_OUTCOMES = ("rolled-back", "restore-failed")
# An attempt left in one of these was saved with a `failed` summary of it, in the same write.
RECOVERY_OUTCOMES = ("crashed",) + SUMMARY_OUTCOMES
MANAGERS = ("unit", "detached")
# A direct rerun ending in one of these keeps an older failure summary; others replace it.
# None of them is a summary outcome, so a summary's own attempt is told from a rerun clocklessly.
KEEPS_FAILED = frozenset(("started", "unsupported-install", "not-above-installed", "no-download",
                          "no-inventory", "no-snapshot", "no-pip-check", "stop-failed"))
TERMINAL = frozenset(("crashed", "rolled-back", "restore-failed", "ok", "not-started",
                      "unsupported-install", "not-above-installed", "no-download",
                      "no-inventory", "no-snapshot", "no-pip-check", "stop-failed"))
# Only a success rewrites `installed`, and maybe_start never reruns a target it recorded.
KEEPS_INSTALLED = TERMINAL - {"ok", "not-started"} | {"started"}
# How far ahead of this clock a journal time may be; later, a started attempt would not age.
CLOCK_SKEW_S = 300
PIP_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 300
SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(
    r"([0-9]{1,9}(?:\.[0-9]{1,9}){0,9})(?:(a|b|rc)([0-9]{1,9}))?"
    r"(?:\.post([0-9]{1,9}))?(?:\.dev([0-9]{1,9}))?")
_PRE = {"a": 0, "b": 1, "rc": 2}
_OWN_RE = re.compile(r"ag2[-_.]sparrow", re.I)


def parse_version(text) -> "tuple | None":
    """A sortable key for a plain PEP 440 version; None for anything else, which is never
    passed to pip."""
    m = _VERSION_RE.fullmatch(text) if isinstance(text, str) and len(text) <= 64 else None
    if m is None:
        return None
    release = [int(p) for p in m.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre, post, dev = m.group(2), m.group(4), m.group(5)
    # A bare .devN sorts before every pre-release of its release.
    stage = (_PRE[pre], int(m.group(3))) if pre else ((-1, 0) if dev and post is None else (3, 0))
    return (tuple(release), stage, int(post) if post else -1,
            int(dev) if dev else float("inf"))


def state_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_NAME


def notice_path(state_dir: Path) -> Path:
    """The hook wakes the agent once with this file's text, then removes it."""
    return Path(state_dir) / NOTICE_NAME


class JournalError(ValueError):
    """The journal exists but cannot be read as one; nothing may assume no attempt is pending."""


def load_state(state_dir: Path) -> dict:
    """The attempt journal; empty only when the file is absent, else JournalError."""
    path = state_path(state_dir)
    try:
        state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_members)
    except FileNotFoundError:
        return {"attempted": {}}
    except (OSError, ValueError, RecursionError) as e:
        raise JournalError(f"the upgrade journal {path} cannot be read ({type(e).__name__})") from None
    try:
        well_formed = _well_formed_journal(state, time.time() + CLOCK_SKEW_S)
    except Exception:  # noqa: BLE001 - a validator failure of any kind is a hold, never a crash
        well_formed = False
    if not well_formed:
        raise JournalError(f"the upgrade journal {path} cannot be read (not a journal)")
    return state


def _unique_members(pairs) -> dict:
    """json's last-wins merge would hide an earlier member from the validator."""
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise ValueError("a duplicate member name")
    return dict(pairs)


# Every (outcome, phase) a writer leaves, with the manager it carries: _phase "quiesced" saves it,
# a restore may start from an attempt that never quiesced. `hook` is saved only with "ok".
NO, YES, MAYBE = "no manager", "a manager", "a manager or none"
_ATTEMPT_VARIANTS = {
    ("started", None): NO, ("started", "snapshot"): NO,
    ("started", "quiesced"): YES, ("started", "installing"): YES, ("started", "installed"): YES,
    ("started", "restoring"): MAYBE,
    ("ok", "ok"): YES,
    ("crashed", "crashed"): MAYBE, ("rolled-back", "rolled-back"): MAYBE,
    ("restore-failed", "restore-failed"): MAYBE, ("restore-failed", "acknowledged"): MAYBE,
    **{(o, o): NO for o in ("not-started", "unsupported-install", "not-above-installed",
                            "no-download", "no-inventory", "no-snapshot", "no-pip-check",
                            "stop-failed")},
}


# Past the gate in _upgrade_locked that parses the installed version and checks it is below
# the target (a snapshot needs it), `from` is a version; a rollback may start from any version.
_BELOW_TARGET = frozenset(
    {("started", p) for p in ("snapshot", "quiesced", "installing", "installed")}
    | {(o, o) for o in ("ok", "no-pip-check", "stop-failed", "not-started")})
_PARSED_ORIGIN = frozenset(
    {("started", "restoring"), ("restore-failed", "acknowledged")}
    | {(o, o) for o in ("rolled-back", "restore-failed")})


def _well_formed_journal(state, latest: float) -> bool:
    return _journal_fault(state, latest) is None


def _journal_fault(state, latest: float) -> "str | None":
    """Why the journal is not one save_state writes, or None: an `attempted` map of
    well-formed attempts, optional `failed` and `installed` summaries of attempts in it, and
    nothing else; no time in it later than `latest`. A recovery attempt keeps its summary
    until a success descending from it drops it, which no held restore-failed allows."""
    if not isinstance(state, dict) or not isinstance(state.get("attempted"), dict):
        return "no attempted map"
    if set(state) - {"attempted", "failed", "installed"}:
        return "an unknown member"
    if not all(parse_version(k) is not None and _well_formed(t, latest)
               for k, t in state["attempted"].items()):
        return "an attempt no writer leaves"
    for k, t in state["attempted"].items():
        origin, variant = parse_version(t["from"]), (t["outcome"], t.get("phase"))
        if variant == ("crashed", "crashed") and "manager" in t:
            variant = ("started", "quiesced")  # only a quiesced attempt settles with its manager
        if (variant in _BELOW_TARGET or variant in _PARSED_ORIGIN) and origin is None:
            return "an attempt past the version gate whose origin is not a version"
        if variant in _BELOW_TARGET and not origin < parse_version(k):
            return "an attempt past the version gate whose origin is not below its target"
    if "failed" in state:
        failed = state["failed"]
        if not _typed(failed, _FAILED, ("target", "from", "installed", "why", "at"), latest):
            return "a failed summary of the wrong shape"
        if not _failed_variant(failed):
            return "a failed summary of no writer's variant"
        if not _failed_consistent(failed):
            return "a rolled-back summary whose installed version is not its from"
        outcome = failed.get("outcome", "crashed")
        if not _summarises(state["attempted"], failed["target"], outcome, KEEPS_FAILED):
            return "a failed summary of no attempt it was saved with"
        # Both are the attempt's `previous`, saved together; only a rerun's attempt may differ.
        tried = state["attempted"][failed["target"]]
        if tried["outcome"] == outcome and tried["from"] != failed["from"]:
            return "a failed summary whose from is not its attempt's"
    if "installed" not in state and any(t["outcome"] == "ok" for t in state["attempted"].values()):
        return "a success without its installed summary"
    if "installed" in state:
        installed = state["installed"]
        if not (_typed(installed, _INSTALLED, ("version", "at", "sha256", "after"), latest)
                and _summarises(state["attempted"], installed["version"], "ok", KEEPS_INSTALLED)):
            return "an installed summary of no success"
        fault = success_lineage(state)[1]
        if fault:
            return f"an installed summary with {fault}"
    if "failed" not in state:
        attempted = state["attempted"]
        recovered = sorted(k for k, t in attempted.items() if t["outcome"] in RECOVERY_OUTCOMES)
        if any(attempted[k].get("phase") == "restore-failed" for k in recovered):
            return "a held restore-failed without its summary"
        for target in recovered:
            fault = success_lineage(state, target)[1]
            if fault:
                return f"a recovery of {target} without its summary, and {fault}"
    return None


def success_lineage(state: dict, terminal: "str | None" = None) -> "tuple[list, str | None]":
    """The chain from `terminal` to the installed success through the edge that success
    saved, `after` (every attempt recorded before it), and the fault that breaks it, None
    only when the success strictly descends from `terminal` with no other success beside it.
    Times, key order and versions are never read."""
    chain = [] if terminal is None else [terminal]
    installed = state.get("installed")
    if installed is None:
        return chain, "no success"
    version, after, attempted = installed["version"], installed["after"], state["attempted"]
    chain.append(version)
    if after != sorted(set(after)):
        return chain, "a lineage not as its writer saves it"
    if version == terminal:
        return chain, "an equal reference"
    if version in after:
        return chain, "a cycle"
    missing = [k for k in after if k not in attempted]
    if missing:
        return chain + missing, "a missing link"
    forks = [k for k, t in attempted.items() if t["outcome"] == "ok" and k != version
             and k not in after]
    if forks:
        return chain + forks, "a fork"
    if terminal is not None and terminal not in after:
        return chain, "a success outside its lineage"
    return chain, None


def _failed_variant(failed: dict) -> bool:
    """The failed summaries the writers save: a crash (no outcome, no problems), a verified
    rollback (rolled-back, no problems) or a failed one (restore-failed, with problems)."""
    if "outcome" not in failed:
        return "problems" not in failed
    problems = failed.get("problems")
    if failed["outcome"] == "rolled-back":
        return problems == []
    return failed["outcome"] == "restore-failed" and isinstance(problems, list) and bool(problems)


def _failed_consistent(failed: dict) -> bool:
    """_rollback appends a problem unless the previous version is what is installed."""
    if failed.get("outcome") not in ("rolled-back", "restore-failed"):
        return True  # a crash summary keeps the text of its attempt's origin
    return parse_version(failed["from"]) is not None and (
        failed.get("outcome") != "rolled-back" or failed["installed"] == failed["from"])


def _summarises(attempted: dict, target: str, outcome: str, kept_by) -> bool:
    """Whether a summary names an attempt it was saved with, so in its `outcome`, or one a
    later rerun replaced with an outcome in `kept_by` (disjoint from it)."""
    tried = attempted.get(target)
    return tried is not None and (tried["outcome"] == outcome or tried["outcome"] in kept_by)


def _plausible(at, latest: float) -> bool:
    """A time a writer could have stamped: a number from 0 to `latest`, so never NaN,
    infinite, past a float's range or so far ahead that a started attempt never ages."""
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    try:
        return 0 <= float(at) <= latest
    except OverflowError:
        return False


def _text(value, latest) -> bool:
    return isinstance(value, str)


def _texts(value, latest) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


# Every field each record's writer can save, with the type it saves it as.
_ATTEMPT = {"at": _plausible, "from": _text, "outcome": _text, "phase": _text, "hook": _text,
            "manager": lambda v, latest: v in MANAGERS}
_FAILED = {"target": _text, "from": _text, "installed": _text, "why": _text, "at": _plausible,
           "outcome": _text, "problems": _texts}
_INSTALLED = {"version": _text, "at": _plausible, "after": _texts,
              "sha256": lambda v, latest: valid_hashes(v) is not None}


def _typed(record, fields: dict, required, latest: float) -> bool:
    """A dict of only these fields, the required ones present, each of its writer's type."""
    return (isinstance(record, dict) and set(record) <= set(fields)
            and all(k in record for k in required)
            and all(fields[k](v, latest) for k, v in record.items()))


def _well_formed(tried, latest: float) -> bool:
    """An attempt record the writer can leave: its fields typed as the writer saves them,
    at an (outcome, phase) variant that allows the manager and hook it carries."""
    if not _typed(tried, _ATTEMPT, ("at", "from", "outcome"), latest):
        return False
    rule = _ATTEMPT_VARIANTS.get((tried["outcome"], tried.get("phase")))
    if rule is None or ("hook" in tried and tried["outcome"] != "ok"):
        return False
    return ("manager" in tried) == (rule == YES) or (rule == MAYBE)


def save_state(state_dir: Path, state: dict) -> None:
    _cli._write(state_path(state_dir), json.dumps(state, sort_keys=True, indent=1) + "\n", 0o600)


def failed_since(state_dir: Path, installed: str = __version__) -> "float | None":
    """When the failed upgrade to or from this version failed; None when none did. An
    unreadable journal counts as failed, since its last write."""
    try:
        failed = load_state(state_dir).get("failed")
    except JournalError:
        with contextlib.suppress(OSError):
            return state_path(state_dir).stat().st_mtime
        return time.time()
    if isinstance(failed, dict) and installed in (failed.get("from"), failed.get("target")):
        at = failed.get("at")
        return float(at) if isinstance(at, (int, float)) else time.time()
    return None


@contextlib.contextmanager
def upgrade_lock(state_dir: Path):
    """Yields True while this process holds the upgrade lock, False when another does."""
    path = Path(state_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            file_lock.lock_fd(fd, blocking=False)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            file_lock.unlock_fd(fd)
    finally:
        os.close(fd)


def upgrade_running(state_dir: Path) -> bool:
    with upgrade_lock(state_dir) as held:
        return not held


def _run(argv: list, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(e).__name__}: {e}")


def _spawn(argv: list, log: Path) -> bool:
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("ab") as fh:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh,
                             stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        print(f"upgrade: could not start ({e})", file=sys.stderr)
        return False
    return True


def _why(r: subprocess.CompletedProcess) -> str:
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


def _distribution():
    """The installed ag2-sparrow, read from disk each call; None when pip did not install it."""
    try:
        return importlib.metadata.distribution(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def unsupported_install(dist) -> str:
    """Why this install cannot be upgraded and restored file by file; '' when it can."""
    if dist is None:
        return "ag2-sparrow is not installed as a package here"
    if dist.read_text("direct_url.json") is not None:
        return "ag2-sparrow is an editable or URL install (direct_url.json)"
    if not dist.files:
        return "ag2-sparrow's install record (RECORD) is missing"
    return ""


def _installed_paths(dist) -> "list[Path]":
    return [Path(os.path.normpath(dist.locate_file(f))) for f in dist.files or []]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _listing(directory: Path) -> "list[str]":
    try:
        return sorted(p.name for p in Path(directory).iterdir())
    except OSError:
        return []


def _record_scope(dist) -> dict:
    """The previous install, read before anything changes: the site dir, the top-level
    names RECORD puts there (`roots`), the recorded files outside it (`extra`, the
    console scripts) and their directories (`bins`), every recorded file (`recorded`),
    and the site dir's listing."""
    site = Path(os.path.normpath(dist.locate_file("")))
    roots, extra, recorded = set(), [], set()
    for path in _installed_paths(dist):
        if "__pycache__" not in path.parts:
            recorded.add(str(path))
        try:
            roots.add(path.relative_to(site).parts[0])
        except (ValueError, IndexError):
            extra.append(str(path))
    bins = sorted({str(Path(p).parent) for p in extra})
    return {"site": str(site), "roots": sorted(roots), "extra": sorted(set(extra)), "bins": bins,
            "recorded": sorted(recorded), "listing": _listing(site)}


def _scripts_dir(scope: dict) -> Path:
    bins = scope.get("bins") or []
    return Path(bins[0]) if len(bins) == 1 else Path(sysconfig.get_path("scripts"))


def wheel_inventory(wheels: Path, version: str, scope: dict) -> "list[str]":
    """Every path the target wheel in `wheels` installs, from its own RECORD and entry
    points: site-relative files, .data/{purelib,platlib} into the site dir, .data/scripts
    and console scripts into the scripts dir, and its whole dist-info dir (pip adds files
    there). ValueError for any other location, which is never installed."""
    found = sorted(Path(wheels).glob(f"ag2_sparrow-{version}-*.whl"))
    if len(found) != 1:
        raise ValueError(f"expected one ag2_sparrow-{version} wheel, found {len(found)}")
    site, scripts = Path(scope["site"]), _scripts_dir(scope)
    info, data = f"ag2_sparrow-{version}.dist-info", f"ag2_sparrow-{version}.data"
    owned = {str(site / info)}
    with zipfile.ZipFile(found[0]) as zf:
        rows = list(csv.reader(zf.read(f"{info}/RECORD").decode("utf-8").splitlines()))
        try:
            entry = zf.read(f"{info}/entry_points.txt").decode("utf-8")
        except KeyError:
            entry = ""
    for row in rows:
        rel = PurePosixPath(row[0]) if row and row[0] else None
        if rel is None:
            continue
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"the wheel names a path outside its install: {row[0]}")
        parts = rel.parts
        if parts[0] != data:
            owned.add(str(site.joinpath(*parts)))
        elif len(parts) > 2 and parts[1] in ("purelib", "platlib"):
            owned.add(str(site.joinpath(*parts[2:])))
        elif len(parts) > 2 and parts[1] == "scripts":
            owned.add(str(scripts.joinpath(*parts[2:])))
        else:
            raise ValueError(f"the wheel installs outside site-packages: {row[0]}")
    points = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    points.optionxform = str
    points.read_string(entry)
    for section in ("console_scripts", "gui_scripts"):
        if points.has_section(section):
            owned.update(str(scripts / name) for name in points.options(section))
    return sorted(owned)


def _files_under(path: Path) -> "list[Path]":
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts) \
        if path.is_dir() else []


def _exclusive(scope: dict) -> "list[Path]":
    """Files in the site dir's ag2_sparrow* entries, which only this package writes."""
    site = Path(scope["site"])
    return [f for name in _listing(site) if _OWN_RE.match(name) for f in _files_under(site / name)]


def _ours(scope: dict) -> "set[str]":
    """The paths the package owns: the previous RECORD, the snapshot and the target
    inventory (`owned`, directories expanded); never a name merely added later."""
    ours = set(scope.get("recorded") or []) | {e["path"] for e in scope.get("files") or []}
    for path in map(Path, scope.get("owned") or []):
        ours.update(str(f) for f in _files_under(path)) if path.is_dir() else ours.add(str(path))
    return ours


def namespace(scope: dict) -> "dict[str, str]":
    """{path: sha256} of every file now present that is ours (_ours) or sits in an
    ag2_sparrow* entry of the site dir; no __pycache__."""
    found = {}
    for path in [*map(Path, _ours(scope)), *_exclusive(scope)]:
        if "__pycache__" not in path.parts and path.is_file():
            found[str(path)] = _sha256(path)
    return found


def _dirs_before(scope: dict, paths) -> "list[str]":
    """The existing ancestor directories of `paths`, below the site and scripts dirs."""
    roots = {Path(scope["site"]), *map(Path, scope.get("bins") or [])}
    found = set()
    for path in map(Path, paths):
        for parent in path.parents:
            if parent in roots or not any(parent.is_relative_to(r) for r in roots):
                break
            if parent.is_dir():
                found.add(str(parent))
        if path.is_dir():
            found.add(str(path))
    return sorted(found)


def snapshot(state_dir: Path, version: str, dist, owned=()) -> Path:
    """Copy the package namespace, hashed, into STATE_DIR/rollback/<version>/, with the
    target inventory `owned` (wheel_inventory) and the directories that existed."""
    dest = Path(state_dir) / ROLLBACK_DIR / version
    tmp = dest.with_name(f".{version}.{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "files").mkdir(parents=True)
    scope = _record_scope(dist)
    if not scope["roots"] and not scope["extra"]:
        raise ValueError("the install recorded no files")
    scope["owned"] = sorted(map(str, owned))
    entries = []
    for i, (path, digest) in enumerate(sorted(namespace(scope).items())):
        name = f"{i:05d}"
        shutil.copy2(path, tmp / "files" / name)
        entries.append({"path": path, "copy": name, "sha256": digest})
    if not entries:
        raise ValueError("the install's files are missing")
    dirs = _dirs_before(scope, [e["path"] for e in entries] + scope["owned"])
    manifest = dict(scope, version=version, files=entries, dirs=dirs)
    (tmp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return dest


def _manifest(state_dir: Path, version: str) -> "tuple[Path, dict]":
    snap = Path(state_dir) / ROLLBACK_DIR / version
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version or not manifest.get("files"):
        raise ValueError(f"the snapshot in {snap} is not one of {version}")
    return snap, manifest


def _added_dirs(manifest: dict) -> "list[str]":
    """Directories the target created (absent from the snapshot's) that still exist."""
    before = set(manifest.get("dirs") or [])
    return [d for d in _dirs_before(manifest, manifest.get("owned") or []) if d not in before]


def namespace_drift(state_dir: Path, version: str) -> "list[str] | None":
    """How the namespace differs from the snapshot of `version` (empty when it matches);
    None when there is no snapshot to compare with. A file that is not ours in an
    ag2_sparrow* entry is "unknown": never removed, and never certified as matching."""
    try:
        _, manifest = _manifest(state_dir, version)
    except (OSError, ValueError):
        return None
    want = {e["path"]: e["sha256"] for e in manifest["files"]}
    have, ours = namespace(manifest), _ours(manifest)
    return sorted([f"added {d}" for d in _added_dirs(manifest)]
                  + [f"{'added' if p in ours else 'unknown'} {p}" for p in have.keys() - want.keys()]
                  + [f"missing {p}" for p in want.keys() - have.keys()]
                  + [f"changed {p}" for p in want.keys() & have.keys() if want[p] != have[p]])


def restore(state_dir: Path, version: str) -> None:
    """Put the snapshot of `version` back: remove every file of ours it does not list
    (with its bytecode and the directories the target created), copy its files in, and
    require the namespace to hash equal to it. RECORD, which a failed install may have
    left stale or half-swapped, is never consulted."""
    snap, manifest = _manifest(state_dir, version)
    want = {e["path"]: e for e in manifest["files"]}
    ours, before = _ours(manifest), set(manifest.get("dirs") or [])
    removed = [Path(p) for p in namespace(manifest) if p in ours and p not in want]
    for path in removed:
        path.unlink()
        for pyc in path.parent.glob(f"__pycache__/{glob.escape(path.stem)}.*.pyc"):
            pyc.unlink(missing_ok=True)
    for entry in manifest["files"]:
        target = Path(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap / "files" / entry["copy"], target)
    site = Path(manifest["site"])
    for name in _listing(site):
        if _OWN_RE.match(name) and (site / name).is_dir():
            for cache in sorted((site / name).rglob("__pycache__"), reverse=True):
                shutil.rmtree(cache, ignore_errors=True)  # the failed version's bytecode
    created = set(_added_dirs(manifest))
    for path in removed:
        created.update(str(p) for p in (path.parent, path.parent / "__pycache__")
                       if str(p) not in before and p.is_dir())
    for directory in sorted(created, key=lambda d: len(Path(d).parts), reverse=True):
        with contextlib.suppress(OSError):
            Path(directory, "__pycache__").rmdir()
        with contextlib.suppress(OSError):
            Path(directory).rmdir()  # only a directory the target created, once empty
    drift = namespace_drift(state_dir, version)
    if drift:
        raise ValueError(f"the files do not match the snapshot of {version}: "
                         + "; ".join(drift[:5]) + (" ..." if len(drift) > 5 else ""))


def _pinned(state_dir: Path, version: str, hashes: dict, argv: list,
            timeout: float) -> subprocess.CompletedProcess:
    """Run pip `argv` on exactly `version`, pinned to every sha256 in `hashes`. pip accepts
    --hash only in a requirements file, so it goes there."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fd, req = tempfile.mkstemp(dir=str(state_dir), prefix=".upgrade-", suffix=".txt")
    pins = " ".join(f"--hash=sha256:{h}" for h in sorted(set(hashes.values())))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{PACKAGE}=={version} {pins}\n")
        return _run([sys.executable, "-m", "pip", *argv, "--no-deps", "--only-binary=:all:",
                     "--disable-pip-version-check", "--require-hashes", "-r", req], timeout)
    finally:
        Path(req).unlink(missing_ok=True)


def wheels_dir(state_dir: Path, version: str) -> Path:
    return Path(state_dir) / WHEELS_DIR / version


def fetch(state_dir: Path, version: str, hashes: dict) -> subprocess.CompletedProcess:
    """Download the pinned wheel into wheels_dir, before anything changes."""
    dest = wheels_dir(state_dir, version)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    return _pinned(state_dir, version, hashes, ["download", "-d", str(dest)], PIP_TIMEOUT_S)


def pip_install(state_dir: Path, version: str, hashes: dict) -> subprocess.CompletedProcess:
    """Install exactly the fetched wheel of `version`, with no index and no dependency
    changes, still pinned to `hashes`."""
    return _pinned(state_dir, version, hashes,
                   ["install", "--no-index", "--find-links", str(wheels_dir(state_dir, version))],
                   PIP_TIMEOUT_S)


def valid_hashes(value) -> "dict | None":
    """A non-empty {wheel filename: sha256} map, else None."""
    if not isinstance(value, dict) or not value:
        return None
    ok = all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.+-]{1,200}", k)
             and isinstance(v, str) and SHA256_RE.fullmatch(v) for k, v in value.items())
    return dict(value) if ok else None


def own_check_problems() -> "set[str] | None":
    """`pip check` lines that name ag2-sparrow (it has no requirements of its own); None
    when pip check could not run or did not finish, which is a failure, never a pass."""
    r = _run([sys.executable, "-m", "pip", "check"], PIP_TIMEOUT_S)
    if r.returncode == 0:
        return set()
    if r.returncode != 1:
        return None
    return {line.strip() for line in f"{r.stdout}\n{r.stderr}".splitlines()
            if _OWN_RE.search(line)}


def install_argv(cfg: dict) -> list:
    """`install` with the choices the config recorded, so a re-run changes nothing else."""
    argv = [_keepalive._command(cfg), "install", "--mxid", cfg.get("AGENT_MXID", ""),
            "--no-start", f"--gateway-instance={_keepalive._instance(cfg)}"]
    for key, flag in (("TASK_DIR", "--task-dir"), ("RESULT_DIR", "--result-dir"),
                      ("STATE_DIR", "--state-dir"), ("ACTIVITY_FILE", "--activity-file"),
                      ("CRON_DIR", "--cron-dir"), ("KEEPALIVE_LOG", "--keepalive-log")):
        if cfg.get(key):
            argv += [flag, cfg[key]]
    try:
        ours = _cli.service_path().read_text(encoding="utf-8").startswith(_cli.SERVICE_MARKER)
    except OSError:
        ours = False
    if not ours:
        argv.append("--no-service")
    if not _keepalive._found(_keepalive.cron_dir(cfg), _keepalive.TASK):
        argv.append("--no-keepalive")
    return argv


def _metadata_version() -> str:
    """The version the install record names, read without importing the package."""
    dist = _distribution()
    return (dist.version or "") if dist is not None else ""


def verify_installed(dist) -> "list[str]":
    """How the installed files differ from the sha256 their RECORD names: a recorded file
    missing or changed, one recorded without a hash, or a file in a package-owned
    directory (ag2_sparrow*) that RECORD does not list. Empty when every byte is known."""
    why = unsupported_install(dist)
    if why:
        return [why]
    problems, recorded = [], set()
    # RECORD is parsed here: Distribution.files drops the rows whose file is missing.
    for row in csv.reader((dist.read_text("RECORD") or "").splitlines()):
        if not row or not row[0]:
            continue
        path = Path(os.path.normpath(dist.locate_file(row[0])))
        mode, _, value = (row[1] if len(row) > 1 else "").partition("=")
        recorded.add(path)
        if not value:
            if path.name != "RECORD" and "__pycache__" not in path.parts:
                problems.append(f"unhashed {path}")
        elif not path.is_file():
            problems.append(f"missing {path}")
        elif mode != "sha256" or base64.urlsafe_b64encode(bytes.fromhex(
                _sha256(path))).rstrip(b"=").decode() != value:
            problems.append(f"changed {path}")
    site = Path(os.path.normpath(dist.locate_file("")))
    for top in (site / n for n in _listing(site) if _OWN_RE.match(n)):
        found = [top] if top.is_file() else top.rglob("*")
        problems += [f"unrecorded {p}" for p in sorted(found) if p.is_file()
                     and "__pycache__" not in p.parts and p not in recorded]
    return problems


def release_hold(state_dir: Path) -> "str | None":
    """For the owner's install: lift a restore-failed hold once the installed files verify
    against their RECORD (verify_installed). None when Sparrow may start, else why not."""
    with upgrade_lock(state_dir) as free:
        if not free:
            return "an upgrade is running"
        held = recovery_blocked(state_dir)
        if held is None:
            return None
        attempted = load_state(state_dir)["attempted"] if "cannot be read" not in held else {}
        if not any(t.get("phase") == "restore-failed" for t in attempted.values()):
            return held
        problems = verify_installed(_distribution())
        if problems:
            return (f"{held}, and the installed files do not verify ({'; '.join(problems[:3])}"
                    f"{' ...' if len(problems) > 3 else ''}); reinstall ag2-sparrow with pip "
                    "--force-reinstall, then run install again")
        if clear_blocked(state_dir):
            print("cleared: a failed self-upgrade's hold on restarting Sparrow (the installed "
                  "files verify against their RECORD)")
        return recovery_blocked(state_dir)


def installed_version() -> str:
    """The version a fresh interpreter imports, which is what Sparrow will run."""
    r = _run([sys.executable, "-c", "import ag2_sparrow; print(ag2_sparrow.__version__)"], 60)
    return r.stdout.strip() if r.returncode == 0 else ""


def _decide(cfg: dict, setup: "dict | None", installed: str) -> "tuple[str, str, dict | None]":
    """(decision, detail, hashes); decision is '' when an upgrade to the target is due."""
    if (cfg.get("AUTO_UPGRADE") or "").strip() == "0":
        return "off", "AUTO_UPGRADE=0 in the install config", None
    package = (setup or {}).get("package")
    package = package if isinstance(package, dict) else {}
    target, hashes = package.get("target_version"), valid_hashes(package.get("target_sha256"))
    if target is None:
        return "no-target", "the served setup names no target_version", None
    want, have = parse_version(target), parse_version(installed)
    if want is None or have is None:
        return "bad-version", f"cannot compare {target!r} with {installed!r}", None
    if want < have:
        return "below-installed", f"target_version {target} is below the installed {installed}", None
    if want == have:
        return "current", f"{installed} is the target_version", None
    floor = package.get("min_version")
    if floor is not None and (parse_version(floor) is None or parse_version(floor) > want):
        return "min-above-target", f"min_version {floor!r} is above target_version {target}", None
    if hashes is None:
        return "no-hashes", f"{target} has no valid target_sha256 map", None
    why = unsupported_install(_distribution())
    if why:
        return "unsupported-install", why, None
    owner = _keepalive._unit_owner()
    if owner == "other":
        return "hand-made-service", (f"{_cli.service_path()} was not written by install, so "
                                     "its Sparrow is not stopped or upgraded unattended"), None
    if owner == _keepalive.UNIT_UNREACHABLE:
        return "unit-unreachable", (f"{_cli.service_path()} is installed but systemctl --user "
                                    "did not answer"), None
    return "", "", hashes


def maybe_start(cfg: dict, setup: "dict | None", installed: str = __version__,
                now: "float | None" = None) -> "str | None":
    """Decide on the served target and start an upgrade when one is due. Returns the
    decision for the keepawake line; None only when there is no served setup."""
    now = time.time() if now is None else now
    state_dir = _keepalive._state_dir(cfg)
    # Before any decision: an interrupted upgrade must be settled even if its target is current.
    with upgrade_lock(state_dir) as held:
        settled = settle_attempt(cfg, state_dir, now) if held else None
    if settled:
        return settled
    if setup is None:
        return None
    blocked = recovery_blocked(state_dir)
    if blocked:
        print(f"upgrade: recovery-blocked: {blocked}", file=sys.stderr)
        return "recovery-blocked"
    decision, detail, hashes = _decide(cfg, setup, installed)
    if decision:
        print(f"upgrade: {decision}: {detail}", file=sys.stderr)
        return decision
    target = setup["package"]["target_version"]
    with upgrade_lock(state_dir) as held:
        if not held:
            print("upgrade: running: an upgrade already holds the lock", file=sys.stderr)
            return "running"
        state = load_state(state_dir)
        tried = state["attempted"].get(target)
        if isinstance(tried, dict):
            print(f"upgrade: {target} was already attempted ({tried.get('outcome')})",
                  file=sys.stderr)
            return f"{target}:attempted"
        # Recorded before the start, so a crash mid-upgrade is never retried for this target.
        record = {"at": now, "from": installed, "outcome": "started"}
        state["attempted"][target] = dict(record)
        save_state(state_dir, state)
    argv = [sys.executable, "-m", "ag2_sparrow.adapters.hatch.self_upgrade", target,
            json.dumps(hashes, sort_keys=True)]
    if not _spawn(argv, state_dir / LOG_NAME):
        # Under the writer lock, and only over the record written above: else it ages to crashed.
        with upgrade_lock(state_dir) as held:
            if held and load_state(state_dir)["attempted"].get(target) == record:
                _finish(state_dir, target, "not-started")
        return f"{target}:not-started"
    print(f"upgrade: started {installed} -> {target}", file=sys.stderr)
    return f"{target}:started"


def settle_attempt(cfg: dict, state_dir: Path, now: float) -> "str | None":
    """Settle a dead attempt, with the upgrade lock held by the caller (so no worker runs):
    one still "started" after STALE_START_S with no phase, or one in any phase, which only
    a lock holder writes. Nothing installed and the namespace equal to the snapshot means
    crashed, Sparrow restarted if the attempt had stopped it; a mutating phase, a drifted
    namespace or an interrupted restore is restored, in a phase that resumes next run."""
    state = load_state(state_dir)
    for target, tried in state["attempted"].items():
        if not isinstance(tried, dict):
            continue
        at, phase = tried.get("at"), tried.get("phase")
        stale = (tried.get("outcome") == "started" and isinstance(at, (int, float))
                 and (phase is not None or now - at > STALE_START_S))
        if not stale:
            continue
        previous, dist = str(tried.get("from") or ""), _distribution()
        current = dist.version if dist is not None else ""
        drift = namespace_drift(state_dir, previous) if previous else None
        why = "the restore did not finish" if phase == "restoring" else "the upgrade did not finish"
        print(f"upgrade: {target} ({phase or 'started'}): {why}; {current or 'no version'} "
              f"is installed", file=sys.stderr)
        if phase not in MUTATING_PHASES and not drift:
            tried.update(outcome="crashed", phase="crashed")
            state["failed"] = {"target": target, "from": previous, "installed": current,
                               "why": why, "at": now}
            save_state(state_dir, state)
            held = recovery_blocked(state_dir) if phase == "quiesced" else None
            if held:
                print(f"error: Sparrow is not started: {held}", file=sys.stderr)
            elif phase == "quiesced":
                ok, how = start_sparrow(cfg, tried.get("manager"))
                print(("started: " if ok else "error: Sparrow did not start: ") + how,
                      file=sys.stderr if not ok else sys.stdout)
            return f"{target}:crashed"
        if drift is None:
            tried.update(outcome="restore-failed", phase="restore-failed")
            state["failed"] = {"target": target, "from": previous, "installed": current,
                               "why": why, "at": now, "outcome": "restore-failed",
                               "problems": [f"no snapshot of {previous or 'the previous version'}"]}
            save_state(state_dir, state)
            print(f"error: {target} left files changed and there is no snapshot of {previous}",
                  file=sys.stderr)
            return f"{target}:restore-failed"
        return f"{target}:{_rollback(cfg, state_dir, previous, target, why, tried.get('manager'))}"
    return None


def _attempt(state_dir: Path, target: str) -> None:
    """Give a worker run a started record: maybe_start's, or a fresh one in place of none
    or of a settled attempt (a hold never reaches here: the worker refused it)."""
    state = load_state(state_dir)
    if state["attempted"].get(target, {}).get("outcome") != "started":
        state["attempted"][target] = {"at": time.time(), "from": _metadata_version(),
                                      "outcome": "started"}
        # A new generation of this key: an earlier success no longer provably came after it.
        installed = state.get("installed")
        if installed and target in installed["after"]:
            installed["after"] = [k for k in installed["after"] if k != target]
        save_state(state_dir, state)


def _phase(state_dir: Path, target: str, phase: str, **fields) -> None:
    """Journal the step about to run, so a dead attempt is settled from where it got to."""
    state = load_state(state_dir)
    tried = state["attempted"].setdefault(target, {})
    if phase == "snapshot":
        tried.pop("manager", None)  # a rerun of a quiesced record has not stopped Sparrow itself
    tried.update(phase=phase, **fields)
    save_state(state_dir, state)


def _finish(state_dir: Path, target: str, outcome: str, **extra) -> None:
    state = load_state(state_dir)
    tried = state["attempted"].setdefault(target, {})
    if _ATTEMPT_VARIANTS.get((outcome, outcome)) == NO:
        tried.pop("manager", None)  # a rerun over a quiesced record never stopped Sparrow itself
    tried.update(outcome=outcome, phase=outcome)
    state.update(extra)
    save_state(state_dir, state)
    if outcome == "restore-failed":
        _cli._write(notice_path(state_dir),
                    f"ag2-sparrow's upgrade to {target} failed and the rollback did not verify; "
                    "Sparrow is left stopped and keepawake will not restart it until the owner "
                    "runs ag2-sparrow-hatch install (see the hatch-upgrade.json journal).\n", 0o600)


def recovery_blocked(state_dir: Path) -> "str | None":
    """Why Sparrow must not be started: an attempt whose files are unverified, in a
    mutating phase or restore-failed, or a journal that cannot be read; None when every
    attempt is settled or cleared, or there is no journal."""
    try:
        attempted = load_state(state_dir)["attempted"]
    except JournalError as e:
        return f"{e}; repair it, or remove it once the installed files are verified"
    for target, tried in attempted.items():
        phase = tried.get("phase")
        if tried.get("outcome") == "restore-failed" and phase != "acknowledged":
            phase = "restore-failed"
        if phase in MUTATING_PHASES or phase == "restore-failed":
            return f"the upgrade to {target} left unverified files ({phase})"
    return None


def clear_blocked(state_dir: Path) -> bool:
    """The owner's install acknowledges a failed recovery: its hold on restarts is lifted
    (the attempt stays recorded, so the target is not retried). Whether anything changed."""
    state, changed = load_state(state_dir), False
    for tried in state["attempted"].values():
        if isinstance(tried, dict) and tried.get("phase") == "restore-failed":
            tried["phase"], changed = "acknowledged", True
    if changed:
        save_state(state_dir, state)
    return changed


def stop_sparrow(cfg: dict) -> "tuple[bool, str, str | None]":
    """Stop the Sparrow this install owns and prove it is gone: (quiet, how, manager); the
    manager ("unit" or "detached") is what starts it again. Never a hand-made unit's."""
    return _keepalive.quiesce_sparrow(cfg)


def start_sparrow(cfg: dict, manager: "str | None") -> "tuple[bool, str]":
    """Start Sparrow through the manager that was stopped, and through nothing else."""
    owner = _keepalive._unit_owner()
    expected = {"unit": "ours", "detached": None}.get(manager, manager)
    if manager not in ("unit", "detached") or owner != expected:
        return False, (f"Sparrow was stopped through {manager or 'no manager'} and "
                       f"{_cli.service_path()} is now {owner or 'absent'}; not started")
    return _keepalive.restart_listener(cfg)


def _rollback(cfg: dict, state_dir: Path, previous: str, target: str, why: str,
              manager: "str | None" = None) -> str:
    """Restore `previous` and check it: "rolled-back" only when the namespace hashes equal
    to its snapshot, install re-ran and Sparrow runs it connected, else "restore-failed"
    with what is installed. No file changes unless Sparrow is proven stopped, and no start
    unless the restore verified."""
    print(f"upgrade to {target} failed ({why}); restoring {previous}", file=sys.stderr)
    _phase(state_dir, target, "restoring")
    problems, verified = [], False
    quiet, how, stopped_by = stop_sparrow(cfg)
    manager = stopped_by or manager
    if not quiet:
        problems.append(f"stop: {how}; nothing was restored")
    else:
        try:
            restore(state_dir, previous)
        except (OSError, ValueError) as e:
            # Unverified files are never started: Sparrow stays stopped for the owner.
            problems.append(f"restore: {e}; Sparrow is left stopped")
        else:
            verified = True
            r = _run(install_argv(cfg), INSTALL_TIMEOUT_S)
            if r.returncode != 0:
                problems.append(f"install: {_why(r)}")
            since = time.time()
            started, how = start_sparrow(cfg, manager)
            if not started:
                problems.append(f"start: {how}")
            elif _keepalive._await_connected(state_dir, _keepalive._instance(cfg), since) != 0:
                problems.append("Sparrow did not reconnect")
    # Unverified files are never imported, not even to read their version.
    installed = installed_version() if verified else _metadata_version()
    if installed != previous:
        problems.append(f"{installed or 'no version'} is installed, not {previous}")
    outcome = "restore-failed" if problems else "rolled-back"
    if problems:
        print(f"error: the rollback to {previous} failed: {'; '.join(problems)}",
              file=sys.stderr)
    _finish(state_dir, target, outcome,
            failed={"target": target, "from": previous, "why": why, "at": time.time(),
                    "outcome": outcome, "installed": installed, "problems": problems})
    return outcome


def run_upgrade(cfg: dict, target: str, hashes: dict) -> str:
    """Upgrade to `target`, pinned to `hashes`, or roll back; the outcome. Skipped when
    another upgrade holds the lock."""
    state_dir = _keepalive._state_dir(cfg)
    with upgrade_lock(state_dir) as held:
        if not held:
            print("upgrade already running; skipped", file=sys.stderr)
            return "already-running"
        try:
            return _upgrade_locked(cfg, state_dir, target, hashes)
        finally:
            shutil.rmtree(Path(state_dir) / WHEELS_DIR, ignore_errors=True)


def _upgrade_locked(cfg: dict, state_dir: Path, target: str, hashes: dict) -> str:
    blocked = recovery_blocked(state_dir)
    if blocked:
        # Read-only: the journal entry that holds must survive the refusal unchanged.
        print(f"upgrade: skipped: {blocked}", file=sys.stderr)
        return "recovery-blocked"
    _attempt(state_dir, target)
    dist = _distribution()
    why = unsupported_install(dist)
    if why:
        print(f"upgrade: skipped: {why}", file=sys.stderr)
        _finish(state_dir, target, "unsupported-install")
        return "unsupported-install"
    # Read again under the lock: the queued target may be stale by now.
    previous = dist.version
    want, have = parse_version(target), parse_version(previous)
    if want is None or have is None or want <= have:
        print(f"upgrade: skipped: {previous} is installed, not below {target}", file=sys.stderr)
        _finish(state_dir, target, "not-above-installed")
        return "not-above-installed"
    r = fetch(state_dir, target, hashes)
    if r.returncode != 0:
        print(f"upgrade: skipped: the {target} wheel did not download: {_why(r)}", file=sys.stderr)
        _finish(state_dir, target, "no-download")
        return "no-download"
    try:
        owned = wheel_inventory(wheels_dir(state_dir, target), target, _record_scope(dist))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, configparser.Error) as e:
        print(f"upgrade: skipped: no inventory of the {target} wheel: {e}", file=sys.stderr)
        _finish(state_dir, target, "no-inventory")
        return "no-inventory"
    try:
        snapshot(state_dir, previous, dist, owned)
    except (OSError, ValueError) as e:
        print(f"upgrade: skipped: no rollback snapshot of {previous}: {e}", file=sys.stderr)
        _finish(state_dir, target, "no-snapshot")
        return "no-snapshot"
    _phase(state_dir, target, "snapshot", **{"from": previous})
    baseline = own_check_problems()
    if baseline is None:
        print("upgrade: skipped: pip check could not run", file=sys.stderr)
        _finish(state_dir, target, "no-pip-check")
        return "no-pip-check"
    quiet, how, manager = stop_sparrow(cfg)
    if not quiet:
        print(f"upgrade: Sparrow is not proven stopped ({how}); nothing was installed",
              file=sys.stderr)
        if manager:
            start_sparrow(cfg, manager)
        _finish(state_dir, target, "stop-failed")
        return "stop-failed"
    _phase(state_dir, target, "quiesced", manager=manager)

    def rollback(why: str) -> str:
        return _rollback(cfg, state_dir, previous, target, why, manager)
    _phase(state_dir, target, "installing")
    r = pip_install(state_dir, target, hashes)
    if r.returncode != 0:
        return rollback(f"pip install: {_why(r)}")
    _phase(state_dir, target, "installed")
    after = own_check_problems()
    if after is None:
        return rollback("pip check could not run")
    new = sorted(after - baseline)
    if new:
        return rollback(f"pip check: {new[0]}")
    r = _run(install_argv(cfg), INSTALL_TIMEOUT_S)
    if r.returncode != 0:
        return rollback(f"install: {_why(r)}")
    hook = next((line.strip() for line in (r.stdout or "").splitlines()
                 if line.startswith("REQUIRED hook:")), None)
    since = time.time()
    ok, how = start_sparrow(cfg, manager)
    if not ok:
        return rollback(f"start: {how}")
    if installed_version() != target:
        return rollback("the new version does not import")
    if _keepalive._await_connected(state_dir, _keepalive._instance(cfg), since) != 0:
        return rollback("Sparrow did not reconnect")
    state = load_state(state_dir)
    state.pop("failed", None)
    after = sorted(k for k in state["attempted"] if k != target)
    state["installed"] = {"version": target, "sha256": hashes, "at": time.time(), "after": after}
    state["attempted"].setdefault(target, {}).update(outcome="ok", phase="ok")
    if hook:
        state["attempted"][target]["hook"] = hook
        _cli._write(notice_path(state_dir),
                    f"ag2-sparrow upgraded itself to {target}, and its hook definition changed; "
                    f"the change was not applied: {hook}\n", 0o600)
    save_state(state_dir, state)
    print(f"upgraded: {previous} -> {target}" + (f"; {hook} (not applied)" if hook else ""))
    return "ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ag2-sparrow-hatch-upgrade")
    ap.add_argument("target")
    ap.add_argument("hashes", help="JSON {wheel filename: sha256}")
    args = ap.parse_args(argv)
    try:
        hashes = valid_hashes(json.loads(args.hashes))
    except ValueError:
        hashes = None
    if parse_version(args.target) is None or hashes is None:
        print("error: target must be a version and hashes a {filename: sha256} map",
              file=sys.stderr)
        return 2
    cfg = _cli._installed_config()
    cfg.setdefault("STATE_DIR", str(_dirs.state_dir()))
    return 0 if run_upgrade(cfg, args.target, hashes) == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
