#!/usr/bin/env python3
"""Validate Sutando's documentation index, metadata catalog, and local links."""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit


LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
ALLOWED_AUDIENCES = {"users", "operators", "contributors", "maintainers", "agents"}
ALLOWED_STATUSES = {"canonical", "active", "draft", "historical"}
REQUIRED_METADATA = {"title", "audience", "status", "canonical_for", "last_verified"}


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[3]


def markdown_files(repo: Path) -> List[Path]:
    files = list((repo / "docs").rglob("*.md"))
    for name in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md"):
        path = repo / name
        if path.exists():
            files.append(path)
    return sorted(set(files))


def docs_files(repo: Path) -> Set[str]:
    return {
        path.relative_to(repo).as_posix()
        for path in (repo / "docs").rglob("*.md")
    }


def raw_link_target(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and ">" in value:
        return value[1:value.index(">")]
    return value.split(maxsplit=1)[0]


def resolve_local_link(repo: Path, source: Path, raw: str) -> Optional[Path]:
    target = raw_link_target(raw)
    if not target or target.startswith("#"):
        return None
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("//"):
        return None
    path_part = unquote(parsed.path)
    if not path_part:
        return None
    if path_part.startswith("/"):
        return (repo / path_part.lstrip("/")).resolve()
    return (source.parent / path_part).resolve()


def links_in(path: Path) -> Iterable[str]:
    return LINK_RE.findall(path.read_text(encoding="utf-8"))


def audit_links(repo: Path, files: Iterable[Path]) -> List[str]:
    errors = []
    repo_real = repo.resolve()
    for source in files:
        for raw in links_in(source):
            resolved = resolve_local_link(repo, source, raw)
            if resolved is None:
                continue
            try:
                resolved.relative_to(repo_real)
            except ValueError:
                errors.append(f"{source.relative_to(repo)}: link escapes repository: {raw}")
                continue
            if not resolved.exists():
                errors.append(f"{source.relative_to(repo)}: broken local link: {raw}")
    return errors


def audit_index(repo: Path, expected: Set[str]) -> List[str]:
    index = repo / "docs" / "README.md"
    if not index.exists():
        return ["docs/README.md: documentation index is missing"]
    indexed = set()
    for raw in links_in(index):
        resolved = resolve_local_link(repo, index, raw)
        if resolved is None or resolved.suffix.lower() != ".md":
            continue
        try:
            indexed.add(resolved.relative_to(repo.resolve()).as_posix())
        except ValueError:
            continue
    required = expected - {"docs/README.md"}
    return [f"docs/README.md: unindexed document: {path}" for path in sorted(required - indexed)]


def valid_iso_date(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def audit_catalog(repo: Path, expected: Set[str]) -> List[str]:
    path = repo / "docs" / "catalog.json"
    if not path.exists():
        return ["docs/catalog.json: metadata catalog is missing"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"docs/catalog.json: invalid JSON: {exc}"]
    documents = payload.get("documents")
    if payload.get("schema_version") != 1 or not isinstance(documents, dict):
        return ["docs/catalog.json: expected schema_version=1 and a documents object"]

    errors = []
    catalog_paths = set(documents)
    for missing in sorted(expected - catalog_paths):
        errors.append(f"docs/catalog.json: missing metadata for {missing}")
    for extra in sorted(catalog_paths - expected):
        errors.append(f"docs/catalog.json: stale metadata for missing document {extra}")

    for doc_path in sorted(expected & catalog_paths):
        meta = documents[doc_path]
        if not isinstance(meta, dict):
            errors.append(f"docs/catalog.json: {doc_path} metadata must be an object")
            continue
        missing_fields = REQUIRED_METADATA - set(meta)
        if missing_fields:
            errors.append(
                f"docs/catalog.json: {doc_path} missing fields: {', '.join(sorted(missing_fields))}"
            )
            continue
        if not isinstance(meta["title"], str) or not meta["title"].strip():
            errors.append(f"docs/catalog.json: {doc_path} title must be non-empty")
        audience = meta["audience"]
        if not isinstance(audience, list) or not audience or any(
            item not in ALLOWED_AUDIENCES for item in audience
        ):
            errors.append(f"docs/catalog.json: {doc_path} has invalid audience")
        if meta["status"] not in ALLOWED_STATUSES:
            errors.append(f"docs/catalog.json: {doc_path} has invalid status")
        if not isinstance(meta["canonical_for"], str) or not meta["canonical_for"].strip():
            errors.append(f"docs/catalog.json: {doc_path} canonical_for must be non-empty")
        if not valid_iso_date(meta["last_verified"]):
            errors.append(f"docs/catalog.json: {doc_path} last_verified must be ISO date or null")
    return errors


def run(repo: Path) -> Tuple[List[str], int, int]:
    repo = repo.resolve()
    expected = docs_files(repo)
    files = markdown_files(repo)
    errors = []
    errors.extend(audit_index(repo, expected))
    errors.extend(audit_catalog(repo, expected))
    errors.extend(audit_links(repo, files))
    return errors, len(expected), len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo_root_from_script())
    args = parser.parse_args()
    errors, docs_count, checked_count = run(args.repo_root)
    if errors:
        for error in errors:
            print(f"docs-audit: ERROR: {error}", file=sys.stderr)
        print(f"docs-audit: FAIL ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print(f"docs-audit: PASS ({docs_count} indexed docs; {checked_count} Markdown files checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
