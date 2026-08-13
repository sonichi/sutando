#!/bin/bash
# Shared Homebrew bin-directory resolution for bash scripts. Source it with:
#
#   source "$REPO/src/homebrew_bin.sh"
#   BREW_BIN="$(resolve_homebrew_bin)"
#
# Asks brew where it lives before probing known prefixes: a host with a custom
# HOMEBREW_PREFIX, or a leftover /opt/homebrew/bin beside a real Intel install,
# gets the right answer instead of the first directory that happens to exist.
#
# The candidate probe stays on one line because REVIEW.md rule 6 exempts an
# /opt/homebrew literal only when paired with its /usr/local companion there.

# Callers embed the result in a launchd plist PATH, so the fallbacks must always
# yield a real directory rather than an empty string.
resolve_homebrew_bin() {
    local prefix candidate
    local candidates=(/opt/homebrew/bin /usr/local/bin)

    if prefix="$(brew --prefix 2>/dev/null)" && [ -n "$prefix" ] && [ -d "$prefix/bin" ]; then
        echo "$prefix/bin"
        return 0
    fi

    for candidate in "${candidates[@]}"; do
        if [ -d "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done

    prefix="$(command -v bash 2>/dev/null)"
    if [ -n "$prefix" ]; then
        dirname "$prefix"
        return 0
    fi

    echo /usr/bin
}
