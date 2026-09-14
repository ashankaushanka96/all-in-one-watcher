#!/bin/bash

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
DEBUG="False"
FORCE="False"
PYTHON_ARGS=("--config" "./config/config.ini")
START_API="False"
START_WATCHER="False"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --api)
      START_API="True"
      shift
      ;;
    --watcher)
      START_WATCHER="True"
      shift
      ;;
    --debug)
      DEBUG="True"
      PYTHON_ARGS+=("--debug")
      shift
      ;;
    --force)
      FORCE="True"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# Neither --api nor --watcher given: start both.
if [ "$START_API" = "False" ] && [ "$START_WATCHER" = "False" ]; then
  START_API="True"
  START_WATCHER="True"
fi

DISABLE_FLAG="$SCRIPT_DIR/stop_watcher"

# --force clears the disable flag left by kill.sh --stop.
if [ "$FORCE" = "True" ] && [ -f "$DISABLE_FLAG" ]; then
  rm -f "$DISABLE_FLAG"
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] --force: removed disable flag ($DISABLE_FLAG)."
fi

cd "$SCRIPT_DIR"

# Serializes concurrent invocations (e.g. restart.sh and the monitor daemon both trying
# to bring the watcher/API back up at once) so the "already running?" check and the
# actual start can never race and produce duplicate processes.
LOCK_FILE="$SCRIPT_DIR/.run.lock"
exec 200>"$LOCK_FILE"
if ! flock -w 30 200; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Could not acquire run.sh lock within 30s; another run.sh invocation may be stuck. Aborting." >&2
  exit 1
fi

RUNNER=(".venv/bin/python" "all_in_one_watcher.py")
BACKUP_DIR="$SCRIPT_DIR/backup_logs"

# Keep only the last 2 days of archived logs.
if [ -d "$BACKUP_DIR" ]; then
  find "$BACKUP_DIR" -type f -name '*.tar.gz' -mtime +2 -exec rm -f {} \;
fi

WATCHER_PATTERN='all_in_one_watcher(\.py|\.bin)?[[:space:]]+(run|watcher)([[:space:]]|$)'
API_PATTERN='all_in_one_watcher(\.py|\.bin)?[[:space:]]+api([[:space:]]|$)'

api_running() {
  [ "$(ps -ef | grep -E "$API_PATTERN" | grep -v grep | wc -l)" -ge 1 ]
}

watcher_running() {
  [ "$(ps -ef | grep -E "$WATCHER_PATTERN" | grep -v grep | wc -l)" -ge 1 ]
}

# Archives whichever of the given files actually exist into backup_logs/<archive_name>.tar.gz.
archive_log_files() {
  archive_name="$1"
  shift
  existing=()
  for f in "$@"; do
    [ -e "$f" ] && existing+=("$f")
  done
  if [ "${#existing[@]}" -eq 0 ]; then
    return 0
  fi
  mkdir -p "$BACKUP_DIR/$archive_name"
  mv "${existing[@]}" "$BACKUP_DIR/$archive_name/"
  (cd "$BACKUP_DIR" && tar -czf "$archive_name.tar.gz" "$archive_name")
  rm -rf "$BACKUP_DIR/$archive_name"
}

# Only back up and (re)start a target that isn't already running (so an idempotent "ensure running" ping is a true no-op).
API_WILL_START="False"
if [ "$START_API" = "True" ] && ! api_running; then
  API_WILL_START="True"
fi

WATCHER_WILL_START="False"
if [ "$START_WATCHER" = "True" ] && [ ! -f "$DISABLE_FLAG" ] && ! watcher_running; then
  WATCHER_WILL_START="True"
fi

# Back up only the log files of what's about to freshly start, into one backup_logs/logs_<timestamp>.tar.gz.
# Never archive the whole logs folder: monitor.log belongs to the long-lived monitor daemon, which run.sh
# does not manage, and moving it out from under that process sends its logs to a deleted inode.
if [ "$API_WILL_START" = "True" ] || [ "$WATCHER_WILL_START" = "True" ]; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Backing up log files for the target(s) about to start..."
  mkdir -p "$BACKUP_DIR"
  shopt -s nullglob
  log_files_to_archive=()
  if [ "$WATCHER_WILL_START" = "True" ]; then
    log_files_to_archive+=(./logs/watcher*.log ./logs/watcher_nohup.out)
  fi
  if [ "$API_WILL_START" = "True" ]; then
    log_files_to_archive+=(./logs/api.log ./logs/api_nohup.out)
  fi
  shopt -u nullglob
  archive_log_files "logs_$(date +%Y%m%d_%H%M%S)" "${log_files_to_archive[@]}"
fi

mkdir -p ./logs

if [ "$START_API" = "True" ]; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Ensuring control API is running..."
  if [ "$API_WILL_START" = "True" ]; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Starting watcher API (debug=$DEBUG)..."
    if [ "$DEBUG" = "True" ]; then
      nohup "${RUNNER[@]}" api "${PYTHON_ARGS[@]}" > ./logs/api_nohup.out 2>&1 200>&- &
    else
      nohup "${RUNNER[@]}" api "${PYTHON_ARGS[@]}" > /dev/null 2>&1 200>&- &
    fi
  else
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Watcher API is already running; skipping start."
  fi
fi

if [ "$START_WATCHER" = "True" ]; then
  if [ -f "$DISABLE_FLAG" ]; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Disable flag found ($DISABLE_FLAG); skipping watcher start." >&2
  elif [ "$WATCHER_WILL_START" = "True" ]; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Starting watcher run (debug=$DEBUG)..."
    if [ "$DEBUG" = "True" ]; then
      nohup "${RUNNER[@]}" watcher "${PYTHON_ARGS[@]}" > ./logs/watcher_nohup.out 2>&1 200>&- &
    else
      nohup "${RUNNER[@]}" watcher "${PYTHON_ARGS[@]}" > /dev/null 2>&1 200>&- &
    fi
  else
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] Watcher is already running; skipping start."
  fi
fi

echo "[$(date +'%Y-%m-%d %H:%M:%S')] run.sh complete"
