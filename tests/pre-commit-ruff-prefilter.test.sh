#!/usr/bin/env bash
# The pre-commit ruff pre-filter must block a violation IN THE STAGED BLOB,
# allow clean staged content, and — critically — never block a contributor who
# has no ruff installed.
#
# Built in a throwaway repo so it cannot touch the developer's index. The
# load-bearing case is the third: staged content clean, working tree dirty. The
# hook lints `git show :path`, not the file on disk, and only that case tells
# the two apart. A hook that linted the worktree would block a commit whose
# staged bytes are fine.
set -uo pipefail

HOOK_SRC="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
ok () { printf '  ok   %s\n' "$1"; }
bad () { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP" || exit 1
git init -q .
git config user.email t@example.com
git config user.name t
mkdir -p .githooks scripts workspace
cp "$HOOK_SRC/.githooks/pre-commit" .githooks/pre-commit
cp "$HOOK_SRC/ruff.toml" ruff.toml 2>/dev/null || true
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
touch workspace/.gitkeep
git add -A && git commit -q --no-verify -m base

have_ruff=0
if command -v ruff >/dev/null 2>&1 || command -v uvx >/dev/null 2>&1 || command -v uv >/dev/null 2>&1; then
    have_ruff=1
fi

if [ "$have_ruff" = 1 ]; then
    # 1. a violation in the STAGED blob is refused
    printf 'import os, sys\nprint(os, sys)\n' > scripts/a.py
    git add scripts/a.py
    if git commit -q -m x >/dev/null 2>&1; then bad "violating staged python is refused"; else ok "violating staged python is refused"; fi
    git reset -q HEAD -- . ; rm -f scripts/a.py

    # 2. clean staged content commits normally
    printf 'import os\nimport sys\nprint(os, sys)\n' > scripts/b.py
    git add scripts/b.py
    if git commit -q -m x >/dev/null 2>&1; then ok "clean staged python commits"; else bad "clean staged python commits"; fi
    rm -f scripts/b.py

    # 3. THE control — staged clean, worktree violating. Proves the hook reads
    #    the staged blob. A worktree-linting hook fails exactly here.
    printf 'import os\nimport sys\nprint(os, sys)\n' > scripts/c.py
    git add scripts/c.py
    printf 'import os, sys\nprint(os, sys)\n' > scripts/c.py
    if git commit -q -m x >/dev/null 2>&1; then ok "staged-clean + dirty worktree commits (staged blob is what is linted)"
    else bad "staged-clean + dirty worktree commits (staged blob is what is linted)"; fi
    git checkout -q -- scripts/c.py 2>/dev/null || true; rm -f scripts/c.py
else
    printf '  skip no ruff runner here; blocking cases not exercised\n'
fi

# 4. fail-open: no runner on PATH must never block a commit.
printf 'import os, sys\nprint(os, sys)\n' > scripts/d.py
git add scripts/d.py
if env PATH=/usr/bin:/bin git commit -q -m x >/dev/null 2>&1; then ok "no ruff on PATH -> commit still allowed (fail-open)"
else bad "no ruff on PATH -> commit still allowed (fail-open)"; fi
rm -f scripts/d.py

# 5. the pre-existing workspace/ guard still refuses.
echo hi > workspace/leak.txt
git add -f workspace/leak.txt
if git commit -q -m x >/dev/null 2>&1; then bad "workspace/ guard still refuses"; else ok "workspace/ guard still refuses"; fi

if [ "$fails" -eq 0 ]; then echo "pre-commit-ruff-prefilter: all checks passed"; else echo "FAILED: $fails"; fi
exit $(( fails > 0 ? 1 : 0 ))
