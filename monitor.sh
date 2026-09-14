#!/bin/bash

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"

DISABLE_FLAG="$SCRIPT_DIR/stop_watcher"

if [ -f "$DISABLE_FLAG" ]; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Disable flag found ($DISABLE_FLAG); exiting." >&2
  exit 0
fi

DEBUG="False"
PYTHON_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --debug)
      DEBUG="True"
      PYTHON_ARGS+=("--debug")
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

cd "$SCRIPT_DIR"

# Two cron ticks landing together both saw "not running" and each started a daemon; -n skips this
# run entirely because whoever holds the lock is already doing the start.
LOCK_FILE="$SCRIPT_DIR/.monitor.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Another monitor.sh is already checking; skipping this run."
  exit 0
fi

RUNNER=(".venv/bin/python" "all_in_one_watcher.py")

numproc=`ps -ef | grep -E 'all_in_one_watcher(\.py|\.bin)?[[:space:]]+monitor([[:space:]]|$)' | grep -v "grep" | wc -l`
if [ $numproc -lt 1 ]
then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Starting watcher monitor (long-lived, debug=$DEBUG)..."
    mkdir -p ./logs
    if [ "$DEBUG" = "True" ]; then
      nohup "${RUNNER[@]}" monitor "${PYTHON_ARGS[@]}" > ./logs/monitor_nohup.out 2>&1 200>&- &
    else
      nohup "${RUNNER[@]}" monitor "${PYTHON_ARGS[@]}" > /dev/null 2>&1 200>&- &
    fi
else
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Watcher monitor is already running; skipping start."
fi
