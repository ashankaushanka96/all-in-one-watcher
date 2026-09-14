import yaml
from pathlib import Path
from functools import lru_cache
import app.variables as var
from app.utills.exceptions import ConfigLoadError
from app.models.settings_model import Settings


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    config_dir = Path(var.WATCHER_DIRECTORY) / "config"
    app_config_path = config_dir / "appconfig.yaml"
    # Gitignored developer-local overrides (e.g. pointing aggregator_backend at 127.0.0.1), never deployed.
    local_config_path = config_dir / "appconfig.local.yaml"
    try:
        with app_config_path.open("r", encoding="utf-8") as file:
            raw_config = yaml.safe_load(file) or {}
            if not isinstance(raw_config, dict):
                raise ConfigLoadError("appconfig.yaml root must be a mapping")

        if local_config_path.is_file():
            with local_config_path.open("r", encoding="utf-8") as file:
                local_config = yaml.safe_load(file) or {}
            if not isinstance(local_config, dict):
                raise ConfigLoadError("appconfig.local.yaml root must be a mapping")
            raw_config = _deep_merge(raw_config, local_config)

        return Settings(**raw_config)
    except FileNotFoundError as exc:
        raise ConfigLoadError(f"Config file {app_config_path} not found.") from exc
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"Error parsing YAML file {app_config_path}: {exc}") from exc
