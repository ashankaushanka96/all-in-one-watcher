#!/usr/bin/env bash

set -euo pipefail

watcher_path="${1:?watcher path is required}"
watcher_main_file_name="${2:?watcher main file name is required}"

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

backup_name="$(date +%Y%m%d_%H%M%S)"
backup_root="deployments/$backup_name"
backup_archive="deployments/$backup_name.tar.gz"

mkdir -p "$backup_root"

if [[ -d "app" ]]; then
  cp -a "app" "$backup_root/app"
fi

if [[ -d "config" ]]; then
  cp -a "config" "$backup_root/config"
fi

if [[ -f "$watcher_main_file_name" ]]; then
  cp -a "$watcher_main_file_name" "$backup_root/"
fi

rm -f -- "$backup_archive"
(cd "deployments" && tar -czf "$backup_name.tar.gz" "$backup_name")
rm -rf -- "$backup_root"
