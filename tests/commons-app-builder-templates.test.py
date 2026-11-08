#!/usr/bin/env python3
"""Tests for skills/commons-app-builder/templates/: each schema, each page's GUIDE.md contract,
and that the two agree. Hermetic, no network. The data paths are exercised in
tests/commons-app-builder-templates.test.mjs; CSS layout needs a browser and is not checked."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _ROOT / "skills" / "commons-app-builder"
_TPL = _SKILL / "templates"

spec = importlib.util.spec_from_file_location("check_page", _SKILL / "scripts" / "check_page.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

passed: list[str] = []


def check(name, cond):
    assert cond, f"FAIL {name}"
    passed.append(name)


TEMPLATES = ["tracker", "poll", "planner", "turn-game"]
# The property types GUIDE.md's prop_add table allows.
TYPES = {
    "title", "text", "number", "date", "checkbox", "select", "status",
    "person", "created_by", "url", "email", "phone", "files",
}
OPTIONED = {"select", "status"}
GROUPS = {"todo", "doing", "done"}

check("templates-dir-exists", _TPL.is_dir())
check("readme-exists", (_TPL / "README.md").is_file())
check("all-four-present", sorted(p.name for p in _TPL.iterdir() if p.is_dir()) == sorted(TEMPLATES))

readme = (_TPL / "README.md").read_text(encoding="utf-8")
guide = (_SKILL / "GUIDE.md").read_text(encoding="utf-8")
check("guide-references-templates", "templates/" in guide and "README" in guide)

for name in TEMPLATES:
    d = _TPL / name
    page_path, schema_path = d / "page.html", d / "schema.json"
    check(f"{name}/page-exists", page_path.is_file())
    check(f"{name}/schema-exists", schema_path.is_file())
    page = page_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # --- the skill's own pre-flight, errors AND warnings -------------------
    res = cp.check(page)
    check(f"{name}/check_page-no-errors", res["ok"] and not res["errors"])
    check(f"{name}/check_page-no-warnings", not res["warnings"])

    # --- schema shape -----------------------------------------------------
    check(f"{name}/schema-has-app-and-summary", bool(schema.get("app")) and bool(schema.get("summary")))
    dbs = schema.get("databases") or []
    check(f"{name}/schema-has-a-database", len(dbs) >= 1)
    primary = [db for db in dbs if db.get("role") == "primary"]
    check(f"{name}/schema-names-one-primary", len(primary) == 1)
    prop_names: set[str] = set()
    for db in dbs:
        check(f"{name}/db-named-app-dash-entity", " — " in db.get("name", ""))
        check(f"{name}/db-built-from-blank", db.get("template") == "blank")
        props = db.get("props") or []
        check(f"{name}/db-has-props", len(props) >= 2)
        for prop in props:
            pname, ptype = prop.get("name"), prop.get("type")
            check(f"{name}/prop-named-{pname}", bool(pname))
            check(f"{name}/prop-type-known-{pname}", ptype in TYPES)
            if ptype in OPTIONED:
                opts = prop.get("options") or []
                check(f"{name}/options-on-{pname}", len(opts) >= 2)
                check(f"{name}/option-names-{pname}", all(o.get("name") for o in opts))
                for o in opts:
                    if "group" in o:
                        check(f"{name}/option-group-{pname}-{o['name']}", o["group"] in GROUPS)
            prop_names.add(pname)
        # GUIDE step 2: a blank template mints the title column, so the schema must not add one.
        check(f"{name}/no-title-prop-declared", not any(p.get("type") == "title" for p in props))
        # Authorship must come from created_by, never a writable field.
        check(f"{name}/has-created-by", any(p.get("type") == "created_by" for p in props))
        for forged in ("Voter", "Author", "Player", "By"):
            bad = [p for p in props if p.get("name") == forged and p.get("type") != "created_by"]
            check(f"{name}/no-writable-authorship-{forged}", not bad)
        views = db.get("views") or []
        check(f"{name}/has-a-table-view", any(v.get("layout") == "table" for v in views))
        for view in views:
            if view.get("groupBy"):
                check(f"{name}/groupBy-is-a-prop-{view['groupBy']}", view["groupBy"] in prop_names)
            if view.get("dateBy"):
                check(f"{name}/dateBy-is-a-prop-{view['dateBy']}", view["dateBy"] in prop_names)
        if db.get("summon_policy") is not None:
            check(f"{name}/summon-policy-known", db["summon_policy"] in {"owner", "room", "off"})

    # --- page <-> schema agreement ----------------------------------------
    # Every column the page names must exist, or the page silently reads undefined.
    used = set(re.findall(r"cells\[['\"]([^'\"]+)['\"]\]", page))
    used |= set(re.findall(r"\.cells\.([A-Za-z_]\w*)", page))
    used |= set(re.findall(r"db\.write\(\s*\w+\s*,\s*[\w.]+\s*,\s*['\"]([^'\"]+)['\"]", page))
    used |= set(re.findall(r"p\.name === ['\"]([^'\"]+)['\"]", page))
    unknown = {u for u in used if u not in prop_names}
    check(f"{name}/page-columns-exist-in-schema:{sorted(unknown)}", not unknown)

    # The title column's name is read from props, never assumed (GUIDE step 3).
    check(f"{name}/title-prop-read-not-assumed", "p.type === 'title'" in page)

    # The page finds its database by artifact.app.database or exact name, and that name is one
    # the schema actually declares.
    check(f"{name}/uses-app-database", "art.app && art.app.database" in page)
    named = set(re.findall(r"byName\(dbs, '([^']+)'\)", page))
    declared = {db.get("name") for db in dbs}
    check(f"{name}/fallback-name-matches-schema:{sorted(named - declared)}", named <= declared)

    # --- the phone-first CSS contract (rules, not rendered layout) ---------
    check(f"{name}/viewport-meta", 'name="viewport"' in page and "width=device-width" in page)
    check(f"{name}/color-scheme", "color-scheme: light dark" in page)
    check(f"{name}/dark-set", "prefers-color-scheme: dark" in page)
    check(f"{name}/min-44px-controls", re.search(r"min-height:\s*44px", page) is not None)
    # Wider layouts only behind a min-width query, so 390px stays one column.
    for q in re.findall(r"@media\s*\(([^)]*)\)", page):
        if "prefers-color-scheme" in q:
            continue
        check(f"{name}/media-query-is-min-width:{q.strip()}", "min-width" in q)
    # A fixed wide width would force sideways scroll at 390px. min-width/max-width are fine:
    # the first is how the wider layout is opted into, the second caps a centred block.
    check(
        f"{name}/no-fixed-wide-px",
        not re.search(r"(?<![-\w])width:\s*(?:[4-9]\d{2}|\d{4,})px", page),
    )
    check(f"{name}/inline-style-only", "<link" not in page and "src=" not in page)
    # Honest states: a consent hint, an error path and an empty state.
    check(f"{name}/consent-retry-text", "waiting for the viewer" in page)
    check(f"{name}/allow-hint", "Allow this page to edit data" in page)
    check(f"{name}/no-bridge-path", "Open this page in s.space" in page)
    check(f"{name}/removed-database-path", "was removed from this room" in page)
    check(f"{name}/subscribes-before-read", page.index("db.onChange(id") < page.index("db.read(id)"))

    # Every template is listed in the README with a "when to use" line.
    check(f"{name}/in-readme", f"{name}/" in readme)
    check(f"{name}/readme-says-when-to-use", "Use it when" in readme)

print(f"commons-app-builder templates: {len(passed)} passed")
