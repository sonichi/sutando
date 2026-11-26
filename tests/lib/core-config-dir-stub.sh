#!/bin/bash
# Stage a scripts/ dir whose sutando-config.sh answers `claude-sutando-config-dir`
# from $TEST_CORE_CONFIG_DIR (exit 1 when unset or empty) and hands every other
# subcommand to the real helper. The credential-proxy installer pins the CORE's
# config dir, which it asks that helper for, so a test names the core's dir here
# instead of through the installer's own environment.
#
#   source tests/lib/core-config-dir-stub.sh
#   stage_core_config_scripts "$REAL_REPO" "$STAGED_REPO"
stage_core_config_scripts() {
  local real="$1" stage="$2" f
  mkdir -p "$stage/scripts"
  for f in "$real"/scripts/*; do
    [ "${f##*/}" = "sutando-config.sh" ] || ln -s "$f" "$stage/scripts/${f##*/}"
  done
  cat > "$stage/scripts/sutando-config.sh" <<STUB
#!/bin/bash
if [ "\$1" = "claude-sutando-config-dir" ]; then
  [ -n "\${TEST_CORE_CONFIG_DIR:-}" ] || { echo "stub: no core config dir" >&2; exit 1; }
  printf '%s' "\$TEST_CORE_CONFIG_DIR"
  exit 0
fi
exec bash "$real/scripts/sutando-config.sh" "\$@"
STUB
  chmod +x "$stage/scripts/sutando-config.sh"
}
