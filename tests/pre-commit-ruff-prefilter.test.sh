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

# --- runner FAILURE must fail open, not refuse (review of #2664) ------------
# A ruff that cannot RUN — bad config, version skew, offline download, cache
# error — is not a lint finding. The first version of this hook discarded the
# exit status and treated any non-empty output as a finding, so a broken runner
# refused clean commits: the exact opposite of the stated fail-open contract.
#
# Deterministic by construction: these fakes shadow ruff on PATH, so the result
# does not depend on which ruff version a contributor or reviewer happens to
# have. Two shapes, because a broken runner does not reliably exit 2.
fake_bin="$TMP/fakebin"; mkdir -p "$fake_bin"

make_fake () {   # $1 = exit code
    cat > "$fake_bin/ruff" <<FAKE
#!/usr/bin/env bash
echo "ruff failed" >&2
echo "  Cause: Failed to parse ruff.toml (unknown field)" >&2
exit $1
FAKE
    chmod +x "$fake_bin/ruff"
}

# A DISTINCT file per iteration: a successful commit consumes the staged file,
# so reusing one name leaves the next iteration with nothing staged and `git
# commit` fails for that reason instead — scoring as "refused" and inverting the
# result. (Cost one false FAIL while writing this.)
for code in 2 1; do
    make_fake "$code"
    printf 'import os\nimport sys\nprint(os, sys)\n' > "scripts/e$code.py"   # CLEAN content
    git add "scripts/e$code.py"
    if PATH="$fake_bin:$PATH" git commit -q -m x >/dev/null 2>&1; then
        ok "broken runner exiting $code -> clean commit still allowed (fail-open)"
    else
        bad "broken runner exiting $code -> clean commit still allowed (fail-open)"
    fi
    git reset -q HEAD -- . 2>/dev/null
    rm -f "scripts/e$code.py"
done
rm -f "$fake_bin/ruff"

# ...and the fail-open must not be blanket: a runner that WORKS and reports a
# real finding still refuses. Without this, "always fail open" would pass above.
cat > "$fake_bin/ruff" <<'FAKE'
#!/usr/bin/env bash
echo "E401 [*] Multiple imports on one line"
echo " --> scripts/f.py:1:1"
exit 1
FAKE
chmod +x "$fake_bin/ruff"
printf 'import os, sys\nprint(os, sys)\n' > scripts/f.py
git add scripts/f.py
if PATH="$fake_bin:$PATH" git commit -q -m x >/dev/null 2>&1; then
    bad "CONTROL: a working runner reporting findings still refuses"
else
    ok "CONTROL: a working runner reporting findings still refuses"
fi
git reset -q HEAD -- . 2>/dev/null; rm -f scripts/f.py "$fake_bin/ruff"

# 5. the pre-existing workspace/ guard still refuses.
echo hi > workspace/leak.txt
git add -f workspace/leak.txt
if git commit -q -m x >/dev/null 2>&1; then bad "workspace/ guard still refuses"; else ok "workspace/ guard still refuses"; fi

if [ "$fails" -eq 0 ]; then echo "pre-commit-ruff-prefilter: all checks passed"; else echo "FAILED: $fails"; fi
exit $(( fails > 0 ? 1 : 0 ))
