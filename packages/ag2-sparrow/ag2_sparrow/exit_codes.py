"""Exit codes the gateway bridge ends with that its supervisors act on; one definition for every reader."""

# EX_UNAVAILABLE: the wrapper relaunches on it without an owner alert, so an outage
# does not queue a "bridge restarted" message every stall period.
POLL_STALL_EXIT_CODE = 69
