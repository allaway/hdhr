#!/usr/bin/env bash
# Install or update hdhr as a systemd service (Raspberry Pi OS / Debian / Ubuntu).
#   git clone https://github.com/allaway/hdhr && cd hdhr && sudo ./deploy/install.sh
# Run it again after `git pull` to update.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi
cd "$(dirname "$0")/.."

echo "==> Installing ffmpeg and python3"
apt-get update -qq
apt-get install -y -qq --no-install-recommends ffmpeg python3

echo "==> Copying app to /opt/hdhr"
install -d /opt/hdhr
install -m 0644 server.py /opt/hdhr/server.py
rm -rf /opt/hdhr/static
cp -r static /opt/hdhr/static
chmod -R a+rX /opt/hdhr

if [ ! -f /etc/default/hdhr ]; then
  echo "==> Creating /etc/default/hdhr (edit it to change options)"
  cat > /etc/default/hdhr <<'EOF'
# Extra options for hdhr (see: python3 /opt/hdhr/server.py --help)
# Examples:
#   HDHR_ARGS="--profile lite"
#   HDHR_ARGS="--device http://192.168.1.50 --port 8080"
HDHR_ARGS=""
EOF
fi

echo "==> Installing and (re)starting the service"
install -m 0644 deploy/hdhr.service /etc/systemd/system/hdhr.service
systemctl daemon-reload
systemctl enable hdhr >/dev/null
systemctl restart hdhr
sleep 3

if systemctl is-active --quiet hdhr; then
  port=$(grep '^HDHR_ARGS' /etc/default/hdhr | grep -o -- '--port [0-9]*' | awk '{print $2}' || true)
  echo
  echo "hdhr is running. Open http://$(hostname -I | awk '{print $1}'):${port:-8080}"
  echo "Logs: journalctl -u hdhr -f"
else
  echo "hdhr failed to start. Check: journalctl -u hdhr -n 50" >&2
  exit 1
fi
