from __future__ import annotations
from pydantic import BaseModel, Field
from typing import List, Optional, Union
from datetime import date, datetime

DateLike = Union[str, date, datetime]

class Connection(BaseModel):
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int

class ScheduleMeta(BaseModel):
    effective_day: str
    start_time: str
    end_time: str

class ConfigMeta(BaseModel):
    tag: Optional[str] = None
    port: Optional[int] = None
    schedules: Optional[List[ScheduleMeta]] = None
    max_up_days: Optional[int] = None
    need_to_up: Optional[bool] = None
    need_to_send_mail: Optional[bool] = None

class ComponentDetailsPayload(BaseModel):
    server_ip: str
    region: str
    component: str
    pid: Optional[int] = None
    ts: int
    listen_port: Optional[int] = None
    connections: Optional[List[Connection]] = None
    state: Optional[str] = None
    needs_to_run: Optional[bool] = None
    port_status: Optional[str] = None
    uptime_seconds: Optional[int] = None
    config_meta: Optional[ConfigMeta] = None

class ServerDetailsPayload(BaseModel):
    ts: int
    region: str
    hostname: str
    fqdn: Optional[str] = None
    primary_ip: str
    all_ips: List[str] = Field(default_factory=list)
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    os_release: Optional[str] = None
    kernel_version: Optional[str] = None
    kernel_release: Optional[str] = None
    architecture: Optional[str] = None
    platform: Optional[str] = None
    python_version: Optional[str] = None
    current_user: Optional[str] = None
    home_directory: Optional[str] = None
    watcher_directory: Optional[str] = None
    apps_directory: Optional[str] = None
    boot_time: Optional[int] = None
    cpu_count_logical: Optional[int] = None
    cpu_count_physical: Optional[int] = None
    total_memory_mb: Optional[int] = None
    watcher_version: Optional[str] = None
    crons: List[str] = Field(default_factory=list)

class Component(BaseModel):
    comp_name: Optional[str] = None
    category: Optional[str] = "component"
    platform: Optional[str] = "Unknown"
    path: Optional[str] = None
    version: Optional[str] = None
    pipeline: Optional[bool] = False
    description: Optional[str] = None
    previous_tag: Optional[str] = None
    release_date: Optional[datetime] = None
    code_repo_url: Optional[str] = None
    config_repo_url: Optional[str] = None
    script_repo_url: Optional[str] = None
    last_run_time: Optional[datetime] = None
    watcher: Optional[bool] = False
    config_meta: Optional[ConfigMeta] = None


class ComponentCollectorRequest(BaseModel):
    status: str = "failure"
    ip: str = Field(default="")
    region: str = Field(default="")
    components: List[Component] = Field(default_factory=list)
    message: str = ""
