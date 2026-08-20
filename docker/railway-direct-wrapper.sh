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
for rc in "$home/.profile" "$home/.bashrc"; do
  if ! grep -q 'HERMES-FLEX-ENV' "$rc" 2>/dev/null; then
    cat >> "$rc" <<EOFRC
# >>> HERMES-FLEX-ENV (managed by railway wrapper — do not edit inside markers)
export HERMES_HOME="/opt/data"
export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:\$PATH"
export HERMES_LAZY_INSTALL_TARGET="/opt/data/lazy-packages"
export PYTHONUSERBASE="/opt/data/.local"
export PIP_CACHE_DIR="/opt/data/.cache/pip"
export UV_CACHE_DIR="/opt/data/.cache/uv"
export npm_config_prefix="/opt/data/.local"
export npm_config_cache="/opt/data/.cache/npm"
# <<< HERMES-FLEX-ENV
EOFRC
  fi
done
chown hermes:hermes "$home/.profile" "$home/.bashrc" 2>/dev/null || true
printf 'flex_env2 symlink=%s dotfiles=seeded\n' "$(readlink /usr/local/bin/hermes)"
# ---------- END FLEX ----------

/command/s6-setuidgid hermes sh -c 'command -v hermes >/dev/null'
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes --version >/dev/null
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python -m hermes_cli.main --help >/dev/null
printf '%s\n' 'hermes_runtime_cli_preflight=passed'

# ---------- MANAGED SCOPE (IT policy layer, 2026-08-13) ----------
# Exercises hermes-agent managed scope (PR #49098): root-owned /etc/hermes
# supplies config.yaml + .env values that win per-leaf over the user layer.
# This block runs in the wrapper's ROOT window (before s6-setuidgid drops to
# the hermes user), which is exactly the enterprise provisioning shape: IT
# automation writes the policy as root; the runtime user can read, not write.
# NOTE: /etc/hermes deliberately — NOT under /opt/data. The hermes user owns
# /opt/data, and owning the parent directory is enough to unlink a root-owned
# subdirectory. The managed dir must live under a root-owned parent.
# Gated on HERMES_MANAGED_RELAY_URL so other fleet boxes are unaffected.
if [ -n "${HERMES_MANAGED_RELAY_URL:-}" ]; then
  mkdir -p /etc/hermes
  cat > /etc/hermes/config.yaml <<EOFMANAGED
# Managed by IT (staging simulation). Users cannot edit or override these keys.
gateway:
  relay_url: ${HERMES_MANAGED_RELAY_URL}
  idp:
    token_url: ${HERMES_MANAGED_IDP_TOKEN_URL}
# Platform ENABLE path: gateway/config.py reads platforms.relay.extra.relay_url
# (or the GATEWAY_RELAY_URL env var) to bring the relay into the connect loop.
# gateway.relay_url above covers the dial/self-provision path only — both are
# needed when the env var is absent.
platforms:
  relay:
    enabled: true
    extra:
      relay_url: ${HERMES_MANAGED_RELAY_URL}
model:
  default: gpt-5.6-terra
  provider: openai-api
# NOTE: providers.<slug>.models is NOT an enforcement key (it widens
# acceptance for models missing from live listings; it never narrows).
# The supported fleet-wide model policy is: pin model.default here +
# gate the /model command via platforms.<p>.extra.allow_admin_from /
# user_allowed_commands (guide 10 appendix A.1/A.2).
EOFMANAGED
  cat > /etc/hermes/.env <<EOFMANAGEDENV
GATEWAY_RELAY_IDP_CLIENT_ID=${HERMES_MANAGED_IDP_CLIENT_ID}
GATEWAY_RELAY_IDP_CLIENT_SECRET=${HERMES_MANAGED_IDP_CLIENT_SECRET}
EOFMANAGEDENV
  chmod 0755 /etc/hermes
  chmod 0644 /etc/hermes/config.yaml /etc/hermes/.env
  chown -R root:root /etc/hermes
  printf 'managed_scope ACTIVE dir=/etc/hermes (relay_url+idp.token_url+model pins in config.yaml; idp client creds in .env)\n'
fi
# ---------- END MANAGED SCOPE ----------

