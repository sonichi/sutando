#!/usr/bin/env bash
# Healthy while the gateway client keeps rewriting its status file (once per poll).
WS="${SUTANDO_CLOUD_WORKSPACE:-/workspace}"
f="$WS/state/gateway-status.${GATEWAY_INSTANCE:-cloud}.json"
if [ ! -f "$f" ]; then echo "unhealthy: no status file at $f"; exit 1; fi
if [ -z "$(find "$f" -mmin -3 2>/dev/null)" ]; then echo "unhealthy: $f older than 3 minutes"; exit 1; fi
echo "healthy: $f"
