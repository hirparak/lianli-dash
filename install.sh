#!/usr/bin/env bash
# Install the systemd units, substituting this checkout's location and your
# XDG directories. The units run as root (the daemon's IPC socket is
# root-owned), so the paths cannot be discovered at runtime — "~" would be
# /root — and are baked in here instead.
#
# usage: ./install.sh [/path/to/lianli-daemon]
set -euo pipefail

DASH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/lianli-dash"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lianli-dash"
DAEMON_BIN=${1:-$(command -v lianli-daemon || true)}

if [ -z "$DAEMON_BIN" ]; then
  echo "lianli-daemon not found on PATH." >&2
  echo "Build it (see README) and pass its path: ./install.sh /path/to/lianli-daemon" >&2
  exit 1
fi

mkdir -p "$CONFIG_DIR" "$DATA_DIR/backgrounds"

for unit in lianli-daemon.service lianli-dash.service; do
  sed -e "s|@DASH_DIR@|$DASH_DIR|g" \
      -e "s|@CONFIG_DIR@|$CONFIG_DIR|g" \
      -e "s|@DATA_DIR@|$DATA_DIR|g" \
      -e "s|@DAEMON_BIN@|$DAEMON_BIN|g" \
      "$DASH_DIR/systemd/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
  echo "installed /etc/systemd/system/$unit"
done

sudo systemctl daemon-reload
echo
echo "Now:  sudo systemctl enable --now lianli-daemon lianli-dash"
