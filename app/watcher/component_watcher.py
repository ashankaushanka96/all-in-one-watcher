from pathlib import Path
import re
import os
import time
import subprocess
from threading import Event
from typing import Optional
from loguru import logger
import psutil
from datetime import datetime, timedelta, timezone
from app.models.component_model import Component, ProcessDetails, ProcessStatus, PortStatus
from app.services.sendmail import get_mail_service
from app.config.settings import load_settings
from app.models.aggregator_models import Connection
from app.services.datadog import Datadog
from app.services.aggregator import Aggregator

settings = load_settings()
SECONDS_PER_DAY = 24 * 60 * 60
SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY
STARTUP_GRACE_SECONDS = 40

class ComponentWatcher:
    def __init__(self, component: Component, stop_event: Event):
        self.component = component
        self.stop_event = stop_event
        self.logger = logger.bind(comp_name=component.name)
        self.mailer = get_mail_service()
        self.process: Optional[psutil.Process] = None
        self.datadog = Datadog(component)
        self.aggregator=Aggregator(component)
        self.previous_status: Optional[ProcessStatus] = None
        self._last_logged_state: Optional[str] = None
        self.start_check_interval = (
            component.startCheckInterval
            if component.startCheckInterval is not None
            else settings.watcher_settings.start_check_interval
        )

    def _log_state(self, state_key: str, message: str) -> None:
        """Logs at INFO only when the state actually changes, so a steady-state component doesn't log every cycle."""
        if state_key == self._last_logged_state:
            self.logger.debug(message)
            return
        self._last_logged_state = state_key
        self.logger.info(message)

    def _safe_realpath(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        try:
            return os.path.realpath(path)
        except Exception:
            return None

    def _process_matches_script_path(self, proc: psutil.Process) -> bool:
        expected_dir = self._safe_realpath(self.component.runScriptPath)

        if not expected_dir:
            return True

        proc_cwd = proc.info.get('cwd')
        if not proc_cwd:
            try:
                proc_cwd = proc.cwd()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                proc_cwd = None

        real_proc_cwd = self._safe_realpath(proc_cwd)
        return real_proc_cwd == expected_dir

    @property
    def _pid(self) -> int:
        pattern = re.compile(rf'\b{self.component.tag}\b|-D{self.component.tag}\b')
        matching_procs = []
        try:
            for proc in psutil.process_iter(['pid','name','cmdline','exe','cwd']):
                cmdline_parts = proc.info['cmdline'] or []
                cmdline = " ".join(cmdline_parts)
                name = proc.info['name'] or ""
                if pattern.search(cmdline) or pattern.search(name):
                    matching_procs.append(proc)

            if len(matching_procs) == 1:
                pid = matching_procs[0].info['pid']
            elif len(matching_procs) > 1:
                found_pids = [proc.info['pid'] for proc in matching_procs]
                self.logger.warning(f"Multiple processes found for tag {self.component.tag}. PIDs: {found_pids}")

                path_matched_pids = [
                    proc.info['pid']
                    for proc in matching_procs
                    if self._process_matches_script_path(proc)
                ]

                if len(path_matched_pids) == 1:
                    pid = path_matched_pids[0]
                elif len(path_matched_pids) > 1:
                    self.logger.warning(
                        f"Multiple processes also matched script path for tag {self.component.tag}. "
                        f"PIDs: {path_matched_pids}"
                    )
                    pid = self.process.pid if self.process and self.process.pid in path_matched_pids else None
                else:
                    pid = None
            else:
                pid = None
        except Exception as e:
            self.logger.exception(f"Process check failed for tag {self.component.tag}: {e}")
            pid = None
        if pid:
            self.component.process.pid = pid
            self.component.process.process_status = ProcessStatus.RUNNING
            if self.process is None or self.process.pid != pid:
                self.process = psutil.Process(pid)
                self.process.cpu_percent(interval=None)
        else:
            self.component.process.pid = None
            self.component.process.process_status = ProcessStatus.STOPPED
            self.process = None
        return pid
    
    def port_checker(self):
        port = self.component.port
        name = self.component.name
        try:
            is_listening = any(
                conn.laddr 
                and conn.laddr.port == port 
                and conn.status == psutil.CONN_LISTEN
                for conn in psutil.net_connections(kind='tcp')
            )
            if is_listening:
                self.component.process.port_status = PortStatus.LISTENING
            else:
                self.component.process.port_status = PortStatus.NOT_LISTENING
        except Exception as e:
            self.logger.exception(f"Port check failed for {name}: {e}")     

    def component_needs_to_run(self) -> bool:
        now_datetime=datetime.now(timezone.utc)
        effective_day = int(now_datetime.strftime('%w'))
        previous_day = (effective_day - 1) % 7
        now_seconds = (
            effective_day * SECONDS_PER_DAY
            + now_datetime.hour * 3600
            + now_datetime.minute * 60
            + now_datetime.second
        )
        candidate_seconds = (now_seconds, now_seconds + SECONDS_PER_WEEK)
        day_windows_to_check = (
            self.component.schedules.get(previous_day, [])
            + self.component.schedules.get(effective_day, [])
        )

        for schedule in day_windows_to_check:
            if any(schedule.startTimeSeconds <= current <= schedule.endTimeSeconds for current in candidate_seconds):
                self.component.process.needs_to_run = True
                return True
        self.component.process.needs_to_run = False
        return False

    def is_within_startup_grace_period(self, grace_seconds: int = STARTUP_GRACE_SECONDS) -> bool:
        now_datetime = datetime.now(timezone.utc)
        effective_day = int(now_datetime.strftime('%w'))
        previous_day = (effective_day - 1) % 7
        now_seconds = (
            effective_day * SECONDS_PER_DAY
            + now_datetime.hour * 3600
            + now_datetime.minute * 60
            + now_datetime.second
        )

        schedules_to_check = (
            self.component.schedules.get(previous_day, [])
            + self.component.schedules.get(effective_day, [])
        )

        for schedule in schedules_to_check:
            start_seconds = schedule.startTimeSeconds
            candidate_start_seconds = (start_seconds, start_seconds + SECONDS_PER_WEEK)
            if any(0 <= now_seconds - candidate <= grace_seconds for candidate in candidate_start_seconds):
                return True

        return False

    def send_status_change_mail(self, current_status: Optional[ProcessStatus], within_startup_grace: bool = False, error_message: Optional[str] = None) -> None:
        if not self.component.needToSendMail or current_status is None:
            return

        if current_status == ProcessStatus.STOPPED and within_startup_grace:
            return

        previous_status = self.previous_status
        status_event = None

        if current_status == ProcessStatus.STOPPED:
            status_event = "DOWN"
        elif previous_status is None:
            self.previous_status = current_status
            return
        elif current_status == previous_status:
            return
        elif current_status == ProcessStatus.RUNNING:
            if previous_status == ProcessStatus.SLEEPING:
                status_event = "STARTED"
            elif previous_status == ProcessStatus.STOPPED:
                status_event = "RESTARTED"

        if status_event:
            self.mailer.mail_send(
                self.component,
                status_event=status_event,
                error_message=error_message,
            )

        self.previous_status = current_status

    def start_component(self) -> Optional[str]:
        self.logger.info(
            f"Starting component {self.component.name} "
            f"(verifying after {self.start_check_interval}s)..."
        )

        if not self.component.runScriptPath or not self.component.runScript:
            self.component.process.process_status = ProcessStatus.STOPPED
            error_message = "runScriptPath/runScript not set; cannot start component."
            self.logger.error(error_message)
            return error_message

        script_dir = Path(self.component.runScriptPath)
        script_file = script_dir / self.component.runScript

        if not script_dir.exists():
            self.component.process.process_status = ProcessStatus.STOPPED
            error_message = f"Directory not found: {script_dir}"
            self.logger.error(error_message)
            return error_message

        if not script_file.exists():
            self.component.process.process_status = ProcessStatus.STOPPED
            error_message = f"Script not found: {script_file}"
            self.logger.error(error_message)
            return error_message

        original_dir = os.getcwd()

        try:
            os.chdir(script_dir)

            process = subprocess.Popen(
                ['bash', self.component.runScript],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True
            )

            time.sleep(self.start_check_interval)

            if process.poll() is not None:
                if process.returncode != 0:
                    stderr_data = ""
                    if process.stderr:
                        stderr_data = process.stderr.read().strip()
                    error_message = (
                        stderr_data
                        or f"Script failed with exit code {process.returncode}"
                    )
                    self.component.process.process_status = ProcessStatus.STOPPED
                    self.logger.error(
                        f"Failed to start {self.component.name}: {error_message}"
                    )
                    return error_message
                else:
                    self.logger.info(
                        f"Start script for {self.component.name} completed successfully"
                    )

            pid = self._pid

            if pid:
                self.component.process.process_status = ProcessStatus.RUNNING
                self.logger.info(
                    f"Component {self.component.name} restarted successfully (PID:{pid})"
                )
            else:
                self.component.process.process_status = ProcessStatus.STOPPED
                error_message = f"Tried to start {self.component.name}, but process still not detected."
                self.logger.warning(error_message)
                return error_message

        except Exception as e:
            self.component.process.process_status = ProcessStatus.STOPPED
            error_message = str(e)

            self.logger.exception(
                f"Failed to execute ./{self.component.runScript} "
                f"for component {self.component.name}. Error: {e}"
            )
            return error_message

        finally:
            try:
                os.chdir(original_dir)
            except Exception as e:
                self.logger.exception(f"Failed to restore working directory to {original_dir}: {e}")

        return None

    def process_details_collector(self):
        if self.process is None:
            return False
        try:
            self.component.process.cpu_percent = round(self.process.cpu_percent(interval=None), 2)
            with self.process.oneshot():
                create_time = self.process.create_time()
                self.component.process.uptime = int((datetime.now(timezone.utc) - datetime.fromtimestamp(create_time, timezone.utc)).total_seconds())
                mem_info = self.process.memory_info()
                self.component.process.memory_used_mb = round(mem_info.rss / (1024 * 1024), 2)
                self.component.process.memory_percent = round(self.process.memory_percent(), 2)
                self.component.process.connections = []
                for c in self.process.net_connections(kind='inet'):
                    if c.status == psutil.CONN_ESTABLISHED and getattr(c, "raddr", None):
                        l_ip = getattr(c.laddr, "ip", None)
                        l_port = getattr(c.laddr, "port", None)
                        r_ip = getattr(c.raddr, "ip", None)
                        r_port = getattr(c.raddr, "port", None)

                        if l_ip and r_ip and isinstance(l_port, int) and isinstance(r_port, int):
                            self.component.process.connections.append(
                                Connection(
                                    local_ip=l_ip,
                                    local_port=l_port,
                                    remote_ip=r_ip,
                                    remote_port=r_port
                                )
                            )
                return True
        except Exception as e:
            self.logger.exception(f"Failed to collect process details for {self.component.name}: {e}")
            return False

    def collect_log_directory_size(self):
        self.component.process.log_directory_size_mb = None

        if not self.component.logDirectory or not os.path.exists(self.component.logDirectory):
            return

        try:
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(self.component.logDirectory):
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    if os.path.isfile(file_path):
                        total_size += os.path.getsize(file_path)
            self.component.process.log_directory_size_mb = round(total_size / (1024 * 1024), 2)
        except Exception as exc:
            self.logger.exception(f"Failed to collect log directory size for {self.component.name}: {exc}")

    def send_details(self, include_metrics: bool = True, include_aggregator: bool = True):
        if self.component.process.pid is not None:
            self.process_details_collector()
        else:
            self.component.process.cpu_percent = 0.0
            self.component.process.memory_percent = 0.0
            self.component.process.memory_used_mb = 0.0
            self.component.process.uptime = 0
        self.collect_log_directory_size()
        if include_metrics and settings.datadog_metrics.enabled:
            self.datadog.send_metrics()
        if include_aggregator and settings.aggregator_backend.enabled and settings.aggregator_backend.base_url:
            self.aggregator.send_details()

    def run(self):
        self.logger.debug(
            f"Watch loop started (tag={self.component.tag}, interval={settings.watcher_settings.watch_interval}s)."
        )
        while not self.stop_event.is_set():
            try:
                self.component.process = ProcessDetails()
                pid = self._pid
                component_needs_to_run = self.component_needs_to_run()
                within_startup_grace = False
                status_change_error_message = None
                down_mail_sent_in_loop = False
                if component_needs_to_run:
                    if self.component.port:
                        self.port_checker()
                    if pid is None:
                        within_startup_grace = self.is_within_startup_grace_period()
                        if within_startup_grace:
                            self._log_state(
                                "grace",
                                f"{self.component.name} is not running, but is within its {STARTUP_GRACE_SECONDS}s "
                                "startup grace period; taking no action.",
                            )
                        else:
                            self._log_state(
                                "stopped",
                                f"{self.component.name} is DOWN"
                                + ("; restarting." if self.component.needToUp else "; needToUp=No, not restarting."),
                            )

                        if not within_startup_grace:
                            self.send_status_change_mail(
                                ProcessStatus.STOPPED,
                                within_startup_grace=False,
                            )
                            down_mail_sent_in_loop = True

                        if self.component.needToUp:
                            if not within_startup_grace:
                                self.send_details()  # send details before attempting restart
                            status_change_error_message = self.start_component()
                    else:
                        self._log_state(f"running:{pid}", f"{self.component.name} is running (PID:{pid}).")
                else:
                    if pid is None:
                        self._log_state("sleeping", f"{self.component.name} is outside its schedule; sleeping.")
                        self.component.process.process_status = ProcessStatus.SLEEPING
                        if self.component.port:
                            self.component.process.port_status = PortStatus.SLEEPING
                    else:
                        self._log_state(
                            f"warning:{pid}",
                            f"{self.component.name} is running (PID:{pid}) outside its schedule; treating as sleeping.",
                        )
                        self.component.process.process_status = ProcessStatus.WARNING
                        if self.component.port:
                            self.component.process.port_status = PortStatus.WARNING
                if not (down_mail_sent_in_loop and self.component.process.process_status == ProcessStatus.STOPPED):
                    self.send_status_change_mail(
                        self.component.process.process_status,
                        within_startup_grace=within_startup_grace,
                        error_message=status_change_error_message,
                    )
                self.send_details()
            except Exception as loop_err:
                self.logger.exception(f"Watch cycle failed for {self.component.name}: {loop_err}")
            finally:
                self.stop_event.wait(settings.watcher_settings.watch_interval)
        self.logger.info(f"Watch loop stopped for {self.component.name}.")
    
