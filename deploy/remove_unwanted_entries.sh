#!/usr/bin/env bash

set -euo pipefail

watcher_path="${1:?watcher path is required}"
shift

for forbidden_path in / /apps /home/app_user/app; do
  if [[ "$watcher_path" == "$forbidden_path" ]]; then
    echo "Refusing to operate on $forbidden_path" >&2
    exit 1
  fi
done

if [[ ! -d "$watcher_path" ]]; then
  echo "Watcher path does not exist or is not a directory: $watcher_path" >&2
  exit 1
fi

cd "$watcher_path"

keep_names=("$@")
removed_any=0

while IFS= read -r -d '' entry_name; do
  keep_entry=0

  for keep_name in "${keep_names[@]}"; do
    if [[ "$entry_name" == "$keep_name" ]]; then
      keep_entry=1
      break
    fi
  done

  if [[ "$keep_entry" -eq 0 ]]; then
    rm -rf -- "$entry_name"
    echo "$entry_name"
    removed_any=1
  fi
done < <(find . -mindepth 1 -maxdepth 1 -printf '%P\0')

if [[ "$removed_any" -eq 0 ]]; then
  echo "NO_CHANGES"
fi
