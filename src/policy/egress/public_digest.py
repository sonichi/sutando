#!/usr/bin/env python3
"""Which 64-hex digests in an outgoing body have verified public provenance.

A SHA-256 digest is shaped exactly like an opaque hex credential, so the secret
filters hold it. This module is the single place that decides a digest is safe:
only when the guard itself fetched the record that published it, from a source
the owner allowlisted. Text in the body is a pointer, never evidence, and a
field name or label never clears anything.

Two halves, both shared by every filter:
  * `verified_public_digests` (egress only) returns the digests proven public.
  * `mask_digests` hides exactly those digests from the detectors and returns
    how to put them back, so every detector downstream sees the same text.

Evidence today: a GitHub check run, cited in the body by URL, in an
allowlisted repository, with the allowlisted check name, created by GitHub
Actions, whose own output states `Assessment digest: \`<digest>\``. Anything
else, any fetch error or an empty allowlist clears nothing (fail closed).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable

DIGEST = re.compile(r"(?<![0-9A-Fa-f])[0-9a-f]{64}(?![0-9A-Fa-f])")
CHECK_RUN_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/"
    r"(?:runs|actions/runs/\d+/job)/(\d+)"
)
PUBLISHED_DIGEST = re.compile(r"Assessment digest: `([0-9a-f]{64})`")
CHECK_RUN_APP = "github-actions"
MAX_CITED_RUNS = 5
FETCH_TIMEOUT_S = 10

Fetch = Callable[[str], dict]


def configured_sources(repo_root) -> tuple:
    """`egress.verified_digest_sources` as ((owner/repo, check name), ...); empty on any error."""
    try:
        try:
            from sutando_config import load_config
        except ImportError:
            return ()
        listed = (load_config(Path(repo_root)).get("egress") or {}).get(
            "verified_digest_sources")
    except Exception:
        return ()
    if not isinstance(listed, list):
        return ()
    sources = []
    for entry in listed:
        if not isinstance(entry, dict):
            continue
        repo, check = entry.get("repo"), entry.get("check_name")
        if isinstance(repo, str) and isinstance(check, str) and repo and check:
            sources.append((repo.lower(), check))
    return tuple(sources)


def gh_fetch(api_path: str) -> dict:
    """GET one GitHub API path with the host's own `gh` credentials."""
    done = subprocess.run(
        ["gh", "api", api_path], capture_output=True, text=True,
        timeout=FETCH_TIMEOUT_S, check=True)
    return json.loads(done.stdout)


def _published_digests(run: dict, owner: str, name: str, check: str) -> set:
    if not isinstance(run, dict) or run.get("name") != check:
        return set()
    if (run.get("app") or {}).get("slug") != CHECK_RUN_APP:
        return set()
    html_url = str(run.get("html_url") or "").lower()
    if not html_url.startswith(f"https://github.com/{owner}/{name}/".lower()):
        return set()
    output = run.get("output") or {}
    text = "\n".join(str(output.get(k) or "") for k in ("title", "summary", "text"))
    return set(PUBLISHED_DIGEST.findall(text))


def verified_public_digests(body: str, sources: Iterable, fetch: Fetch = gh_fetch) -> frozenset:
    """Digests in `body` that a cited, allowlisted check run itself published."""
    allowed = {}
    for repo, check in sources or ():
        allowed.setdefault(str(repo).lower(), set()).add(check)
    candidates = set(DIGEST.findall(body or ""))
    if not allowed or not candidates:
        return frozenset()
    verified: set = set()
    cited = list(dict.fromkeys(CHECK_RUN_URL.findall(body)))[:MAX_CITED_RUNS]
    for owner, name, run_id in cited:
        checks = allowed.get(f"{owner}/{name}".lower())
        if not checks:
            continue
        try:
            run = fetch(f"repos/{owner}/{name}/check-runs/{run_id}")
        except Exception:
            continue
        for check in checks:
            verified |= _published_digests(run, owner, name, check)
    return frozenset(verified & candidates)


def mask_digests(text: str, digests: Iterable[str]):
    """Return (masked text, restore) with each verified digest swapped for a placeholder.

    Returns the text unchanged when a placeholder already occurs in it, so a
    body cannot pre-plant one to have arbitrary text substituted back.
    """
    pairs = [(f"verified-public-digest-{i}-end", d)
             for i, d in enumerate(sorted(set(digests or ())))]
    if not pairs or any(p in text for p, _ in pairs):
        return text, (lambda redacted: redacted)
    masked = text
    for placeholder, digest in pairs:
        masked = re.sub(rf"(?<![0-9A-Fa-f]){digest}(?![0-9A-Fa-f])", placeholder, masked)

    def restore(redacted: str) -> str:
        for placeholder, digest in pairs:
            redacted = redacted.replace(placeholder, digest)
        return redacted

    return masked, restore
