#!/bin/sh
# redprobe one-command install, as an ordinary user (no root, no open ports):
#   sh install.sh https://<backend> <enroll-token> [name] [region]
# Puts redprobe.py into ~/.local/share/redprobe, the config into ~/.config/redprobe/redprobe.conf (mode 600)
# and runs it as a systemd user service (or prints a cron line when systemd --user is not available).
set -e
SERVER=${1:?backend URL}
TOKEN=${2:?enroll token}
NAME=${3:-$(hostname)}
REGION=${4:-}
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
DIR="$HOME/.local/share/redprobe"; CONF_DIR="$HOME/.config/redprobe"
mkdir -p "$DIR" "$CONF_DIR"
cp "$(dirname "$0")/redprobe.py" "$DIR/redprobe.py"
umask 077
cat > "$CONF_DIR/redprobe.conf" <<EOF
REDPROBE_SERVER=$SERVER
REDPROBE_ENROLL_TOKEN=$TOKEN
REDPROBE_NAME=$NAME
REDPROBE_REGION=$REGION
REDPROBE_MAX_PPS=20
EOF
python3 "$DIR/redprobe.py" --register
if systemctl --user status >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/redprobe.service" <<EOF
[Unit]
Description=redprobe measurement probe
After=network-online.target
[Service]
ExecStart=/usr/bin/env python3 $DIR/redprobe.py
Restart=always
RestartSec=30
MemoryMax=96M
CPUQuota=20%
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now redprobe.service
  echo "redprobe runs as a user service. For start at boot without login: loginctl enable-linger $USER"
else
  echo "systemd --user is not available. Add to crontab:"
  echo "@reboot /usr/bin/env python3 $DIR/redprobe.py >/dev/null 2>&1"
fi
