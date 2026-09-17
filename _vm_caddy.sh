#!/usr/bin/env bash
set -e
HOST=8.235.41.127.sslip.io

echo "=== 1. download caddy ==="
if [ ! -x /usr/local/bin/caddy ]; then
  curl -sL "https://caddyserver.com/api/download?os=linux&arch=amd64" -o /tmp/caddy
  sudo install -m 0755 /tmp/caddy /usr/local/bin/caddy
fi
/usr/local/bin/caddy version

echo "=== 2. Caddyfile ==="
sudo mkdir -p /etc/caddy
sudo tee /etc/caddy/Caddyfile > /dev/null <<CADDY
$HOST {
    reverse_proxy localhost:8080
}
CADDY

echo "=== 3. caddy user + systemd ==="
sudo useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy 2>/dev/null || true
sudo mkdir -p /var/lib/caddy && sudo chown caddy:caddy /var/lib/caddy
sudo tee /etc/systemd/system/caddy.service > /dev/null <<UNIT
[Unit]
Description=Caddy
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=XDG_DATA_HOME=/var/lib/caddy
Environment=XDG_CONFIG_HOME=/var/lib/caddy

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable caddy
sudo systemctl restart caddy
echo "=== 4. wait 25s for cert issuance ==="
sleep 25
sudo systemctl --no-pager status caddy | head -8
echo "=== 5. local https test ==="
curl -sk -o /dev/null -w "HTTPS(local) %{http_code}\n" https://$HOST/ --resolve $HOST:443:127.0.0.1 || true
echo "=== caddy logs ==="
sudo journalctl -u caddy --no-pager -n 15 | tail -15
