#!/bin/sh
set -eu

# Repurposed Railway template services launch commands under Railway's PID 1
# shim, which conflicts with s6-overlay's requirement that /init be PID 1.
# Run the Hermes relay gateway directly and foreground it instead.
home="${HERMES_HOME:-/opt/data}"
export HERMES_HOME="$home"
mkdir -p "$home"
chown hermes:hermes "$home"

# ---------- FLEX STAGING SETUP (2026-07-27) ----------
# Give the agent full self-service on its own box:
#  - HOME on the durable volume: pip --user, npm, uv, tool caches, dotfiles
#    all land somewhere writable that survives redeploys (the hermes user's
#    passwd HOME is not writable; without this, "hermes not in PATH"-style
#    constraints appear whenever a subshell re-inits from HOME).
#  - hermes + venv + user-local bins explicitly on PATH in the gateway's own
#    environment (terminal-tool subshells inherit it).
#  - Lazy installs re-enabled, targeted at the volume (sys.path APPEND — adds
#    modules, can never shadow core; the sealed /opt/hermes venv stays sealed).
#  - PYTHONUSERBASE + PIP_/UV_ caches on the volume so `pip install --user`
#    and `uv venv/pip` work for the agent's own projects.
export HOME="$home"
export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:$home/.local/bin:${PATH}"
unset HERMES_DISABLE_LAZY_INSTALLS || true
export HERMES_LAZY_INSTALL_TARGET="${HERMES_LAZY_INSTALL_TARGET:-$home/lazy-packages}"
export PYTHONUSERBASE="$home/.local"
export PIP_CACHE_DIR="$home/.cache/pip"
export UV_CACHE_DIR="$home/.cache/uv"
export npm_config_prefix="$home/.local"
export npm_config_cache="$home/.cache/npm"
mkdir -p "$home/.local/bin" "$home/.cache/pip" "$home/.cache/uv" "$home/.cache/npm" "$HERMES_LAZY_INSTALL_TARGET"
chown -R hermes:hermes "$home/.local" "$home/.cache" "$HERMES_LAZY_INSTALL_TARGET" 2>/dev/null || true
printf 'flex_env HOME=%s PATH_head=%s lazy_target=%s\n' "$HOME" "${PATH%%:*}" "$HERMES_LAZY_INSTALL_TARGET"
# ---------- END FLEX ----------

/command/s6-setuidgid hermes sh -c 'command -v hermes >/dev/null'
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes --version >/dev/null
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python -m hermes_cli.main --help >/dev/null
printf '%s\n' 'hermes_runtime_cli_preflight=passed'
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.default gpt-5.6-terra
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.provider openai-api
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.base_url https://api.openai.com/v1
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.api_mode codex_responses
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set agent.reasoning_effort medium
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set streaming.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set streaming.transport auto
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set display.platforms.slack.tool_progress all
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set display.platforms.slack.tool_progress_grouping accumulate
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.metrics_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.diagnostic_events_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.warning_error_events_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.export_interval_seconds 15
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.endpoint http://otel-collector.railway.internal:4318/v1/traces
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python /opt/hermes/docker/ensure_platform_toolset.py slack terminal
cd "$home"
exec /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes gateway run --no-supervise -v
