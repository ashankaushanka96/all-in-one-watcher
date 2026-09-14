import json
import os
from typing import Optional

import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from loguru import logger

api_logger = logger.bind(comp_name="WatcherAPI")


def _load_from_aws(secret_name: Optional[str], region_name: Optional[str]) -> Optional[str]:
    if not secret_name or not region_name:
        return None
    try:
        client = boto3.client("secretsmanager", region_name=region_name)
        response = client.get_secret_value(SecretId=secret_name)
        secret = response.get("SecretString") or ""
        secrets_dict = json.loads(secret) if secret else {}
        api_key = secrets_dict.get("API_KEY")
        if api_key:
            api_logger.info("Loaded watcher API key from AWS Secrets Manager.")
            return api_key
    except (NoCredentialsError, PartialCredentialsError):
        api_logger.warning("AWS credentials not available for watcher API key lookup.")
    except Exception as exc:
        api_logger.warning(f"Failed to load watcher API key from AWS Secrets Manager: {exc}")
    return None


def _load_from_env_file(env_path: str) -> Optional[str]:
    try:
        from dotenv import load_dotenv
    except Exception:
        api_logger.warning("python-dotenv not installed; cannot load watcher API key from env file.")
        return None

    load_dotenv(dotenv_path=os.path.expanduser(env_path))
    return os.getenv("WATCHER_API_KEY")


def load_watcher_api_key(api_settings, region_name: Optional[str]) -> Optional[str]:
    """Resolves the shared API key: AWS Secrets Manager first, then a local .env file, then a process env var."""
    credentials = api_settings.api_key_credentials

    api_key = _load_from_aws(credentials.secret.name, region_name)
    if api_key:
        return api_key

    api_key = _load_from_env_file(credentials.env.path)
    if api_key:
        api_logger.info("Loaded watcher API key from ENV file.")
        return api_key

    api_key = os.getenv("WATCHER_API_KEY")
    if api_key:
        api_logger.info("Loaded watcher API key from process environment.")
        return api_key

    return None
