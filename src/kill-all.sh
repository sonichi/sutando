#!/bin/bash
# Kill ALL sutando-related processes reliably

echo "Stopping Sutando..."

# Kill by port (catches the actual servers + their children)
for port in 9900 9901 8080 7843 7844 7845; do
  lsof -ti :$port | xargs kill 2>/dev/null
done

# Kill by name pattern
pkill -f "voice-agent" 2>/dev/null
pkill -f "web-client.ts" 2>/dev/null
pkill -f "agent-api.py" 2>/dev/null
pkill -f "dashboard.py" 2>/dev/null
pkill -f "screen-capture-server" 2>/dev/null
pkill -f "telegram-bridge" 2>/dev/null
pkill -f "discord-bridge" 2>/dev/null
pkill -f "watch-tasks" 2>/dev/null
pkill -f "sutando-widget" 2>/dev/null
pkill -f "esbuild.*--service" 2>/dev/null
pkill -f "credential-proxy" 2>/dev/null

sleep 1

# Force kill anything still on our ports
for port in 9900 9901 8080 7843 7844 7845; do
  lsof -ti :$port | xargs kill -9 2>/dev/null
done

echo "All Sutando processes stopped."
