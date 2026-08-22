#!/usr/bin/env python3
"""src/ root growth ratchet — enforces docs/src-ownership.yaml (P2a root-freeze).

Every top-level entry in src/ must be registered with an owner category.
Ownership only: this suite moves nothing and asserts no behavior. A new
unregistered root module fails here — register it (with a category) or put
it in a package directory that is already owned.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(REPO, "docs", "src-ownership.yaml")
SRC = os.path.join(REPO, "src")
CATEGORIES = {"sutando", "ag2-sparrow", "integration", "policy",
              "compatibility", "entrypoint", "retire"}
IGNORED = {"__pycache__", ".DS_Store"}

failures = []
def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond: failures.append(label)

def parse(path):
    # dependency-light YAML subset: "- path:" items with owner/note fields
    sections, cur, item = {}, None, None
    for raw in open(path):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\w+):\s*$", line)
        if m:
            cur = m.group(1); sections[cur] = []; item = None; continue
        m = re.match(r"^  - path:\s*(\S+)\s*$", line)
        if m and cur:
            item = {"path": m.group(1)}; sections[cur].append(item); continue
        m = re.match(r"^    (\w+):\s*(.+?)\s*$", line)
        if m and item is not None:
            item[m.group(1)] = m.group(2)
    return sections

sections = parse(MANIFEST)
modules = sections.get("modules", [])
pending = sections.get("pending", [])

registered = {}
for it in modules + pending:
    name = it["path"].rstrip("/")
    check(name not in registered, f"manifest lists {name!r} exactly once")
    registered[name] = it
    check(it.get("owner") in CATEGORIES,
          f"{name!r} owner {it.get('owner')!r} is a known category")

on_disk = sorted(e for e in os.listdir(SRC) if e not in IGNORED)

# THE RATCHET: every root entry must be registered
unregistered = [e for e in on_disk if e not in registered]
check(not unregistered,
      f"every src/ root entry is registered (unregistered: {unregistered})")

# no stale rows: every non-pending manifest entry exists on disk
pending_names = {it["path"].rstrip("/") for it in pending}
stale = [n for n in registered
         if n not in on_disk and n not in pending_names]
check(not stale, f"every registered module exists on disk (stale: {stale})")

# dir/file shape agrees with the trailing-slash convention in the manifest
for it in modules:
    name = it["path"].rstrip("/")
    if name in on_disk:
        is_dir = os.path.isdir(os.path.join(SRC, name))
        check(it["path"].endswith("/") == is_dir,
              f"{name!r} dir/file shape matches its manifest row")

# shim census: every phase-1a alias file on disk is registered as compatibility
for e in on_disk:
    p = os.path.join(SRC, e)
    if os.path.isfile(p) and e.endswith(".py"):
        head = open(p, errors="replace").read(400)
        if re.search(r"Alias of\s", head):
            check(registered.get(e, {}).get("owner") == "compatibility",
                  f"alias shim {e!r} is registered as compatibility")

# migration debt named: sync_from_src.py must stay declared in the manifest header
check("sync_from_src.py" in open(MANIFEST).read(),
      "manifest names sync_from_src.py as migration debt")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
