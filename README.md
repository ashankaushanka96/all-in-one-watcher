# All-in-One Watcher

## Overview

The **All-in-One Watcher** script (`all_in_one_watcher.py`) is designed to monitor specific processes on a Unix-based system. If a monitored process is not running, the script can be configured to either restart the process or send an alert email. The script can also monitor system resources, log directory sizes, and send process details to Datadog for real-time monitoring.

## Features

  - **Process Monitoring**: Continuously checks if a specified process is running and can restart it if required.
  - **Email Alerts**: Sends email notifications if the process is down, including system and component details.
  - **Logging**: Logs actions, events, and errors to rotating log files using the loguru library. The active `watcher.log`/`api.log` rotate daily, with rotated segments kept for 7 days; `run.sh` additionally archives finished logs into `backup_logs/*.tar.gz`, pruned after 2 days.
  - **Resource Monitoring**: Monitors and sends CPU, memory, and uptime metrics of the processes to Datadog.
  - **Log Directory Size Monitoring**: Tracks the size of log directories and sends data to Datadog.
  - **Port Monitoring**: Checks the availability of ports for specific components and logs the status.
  - **Configurable Actions**: The script is highly customizable through a configuration file:
      - It can restart a process if it's not running.
      - It can send alerts based on component status or resource thresholds.
  - **Threaded Watchers**: Each process is monitored in a separate thread, enabling efficient parallel monitoring.
  - **Supports AWS SES for SMTP**: Sends alerts using AWS SES for email notifications.

## Prerequisites

- Python 3.9 or later
- Unix-based operating system
- AWS SES (Simple Email Service) setup for SMTP
- The following Python libraries:
  - `loguru`
  - `python-dotenv`
  - `datadog`
  - `psutil`

## Configs

### Config.ini Block Explanation

- **tag**: The name of the process to monitor.
- **port** *(optional)*: The port to be monitored for availability.
- **startTime**: The time of day when monitoring should start (HH:MM format).
- **endTime**: The time of day when monitoring should end (HH:MM format).
- **runningDates**: Days of the week when the process should be monitored (comma-separated, 0=Sunday, 6=Saturday).
- **maxUpDays** *(optional)*: The maximum number of days the process is allowed to run before triggering a alert in datadog.
- **name**: The human-readable name of the component.
- **needToUp**: Set to `Yes` if the process should be restarted if found not running; otherwise `No`.
- **needToSendMail**: Set to `Yes` if an email should be sent if the process is not running; otherwise `No`.
- **runScriptPath**: The path to the directory containing the restart script.
- **logDirectory** *(optional)*: The directory path where logs are stored for size monitoring. Set to `No` to disable log directory monitoring outright. Set to `Auto` for components whose log path is only known at instance boot — e.g. an autoscaling group member writing to a per-instance directory on a shared EFS mount. With `Auto`, the watcher reads the `export log_path=...` line straight out of `runScript` itself (resolved as `{runScriptPath}/{runScript}`), substitutes any `$instance_id`/`${instance_id}` reference in it with this EC2 instance's own instance-id (resolved once via IMDSv2, falling back to `/var/lib/cloud/data/instance-id` and then the `ec2-metadata` CLI), and appends a fixed `logs` subdirectory, since the app writes into `<resolved path>/logs` rather than directly into it. Because the value is read live from that script on every watcher startup, a version or component bump baked into it (e.g. CI/CD regenerating `AUTHX.X.X.1.2613.0.003`) needs no config.ini change. Leave it as a plain, fixed path for fixed-server components.
- **runScript**: The name of the script to run if the process needs to be restarted. For a `logDirectory = Auto` component this should point at the boot script that both sets `log_path` and starts the process (e.g. `start.sh`), not a plain `run.sh` that lacks it.
- **startCheckInterval** *(optional)*: Seconds to wait after launching `runScript` before checking whether the process actually came up, for components that are slow to start. Omit it to use `watcher_settings.start_check_interval` from `appconfig.yaml` (default 2). When a component is spread over several config.ini sections, they must all agree on this value, like the other shared fields.

