#!/bin/bash
# Sourced by hooks: who this Claude session is. The marked core (SUTANDO_CORE_SESSION=1)
# and an enrolled pool worker (SUTANDO_INSTANCE_ID) are identified; any other session is a guest.
sutando_session_identified() {
  [ -n "${SUTANDO_INSTANCE_ID:-}" ] || [ "${SUTANDO_CORE_SESSION:-}" = "1" ]
}
