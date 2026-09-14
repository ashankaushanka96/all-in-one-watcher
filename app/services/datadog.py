from typing import Optional, List
from loguru import logger
from datadog import statsd
from app.models.component_model import DATADOG_PORT_MAP,DATADOG_PROCESS_MAP, Component, ServiceCheckStatus



class Datadog:
    def __init__(self, component: Component):
        self.component = component
        self.logger = logger.bind(comp_name=component.name)

    def safe_statsd_service_check(self, name: str, status: int, tags: Optional[List[str]]=None):
        try:
            statsd.service_check(name, status, tags=tags or [])
        except Exception as e:
            self.logger.debug(f"StatsD service_check failed for {name}: {e}")

    def safe_statsd_gauge(self, name: str, value: float, tags: Optional[List[str]]=None):
        try:
            statsd.gauge(name, value, tags=tags or [])
        except Exception as e:
            self.logger.debug(f"StatsD gauge failed for {name}: {e}")   

    def send_metrics(self):
        process = self.component.process
        if process is None:
            return

        process_status = process.process_status
        port_status = process.port_status
        uptime = process.uptime
        cpu_percent = process.cpu_percent
        memory_percent = process.memory_percent
        memory_used_mb = process.memory_used_mb
        log_directory_size_mb = process.log_directory_size_mb
        max_up_days = self.component.maxUpDays
        max_uptime_seconds = max_up_days * 86400

        tags = [f"component:{self.component.name}"]

        if process_status is not None:
            process_status = DATADOG_PROCESS_MAP.get(process_status, ServiceCheckStatus.UNKNOWN)
            tags.append(f"port:{self.component.port}")
            self.safe_statsd_service_check("feed.component.process.status", process_status, tags=tags)
            self.safe_statsd_gauge("feed.component.process.status.value", process_status.value, tags=tags)

        if port_status is not None:
            port_status = DATADOG_PORT_MAP.get(port_status, ServiceCheckStatus.UNKNOWN)
            self.safe_statsd_service_check("feed.component.port.status", port_status, tags=tags)
            self.safe_statsd_gauge("feed.component.port.status.value", port_status.value, tags=tags)

        if uptime is not None:
            self.safe_statsd_gauge("feed.component.process.uptime", uptime, tags=tags)
            uptime_max_exceeded_status = (ServiceCheckStatus.CRITICAL if uptime > max_uptime_seconds else ServiceCheckStatus.OK)
            self.safe_statsd_service_check("feed.component.process.uptime.exceeded.status", uptime_max_exceeded_status, tags=tags)
            self.safe_statsd_gauge("feed.component.process.uptime.exceeded.status.value", uptime_max_exceeded_status.value, tags=tags)
        if cpu_percent is not None:
            self.safe_statsd_gauge("feed.component.process.cpu.percent", cpu_percent, tags=tags)
        if memory_percent is not None:
            self.safe_statsd_gauge("feed.component.process.memory.percent", memory_percent, tags=tags)
        if memory_used_mb is not None:
            self.safe_statsd_gauge("feed.component.process.memory.used", memory_used_mb, tags=tags)
        if log_directory_size_mb is not None:
            self.safe_statsd_gauge("feed.component.log_directory.size", log_directory_size_mb, tags=tags)
            
