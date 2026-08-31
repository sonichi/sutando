#!/usr/bin/env bash
# A capped bundle capture must say it capped.
#
# gather.sh wrote prs-open.txt with `gh pr list --limit 20`. Measured 2026-08-31
# on sonichi/sutando: 20 of 117 open PRs captured, 30 of 366 recent merges — with
# nothing in either file marking it as a page. A reader (human or agent) opening
# a file named "prs-open" takes it for the open set, and a diagnosis built on 17%
# of the population looks exactly like one built on all of it.
#
# These run the real script against a stubbed gh, so a regression in the footer
# logic fails here rather than being described.
#
# Run: bash tests/self-diagnose-capped-capture.test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAP="$REPO_ROOT/skills/self-diagnose/scripts/capped-capture.sh"
fails=0; checks=0
check() {
	local desc="$1"; shift
	checks=$((checks + 1))
	if "$@"; then echo "  ok   $desc"; else echo "  FAIL $desc"; fails=$((fails + 1)); fi
}

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Stub gh: `--limit 1000 --json number` answers the population question, anything
# else returns POP_SEEN rows. POP is what the stub pretends the repo holds.
mk_stub() {  # <population>
	cat > "$TMP/gh" <<STUB
#!/usr/bin/env bash
pop=$1
for a in "\$@"; do [ "\$a" = "1000" ] && counting=1; done
if [ -n "\${counting:-}" ]; then printf '%s\n' "\$pop"; exit 0; fi
i=0; while [ "\$i" -lt "\$pop" ] && [ "\$i" -lt 20 ]; do echo "#\$i row"; i=\$((i+1)); done
STUB
	chmod +x "$TMP/gh"
}

# --- capped: the footer must name the population and the omission ---
mk_stub 117
bash "$CAP" "$TMP/gh" "$TMP/out.txt" 20 '.[]' --state open --json number
check "capped run appends a footer naming total and omitted count" \
	grep -q '20 of 117 shown .* 97 omitted' "$TMP/out.txt"
check "capped run still writes the rows themselves" \
	grep -q '^#0 row$' "$TMP/out.txt"

# --- NOT capped: a complete capture must NOT claim truncation ---
mk_stub 7
bash "$CAP" "$TMP/gh" "$TMP/full.txt" 20 '.[]' --state open --json number
check "uncapped run appends no footer" \
	bash -c '! grep -q "shown\|omitted\|truncated" "$1"' _ "$TMP/full.txt"

# --- population unknown: must degrade to a warning, never to silence ---
printf '#!/usr/bin/env bash\nfor a in "$@"; do [ "$a" = "1000" ] && exit 3; done\necho "#1 row"\n' \
	> "$TMP/gh"; chmod +x "$TMP/gh"
bash "$CAP" "$TMP/gh" "$TMP/unk.txt" 20 '.[]' --state open --json number
check "uncountable population still warns the file may be truncated" \
	grep -q 'population unknown' "$TMP/unk.txt"

# --- the caller must be wired to the helper, with the gh binary in place ---
G="$REPO_ROOT/skills/self-diagnose/scripts/gather.sh"
# Match the dispatch as WRITTEN: the helper path is held in $_cap, so a grep for
# the literal filename on the call line returns 0 and reads as "not wired".
check "gather.sh binds _cap to the helper" \
	grep -q '_cap=.*capped-capture\.sh' "$G"
check "gather.sh routes both PR captures through it, gh binary first" \
	bash -c 'test "$(grep -cF "$2" "$1")" = 2' _ "$G" 'bash "$_cap" "$GH"'

check "gather.sh no longer caps a PR list inline" \
	bash -c '! grep -q "pr list .*--limit" "$1"' _ "$G"

echo ""
if [ "$fails" -eq 0 ]; then echo "OK — $checks checks passed"; else echo "$fails of $checks FAILED"; fi
exit $((fails > 0))
