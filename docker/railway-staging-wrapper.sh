#!/command/with-contenv sh
set -eu

# Railway may retain a legacy service start command when a service is repurposed.
# This staging entrypoint intentionally ignores those arguments and launches the
# exact Hermes relay build as a gateway after seeding non-secret model settings.
home="${HERMES_HOME:-/opt/data}"
export HERMES_HOME="$home"

# ---------- FLEX STAGING SETUP (2026-07-27) ----------
# Mirror of railway-direct-wrapper.sh: HOME + user-local PATH on the durable
# volume, lazy installs re-enabled onto the volume, pip/uv/npm self-service.
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

# Flex round 2 (2026-07-27b): the gateway env exports above don't survive a
# LOGIN shell — the agent's terminal tool spawns `bash -l`, /etc/profile
# resets PATH to the distro default, and HOME(/opt/data) had no dotfiles to
# restore it => "hermes: command not found" inside the agent's own terminal.
# Two independent fixes, either alone is sufficient:
#  1. Symlink hermes (the privilege shim) into /usr/local/bin — present in
#     every default PATH, immune to profile resets.
#  2. Seed marker-managed dotfiles on the volume HOME so login/interactive
#     shells rebuild the full flex env (PATH, caches, lazy target).
ln -sf /opt/hermes/bin/hermes /usr/local/bin/hermes
# Persistent dotfiles are controlled by the hermes user. Seed them only after
# dropping privileges, and reject symlinks or non-regular files in the seeder.
s6-setuidgid hermes /opt/hermes/docker/seed_flex_dotfiles.sh
printf 'flex_env2 symlink=%s dotfiles=seeded\n' "$(readlink /usr/local/bin/hermes)"
# ---------- END FLEX ----------

s6-setuidgid hermes sh -c 'command -v hermes >/dev/null'
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes --version >/dev/null
s6-setuidgid hermes /opt/hermes/.venv/bin/python -m hermes_cli.main --help >/dev/null
printf '%s\n' 'hermes_runtime_cli_preflight=passed'
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.default gpt-5.6-terra
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.provider openai-api
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.base_url https://api.openai.com/v1
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.api_mode codex_responses
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set agent.reasoning_effort medium
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set display.platforms.slack.live_status verb
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.resource_attributes.deployment.environment.name staging
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.metrics_enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.diagnostic_events_enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.warning_error_events_enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.export_interval_seconds 15
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.enabled true
s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.endpoint http://otel-collector.railway.internal:4318/v1/traces
s6-setuidgid hermes /opt/hermes/.venv/bin/python /opt/hermes/docker/ensure_platform_toolset.py slack terminal
cd "$home"
exec /opt/hermes/docker/main-wrapper.sh gateway run