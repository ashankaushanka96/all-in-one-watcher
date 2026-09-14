import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from app.config.constants import SUBPROCESS_TIMEOUT

IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
IMDS_INSTANCE_ID_URL = "http://169.254.169.254/latest/meta-data/instance-id"
IMDS_TIMEOUT = 2
CLOUD_INIT_INSTANCE_ID_FILE = Path("/var/lib/cloud/data/instance-id")

_helpers_logger = logger.bind(comp_name="Helpers")


def _instance_id_from_imds() -> Optional[str]:
    try:
        token_resp = requests.put(
            IMDS_TOKEN_URL,
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            timeout=IMDS_TIMEOUT,
        )
        token_resp.raise_for_status()
        resp = requests.get(
            IMDS_INSTANCE_ID_URL,
            headers={"X-aws-ec2-metadata-token": token_resp.text},
            timeout=IMDS_TIMEOUT,
        )
        resp.raise_for_status()
        instance_id = resp.text.strip()
        return instance_id or None
    except Exception as exc:
        _helpers_logger.debug(f"IMDSv2 instance-id lookup failed: {exc}")
        return None


def _instance_id_from_cloud_init_file() -> Optional[str]:
    try:
        if CLOUD_INIT_INSTANCE_ID_FILE.is_file():
            instance_id = CLOUD_INIT_INSTANCE_ID_FILE.read_text(encoding="utf-8").strip()
            return instance_id or None
    except Exception as exc:
        _helpers_logger.debug(f"Reading {CLOUD_INIT_INSTANCE_ID_FILE} failed: {exc}")
    return None


def _instance_id_from_ec2_metadata_cli() -> Optional[str]:
    try:
        result = subprocess.run(
            ["ec2-metadata", "-i"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            return None
        # Output looks like "instance-id: i-0123456789abcdef0"
        _, _, value = result.stdout.strip().partition(":")
        instance_id = value.strip()
        return instance_id or None
    except Exception as exc:
        _helpers_logger.debug(f"ec2-metadata CLI instance-id lookup failed: {exc}")
        return None


@lru_cache(maxsize=1)
def get_instance_id() -> Optional[str]:
    """Resolves this EC2 instance's instance-id once per process, for components whose log path is only known at boot (e.g. ASG members writing to a shared EFS mount under a per-instance directory)."""
    for resolver in (
        _instance_id_from_imds,
        _instance_id_from_cloud_init_file,
        _instance_id_from_ec2_metadata_cli,
    ):
        instance_id = resolver()
        if instance_id:
            return instance_id

    _helpers_logger.warning("Could not resolve EC2 instance-id via IMDSv2, cloud-init, or ec2-metadata CLI.")
    return None
