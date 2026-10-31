#!/usr/bin/env bash
# Container entrypoint.
#   no arguments  -> run a pass, sleep INTERVAL_HOURS, repeat (INTERVAL_HOURS=0: run once and exit)
#   arguments     -> one-off run with those flags, e.g.  docker run ... --dry-run --limit 100
set -u

if [ "$#" -gt 0 ]; then
  exec python /app/lyrics_sync.py "$@"
fi

child=0
on_term() {
  # pass the stop signal on so the current track finishes and state is saved
  if [ "$child" -ne 0 ]; then
    kill -TERM "$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  exit 0
}
trap on_term TERM INT

interval="${INTERVAL_HOURS:-24}"
sleep_seconds=$(python -c "import sys; print(max(0, int(float(sys.argv[1]) * 3600)))" "$interval" 2>/dev/null) || {
  echo "INTERVAL_HOURS must be a number (got '$interval')" >&2
  exit 2
}

while true; do
  python /app/lyrics_sync.py & child=$!
  wait "$child"
  if [ "$sleep_seconds" -eq 0 ]; then
    exit 0
  fi
  echo "next pass in ${interval} h"
  sleep "$sleep_seconds" & child=$!
  wait "$child"
done
