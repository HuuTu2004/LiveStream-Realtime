#!/usr/bin/env bash
###############################################################################
# Install systemd unit cho LiveTalking — auto-restart on crash/OOM/reboot.
###############################################################################
set -e
cd "$(dirname "$0")/../.."

UNIT_SRC="$(pwd)/scripts/vastai/livetalking.service"
UNIT_DST="/etc/systemd/system/livetalking.service"

if [ ! -f "$UNIT_SRC" ]; then
  echo "[install] missing $UNIT_SRC"
  exit 1
fi

# Vast container may not have systemd. Probe first.
if ! command -v systemctl >/dev/null 2>&1; then
  echo "[install] systemd not available — install supervisord fallback"
  apt-get install -y -qq supervisor || true
  cat > /etc/supervisor/conf.d/livetalking.conf <<'CONF'
[program:livetalking]
command=/bin/bash /tmp/start_prod.sh
directory=/workspace/LiveTalking
autostart=true
autorestart=true
startretries=10
startsecs=300
stopwaitsecs=30
killasgroup=true
stopasgroup=true
stdout_logfile=/workspace/LiveTalking/logs/start.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=5
stderr_logfile=/workspace/LiveTalking/logs/start.log
stderr_logfile_maxbytes=50MB
stderr_logfile_backups=5
environment=PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CONF
  supervisorctl reread || true
  supervisorctl update || true
  supervisorctl start livetalking || true
  echo "[install] supervisord livetalking installed. Status: supervisorctl status livetalking"
  exit 0
fi

# systemd path
cp "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable livetalking.service
systemctl restart livetalking.service
sleep 3
systemctl status livetalking.service --no-pager | head -15
echo "[install] systemd livetalking installed. Status: systemctl status livetalking"
