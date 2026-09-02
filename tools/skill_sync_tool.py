#!/usr/bin/env python3
"""In-process Skill Sync tool.

Runs synchronization inside the Hermes process so the identity resolver can use
its protected relay credentials. Those credentials intentionally never enter a
terminal subprocess.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit

from tools import skills_sync_client as ssc
from tools.registry import registry, tool_error


def _ready_identity() -> dict[str, Any]:
    if not ssc.sync_feature_enabled():
        raise ssc.SyncInertError("sync feature is disabled for this Hermes instance")
    base_url = ssc.resolve_sync_base_url()
    if not base_url:
        raise ssc.SyncInertError("no sync base URL is configured")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ssc.SyncInertError("sync base URL must use HTTPS")

    # A managed relay credential must only leave the process for the same host
    # the operator configured as its relay. The environment value is a process-
    # level trust anchor: unlike config.yaml, an agent tool cannot rewrite it.
    relay_url = os.environ.get("GATEWAY_RELAY_URL", "").strip()
    if relay_url:
        relay_host = urlsplit(relay_url).hostname
        if not relay_host or parsed.hostname != relay_host:
            raise ssc.SyncInertError(
                "sync base URL host must match the configured relay host"
            )

    identity = ssc.resolve_identity()
    if not identity.get("access_allowed", identity.get("nous_admin", False)):
        raise ssc.SyncInertError("sync is not enabled for this identity")
    return identity


def _result_succeeded(result: Any) -> bool:
    return not isinstance(result, dict) or result.get("ok") is not False


def skill_sync_tool(*, action: str) -> str:
    """Run one Skill Sync operation in the authenticated agent process."""
    if action == "status":
        return json.dumps(
            {"success": True, "action": "status", "status": ssc.sync_status()},
            ensure_ascii=False,
        )
    if action == "pull":
        try:
            identity = _ready_identity()
            with ssc.sync_operation():
                result = ssc.pull_skills(identity=identity)
        except (ssc.SyncInertError, ssc.SyncError) as exc:
            return tool_error(f"Skill sync failed: {exc}")
        return json.dumps(
            {
                "success": _result_succeeded(result),
                "action": "pull",
                "result": result,
            },
            ensure_ascii=False,
        )
    if action == "push":
        try:
            identity = _ready_identity()
            with ssc.sync_operation():
                result = ssc.push_skills(
                    identity=identity,
                    message="hermes skill_sync push",
                )
        except (ssc.SyncInertError, ssc.SyncError) as exc:
            return tool_error(f"Skill sync failed: {exc}")
        return json.dumps(
            {
                "success": _result_succeeded(result),
                "action": "push",
                "result": result,
            },
            ensure_ascii=False,
        )
    if action == "now":
        try:
            identity = _ready_identity()
            with ssc.sync_operation():
                pull_result = ssc.pull_skills(identity=identity)
                push_result = ssc.push_skills(
                    identity=identity,
                    message="hermes skill_sync now",
                )
        except (ssc.SyncInertError, ssc.SyncError) as exc:
            return tool_error(f"Skill sync failed: {exc}")
        return json.dumps(
            {
                "success": (
                    _result_succeeded(pull_result)
                    and _result_succeeded(push_result)
                ),
                "action": "now",
                "pull": pull_result,
                "push": push_result,
            },
            ensure_ascii=False,
        )
    return tool_error(f"Unknown skill sync action: {action}")


def _handle_skill_sync(args: dict[str, Any], **_kwargs: Any) -> str:
    return skill_sync_tool(action=str(args.get("action", "")))


SKILL_SYNC_SCHEMA = {
    "name": "skill_sync",
    "description": (
        "Synchronize personal skills from inside Hermes. Use action='now' when "
        "the user asks to sync their skills; it pulls remote changes, then pushes "
        "local eligible skills. Use this tool instead of the terminal or shell "
        "CLI because protected relay identity credentials are intentionally "
        "unavailable to subprocesses. This tool is safe to call from scheduled "
        "agent jobs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "pull", "push", "now"],
                "description": (
                    "status: inspect configuration and state; pull: fetch remote "
                    "skills; push: publish local eligible skills; now: pull then push."
                ),
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="skill_sync",
    toolset="skills",
    schema=SKILL_SYNC_SCHEMA,
    handler=_handle_skill_sync,
    emoji="🔄",
)
