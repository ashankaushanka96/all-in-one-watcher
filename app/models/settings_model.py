from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class MetaDataConfig(BaseModel):
    region: Optional[str] = "us-east-1"

class AggregatorBackendConfig(BaseModel):
    base_url: Optional[str] = None
    enabled: bool = True
    component_db_ingest_path: str = "/api/v1/components/add-modify"
    component_ingest_path: str = "/api/v1/component/ingest-component"
    server_details_ingest_path: str = "/api/v1/server/ingest-server-details"

class SecretConfig(BaseModel):
    name: Optional[str] = None

class EnvConfig(BaseModel):
    path: str = "~/.env"

class MailCredentialsConfig(BaseModel):
    env: EnvConfig = Field(default_factory=EnvConfig)
    secret: SecretConfig = Field(default_factory=SecretConfig)

class MailConfigs(BaseModel):
    from_address: str = "watcher@example.com"
    to_addresses: List[str] = Field(default_factory=lambda: ["alerts@example.com"])
    mail_send: bool = True
    mail_credentials: MailCredentialsConfig = Field(default_factory=MailCredentialsConfig)

class DatadogMetricsConfig(BaseModel):
    enabled: bool = True

class WatcherSettingsConfig(BaseModel):
    watch_interval: int = 20
    component_collection_interval: int = 10800
    monitor_interval: int = 60
    start_check_interval: int = Field(default=2, ge=0)

class WatcherApiCredentialsConfig(BaseModel):
    env: EnvConfig = Field(default_factory=EnvConfig)
    secret: SecretConfig = Field(default_factory=SecretConfig)

class WatcherApiConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9500
    api_key_credentials: WatcherApiCredentialsConfig = Field(default_factory=WatcherApiCredentialsConfig)

class ComponentClassificationConfig(BaseModel):
    tool_names: List[str] = Field(default_factory=lambda: ["rtconsole"])
    tool_prefixes: List[str] = Field(default_factory=list)
    tool_suffixes: List[str] = Field(default_factory=list)

class Settings(BaseModel):
    meta_data: MetaDataConfig = Field(default_factory=MetaDataConfig)
    aws: Dict[str, Any] = Field(default_factory=dict)
    aggregator_backend: AggregatorBackendConfig = Field(default_factory=AggregatorBackendConfig)
    mail_configs: MailConfigs = Field(default_factory=MailConfigs)
    datadog_metrics: DatadogMetricsConfig = Field(default_factory=DatadogMetricsConfig)
    watcher_settings: WatcherSettingsConfig = Field(default_factory=WatcherSettingsConfig)
    watcher_api: WatcherApiConfig = Field(default_factory=WatcherApiConfig)
    component_classification: ComponentClassificationConfig = Field(default_factory=ComponentClassificationConfig)
