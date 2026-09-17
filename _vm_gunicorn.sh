#!/usr/bin/env bash
set -e
APP=/opt/maehong

echo "=== 1. gunicorn 설치 ==="
$APP/venv/bin/pip install --quiet gunicorn
$APP/venv/bin/gunicorn --version

echo "=== 2. systemd 유닛 갱신 (gunicorn) ==="
sudo tee /etc/systemd/system/maehong.service > /dev/null <<UNIT
[Unit]
Description=Maehong L&F Dashboard (gunicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=jgkim
WorkingDirectory=$APP
Environment=APP_BASE_DIR=$APP
Environment=DATA_DIR=$APP/data
Environment=PORT=8080
Environment=TZ=Asia/Seoul
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/venv/bin/gunicorn -w 1 --threads 8 -b 0.0.0.0:8080 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

echo "=== 3. 재시작 ==="
sudo systemctl daemon-reload
sudo systemctl restart maehong
echo "=== 4. 기동 대기 ==="
for i in $(seq 1 12); do
  c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 http://localhost:8080/login || echo 000)
  echo "try$i /login HTTP $c"
  [ "$c" = "200" ] && break
  sleep 5
done
echo "=== 5. 상태 ==="
systemctl is-active maehong
ps -o rss= -C gunicorn 2>/dev/null | awk '{s+=$1} END{printf "gunicorn RSS: %d MB\n", s/1024}'
sudo journalctl -u maehong --no-pager -n 5 | tail -5
