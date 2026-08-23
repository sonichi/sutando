#!/usr/bin/env bash
# pool-runtime-drive.sh — sourceable library: the ONE owner of per-runtime
# session-driving policy for the core pool (#880). Sourcing has no side effects.
#
# Two adapters bind it and neither may keep a private copy:
#   scripts/pool-core-wrapper.sh — the in-session sweep nudge
#   scripts/kick-pool.sh         — the watchdog that recovers a stalled session
# It owns: the runtime allowlist, runtime resolution from an installed plist,
# prompt/idle/menu/busy markers, the nudge text, the submit key, and the
# busy -> menu -> staged -> type -> submit sequence. Adapters keep only their
# own tmux mechanics (which binary, which socket), injected as a function name.
#
# Recognition is POSITIVE and fails CLOSED: a session is typed into only when
# the pane shows that runtime's idle prompt. An unrecognized pane, an
# unresolvable runtime, or a runtime this library does not know is skipped and
# logged — never typed into with another runtime's text.
#
# Contract:
#   pool_runtime_supported <runtime>                  -> rc 0 known, 1 unknown
#   pool_runtime_from_plist <plist>                   -> prints runtime; rc 1 unresolvable
#   pool_drive_nudge_text <runtime> <core-id>         -> prints the pool-entry text
#   pool_drive_nudge <runtime> <session> <tmux-fn> <core-id>
#         -> rc 0 sent, 1 deferred (session busy), 2 unsupported runtime
#   pool_drive_kick <runtime> <session> <tmux-fn> <core-id>
#         -> rc 0 kicked, 1 skipped (busy/menu/staged/unrecognized), 2 unsupported
# <tmux-fn> is the NAME of a caller-provided function that runs tmux with that
# caller's binary and socket; this library never resolves tmux itself.

# Claude Code renders the prompt separator as U+00A0, not a space: an ASCII-only
# marker matches neither its idle nor its staged input line.
POOL_DRIVE_NBSP=$'\302\240'
POOL_DRIVE_ESC=$'\033'

pool_runtime_supported() {
  case "${1:-}" in claude|codex) return 0 ;; *) return 1 ;; esac
}

# Absent POOL_RUNTIME means a plist written before the runtime dimension
# existed, which could only have been claude. A file that is not a readable XML
# plist is unresolvable, which is NOT the same as absent.
pool_runtime_from_plist() {
  local plist="${1:-}" body rt
  [ -n "$plist" ] && [ -r "$plist" ] || return 1
  body=$(tr '\n' ' ' < "$plist" 2>/dev/null) || return 1
  case "$body" in *"<plist"*) : ;; *) return 1 ;; esac
  rt=$(printf '%s' "$body" | sed -n \
    's:.*<key>POOL_RUNTIME</key>[[:space:]]*<string>\([^<]*\)</string>.*:\1:p')
  [ -n "$rt" ] || rt=claude
  pool_runtime_supported "$rt" || return 1
  printf '%s' "$rt"
}

pool_drive_nudge_text() {
  case "${1:-}" in
    claude) printf '%s' '/proactive-loop-pool pass' ;;
    codex)
      # Codex has no slash-command surface, so the pool-mode entry is a prompt.
      # Keep it pointing at CODEX.md rather than restating the claim protocol.
      printf '%s' "Sutando pool mode. You are core-${2:-}. Do not read task files or write results/ directly — follow skills/proactive-loop-pool/CODEX.md: acquire work first, and complete only through the finish helper." ;;
    *) return 2 ;;
  esac
}

# Per-runtime knobs. Every Claude-specific assumption kick-pool used to carry
# inline is one row here, so a second runtime cannot silently inherit it.
_pool_drive_knob() {
  local rt="$1" knob="$2" esc="$POOL_DRIVE_ESC" nb="$POOL_DRIVE_NBSP"
  case "$rt:$knob" in
    # capture_mode: `ansi` keeps tmux's SGR codes, which is the only way to tell
    # codex's dim placeholder hint from text a caller actually staged.
    claude:capture_mode) printf 'raw' ;;
    codex:capture_mode) printf 'ansi' ;;
    # Both TUIs happen to print the same busy string; stated per runtime so the
    # coincidence is a declared assumption rather than a silent one.
    claude:busy_marker) printf '%s' 'esc to interrupt' ;;
    codex:busy_marker) printf '%s' 'esc to interrupt' ;;
    claude:menu_re) printf '%s' 'Esc to cancel|Enter to select' ;;
    codex:menu_re) printf '%s' 'Press enter to continue' ;;
    # menu_key empty = no safe dismissal. Codex's startup dialogs take Enter,
    # and Enter there selects the highlighted item (e.g. "Update now").
    claude:menu_key) printf '%s' 'Escape' ;;
    codex:menu_key) printf '' ;;
    claude:prompt_re) printf '%s' "^❯([ $nb]|\$)" ;;
    codex:prompt_re) printf '%s' "^${esc}\\[1m›${esc}\\[0m" ;;
    claude:idle_re) printf '%s' "^❯[ $nb]*\$" ;;
    codex:idle_re) printf '%s' "^${esc}\\[1m›${esc}\\[0m *(${esc}\\[2m.*)?\$" ;;
    claude:staged_submit_re) printf '%s' "^❯[ $nb]*/proactive-loop-pool[ $nb]*\$" ;;
    codex:staged_submit_re) printf '%s' "^${esc}\\[1m›${esc}\\[0m Sutando pool mode\\." ;;
    claude:submit_key) printf '%s' 'Enter' ;;
    # Codex's TUI submits on C-m; the symbolic Enter can stage without sending.
    codex:submit_key) printf '%s' 'C-m' ;;
    *) return 2 ;;
  esac
}

