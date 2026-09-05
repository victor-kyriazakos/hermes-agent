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
# ---------------------------------------------------------------------------
# Baked profiles (4x4x1 pilot, PRD v3 §4.3): the connector routes
# `carol@C2 -> inst-dmitry:review` by stamping source.profile; this gateway
# must SERVE that profile or the turn is dropped fail-closed. Profiles are
# declared by HERMES_STAGING_PROFILES (comma list, default none). Each is
# created once (idempotent: skipped when present) as a clone of the default
# profile's config + .env, so model and credentials follow, then given its
# own SOUL. Multiplexing is switched on only when at least one is declared.
# Interim: profiles are baked here, not managed. Managed profiles (Coatue's
# pattern) belong next to skill sync; tracked in the working page.
# ---------------------------------------------------------------------------
if [ -n "${HERMES_STAGING_PROFILES:-}" ]; then
  for p in $(printf '%s' "$HERMES_STAGING_PROFILES" | tr ',' ' '); do
    if [ -d "$home/profiles/$p" ]; then
      printf 'baked_profile present=%s\n' "$p"
      continue
    fi
    if /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes profile create "$p" --clone >/dev/null 2>&1; then
      # `--clone` copies config + .env and writes a template SOUL; replace the
      # SOUL with the role for this pilot. First creation only: an operator
      # may edit the SOUL on the volume afterwards and it must survive restarts.
      /command/s6-setuidgid hermes sh -c "cat > '$home/profiles/$p/SOUL.md'" <<EOFSOUL
# $p

You are the **$p** profile of this agent. You share the agent's identity and
credentials but serve a distinct role selected by the channel you were reached
in. When asked who you are, say you are the $p profile and name the role.

Role for review: a careful code and document reviewer. Lead with the single
most important finding, then a short ordered list. Prefer questions that expose
risk over praise. Never rewrite the author's work unasked.
EOFSOUL
      printf 'baked_profile created=%s soul=written\n' "$p"
    else
      printf 'baked_profile create FAILED=%s\n' "$p"
    fi
  done
  # Verified at this SHA: writes gateway.multiplex_profiles, read by
  # load_gateway_config(); profiles_to_serve() then lists default + baked.
  /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes config set gateway.multiplex_profiles true --force >/dev/null
  printf 'baked_profiles multiplex=on set=%s\n' "$HERMES_STAGING_PROFILES"
fi

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
cat > "$relay_dir/plugins.toml" <<EOFTOML
version = 1

[[components]]
kind = "observability"
enabled = true

[components.config]
version = 2

[components.config.opentelemetry]
enabled = true
endpoint = "http://otel-collector.railway.internal:4318/v1/traces"
transport = "http_binary"
service_name = "hermes-gateway"
service_namespace = "nous-enterprise-staging"
mark_projection = "inherit"
mark_exclude_names = ["llm.chunk"]

[components.config.opentelemetry.resource_attributes]
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
# ---------- END RELAY TELEMETRY ----------

cd "$home"
exec /command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes gateway run --no-supervise -v