# NOTE: model.default/model.provider sets tolerate managed-scope refusal —
# when IT pins these keys the refusal IS the correct behavior, not a boot error.
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.default gpt-5.6-terra || true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.provider openai-api || true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.base_url https://api.openai.com/v1
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set model.api_mode codex_responses
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set agent.reasoning_effort medium
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set streaming.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set streaming.transport auto
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set display.platforms.slack.tool_progress all
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set display.platforms.slack.tool_progress_grouping accumulate
# Continuable-cron testing (2026-08-20): mirror every origin delivery into the
# target chat's session so replies to a cron brief continue in-context even
# when the job was created without attach_to_session (the B-matrix global-on
# cells; per-job attach_to_session=false still opts out — precedence tested).
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set cron.mirror_delivery true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.metrics_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.diagnostic_events_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.warning_error_events_enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.gateway_health_export.export_interval_seconds 15
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.enabled true
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set monitoring.export.otlp.endpoint http://otel-collector.railway.internal:4318/v1/traces
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python /opt/hermes/docker/ensure_platform_toolset.py slack terminal

# ---------- RELAY SLACK CONFIG MATRIX (2026-08-19, rc.4 parity testing) ----------
# Per-box relay Slack behavior knobs (platforms.relay.extra.slack.*) driven by
# ONE Railway service var so the fleet can hold a different combination per box
# for the #90038/#214 parity matrix without branch churn.
#
#   HERMES_RELAY_SLACK_KNOBS="reply_in_thread=false,cron_continuable_surface=in_channel,markdown_blocks=true"
#
# Comma-separated key=value pairs; keys land verbatim under
# platforms.relay.extra.slack.<key>. Unset var = no writes = shipping defaults
# (thread surface, threaded replies, plain mrkdwn) — the control cell.
if [ -n "${HERMES_RELAY_SLACK_KNOBS:-}" ]; then
  printf 'relay_slack_knobs ACTIVE: %s\n' "${HERMES_RELAY_SLACK_KNOBS}"
  echo "${HERMES_RELAY_SLACK_KNOBS}" | tr ',' '\n' | while IFS='=' read -r k v; do
    [ -n "$k" ] && [ -n "$v" ] && \
      /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set "platforms.relay.extra.slack.${k}" "${v}" || true
  done
fi
# ---------- END RELAY SLACK CONFIG MATRIX ----------

# ---------- CREDENTIAL POOL SEEDING (2026-08-10) ----------
# Terminal/execute_code subprocesses deliberately strip provider API keys
# from the child env (GHSA-rhgp-j443-p4rf posture; env_passthrough refuses
# to re-allow them). A `hermes` CLI child spawned by the agent therefore
# cannot inherit OPENAI_API_KEY and fails with "No usable credentials".
# The sanctioned lane is the auth store / credential pool on the durable
# volume (auth.json), which the CLI resolution chain consults after env.
# Seed it idempotently from the service var so spawned CLI probes
# (multi-model telemetry tests) resolve credentials without env leakage.
if [ -n "${OPENAI_API_KEY:-}" ]; then
  /command/s6-setuidgid hermes env OPENAI_API_KEY="$OPENAI_API_KEY" \
    /opt/hermes/.venv/bin/python - <<'PYEOF'
import os, sys
from agent.credential_pool import load_pool
from hermes_cli.auth import has_usable_secret
try:
    pool = load_pool("openai-api")
    entries = pool.entries() if pool else []
    # Diagnostic (no secrets): label/auth_type/usable per entry, so boot
    # logs explain WHY seeding did or didn't run.
    for e in entries:
        d = e.to_dict() if hasattr(e, "to_dict") else {}
        print("credential_pool openai-api entry: label=%s auth_type=%s usable=%s exhausted=%s" % (
            d.get("label"), d.get("auth_type"),
            has_usable_secret(d.get("api_key")), bool(d.get("exhausted_at"))))
    # Idempotence keyed on a USABLE api-key entry, not mere entry count —
    # an OAuth remnant or exhausted row must not suppress seeding.
    def _usable(e):
        d = e.to_dict() if hasattr(e, "to_dict") else {}
        return d.get("auth_type", "api_key") == "api_key" and \
            has_usable_secret(d.get("api_key")) and not d.get("exhausted_at")
    if any(_usable(e) for e in entries):
        print("credential_pool openai-api: usable api-key entry present — no seeding needed")
        sys.exit(0)
    from hermes_cli import auth_commands
    class _A:  # argparse shim
        provider = "openai-api"; auth_type = "api_key"
        api_key = os.environ["OPENAI_API_KEY"]; label = "railway-service-var"
    auth_commands.auth_add_command(_A())
    print("credential_pool openai-api: seeded from service var")
except SystemExit:
    raise
except Exception as exc:
    print("credential_pool seeding failed (non-fatal): %s" % exc)
PYEOF
fi
# ---------- END CREDENTIAL POOL SEEDING ----------

