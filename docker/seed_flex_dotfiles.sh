#!/bin/sh
set -eu

home="${HERMES_HOME:-${HOME:?HOME is required}}"

for rc in "$home/.profile" "$home/.bashrc"; do
  if [ -L "$rc" ]; then
    printf 'flex_env2 refusing symlink: %s\n' "$rc" >&2
    exit 1
  fi
  if [ -e "$rc" ] && [ ! -f "$rc" ]; then
    printf 'flex_env2 refusing non-regular file: %s\n' "$rc" >&2
    exit 1
  fi
  if ! grep -q 'HERMES-FLEX-ENV' "$rc" 2>/dev/null; then
    umask 077
    cat >> "$rc" <<'EOFRC'
# >>> HERMES-FLEX-ENV (managed by railway wrapper; do not edit inside markers)
export HERMES_HOME="/opt/data"
export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:$PATH"
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

printf '%s\n' 'flex_env2 dotfiles=seeded'
