#!/usr/bin/env python3
"""Set the flat OpenTelemetry deployment environment resource attribute."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import yaml

from utils import atomic_yaml_write


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("usage: ensure_monitoring_resource_attributes.py <environment>", file=sys.stderr)
        return 2

    environment = sys.argv[1].strip()
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    config_path = home / "config.yaml"
    config: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise TypeError("config root must be a mapping")
            config = loaded

    monitoring = config.setdefault("monitoring", {})
    gateway = monitoring.setdefault("gateway_health_export", {})
    attributes = gateway.setdefault("resource_attributes", {})
    if not isinstance(attributes, dict):
        raise TypeError("monitoring gateway resource_attributes must be a mapping")

    attributes.pop("deployment", None)
    attributes["deployment.environment.name"] = environment
    home.mkdir(parents=True, exist_ok=True)
    atomic_yaml_write(config_path, config, sort_keys=False)
    print(f"hermes_monitoring_environment={environment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