# ---------- RELAY ANALYTICS/AUDIT TELEMETRY (2026-08-07) ----------
# Rich run/LLM/tool/skill/approval/subagent lifecycle export via the
# observability/nemo_relay plugin, broadcast as OTLP to the same private
# staging collector as the content-free monitoring plane. Bounded mark
# export (llm.chunk excluded); the collector sanitize pass is the second
# fence. service.instance.id is derived with the SAME recipe as the
# monitoring exporter (sha256 of persisted monitoring.install_id, 24 hex
# chars) so the collector operator_join table labels both planes alike.
# Boxes with HERMES_STAGING_TRAJECTORIES=1 (Carol) also write full ATOF
# event streams + ATIF trajectories to the durable volume as the deep
# audit evidence tier.
# PROBE GUARD (2026-08-10, delegation-stall isolation): set
# HERMES_STAGING_DISABLE_NEMO_RELAY=1 on a box to boot WITHOUT the relay
# observability plugin (health/monitoring plane unaffected). Used to
# falsify "plugin scope finalization wedges child run_conversation".
# Remove the variable to restore normal telemetry on next deploy.
if [ "${HERMES_STAGING_DISABLE_NEMO_RELAY:-0}" = "1" ]; then
  /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes plugins disable observability/nemo_relay || true
  printf 'relay_telemetry DISABLED by HERMES_STAGING_DISABLE_NEMO_RELAY probe guard\n'
else
/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes plugins enable observability/nemo_relay
instance_hash="$(/command/s6-setuidgid hermes /opt/hermes/.venv/bin/python - <<'PYEOF'
import hashlib
from hermes_cli.config import load_config
from agent.monitoring.policy import ensure_install_id
iid = ensure_install_id(load_config())
value = str(iid or "unknown").encode("utf-8", errors="replace")
print(f"sha256:{hashlib.sha256(value).hexdigest()[:24]}")
PYEOF
)"
relay_dir="$home/.nemo-relay"
mkdir -p "$relay_dir"
# nemo-relay 0.7.x requires observability config VERSION 3: the flat
# [components.config.opentelemetry] block became a typed endpoints LIST
# ([[...opentelemetry.endpoints]] with type="full"). The old version-2 shape
# fails 0.7.2's validator (observability.unsupported_config_version +
# legacy_opentelemetry_field errors) and the plugin logs it only at DEBUG —
# spans/analytics silently stop (staging outage 2026-08-09, freeze bump
# nemo-relay 0.6.x -> 0.7.1). Validated against 0.7.2's real
# _validate_plugin_config: this shape returns zero diagnostics.
cat > "$relay_dir/plugins.toml" <<EOFTOML
version = 1

[[components]]
kind = "observability"
enabled = true

[components.config]
version = 3

[components.config.opentelemetry]
enabled = true

[[components.config.opentelemetry.endpoints]]
type = "full"
endpoint = "http://otel-collector.railway.internal:4318/v1/traces"
transport = "http_binary"
service_name = "hermes-gateway"
service_namespace = "nous-enterprise-staging"
mark_projection = "inherit"
mark_exclude_names = ["llm.chunk"]

[components.config.opentelemetry.endpoints.resource_attributes]
"service.instance.id" = "$instance_hash"
"telemetry.scope" = "relay_lifecycle"
EOFTOML
if [ "${HERMES_STAGING_TRAJECTORIES:-0}" = "1" ]; then
  mkdir -p "$relay_dir/atof" "$relay_dir/atif"
  cat >> "$relay_dir/plugins.toml" <<EOFTOML

[components.config.atof]
enabled = true

[[components.config.atof.sinks]]
type = "file"
output_directory = "$relay_dir/atof"
filename = "events.jsonl"
mode = "append"

[components.config.atif]
enabled = true
output_directory = "$relay_dir/atif"
filename_template = "trajectory-{session_id}.json"
agent_name = "Hermes Agent Staging"
agent_version = "$(/opt/hermes/.venv/bin/hermes --version 2>/dev/null | head -1 || echo unknown)"
EOFTOML
fi
chown -R hermes:hermes "$relay_dir"
export HERMES_NEMO_RELAY_PLUGINS_TOML="$relay_dir/plugins.toml"
printf 'relay_telemetry plugins_toml=%s instance=%s trajectories=%s\n' \
  "$HERMES_NEMO_RELAY_PLUGINS_TOML" "$instance_hash" "${HERMES_STAGING_TRAJECTORIES:-0}"
fi
# ---------- END RELAY TELEMETRY ----------

cd "$home"
exec /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes gateway run --no-supervise -v
