#!/usr/bin/env python3
import sys
from pathlib import Path

import yaml

DEPLOYMENT_OWNED_PATHS = {
    ("aggregator_backend", "base_url"),
}


def load_yaml(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


def merge_with_deployment_shape(deployed_value, existing_value, path=()):
    if path in DEPLOYMENT_OWNED_PATHS:
        return deployed_value

    if isinstance(deployed_value, dict):
        if not isinstance(existing_value, dict):
            return deployed_value

        merged = {}
        for key, deployed_child in deployed_value.items():
            child_path = path + (key,)
            if key in existing_value:
                merged[key] = merge_with_deployment_shape(
                    deployed_child,
                    existing_value[key],
                    child_path,
                )
            else:
                merged[key] = deployed_child
        return merged

    if existing_value is not None:
        return existing_value

    return deployed_value


def apply_environment_overrides(merged_yaml, environment_name: str, region_name: str):
    meta_data = merged_yaml.setdefault("meta_data", {})
    if environment_name != "prod-dc":
        meta_data["region"] = region_name

    datadog_metrics = merged_yaml.setdefault("datadog_metrics", {})
    if environment_name == "uat":
        datadog_metrics["enabled"] = False

    return merged_yaml


def main():
    if len(sys.argv) != 6:
        raise SystemExit(
            "usage: merge_yaml_preserve_existing.py "
            "<deployed> <existing> <output> <environment_name> <region_name>"
        )

    deployed_path = Path(sys.argv[1])
    existing_path = Path(sys.argv[2])
    output_path = Path(sys.argv[3])
    environment_name = sys.argv[4]
    region_name = sys.argv[5]

    deployed_yaml = load_yaml(deployed_path)
    existing_yaml = load_yaml(existing_path)
    merged_yaml = merge_with_deployment_shape(deployed_yaml, existing_yaml)
    merged_yaml = apply_environment_overrides(
        merged_yaml,
        environment_name,
        region_name,
    )

    with output_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(merged_yaml, handle, sort_keys=False)


if __name__ == "__main__":
    main()