_pool_drive_capture() {
  local rt="$1" sess="$2" tmux_fn="$3" scroll="${4:-}"
  local flags=()
  [ "$(_pool_drive_knob "$rt" capture_mode)" = "ansi" ] && flags+=(-e)
  [ -n "$scroll" ] && flags+=(-S "-$scroll")
  "$tmux_fn" capture-pane -t "$sess" -p ${flags[@]+"${flags[@]}"} 2>/dev/null
}

# Type the runtime's pool entry and submit it. Claude's input IS a durable
# queue, so its nudge is unguarded; codex interleaves keystrokes into a running
# turn, so its nudge stays unspent until the session is free.
pool_drive_send() {
  local rt="$1" sess="$2" tmux_fn="$3" text="$4"
  case "$rt" in
    claude)
      "$tmux_fn" send-keys -t "$sess" "$text" "$(_pool_drive_knob "$rt" submit_key)"
      ;;
    codex)
      "$tmux_fn" send-keys -t "$sess" -l -- "$text"
      sleep 0.15
      "$tmux_fn" send-keys -t "$sess" "$(_pool_drive_knob "$rt" submit_key)"
      ;;
    *) return 2 ;;
  esac
  return 0
}

pool_drive_nudge() {
  local rt="$1" sess="$2" tmux_fn="$3" core_id="$4" text
  pool_runtime_supported "$rt" || return 2
  if [ "$rt" = "codex" ]; then
    if "$tmux_fn" capture-pane -p -t "$sess" 2>/dev/null \
        | tail -12 | grep -Fq "$(_pool_drive_knob "$rt" busy_marker)"; then
      return 1
    fi
  fi
  text=$(pool_drive_nudge_text "$rt" "$core_id") || return 2
  pool_drive_send "$rt" "$sess" "$tmux_fn" "$text"
}

pool_drive_kick() {
  local rt="$1" sess="$2" tmux_fn="$3" core_id="$4"
  local pane last menu_re menu_key staged_re
  if ! pool_runtime_supported "$rt"; then
    echo "$sess: UNRESOLVED RUNTIME ('$rt') — skip (won't type another runtime's text)"
    return 2
  fi
  pane=$(_pool_drive_capture "$rt" "$sess" "$tmux_fn" 8)

  if printf '%s\n' "$pane" | grep -qF "$(_pool_drive_knob "$rt" busy_marker)"; then
    echo "$sess: BUSY (processing) — skip"
    return 1
  fi

  menu_re=$(_pool_drive_knob "$rt" menu_re)
  menu_key=$(_pool_drive_knob "$rt" menu_key)
  if [ -n "$menu_re" ] && printf '%s\n' "$pane" | grep -qE "$menu_re"; then
    if [ -z "$menu_key" ]; then
      echo "$sess: in interactive menu, no safe dismiss key for $rt — skip"
      return 1
    fi
    echo "$sess: in interactive menu → $menu_key"
    "$tmux_fn" send-keys -t "$sess" "$menu_key"
    sleep 1
    pane=$(_pool_drive_capture "$rt" "$sess" "$tmux_fn" 8)
  fi

  # The LAST prompt-marker line is the live input box; earlier ones are history.
  last=$(printf '%s\n' "$pane" | grep -E "$(_pool_drive_knob "$rt" prompt_re)" | tail -1)
  if [ -z "$last" ]; then
    echo "$sess: no $rt prompt recognized in pane — skip (fail closed)"
    return 1
  fi

  staged_re=$(_pool_drive_knob "$rt" staged_submit_re)
  if [ -n "$staged_re" ] && printf '%s\n' "$last" | grep -qE "$staged_re"; then
    echo "$sess: pool entry staged → $(_pool_drive_knob "$rt" submit_key)"
    "$tmux_fn" send-keys -t "$sess" "$(_pool_drive_knob "$rt" submit_key)"
    return 0
  fi

  if ! printf '%s\n' "$last" | grep -qE "$(_pool_drive_knob "$rt" idle_re)"; then
    echo "$sess: HAS STAGED INPUT — skip (won't overwrite)"
    return 1
  fi

  echo "$sess: idle REPL → type + send $rt pool entry"
  pool_drive_send "$rt" "$sess" "$tmux_fn" "$(pool_drive_nudge_text "$rt" "$core_id")"
}
