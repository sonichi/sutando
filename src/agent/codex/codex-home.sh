# Shell adapter for src/codex_home.py, the one owner of the Codex core's home: resolve it,
# check it, create it only when permitted. Every launcher sources this; none creates the home.
#
# sutando_codex_home_prepare <repo> [python]
#   Exports the configured env (also in SUTANDO_CODEX_HOME_ENV / _VALUE), sets
#   SUTANDO_CODEX_HOME to the one home later steps use (empty when they must not write)
#   and SUTANDO_CODEX_HOME_REFUSED=1 on a refusal.
#   Returns 1 when the configuration cannot be read.

sutando_codex_desktop_hosted() {
  local real parent
  real="$(cd "$1" 2>/dev/null && pwd -P)" || return 1
  parent="${real%/*}"
  [ "${parent##*/}" = engine ] && [ -f "$parent/AGENTS.md" ]
}

sutando_codex_home_prepare() {
  local repo="$1" py="${2:-}" out rc=0 env="" value="" path=""
  SUTANDO_CODEX_HOME=""
  SUTANDO_CODEX_HOME_REFUSED=""
  SUTANDO_CODEX_HOME_ENV=""
  SUTANDO_CODEX_HOME_VALUE=""
  if [ -z "$py" ] && [ -r "$repo/scripts/python-binary.sh" ]; then
    # shellcheck source=../../../scripts/python-binary.sh
    . "$repo/scripts/python-binary.sh"
    py="$(resolve_python "$repo" 2>/dev/null || true)"
  fi
  if [ -n "$py" ]; then
    out="$("$py" "$repo/src/codex_home.py" prepare "$repo")" || rc=$?
  else
    rc=127
  fi
  { IFS= read -r env; IFS= read -r value; IFS= read -r path; } <<< "${out:-}"
  case "$rc" in
    0|3)
      if [ -n "$env" ] && [ -n "$value" ]; then
        export "$env=$value"
        SUTANDO_CODEX_HOME_ENV="$env"
        SUTANDO_CODEX_HOME_VALUE="$value"
      fi
      ;;
  esac
  # Only an explicit answer with a home permits; exit 0 with no path is a failure too.
  [ "$rc" = 0 ] && [ -z "$path" ] && rc=2
  case "$rc" in
    0) SUTANDO_CODEX_HOME="$path"; return 0 ;;
    3) ;;
    *)
      # Not desktop-hosted: nothing could be read, so nothing is created, as before.
      if ! sutando_codex_desktop_hosted "$repo"; then
        SUTANDO_CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
        [ "$rc" = 127 ] && return 0
        echo "codex home: could not resolve the configured Codex home" >&2
        return 1
      fi
      ;;
  esac
  SUTANDO_CODEX_HOME_REFUSED=1
  echo "  ⚠ the configured Codex home was refused (exit $rc); not creating it, and nothing is written there" >&2
  return 0
}
