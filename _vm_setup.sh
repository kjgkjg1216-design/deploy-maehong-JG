#!/usr/bin/env bash
set -e

APP=/opt/maehong

echo "=== 1. extract code ==="
sudo mkdir -p $APP
sudo chown jgkim:jgkim $APP
tar -xzf /tmp/maehong_deploy.tar.gz -C $APP
mkdir -p $APP/data

echo "=== 2. seed data ==="
if [ -d /tmp/seed ]; then
  cp -f /tmp/seed/* $APP/data/ 2>/dev/null || true
fi
ls -1 $APP/data | wc -l
echo "data files above"

echo "=== 3. venv + pip (takes a few min) ==="
python3 -m venv $APP/venv
$APP/venv/bin/pip install --upgrade pip wheel -q
$APP/venv/bin/pip install -r $APP/requirements.txt > /tmp/pip.log 2>&1
echo "PIP rc=$?"
tail -4 /tmp/pip.log

echo "=== 4. import check ==="
$APP/venv/bin/python -c "import pandas,flask,openai,firebase_admin; print('imports OK pandas', pandas.__version__)"

echo "=== 5. systemd service ==="
sudo tee /etc/systemd/system/maehong.service > /dev/null <<UNIT
[Unit]
Description=Maehong L&F Dashboard
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
ExecStart=$APP/venv/bin/python $APP/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable maehong
sudo systemctl restart maehong
echo "=== 6. wait 12s then status ==="
sleep 12
sudo systemctl --no-pager status maehong | head -12
echo "=== recent logs ==="
sudo journalctl -u maehong --no-pager -n 20
