#!/usr/bin/env bash
# Unbounded, this probe blocks forever on a session with nobody to answer the
# AppleScript prompt, and startup never reaches the services after it.

ACCESSIBILITY_PROBE_TIMEOUT_S="${ACCESSIBILITY_PROBE_TIMEOUT_S:-5}"

# 0 granted, 124 TIMED OUT, other non-zero denied. Timeout is a fact about the
# session and denial one about the grant; only denial means System Settings.
accessibility_probe() {
  local secs="${1:-$ACCESSIBILITY_PROBE_TIMEOUT_S}"
  # perl must FORK, not exec: after exec the ALRM handler is gone and the child
  # dies as signal 14, reporting 142 for something that is not a denial.
  perl -e '
    my $secs = shift;
    my $pid = fork();
    die "fork failed\n" unless defined $pid;
    if (!$pid) { exec @ARGV; exit 127; }
    $SIG{ALRM} = sub { kill "KILL", $pid; waitpid $pid, 0; exit 124 };
    alarm $secs;
    waitpid $pid, 0;
    alarm 0;
    exit($? >> 8 ? $? >> 8 : ($? ? 1 : 0));
  ' "$secs" osascript -e \
    'tell application "System Events" to get name of first process whose frontmost is true' \
    > /dev/null 2>&1
}
