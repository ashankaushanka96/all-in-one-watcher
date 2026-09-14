import os
import socket
import subprocess
import time
from threading import Event

from datadog import statsd
from loguru import logger

import app.variables as var
from app.config.constants import SUBPROCESS_TIMEOUT
from app.config.settings import load_settings
from app.services.sendmail import get_mail_service

MONITOR_LOGGER_NAME = "WatcherMonitor"
MAX_START_ATTEMPTS = 5

# Matches by subcommand ("watcher"/legacy "run"), not filename, so it can't be fooled by the API's own process.
WATCHER_PS_PATTERN = r"ps -ef | grep -E 'all_in_one_watcher(\.py|\.bin)?[[:space:]]+(run|watcher)([[:space:]]|$)' | grep -v grep"
API_PS_PATTERN = r"ps -ef | grep -E 'all_in_one_watcher(\.py|\.bin)?[[:space:]]+api([[:space:]]|$)' | grep -v grep"


class WatcherMonitor:
    def __init__(self):
        self.logger = logger.bind(comp_name=MONITOR_LOGGER_NAME)
        self.script_directory = var.WATCHER_DIRECTORY
        self.settings = load_settings()
        self.region = self.settings.meta_data.region

        mail_service = get_mail_service()
        self.mail_service = mail_service
        self.watcher_mail_send = self.settings.mail_configs.mail_send and mail_service.mail_enabled

        self.watcher_status_send = self.settings.datadog_metrics.enabled
        self.interval = self.settings.watcher_settings.monitor_interval

        self.stat_name = 'feed.component.watcher.status'

    def is_watcher_running(self):
        result = subprocess.run(
            ['bash', '-c', WATCHER_PS_PATTERN],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return bool(result.stdout.strip())

    def is_api_running(self):
        result = subprocess.run(
            ['bash', '-c', API_PS_PATTERN],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return bool(result.stdout.strip())

    def ensure_api_running(self):
        # Only called when the API is already known to be down - see run_check().
        try:
            result = subprocess.run(
                ['bash', 'run.sh', '--api'],
                cwd=self.script_directory,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:
            self.logger.warning(f"Failed to ensure watcher API is running: {e}")
            return

        if self.is_api_running():
            self.logger.info("Control API restarted successfully.")
        else:
            self.logger.error(
                f"Control API did not come up after run.sh --api (exit={result.returncode}). "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
            )

    def detect_primary_ip(self) -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                ip = sock.getsockname()[0]
                if ip:
                    return ip
        except Exception:
            pass

        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip:
                return ip
        except Exception:
            pass

        return "unknown"

    def send_email(self, status):
        try:
            ip_address = self.detect_primary_ip()
            self.mail_service.watcher_status_send(status, ip_address)
        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")

    def start_watcher(self, also_start_api: bool = False):
        watcherRunScriptPath = self.script_directory
        watcherRunScript = "run.sh"
        os.chdir(watcherRunScriptPath)

        # Starts both together when the API's down too, so run.sh's backup happens once.
        script_args = ["--watcher", "--api"] if also_start_api else ["--watcher"]
        self.logger.info(f"Starting watcher via run.sh {' '.join(script_args)}...")

        for attempt in range(1, MAX_START_ATTEMPTS + 1):
            try:
                result = subprocess.run(
                    ['bash', watcherRunScript, *script_args],
                    capture_output=True,
                    text=True,
                    timeout=SUBPROCESS_TIMEOUT,
                )
                script_output = f"exit={result.returncode} stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
            except Exception as e:
                script_output = f"run.sh invocation failed: {e}"

            time.sleep(2)
            if self.is_watcher_running():
                self.logger.info(f"Watcher started successfully on attempt {attempt}/{MAX_START_ATTEMPTS}.")
                if self.watcher_mail_send:
                    self.send_email("started")
                if self.watcher_status_send:
                    statsd.service_check(self.stat_name, 0)
                return
            self.logger.warning(
                f"Watcher did not come up on attempt {attempt}/{MAX_START_ATTEMPTS}; "
                f"it likely crashed on startup (check logs/watcher.log). {script_output}"
            )

        # Every attempt crashed; send one alert instead of one per attempt.
        self.logger.error(
            f"Watcher failed to start after {MAX_START_ATTEMPTS} attempts; giving up until the next check."
        )
        if self.watcher_status_send:
            statsd.service_check(self.stat_name, 2)
        if self.watcher_mail_send:
            self.send_email("down")

    def run_check(self):
        watcher_up = self.is_watcher_running()
        if self.watcher_status_send:
            if watcher_up:
                statsd.service_check(self.stat_name, 0)
            else:
                statsd.service_check(self.stat_name, 2)

        if watcher_up:
            # Still make sure the API is up if it's the only one down.
            if not self.is_api_running():
                self.logger.warning("Watcher is running but the control API is down; starting it.")
                self.ensure_api_running()
            else:
                self.logger.debug("Check OK: watcher and control API are both running.")
            return

        api_up = self.is_api_running()
        self.logger.warning(f"Watcher process is DOWN (control API {'up' if api_up else 'also down'}); restarting.")

        if self.watcher_mail_send:
            self.send_email("down")

        # If the API's down too, bring both up together (see start_watcher()).
        self.start_watcher(also_start_api=not api_up)

    def run_forever(self, stop_event: Event):
        self.logger.info(
            f"Monitor loop started (interval={self.interval}s, mail={self.watcher_mail_send}, "
            f"datadog={self.watcher_status_send})."
        )
        while not stop_event.is_set():
            try:
                self.run_check()
            except Exception as e:
                self.logger.exception(f"Monitor check failed; will retry in {self.interval}s: {e}")
            stop_event.wait(self.interval)
        self.logger.info("Monitor loop stopped.")
