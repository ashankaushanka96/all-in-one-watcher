#!/bin/sh
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
DISABLE_FLAG="$SCRIPT_DIR/stop_watcher"

# "run" is the pre-rename subcommand name, matched too so old watchers still get cleaned up.
WATCHER_PATTERN='all_in_one_watcher(\.py|\.bin)?[[:space:]]+(run|watcher)([[:space:]]|$)'
API_PATTERN='all_in_one_watcher(\.py|\.bin)?[[:space:]]+api([[:space:]]|$)'
MONITOR_PATTERN='all_in_one_watcher(\.py|\.bin)?[[:space:]]+monitor([[:space:]]|$)'
LEGACY_PATTERN='all_in_one_watcher\.py[[:space:]]+--config'

KILL_API="false"
KILL_WATCHER="false"
KILL_MONITOR="false"
DISABLE="false"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --api)
      KILL_API="true"
      shift
      ;;
    --watcher)
      KILL_WATCHER="true"
      shift
      ;;
    --monitor)
      KILL_MONITOR="true"
      shift
      ;;
    --stop)
      # Also sets the disable flag, cleared later via run.sh --force.
      KILL_WATCHER="true"
      DISABLE="true"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# No target selected: kill the watcher and API. The monitor is only stopped when asked for explicitly.
if [ "$KILL_API" = "false" ] && [ "$KILL_WATCHER" = "false" ] && [ "$KILL_MONITOR" = "false" ]; then
  KILL_API="true"
  KILL_WATCHER="true"
fi

if [ "$DISABLE" = "true" ]; then
  touch "$DISABLE_FLAG"
  echo "Created disable flag at $(realpath "$DISABLE_FLAG")"
fi

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Looking for the watcher and API processes..."

if [ "$KILL_WATCHER" = "true" ]; then
  WATCHER_PID=`ps -ef | grep -vw grep | grep -E "$WATCHER_PATTERN" | awk '{print $2}'`
  if [ -n "$WATCHER_PID" ] ;then
      echo "[$(date +'%Y-%m-%d %H:%M:%S')] Killing watcher (PID $WATCHER_PID)"
      kill -9 $WATCHER_PID
  else
      echo "[$(date +'%Y-%m-%d %H:%M:%S')] Watcher is not running"
  fi

  LEGACY_PID=`ps -ef | grep -vw grep | grep -E "$LEGACY_PATTERN" | awk '{print $2}'`
  if [ -n "$LEGACY_PID" ] ;then
      echo "[$(date +'%Y-%m-%d %H:%M:%S')] Killing leftover pre-migration watcher (PID $LEGACY_PID)"
      kill -9 $LEGACY_PID
  fi
fi

if [ "$KILL_API" = "true" ]; then
  API_PID=`ps -ef | grep -vw grep | grep -E "$API_PATTERN" | awk '{print $2}'`
  if [ -n "$API_PID" ] ;then
      echo "[$(date +'%Y-%m-%d %H:%M:%S')] Killing API (PID $API_PID)"
      kill -9 $API_PID
  else
      echo "[$(date +'%Y-%m-%d %H:%M:%S')] API is not running"
  fi
fi

if [ "$KILL_MONITOR" = "true" ]; then
  MONITOR_PID=`ps -ef | grep -vw grep | grep -E "$MONITOR_PATTERN" | awk '{print $2}'`
  if [ -n "$MONITOR_PID" ] ;then
      echo "[$(date +'%Y-%m-%d %H:%M:%S')] Killing monitor (PID $MONITOR_PID)"
      kill -9 $MONITOR_PID
  else
      echo "[$(date +'%Y-%m-%d %H:%M:%S')] Monitor is not running"
  fi
fi

if [ -f "$SCRIPT_DIR/all_in_one_watcher.bin" ]; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Removing leftover all_in_one_watcher.bin (this deployment runs from Python source via venv now)"
    rm -f "$SCRIPT_DIR/all_in_one_watcher.bin"
fi

echo "[$(date +'%Y-%m-%d %H:%M:%S')] kill.sh complete"

exit 0
