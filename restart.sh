#!/bin/bash

DIR="$( cd -P "$( dirname "$0" )" && pwd )"

cd $DIR

RESTART_API="false"
RESTART_WATCHER="false"
DEBUG="false"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --api)
      RESTART_API="true"
      shift
      ;;
    --watcher)
      RESTART_WATCHER="true"
      shift
      ;;
    --debug)
      DEBUG="true"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

RUN_DEBUG_ARGS=()
if [ "$DEBUG" = "true" ]; then
  RUN_DEBUG_ARGS=("--debug")
fi

# No target selected: restart everything.
if [ "$RESTART_API" = "false" ] && [ "$RESTART_WATCHER" = "false" ]; then
  RESTART_API="true"
  RESTART_WATCHER="true"
fi

if [ "$RESTART_API" = "true" ] && [ "$RESTART_WATCHER" = "true" ]; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: stopping watcher and API..."
  ./kill.sh --api --watcher

  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: waiting for processes to exit..."
  sleep 2

  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: starting watcher and API..."
  ./run.sh "${RUN_DEBUG_ARGS[@]}"

  sleep 2
elif [ "$RESTART_WATCHER" = "true" ]; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: stopping watcher (API left running)..."
  ./kill.sh --watcher

  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: waiting for processes to exit..."
  sleep 2

  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: starting watcher..."
  ./run.sh --watcher "${RUN_DEBUG_ARGS[@]}"

  sleep 2
elif [ "$RESTART_API" = "true" ]; then
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: stopping API..."
  ./kill.sh --api

  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: waiting for API to exit..."
  sleep 2

  echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart: starting API..."
  ./run.sh --api "${RUN_DEBUG_ARGS[@]}"

  sleep 2
fi

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restart complete"

exit 0
