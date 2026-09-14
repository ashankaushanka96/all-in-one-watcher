import getpass
import os
import platform
import pwd
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import psutil
import requests
from loguru import logger

import app.variables as var
from app.config.constants import REQUEST_TIMEOUT, SUBPROCESS_TIMEOUT
from app.config.settings import load_settings
from app.models.aggregator_models import ServerDetailsPayload

settings = load_settings()


def detect_primary_ip() -> str:
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


class ServerDetailsSender:
    def __init__(self):
        self.logger = logger.bind(comp_name="ServerDetails")
        self.server_details_ingest_url = settings.aggregator_backend.base_url.rstrip("/") + settings.aggregator_backend.server_details_ingest_path

    def _read_os_release(self) -> Dict[str, str]:
        os_release = Path("/etc/os-release")
        data: Dict[str, str] = {}
        if not os_release.exists():
            return data

        try:
            for line in os_release.read_text(encoding="utf-8").splitlines():
                if "=" not in line or line.startswith("#"):
                    continue
                key, value = line.split("=", 1)
                data[key] = value.strip().strip('"')
        except Exception as exc:
            self.logger.debug(f"Failed to parse /etc/os-release: {exc}")
        return data

    def _get_watcher_version(self) -> Optional[str]:
        version_path = Path(var.WATCHER_DIRECTORY) / "version.txt"
        if not version_path.exists():
            return None
        try:
            content = version_path.read_text(encoding="utf-8").strip()
            return content or None
        except Exception as exc:
            self.logger.debug(f"Failed to read watcher version.txt: {exc}")
            return None

    def _get_all_ips(self) -> List[str]:
        ips = set()
        primary_ip = detect_primary_ip()
        if primary_ip and primary_ip != "unknown":
            ips.add(primary_ip)

        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127."):
                    ips.add(ip)
        except Exception as exc:
            self.logger.debug(f"Failed to resolve additional IPs: {exc}")

        return sorted(ips)

    def _get_crons(self) -> List[str]:
        try:
            result = subprocess.run(
                ["crontab", "-l"],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
            )
        except FileNotFoundError:
            self.logger.warning("crontab command not found; skipping cron collection.")
            return []
        except Exception as exc:
            self.logger.warning(f"Failed to read user crontab: {exc}")
            return []

        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            if "no crontab for" in stderr:
                return []
            self.logger.warning(
                f"crontab -l exited with code {result.returncode}: {result.stderr.strip()}"
            )
            return []

        return result.stdout.splitlines()

    def collect_payload(self) -> ServerDetailsPayload:
        uname = platform.uname()
        os_release = self._read_os_release()
        primary_ip = detect_primary_ip()
        hostname = socket.gethostname()

        try:
            fqdn = socket.getfqdn()
        except Exception:
            fqdn = None

        try:
            current_user = getpass.getuser()
        except Exception:
            current_user = None

        home_directory = os.path.expanduser("~")
        boot_time = int(psutil.boot_time()) if hasattr(psutil, "boot_time") else None
        total_memory_mb = None
        try:
            total_memory_mb = int(psutil.virtual_memory().total / (1024 * 1024))
        except Exception as exc:
            self.logger.debug(f"Failed to read system memory: {exc}")

        return ServerDetailsPayload(
            ts=int(time.time()),
            region=settings.meta_data.region or "",
            hostname=hostname,
            fqdn=fqdn,
            primary_ip=var.SERVER_IP or primary_ip,
            all_ips=self._get_all_ips(),
            os_name=os_release.get("NAME") or uname.system,
            os_version=os_release.get("VERSION") or os_release.get("VERSION_ID"),
            os_release=os_release.get("PRETTY_NAME") or platform.platform(),
            kernel_version=uname.version,
            kernel_release=uname.release,
            architecture=uname.machine,
            platform=platform.platform(),
            python_version=sys.version.split()[0],
            current_user=current_user,
            home_directory=home_directory,
            watcher_directory=var.WATCHER_DIRECTORY,
            apps_directory=var.APPS_DIRECTORY,
            boot_time=boot_time,
            cpu_count_logical=psutil.cpu_count(logical=True),
            cpu_count_physical=psutil.cpu_count(logical=False),
            total_memory_mb=total_memory_mb,
            watcher_version=self._get_watcher_version(),
            crons=self._get_crons(),
        )

    def send_details(self):
        if not settings.aggregator_backend.enabled:
            self.logger.debug("Aggregator backend disabled; skipping server details send.")
            return
        if not self.server_details_ingest_url:
            self.logger.debug("Aggregator server details URL not configured; skipping.")
            return

        payload = self.collect_payload()
        self.logger.info("Sending server details to aggregator backend...")
        self.logger.debug(f"Server details payload: {payload.model_dump_json()}")

        try:
            resp = requests.post(
                self.server_details_ingest_url,
                json=payload.model_dump(mode="json"),
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            self.logger.info("Server details sent successfully.")
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                f"Failed to send server details: {exc}"
                f"{f' | body: {resp.text}' if 'resp' in locals() else ''}"
            )
