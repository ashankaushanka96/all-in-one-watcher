#!/usr/bin/env bash
# Extract watcher_stuff.tar.gz somewhere, then run <extracted>/deploy/install.sh with the args below.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: install.sh <watcher_path> <watcher_main_file_name> <environment_name> <region_name> <cron_update> <deployment_existed>
EOF
}

if [[ $# -ne 6 ]]; then
  usage
  exit 1
fi

watcher_path="$1"
watcher_main_file_name="$2"
environment_name="$3"
region_name="$4"
cron_update="$5"
deployment_existed="$6"

log() { echo "[install.sh] $*"; }

for forbidden_path in / /apps /home/app_user/app; do
  if [[ "$watcher_path" == "$forbidden_path" ]]; then
    echo "Refusing to operate on $forbidden_path" >&2
    exit 1
  fi
done

deploy_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
extract_dir="$(dirname "$deploy_dir")"
venv_path="$watcher_path/.venv"

if [[ ! -d "$extract_dir" ]]; then
  echo "Extracted watcher payload not found next to install.sh: $extract_dir" >&2
  exit 1
fi

mkdir -p "$watcher_path"
cd "$watcher_path"
mkdir -p config logs deployments

if [[ "$deployment_existed" == "true" ]]; then
  log "Backing up current deployment"
  "$deploy_dir/remove_unwanted_entries.sh" "$watcher_path" \
    config deployments logs app stop_watcher "$watcher_main_file_name" \
    "$(basename "$extract_dir")"
  "$deploy_dir/backup_current_deployment.sh" "$watcher_path" "$watcher_main_file_name"
fi

log "Copying release files"
(cd "$extract_dir" && find . -maxdepth 1 -type f -exec cp -f {} "$watcher_path"/ \;)

# Kill any leftover PyInstaller-binary run/api process; restart.sh never touches it.
pkill -9 -f 'all_in_one_watcher\.bin[[:space:]]+(run|api)([[:space:]]|$)' 2>/dev/null || true
rm -f -- \
  "$watcher_path/all_in_one_watcher.bin" \
  "$watcher_path/all_in_one_watcher.bin-amd64" \
  "$watcher_path/all_in_one_watcher.bin-arm64"

if [[ ! -f "$watcher_path/config/config.ini" && -f "$extract_dir/config/config.ini" ]]; then
  log "Seeding config.ini"
  cp -p "$extract_dir/config/config.ini" "$watcher_path/config/"
fi

if [[ ! -f "$watcher_path/config/appconfig.yaml" && -f "$extract_dir/config/appconfig.yaml" ]]; then
  log "Seeding appconfig.yaml"
  cp -p "$extract_dir/config/appconfig.yaml" "$watcher_path/config/appconfig.yaml"
fi

if [[ -d "$extract_dir/app" ]]; then
  log "Copying app package"
  mkdir -p "$watcher_path/app"
  cp -a "$extract_dir/app/." "$watcher_path/app/"
fi

if [[ -f "$extract_dir/config/appconfig.yaml" ]]; then
  log "Merging appconfig.yaml"
  python3 "$deploy_dir/merge_yaml_preserve_existing.py" \
    "$extract_dir/config/appconfig.yaml" \
    "$watcher_path/config/appconfig.yaml" \
    "$watcher_path/config/appconfig.yaml" \
    "$environment_name" \
    "$region_name"
fi

# Safe to remove while running from inside it - unlink only, the open file stays until this process exits.
rm -rf -- "$extract_dir"

log "Rebuilding virtual environment"
# pip's resolver output is logged to a file instead of stdout; only the tail is shown on failure.
install_log="$watcher_path/logs/install.log"
run_quiet() {
  if ! "$@" >>"$install_log" 2>&1; then
    echo "Command failed: $*" >&2
    echo "--- last 50 lines of $install_log ---" >&2
    tail -n 50 "$install_log" >&2
    return 1
  fi
}

rm -rf -- "$venv_path"
python3 -m venv "$venv_path"
export PIP_DISABLE_PIP_VERSION_CHECK=1
run_quiet "$venv_path/bin/python3" -m pip install --upgrade pip setuptools wheel
run_quiet "$venv_path/bin/pip" install -r "$watcher_path/requirements.txt"

chmod 755 "$watcher_path"/*.sh

if [[ "$cron_update" == "true" ]]; then
  log "Updating crontab"
  backup_cron="$watcher_path/app_user.cron"
  new_cron="$watcher_path/app_user.updated.cron"
  crontab -l 2>/dev/null > "$backup_cron" || true
  python3 "$deploy_dir/cleanup_crontab.py" \
    "$backup_cron" "$new_cron" \
    'all in one watcher|all_in_one_watcher|all\-in\-one\-watcher' \
    "$watcher_path"
  crontab "$new_cron"
  rm -f -- "$new_cron"
fi

if [[ -x "$watcher_path/restart.sh" ]]; then
  log "Restarting watcher service"
  "$watcher_path/restart.sh" || log "restart.sh exited non-zero (ignored)"
else
  log "restart.sh not found or not executable, skipping restart"
fi

log "Install complete"
