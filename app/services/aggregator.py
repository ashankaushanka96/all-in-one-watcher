from loguru import logger
import time
import requests
from app.models.aggregator_models import ComponentDetailsPayload
from app.models.component_model import Component
import app.variables as var
from app.config.settings import load_settings
from app.config.constants import REQUEST_TIMEOUT

settings = load_settings()

class Aggregator:
    def __init__(self, component: Component):
        self.component = component
        self.logger = logger.bind(comp_name=component.name)
        self.component_ingest_url = settings.aggregator_backend.base_url.rstrip("/") + settings.aggregator_backend.component_ingest_path
        self._send_failing = False
    
    def send_details(self):
        payload = ComponentDetailsPayload(
            server_ip=var.SERVER_IP,
            region=settings.meta_data.region,
            component=self.component.name,
            pid=0 if self.component.process.pid is None else self.component.process.pid,
            ts=int(time.time()),
            listen_port=0 if self.component.port is None else self.component.port,
            connections=self.component.process.connections if self.component.process else [],
            state=self.component.process.process_status.value if self.component.process and self.component.process.process_status else None,
            needs_to_run=self.component.process.needs_to_run if self.component.process else None,
            port_status=self.component.process.port_status.value if self.component.process and self.component.process.port_status else None,
            uptime_seconds=self.component.process.uptime if self.component.process else None,
        )

        try:
            resp = requests.post(self.component_ingest_url, json=payload.model_dump(mode='json'), timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            if self._send_failing:
                self._send_failing = False
                self.logger.info(f"Aggregator ingest recovered for {self.component.name}.")
        except requests.exceptions.RequestException as e:
            detail = (
                f"Failed to send component details for {self.component.name} to {self.component_ingest_url}: {e}"
                f"{f' | body: {resp.text[:300]}' if 'resp' in locals() else ''}"
            )
            # Only the first failure of a streak is logged at ERROR; a backend outage would otherwise log every cycle, per component.
            if self._send_failing:
                self.logger.debug(detail)
            else:
                self._send_failing = True
                self.logger.error(detail)
