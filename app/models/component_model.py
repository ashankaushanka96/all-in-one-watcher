from typing import Dict, List, Optional
from datetime import time 
from pydantic import BaseModel, Field
from enum import Enum
from app.models.aggregator_models import Connection

class ProcessStatus(str, Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    SLEEPING = "SLEEPING"
    WARNING = "WARNING"

class PortStatus(str, Enum):
    LISTENING = "LISTENING"
    NOT_LISTENING = "NOT_LISTENING"
    SLEEPING = "SLEEPING"
    WARNING = "WARNING"

class ServiceCheckStatus(int, Enum):
    OK = 0
    WARNING = 1
    CRITICAL = 2
    UNKNOWN = 3


DATADOG_PROCESS_MAP = {
    ProcessStatus.RUNNING: ServiceCheckStatus.OK,
    ProcessStatus.SLEEPING: ServiceCheckStatus.OK,
    ProcessStatus.STOPPED: ServiceCheckStatus.CRITICAL,
    ProcessStatus.WARNING: ServiceCheckStatus.WARNING
}

DATADOG_PORT_MAP = {
    PortStatus.LISTENING: ServiceCheckStatus.OK,
    PortStatus.SLEEPING: ServiceCheckStatus.OK,
    PortStatus.NOT_LISTENING: ServiceCheckStatus.CRITICAL,
    PortStatus.WARNING: ServiceCheckStatus.WARNING
}

class ProcessDetails(BaseModel):
    pid: Optional[int] = None
    needs_to_run: Optional[bool] = False
    process_status: Optional[ProcessStatus] = None
    port_status: Optional[PortStatus] = None
    uptime: Optional[int] = None
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    memory_used_mb: Optional[float] = None
    log_directory_size_mb: Optional[float] = None
    connections: Optional[List[Connection]] = []

class ScheduleWindow(BaseModel):
    effectiveDay: int
    startTime: time
    startTimeSeconds: int
    endTime: time
    endTimeSeconds: int



class Component(BaseModel):
    id: str
    tag: str
    name: str
    port: Optional[int] = None
    schedules: Dict[int, List[ScheduleWindow]] = Field(default_factory=dict)
    maxUpDays: int = 1
    needToUp: bool = False
    needToSendMail: bool = False
    runScriptPath: Optional[str] = None
    runScript: Optional[str] = None
    logDirectory: Optional[str] = None
    startCheckInterval: Optional[int] = Field(default=None, ge=0)
    process: Optional[ProcessDetails] = None
