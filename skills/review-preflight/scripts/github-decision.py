#!/usr/bin/env python3
"""Read-only PR policy receipt, including mutable decisions even at unchanged heads."""
import argparse
import datetime
import json
import re
import subprocess
import time
import sys
from pathlib import Path
from urllib.parse import quote

from decision_evidence import classify


def collect(repo, number, expected_head=None, runner=None, budget=24):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not isinstance(number, int) or number < 1:
        raise ValueError("expected OWNER/REPO and positive PR number")
    deadline = time.monotonic() + budget
    errors = []

    def read(args):
        try:
            if runner:
                proc = runner(["gh"] + args)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                proc = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=min(6, remaining))
            if proc.returncode != 0:
                raise ValueError()
            return json.loads(proc.stdout)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            errors.append("Decision query unavailable; do not infer absence")
            return None

    endpoint = f"repos/{repo}/pulls/{number}"
    before = read(["api", endpoint])
    base = ((before or {}).get("base") or {}).get("ref") if isinstance(before, dict) else None
    status = read(["pr", "view", str(number), "--repo", repo, "--json", "headRefOid,reviewDecision,mergeStateStatus"])
    rules = read(["api", f"repos/{repo}/rules/branches/{quote(base, safe='')}"]) if base else None
    pages = read(["api", f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate", "--slurp"])
    comments = [c for page in pages for c in page] if isinstance(pages, list) and all(isinstance(page, list) for page in pages) else None
    after = read(["api", endpoint])
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
        from chat_redaction import redact_chat_body
    except Exception:
        redact_chat_body = lambda text: "[text withheld: redactor unavailable]"
        errors.append("Decision text redaction unavailable")
    result = classify(before, after, status, rules, comments, expected_head, redact=redact_chat_body)
    result.update(schema=1, repository=repo, pr=number,
                  observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    result["errors"].extend(errors)
    if errors:
        result["evidence_status"] = "unknown"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("pr", type=int)
    parser.add_argument("--expect-head")
    args = parser.parse_args()
    result = collect(args.repo, args.pr, args.expect_head)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["evidence_status"] == "stable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
