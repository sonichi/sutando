#!/usr/bin/env bash
# The pre-filter must refuse a violation in the STAGED BLOB, allow clean staged
# content, and never block a contributor with no ruff installed.
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

# Shadow `ruff` with a fake WORKING one: a runner that merely EXISTS may be
# unable to lint (uncached, offline), and these cases need capability.
work_bin="$TMP/workbin"; mkdir -p "$work_bin"
cat > "$work_bin/ruff" <<'WORKING'
#!/usr/bin/env bash
# Minimal stand-in for `ruff check --stdin-filename <path> -`: read stdin and
# report E401 exactly when the staged bytes contain a multi-import line.
body="$(cat)"
if grep -qE '^import [A-Za-z_]+, ' <<< "$body"; then
    echo "E401 [*] Multiple imports on one line"
    exit 1
fi
exit 0
WORKING
chmod +x "$work_bin/ruff"
export PATH="$work_bin:$PATH"

if true; then
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

# Both directions are needed: the violating path catches the word-split, the
# clean one proves the fix did not start refusing every path with a space.
printf 'import os, sys\nprint(os, sys)\n' > "scripts/my check.py"
git add "scripts/my check.py"
if git commit -q -m x >/dev/null 2>&1; then
    bad "spaced filename: violation is still refused (not word-split away)"
else
    ok "spaced filename: violation is still refused (not word-split away)"
fi
git reset -q HEAD -- . 2>/dev/null; rm -f "scripts/my check.py"

printf 'import os\nimport sys\nprint(os, sys)\n' > "scripts/my clean.py"
git add "scripts/my clean.py"
if git commit -q -m x >/dev/null 2>&1; then
    ok "spaced filename: clean file still commits"
else
    bad "spaced filename: clean file still commits"
fi
git reset -q HEAD -- . 2>/dev/null; rm -f "scripts/my clean.py"

# A runner that cannot RUN is not a finding and must fail open. Two shapes,
# because a broken runner does not reliably exit 2; the fakes shadow ruff.
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
# so a reused name leaves nothing staged and that failure scores as "refused".
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
