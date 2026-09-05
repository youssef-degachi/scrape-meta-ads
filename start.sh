#!/usr/bin/env bash
# Start the Ads Leads website. Open http://localhost:8000 in your browser.
cd "$(dirname "$0")"
PORT="${1:-8000}"
# stop an older copy of this server if it is still holding the port
OLD=$(pgrep -f "python serve.py ${PORT}$")
if [ -n "$OLD" ]; then
  echo "Stopping previous server (pid $OLD)"; kill $OLD; sleep 0.5
fi
exec .venv/bin/python serve.py "$PORT"
