#!/bin/sh
set -eu

# Repurposed Railway template services launch commands under Railway's PID 1
# shim, which conflicts with s6-overlay's requirement that /init be PID 1.
# Run the Hermes relay gateway directly and foreground it instead.
home="${HERMES_HOME:-/opt/data}"
export HERMES_HOME="$home"
mkdir -p "$home"
chown hermes:hermes "$home"
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.default gpt-5.6-terra
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.provider openai-api
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.base_url https://api.openai.com/v1
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.api_mode codex_responses
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set agent.reasoning_effort medium
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.metrics_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.diagnostic_events_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.warning_error_events_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.export_interval_seconds 15
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.endpoint http://otel-collector.railway.internal:4318/v1/traces
cd "$home"
exec /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes gateway run --no-supervise