#!/usr/bin/env python3
"""Allowlisted GitHub capability discovery and atomic skill installation."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
from util_paths import claude_home_path  # noqa: E402

MANIFEST = SKILL_DIR / "manifest.json"
METADATA = ".sutando-source.json"
MAX_FILES = 500
MAX_BYTES = 25 * 1024 * 1024
TEXT_LIMIT = 512 * 1024


@dataclasses.dataclass(frozen=True)
class Source:
    id: str
    repo: str
    root: str
    kind: str
    installable: bool


def load_sources() -> dict[str, Source]:
    manifest = json.loads(MANIFEST.read_text())
    raw = manifest["config"]["TRUSTED_CAPABILITY_SOURCES"]
    return {item["id"]: Source(**item) for item in json.loads(raw)}


class GitHubClient:
    def __init__(self, api_base: str = "https://api.github.com") -> None:
        self.api_base = api_base.rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "sutando-trusted-capabilities/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"GitHub request failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub request failed: {exc.reason}") from exc

    def json(self, path: str) -> Any:
        return json.loads(self._request(f"{self.api_base}{path}"))

    def head(self, repo: str) -> tuple[str, str]:
        info = self.json(f"/repos/{repo}")
        branch = info["default_branch"]
        commit = self.json(f"/repos/{repo}/commits/{urllib.parse.quote(branch)}")["sha"]
        return branch, commit

    def tree(self, repo: str, commit: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(commit, safe="")
        payload = self.json(f"/repos/{repo}/git/trees/{encoded}?recursive=1")
        if payload.get("truncated"):
            raise RuntimeError(f"{repo} tree is too large for safe recursive discovery")
        return payload["tree"]

    def file(self, repo: str, commit: str, path: str) -> bytes:
        owner, name = repo.split("/", 1)
        quoted_path = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))
        url = f"https://raw.githubusercontent.com/{owner}/{name}/{commit}/{quoted_path}"
        return self._request(url)


def clean_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe repository path: {value!r}")
    return path.as_posix().rstrip("/")


def source_path(source: Source, value: str) -> str:
    path = clean_repo_path(value)
    if source.root != ".":
        root = clean_repo_path(source.root)
        if path != root and not path.startswith(root + "/"):
            raise ValueError(f"{path} is outside trusted root {root} for {source.id}")
    return path


def skill_paths(source: Source, tree: list[dict[str, Any]]) -> list[str]:
    root = "" if source.root == "." else source.root.rstrip("/") + "/"
    found = []
    for entry in tree:
        path = entry.get("path", "")
        if entry.get("type") == "blob" and path.startswith(root) and path.endswith("/SKILL.md"):
            found.append(path[: -len("/SKILL.md")])
    return sorted(found)


def files_under(
    path: str, tree: list[dict[str, Any]], *, require_skill: bool = True
) -> list[dict[str, Any]]:
    path = clean_repo_path(path)
    prefix = path + "/"
    files = [
        entry
        for entry in tree
        if entry.get("type") == "blob" and entry.get("path", "").startswith(prefix)
    ]
    if require_skill and not any(entry["path"] == f"{path}/SKILL.md" for entry in files):
        raise ValueError(f"{path} is not a skill directory (SKILL.md not found)")
    if not files:
        raise ValueError(f"no files found below {path}")
    if len(files) > MAX_FILES:
        raise ValueError(f"skill has {len(files)} files; safety limit is {MAX_FILES}")
    declared = sum(int(entry.get("size") or 0) for entry in files)
    if declared > MAX_BYTES:
        raise ValueError(f"skill is {declared} bytes; safety limit is {MAX_BYTES}")
    return files


def assess(entries: list[dict[str, Any]], contents: dict[str, bytes] | None = None) -> list[str]:
    findings: set[str] = set()
    contents = contents or {}
    executable_ext = {".sh", ".py", ".js", ".ts", ".rb", ".go", ".rs"}
    native_ext = {".dylib", ".so", ".dll", ".exe", ".bin"}
    for entry in entries:
        path = entry["path"]
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in executable_ext:
            findings.add("contains executable scripts")
        if suffix in native_ext:
            findings.add("contains native or binary files")
        if PurePosixPath(path).name in {"package.json", "requirements.txt", "pyproject.toml"}:
            findings.add("declares external dependencies")
        body = contents.get(path, b"")[:TEXT_LIMIT].decode("utf-8", "ignore")
        if re.search(r"\b(curl|wget|requests\.|urllib|fetch\(|https?://)", body):
            findings.add("references network access")
        if re.search(r"\b(subprocess|child_process|os\.system|eval\(|exec\()", body):
            findings.add("can launch commands or evaluate code")
        if re.search(r"\b(rm\s+-|unlink\(|rmtree\(|shutil\.move)", body):
            findings.add("contains destructive filesystem operations")
    return sorted(findings) or ["no static risk signals found (manual review still required)"]


def fetch_contents(
    client: GitHubClient,
    source: Source,
    commit: str,
    entries: list[dict[str, Any]],
) -> dict[str, bytes]:
    total = 0
    output: dict[str, bytes] = {}
    for entry in entries:
        data = client.file(source.repo, commit, entry["path"])
        total += len(data)
        if total > MAX_BYTES:
            raise ValueError(f"download exceeded safety limit of {MAX_BYTES} bytes")
        output[entry["path"]] = data
    return output


def destination_root(explicit: str | None) -> Path:
    return Path(explicit).expanduser().resolve() if explicit else claude_home_path("skills")


def slug_for(path: str) -> str:
    slug = PurePosixPath(path).name.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ValueError(f"skill directory name is not a safe slug: {slug!r}")
    return slug


def install_skill(
    source: Source,
    path: str,
    commit: str,
    entries: list[dict[str, Any]],
    contents: dict[str, bytes],
    dest_root: Path,
) -> Path:
    slug = slug_for(path)
    dest_root.mkdir(parents=True, exist_ok=True)
    target = dest_root / slug
    temp = Path(tempfile.mkdtemp(prefix=f".{slug}.", dir=dest_root))
    try:
        prefix = clean_repo_path(path) + "/"
        for entry in entries:
            relative = PurePosixPath(entry["path"][len(prefix) :])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe skill member: {entry['path']}")
            output = temp.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(contents[entry["path"]])
        metadata = {
            "schema_version": 1,
            "source": source.id,
            "repo": source.repo,
            "path": clean_repo_path(path),
            "commit": commit,
        }
        (temp / METADATA).write_text(json.dumps(metadata, indent=2) + "\n")
        backup = target.with_name(f".{slug}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(temp, target)
        except BaseException:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return target
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def resolve_source(source_id: str) -> Source:
    sources = load_sources()
    if source_id not in sources:
        raise ValueError(f"source is not trusted: {source_id}")
    return sources[source_id]


def cmd_sources(_args: argparse.Namespace, _client: GitHubClient) -> int:
    for source in load_sources().values():
        action = "installable" if source.installable else "discovery-only"
        print(f"{source.id:24} {source.kind:6} {action:14} {source.repo}/{source.root}")
    return 0


def cmd_search(args: argparse.Namespace, client: GitHubClient) -> int:
    needle = args.query.lower()
    matches = 0
    for source in load_sources().values():
        if source.kind != "skill":
            _branch, commit = client.head(source.repo)
            tree = client.tree(source.repo, commit)
            if source.kind == "tool":
                root = "" if source.root == "." else source.root.rstrip("/") + "/"
                paths = sorted(
                    {
                        entry["path"]
                        for entry in tree
                        if entry.get("type") == "blob"
                        and entry.get("path", "").startswith(root)
                        and needle in entry.get("path", "").lower()
                    }
                )
                for path in paths:
                    print(f"{source.id}:{path} @ {commit[:12]} [discovery-only]")
                    matches += 1
                    if matches >= args.limit:
                        return 0
            else:
                readmes = [
                    entry["path"]
                    for entry in tree
                    if entry.get("type") == "blob"
                    and PurePosixPath(entry.get("path", "")).parent == PurePosixPath(".")
                    and PurePosixPath(entry["path"]).name.lower().startswith("readme")
                ]
                for readme in readmes[:1]:
                    text = client.file(source.repo, commit, readme).decode("utf-8", "replace")
                    for line in text.splitlines():
                        if needle in line.lower():
                            compact = re.sub(r"\s+", " ", line).strip()
                            position = compact.lower().find(needle)
                            start = max(0, position - 60)
                            summary = compact[start : start + 180]
                            if start:
                                summary = "…" + summary
                            if start + 180 < len(compact):
                                summary += "…"
                            print(f"{source.id}:{readme} @ {commit[:12]} — {summary}")
                            matches += 1
                            if matches >= args.limit:
                                return 0
            continue
        _branch, commit = client.head(source.repo)
        for path in skill_paths(source, client.tree(source.repo, commit)):
            if needle in f"{source.id} {path}".lower():
                print(f"{source.id}:{path} @ {commit[:12]}")
                matches += 1
                if matches >= args.limit:
                    return 0
    if not matches:
        print("No matching capabilities found.")
        return 1
    return 0


def resolve_remote(
    client: GitHubClient,
    source: Source,
    path: str,
    *,
    require_skill: bool = True,
    commit: str | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, bytes]]:
    path = source_path(source, path)
    if commit is None:
        _branch, commit = client.head(source.repo)
    entries = files_under(
        path, client.tree(source.repo, commit), require_skill=require_skill
    )
    contents = fetch_contents(client, source, commit, entries)
    return commit, entries, contents


def cmd_inspect(args: argparse.Namespace, client: GitHubClient) -> int:
    source = resolve_source(args.source)
    if source.kind == "index":
        raise ValueError(f"{source.id} is an index; inspect its linked project directly")
    commit, entries, contents = resolve_remote(
        client, source, args.path, require_skill=source.kind == "skill"
    )
    print(f"source: {source.id} ({source.repo})")
    print(f"path: {source_path(source, args.path)}")
    print(f"commit: {commit}")
    print(f"files: {len(entries)}")
    print("risk findings:")
    for finding in assess(entries, contents):
        print(f"- {finding}")
    return 0


def cmd_install(args: argparse.Namespace, client: GitHubClient) -> int:
    source = resolve_source(args.source)
    if not source.installable:
        raise ValueError(
            f"{source.id} is discovery-only; tool installation needs a source-specific review"
        )
    path = source_path(source, args.path)
    if args.yes and not args.commit:
        raise ValueError("install write requires --commit <sha> from the dry run")
    if args.commit and not re.fullmatch(r"[0-9a-fA-F]{40}", args.commit):
        raise ValueError("--commit must be a full 40-character Git SHA")
    commit, entries, contents = resolve_remote(
        client, source, path, commit=args.commit
    )
    root = destination_root(args.dest_root)
    target = root / slug_for(path)
    print(f"resolved: {source.repo}:{path} @ {commit}")
    print(f"destination: {target}")
    for finding in assess(entries, contents):
        print(f"risk: {finding}")
    if not args.yes:
        print(
            "dry run only; install this exact reviewed commit with: "
            f"install {args.source} {path} --commit {commit} --yes"
        )
        return 0
    installed = install_skill(source, path, commit, entries, contents, root)
    print(f"installed: {installed}")
    return 0


def cmd_update(args: argparse.Namespace, client: GitHubClient) -> int:
    root = destination_root(args.dest_root)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        raise ValueError(f"unsafe skill slug: {args.slug!r}")
    target = root / args.slug
    metadata_path = target / METADATA
    if not metadata_path.is_file():
        raise ValueError(f"no managed install metadata at {metadata_path}")
    metadata = json.loads(metadata_path.read_text())
    source = resolve_source(metadata["source"])
    if not source.installable:
        raise ValueError(
            f"{source.id} is no longer installable; update is disabled by current policy"
        )
    if args.yes and not args.commit:
        raise ValueError("update write requires --commit <sha> from the dry run")
    if args.commit and not re.fullmatch(r"[0-9a-fA-F]{40}", args.commit):
        raise ValueError("--commit must be a full 40-character Git SHA")
    commit, entries, contents = resolve_remote(
        client, source, metadata["path"], commit=args.commit
    )
    if commit == metadata["commit"]:
        print(f"up to date: {args.slug} @ {commit}")
        return 0
    print(f"update: {metadata['commit']} -> {commit}")
    for finding in assess(entries, contents):
        print(f"risk: {finding}")
    if not args.yes:
        print(
            "dry run only; update to this exact reviewed commit with: "
            f"update {args.slug} --commit {commit} --yes"
        )
        return 0
    installed = install_skill(source, metadata["path"], commit, entries, contents, root)
    print(f"updated: {installed}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--api-base", default="https://api.github.com", help=argparse.SUPPRESS)
    sub = result.add_subparsers(dest="command", required=True)
    sources = sub.add_parser("sources", help="list the allowlisted sources")
    sources.set_defaults(func=cmd_sources)
    search = sub.add_parser("search", help="search skill paths and discovery sources")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=cmd_search)
    inspect = sub.add_parser("inspect", help="inspect a skill at the current upstream commit")
    inspect.add_argument("source")
    inspect.add_argument("path")
    inspect.set_defaults(func=cmd_inspect)
    install = sub.add_parser("install", help="inspect and atomically install a trusted skill")
    install.add_argument("source")
    install.add_argument("path")
    install.add_argument("--dest-root")
    install.add_argument("--commit")
    install.add_argument("--yes", action="store_true")
    install.set_defaults(func=cmd_install)
    update = sub.add_parser("update", help="update a previously managed skill")
    update.add_argument("slug")
    update.add_argument("--dest-root")
    update.add_argument("--commit")
    update.add_argument("--yes", action="store_true")
    update.set_defaults(func=cmd_update)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args, GitHubClient(args.api_base))
    except (KeyError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"trusted-capabilities: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
