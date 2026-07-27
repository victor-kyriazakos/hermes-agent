#!/usr/bin/env python3
"""Add one toolset to an explicitly configured platform without widening scope."""

from __future__ import annotations

import re
import sys

from hermes_cli.config import load_config, save_config


_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def ensure_platform_toolset(platform: str, toolset: str) -> str:
    if not _SAFE_NAME.fullmatch(platform) or not _SAFE_NAME.fullmatch(toolset):
        raise ValueError("platform and toolset must be simple lowercase names")

    config = load_config()
    platform_toolsets = config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        return "default"

    configured = platform_toolsets.get(platform)
    if not isinstance(configured, list):
        return "default"
    if toolset in configured:
        return "present"

    configured.append(toolset)
    save_config(config)
    return "added"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: ensure_platform_toolset.py PLATFORM TOOLSET", file=sys.stderr)
        return 2
    result = ensure_platform_toolset(sys.argv[1], sys.argv[2])
    print(f"hermes_platform_toolset={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
