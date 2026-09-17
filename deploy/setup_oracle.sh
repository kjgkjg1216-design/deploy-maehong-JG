#!/usr/bin/env bash
# Oracle Ubuntu VM 서버 초기 셋업 — VM에 코드(/opt/maehong)와 .env, firebase json 올린 뒤 실행.
#   sudo bash /opt/maehong/deploy/setup_oracle.sh
set -e

APP=/opt/maehong
echo "[1/6] 패키지 설치 (python, caddy)"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip debian-keyring debian-archive-keyring apt-transport-https curl

echo "[2/6] 파이썬 가상환경 + 의존성"
cd "$APP"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "[3/6] 데이터 디렉토리"
mkdir -p "$APP/data"
sudo touch /var/log/maehong.log

echo "[4/6] systemd 서비스 등록"
sudo cp "$APP/deploy/maehong.service" /etc/systemd/system/maehong.service
sudo systemctl daemon-reload
sudo systemctl enable maehong
sudo systemctl restart maehong

echo "[5/6] Caddy 설치 (자동 HTTPS)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update && sudo apt-get install -y caddy
fi
sudo cp "$APP/deploy/Caddyfile" /etc/caddy/Caddyfile
echo ">>> /etc/caddy/Caddyfile 의 YOUR_DOMAIN 을 실제 도메인으로 수정 후: sudo systemctl restart caddy"

echo "[6/6] 방화벽(우분투 내부) — Oracle 콘솔의 Security List도 별도 개방 필요"
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT || true
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
sudo netfilter-persistent save 2>/dev/null || true

echo
echo "[완료] 앱 상태: sudo systemctl status maehong --no-pager"
echo "       로그:    tail -f /var/log/maehong.log"
