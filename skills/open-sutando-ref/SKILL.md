---
name: open-sutando-ref
description: "Resolve a fuzzy GitHub reference (issue number, PR number, or text description) to a URL and optionally open it in the browser."
user-invocable: true
---

# Open Sutando Ref

Resolves natural-language and numeric GitHub references to URLs and
optionally navigates to them. Closes [#903](https://github.com/sonichi/sutando/issues/903).

**Usage**: `/open-sutando-ref [--open] <ref>`

ARGUMENTS: $ARGUMENTS

## Reference formats

- `#874` / `874` — numeric (auto-detects PR vs issue)
- `PR 874` / `pull request 874` — explicit PR
- `issue 874` — explicit issue
- `"the result-marker PR"` — fuzzy text search via `gh pr/issue list --search`
- `"multi-core scheduler"` — searches both PRs and issues, returns best match

## What this skill does

1. Parse the reference: numeric, explicit PR/issue, or fuzzy query.
2. For numeric refs: check whether the number exists as a PR, fall back to issue.
3. For fuzzy refs: run `gh pr list --search <query>` then `gh issue list --search <query>`; return the top match URL.
4. Print the URL to stdout.
5. If `--open` flag is present, call `open <url>` to navigate in the default browser.

## Configuration

- `SUTANDO_GH_REPO` — override the target repo (default: inferred from `gh repo view` or `sonichi/sutando`).

## When to use

- Voice command: "open the result-marker PR" → invoke `resolve.py --open "result-marker PR"`
- Chat shorthand: "check #874" → invoke `resolve.py "#874"`
- Proactive loop step 10: when forwarding Discord references, resolve before posting
