#!/command/with-contenv sh
set -eu

# Railway may retain a legacy service start command when a service is repurposed.
# This staging entrypoint intentionally ignores those arguments and launches the
# exact Hermes relay build as a gateway after seeding non-secret model settings.
home="${HERMES_HOME:-/opt/data}"
export HERMES_HOME="$home"
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.default gpt-5.6-terra
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.provider openai-api
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.base_url https://api.openai.com/v1
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.api_mode codex_responses
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set agent.reasoning_effort medium
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.metrics_enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.diagnostic_events_enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.warning_error_events_enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.export_interval_seconds 15
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.endpoint http://otel-collector.railway.internal:4318/v1/traces
cd "$home"
exec /opt/hermes/docker/main-wrapper.sh gateway run