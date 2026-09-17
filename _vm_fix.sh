#!/usr/bin/env bash
set -e
echo "=== stop service (ensure halted) ==="
sudo systemctl stop maehong || true

echo "=== 1. ENABLE_AUTO_FETCH=0 via systemd drop-in ==="
sudo mkdir -p /etc/systemd/system/maehong.service.d
sudo tee /etc/systemd/system/maehong.service.d/override.conf > /dev/null <<OV
[Service]
Environment=ENABLE_AUTO_FETCH=0
OV

echo "=== 2. add 2GB swap (safety net) ==="
if [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
fi
free -m | awk 'NR==1; NR==2{print}; /Swap/{print}'

echo "=== 3. reload + start ==="
sudo systemctl daemon-reload
sudo systemctl start maehong
echo "=== 4. wait for app ==="
for i in $(seq 1 12); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 http://localhost:8080/ || echo 000)
  echo "try$i HTTP $c"
  [ "$c" = "200" ] && break
  sleep 5
done
echo "=== 5. status + mem ==="
systemctl is-active maehong
sudo systemctl show maehong -p MemoryCurrent --value
sudo journalctl -u maehong --no-pager -n 6 | tail -6
