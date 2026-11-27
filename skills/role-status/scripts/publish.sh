#!/usr/bin/env bash
# The core execs this; a bare PATH python3 may be the macOS CLT stub, whose mere
# execution raises a dialog, so the interpreter comes from the repo's resolver.
set -u
case "${BASH_SOURCE[0]}" in */*) _d="${BASH_SOURCE[0]%/*}";; *) _d=".";; esac
_d="$(CDPATH= cd -- "$_d" && pwd -P)" || exit 2
_repo="$(CDPATH= cd -- "$_d/../../.." && pwd -P)" || exit 2
_pb="$_repo/scripts/python-binary.sh"
[ -r "$_pb" ] || { echo "role-status/publish.sh: resolver missing: $_pb" >&2; exit 2; }
# shellcheck source=../../../scripts/python-binary.sh
. "$_pb"
_py="$(require_python "$_repo" "publish a role-status result")" || {
  echo "role-status/publish.sh: no runnable python3 resolved (SUTANDO_PY=${SUTANDO_PY:-<unset>}); nothing published" >&2
  exit 2
}
exec "$_py" "$_d/publish.py" "$@"