#### Example:

```ini
[SearchService]
tag = search-service
port = 8080
startTime = 00:00:00
endTime = 23:59:59
runningDates = [0,1,2,3,4,5,6]
maxUpDays = 7
name = Search Service
needToUp = Yes
needToSendMail = Yes
runScriptPath = /apps/search-service
logDirectory = /apps/search-service/logs
runScript = run.sh
```

#### Example: autoscaling group component with a dynamic, per-instance log path

`start.sh` lives in `/apps/authx` and, each time CI/CD deploys a new version, sets the log path before launching the process itself:

```bash
instance_id=$(ec2-metadata -i | awk '{print $2}')
export log_path=/var/log/authx/writer/AUTHX.X.X.1.2613.0.003/$instance_id
...
./run.sh
```

`config.ini` doesn't need to know the version, the `$instance_id` value, or that AuthX writes into a `logs` subfolder under `$log_path` — that's a fixed convention the watcher appends automatically for `logDirectory = Auto` components. Just point `runScript` at `start.sh` (it starts the process too, since it ends by calling `./run.sh`):

```ini
[AuthxWriter]
tag = authx
port = 8091
startTime = 00:00:00
endTime = 23:59:59
runningDates = [0,1,2,3,4,5,6]
maxUpDays = 7
name = Authx Writer
needToUp = Yes
needToSendMail = Yes
runScriptPath = /apps/authx
runScript = start.sh
logDirectory = Auto
```

### .env Explanation

- **USERNAME**: AWS SES SMTP username.
- **PASSWORD**:  AWS SES SMTP password.
- **LOGS_SIZE_SEND**: Set to `True` to enable log size monitoring.
- **PROCESS_DETAILS_SEND**: Set to `True` to enable process detail monitoring.

### Steps to Install

1. Clone or copy this repository to the target path (e.g. `/apps/all_in_one_watcher`).
2. Run `./setup.sh` to create the `.venv` virtual environment and install `requirements.txt`.
3. Fill in `config/config.ini` (components to watch) and `config/appconfig.yaml` (mail, Datadog, region, control API settings).
4. Run `./run.sh` to start both the watcher and its control API.

For fleet deployments, `deploy/install.sh` (driven by the Ansible playbook in `ansible/`) handles backing up the current deployment, rebuilding the venv, updating crontab, and restarting the service on a target host.

## Running the Watcher

`run.sh`, `kill.sh`, and `restart.sh` accept `--api`/`--watcher` to target just one process; with neither, they act on both. The monitor is managed separately, via `monitor.sh` (and `kill.sh --monitor` to stop it).

- **`run.sh`** - starts the watcher and/or control API. `--debug` enables debug-level logging; `--force` clears a `stop_watcher` disable flag (left by `kill.sh --stop`) before starting.
- **`kill.sh`** - stops the watcher and/or control API; with no arguments it stops both, never the monitor. `--monitor` stops the long-lived monitor process, and can be combined with the other targets. `--stop` also leaves the disable flag so the watcher won't be started again until `run.sh --force` (or clearing the flag file) runs.
- **`restart.sh`** - stops then starts the watcher and/or API via `run.sh`; `--debug` is forwarded to it. It does not touch the long-lived monitor daemon.
- **`monitor.sh`** - ensures the long-lived monitor process (`all_in_one_watcher.py monitor`) is running, starting it in the background if it isn't; intended to run frequently from cron as a self-healing check. `--debug` enables debug-level logging and, like `run.sh`, keeps the nohup output file (`monitor_nohup.out`) instead of discarding it. The monitor process itself loops forever, checking the watcher/API and restarting whichever is down every `watcher_settings.monitor_interval` seconds (default 60s), fetching its mail credentials once at startup rather than on every check.

Logs are written to `logs/` (`watcher.log`, `api.log`, `monitor.log`). Each `run.sh` invocation archives whatever is about to (re)start into `backup_logs/logs_<timestamp>.tar.gz` first, and prunes archives older than 2 days.
