#!/usr/bin/env python3
"""Guards the task-filename <-> task-id boundary owned by task_archive.

`task_archive` owns BOTH directions of the pool's filename grammar:

    task_id_from_filename(name) -> id      file -> id
    find_task_file(dir, id)     -> path     id -> file

Every defect in this class was a call site re-deriving one direction inline,
and each was found by hand. The contract is pinned (tests/task-archive.test.py);
the DELEGATION was not, which is why sites drifted after the owner was extracted.

SCOPE, stated because silence is not coverage: the scans read `*.py` only.
Shell sites are enumerated in section 5 and NOT gated. A zero for shell here
would be a zero this file never looked for.

The scans are TOKEN-SPECIFIC — they key on the claim suffixes themselves, not
on the words "task" or "id" — so they cannot be satisfied by renaming and
cannot false-positive on a results/, processed/ or archive/ path, none of which
is ever renamed to a claimed or assigned name.
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
ROOTS = ["src", "scripts", "skills", "packages"]
# The two copies of the owner: exempt from the scans (they ARE the grammar), and
# required to stay in sync — one-sided fixes are this same defect a level up.
OWNERS = ("src/task_archive.py", "packages/ag2-sparrow/ag2_sparrow/task_archive.py")

# Known instances, each tracked by a named PR, so this guard covers the unbounded
# future set now. DELETE a row when its fix lands; section 6 fails on a stale one.
KNOWN = {}

fails = []
known_hit = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        fails.append(label)


def code_lines(path):
    """(1-based FILE line number, text) for non-comment lines.

    Enumerating the filtered list instead reports an index into that list as a
    file line, sending the next reader to the wrong place.
    """
    return [(i, ln) for i, ln
            in enumerate(path.read_text(errors="replace").splitlines(), 1)
            if not ln.strip().startswith("#")]


def record(site, text, bucket):
    """Route a hit to the allowlist or to the failing bucket."""
    (known_hit if site in KNOWN else bucket).append(f"{site}  {text[:70]}")


# ── 1. both exports exist, in both copies, with the same grammar ─────────────
print("── the owner exports both directions, in both copies ──")
sys.path.insert(0, str(REPO / "src"))
import task_archive as ta  # noqa: E402

for name in ("task_id_from_filename", "find_task_file"):
    check(f"src/task_archive exports {name}", hasattr(ta, name))

src_txt = (REPO / OWNERS[0]).read_text()
vend_txt = (REPO / OWNERS[1]).read_text()
for pat in ("_ID_STATE", "_ID_PLAIN"):
    a = re.search(rf"^{pat}\s*=\s*re\.compile\((.+)$", src_txt, re.M)
    b = re.search(rf"^{pat}\s*=\s*re\.compile\((.+)$", vend_txt, re.M)
    check(f"{pat} is byte-identical in both copies of the owner",
          a is not None and b is not None and a.group(1) == b.group(1),
          f"src={a.group(1) if a else None!r} vendored={b.group(1) if b else None!r}")

# ── 2. file -> id is never hand-rolled ───────────────────────────────────────
print("── file -> id goes through task_id_from_filename ──")
HANDROLLED = re.compile(r'\.(?:split|partition)\(\s*["\']\.(?:claimed|assigned)-')
def scan_handrolled(base, roots, bucket):
    seen = 0
    for root in roots:
        for p in sorted((base / root).rglob("*.py")):
            rel = str(p.relative_to(base))
            if rel in OWNERS:
                continue
            seen += 1
            for i, ln in code_lines(p):
                if HANDROLLED.search(ln):
                    record(f"{rel}:{i}", ln.strip(), bucket)
    return seen


hits = []
SEEN_A = scan_handrolled(REPO, ROOTS, hits)
check("no call site splits a filename on a claim suffix to derive an id",
      not hits, "; ".join(hits))

# ── 3. id -> file handles the states in pairs ────────────────────────────────
# Two states of one rename: globbing one reads a live task as absent.
print("── id -> file handles .claimed- and .assigned- together ──")
CLAIMED_GLOB = re.compile(r'glob\(\s*f?["\'][^"\']*\.claimed-')
ASSIGNED = re.compile(r"\.assigned-")
# A deliberate claimed-only check declares itself with a greppable token: prose
# would make the exemption self-granting by the very author who needs it.
MARKER = re.compile(r"#\s*claimed-only:")
def scan_unpaired(base, roots, bucket):
    seen = 0
    for root in roots:
        for p in sorted((base / root).rglob("*.py")):
            rel = str(p.relative_to(base))
            if rel in OWNERS:
                continue
            seen += 1
            lines = p.read_text(errors="replace").splitlines()
            for i, ln in enumerate(lines):
                if ln.strip().startswith("#") or not CLAIMED_GLOB.search(ln):
                    continue
                window = lines[max(0, i - 3):i + 4]
                # Pairing must be visible in CODE, so strip comments first.
                if ASSIGNED.search("\n".join(x.split("#", 1)[0] for x in window)):
                    continue
                if any(MARKER.search(x) for x in window):
                    continue
                record(f"{rel}:{i + 1}", ln.strip(), bucket)
    return seen


unpaired = []
SEEN_B = scan_unpaired(REPO, ROOTS, unpaired)
check("no call site globs .claimed- without .assigned- or a `# claimed-only:` marker",
      not unpaired, "; ".join(unpaired))

# ── 4. the scans can still FAIL ──────────────────────────────────────────────
# A scan whose pattern matches nothing scores zero by construction.
print("── the scans are able to fire ──")
check("the file->id scan flags a synthetic hand-rolled split",
      bool(HANDROLLED.search('tid = f.name.split(".claimed-")[0]')))
_synth = ['def g(t):', '    return next(d.glob(f"{t}.claimed-worker-*.txt"), None)']
check("the id->file scan flags a synthetic unpaired glob",
      any(CLAIMED_GLOB.search(l) and not ASSIGNED.search("\n".join(_synth))
          for l in _synth))
_paired = ['    a = any(d.glob(f"{t}.claimed-*"))',
           '    b = any(d.glob(f"{t}.assigned-*"))']
check("the id->file scan does NOT flag a paired glob",
      not any(CLAIMED_GLOB.search(l) and not ASSIGNED.search("\n".join(_paired))
              for l in _paired))
_prose = ['    # the .assigned- case is handled elsewhere',
          '    a = any(d.glob(f"{t}.claimed-*"))']
check("a comment mentioning .assigned- does NOT exempt (prose is not a marker)",
      any(CLAIMED_GLOB.search(l)
          and not ASSIGNED.search("\n".join(x.split("#", 1)[0] for x in _prose))
          and not any(MARKER.search(x) for x in _prose)
          for l in _prose))

# ── 4b. the scans reach real FILES, not just strings ─────────────────────────
# Section 4 exercises the patterns only, never the walk that feeds them.
print("── the scans reach files on disk ──")
_tmp = Path(tempfile.mkdtemp(prefix="deleg-walk-"))
(_tmp / "src").mkdir(parents=True)
(_tmp / "src" / "planted_split.py").write_text(
    'def f(n):\n    return n.split(".claimed-")[0]\n')
(_tmp / "src" / "planted_glob.py").write_text(
    'def g(d, t):\n    return next(d.glob(f"{t}.claimed-worker-*.txt"), None)\n')
(_tmp / "src" / "clean.py").write_text('def h():\n    return 1\n')
_a, _b = [], []
scan_handrolled(_tmp, ["src"], _a)
scan_unpaired(_tmp, ["src"], _b)
check("the file->id walk finds a planted violation on disk",
      any("planted_split.py:2" in x for x in _a), str(_a))
check("the id->file walk finds a planted violation on disk",
      any("planted_glob.py:2" in x for x in _b), str(_b))
check("neither walk flags the clean file in the same tree",
      not any("clean.py" in x for x in _a + _b), str(_a + _b))
# 4b supplies its own roots, so it cannot see ROOTS pointing somewhere empty.
check("the repo walk examined files under ROOTS",
      SEEN_A > 100 and SEEN_B > 100, f"examined {SEEN_A}/{SEEN_B} files")
shutil.rmtree(_tmp, ignore_errors=True)

# ── 5. shell sites: enumerated, not gated ────────────────────────────────────
print("── shell sites (reported, not gated — see the docstring) ──")
SH = re.compile(r"claimed-worker-")
sh_sites = []
for root in ROOTS:
    for p in sorted((REPO / root).rglob("*.sh")):
        lines = p.read_text(errors="replace").splitlines()
        for i, ln in enumerate(lines):
            if SH.search(ln) and not ln.strip().startswith("#"):
                win = "\n".join(lines[max(0, i - 3):i + 4])
                tag = "" if ".assigned-" in win else "  (claimed-only)"
                sh_sites.append(f"{p.relative_to(REPO)}:{i + 1}{tag}")
for site in sh_sites:
    print(f"  note {site}")
print(f"  ({len(sh_sites)} shell site(s), not gated)")

# ── 6. the allowlist must not outlive its rows ───────────────────────────────
print("── the allowlist is live ──")
matched = {h.split("  ")[0] for h in known_hit}
stale = sorted(set(KNOWN) - matched)
# Routed, not just reported: whoever merges the fix trips this, and they are not
# the person who knows why an unrelated test went red.
detail = "; ".join(
    f"{site} no longer matches — its fix ({KNOWN[site]}) has landed, so DELETE "
    f"this row from KNOWN in {Path(__file__).name}" for site in stale)
check("no stale allowlist rows (delete a row once its fix lands)",
      not stale, detail)
for site in sorted(matched):
    print(f"  note allowlisted: {site}  -> {KNOWN[site]}")

print()
if fails:
    print(f"FAIL — {len(fails)}: {fails}")
    sys.exit(1)
print("PASS — task-id derivation delegation boundary")
