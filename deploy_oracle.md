# Oracle Cloud Always Free 배포 (매홍 L&F 대시보드)

PC가 꺼져도 24시간 유지 + **월 $0**. 리눅스 ARM VM에 직접 올린다.
코드는 이미 클라우드 대응 완료(BASE_DIR/DATA_DIR/PORT 환경변수화).

---
## A. Oracle 계정 + VM 생성  (본인이 웹에서)

1. https://www.oracle.com/cloud/free 가입 (카드 인증 필요하나 **Always Free는 청구 안 됨**).
   - 리전은 **한국과 가까운 곳**(예: Japan East-Tokyo, South Korea-Chuncheon)이 빠름.
2. **인스턴스 생성** (Compute → Instances → Create):
   - 이미지: **Ubuntu 24.04 (Canonical)**
   - Shape: **Ampere ARM (VM.Standard.A1.Flex)** → OCPU **2**, RAM **8GB** (Always Free 범위)
     - "out of capacity" 뜨면 OCPU/RAM 줄이거나 다른 가용성 도메인/리전 재시도 (ARM은 자리 경쟁 있음)
   - SSH 키: 새 키 생성 → **private key 다운로드** (예: `C:\Users\jgkim\.ssh\oracle_maehong.key`)
3. **포트 개방** (네트워킹 → VCN → Security List → Default → Ingress Rules 추가):
   - Source `0.0.0.0/0`, TCP **80**
   - Source `0.0.0.0/0`, TCP **443**
4. 생성된 인스턴스의 **Public IP** 메모.

5. **무료 도메인** (HTTPS·구글로그인에 필요): https://www.duckdns.org 로그인 →
   서브도메인 만들고(예: `maehong`) **current ip 칸에 위 Public IP 입력 후 update**.
   → 도메인 = `maehong.duckdns.org`

> 여기까지 끝나면 알려주세요. B단계부터는 제가 본인 PC에서 SSH로 접속해 진행할 수 있습니다.

---
## B. 서버 셋업  (PC에서 SSH로 — 같이 진행)

```powershell
# (1) 권한 정리 후 접속 테스트
icacls C:\Users\jgkim\.ssh\oracle_maehong.key /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i C:\Users\jgkim\.ssh\oracle_maehong.key ubuntu@<PUBLIC_IP>
```

```powershell
# (2) 코드 업로드 (data/ 제외, 약 코드 몇 MB). PC 프로젝트 폴더에서:
scp -i C:\Users\jgkim\.ssh\oracle_maehong.key -r `
  app.py fetch_all.py fetch_bom.py fetch_monday.py fetch_monday_dashboard.py `
  _safe_csv.py requirements.txt deploy `
  ubuntu@<PUBLIC_IP>:/tmp/maehong_code
# 서버에서 /opt/maehong 으로 이동
ssh -i ...key ubuntu@<IP> "sudo mkdir -p /opt/maehong && sudo cp -r /tmp/maehong_code/* /opt/maehong/ && sudo chown -R ubuntu:ubuntu /opt/maehong"
```

```powershell
# (3) 시크릿 파일 업로드 (.env + firebase json)
scp -i ...key .env ubuntu@<IP>:/opt/maehong/.env
scp -i ...key maehong-scm-firebase-adminsdk-fbsvc-8cae1845b3.json ubuntu@<IP>:/opt/maehong/
```

```powershell
# (4) 최신 데이터 시드 (60MB)
python seed_latest.py
scp -i ...key _seed_data/* ubuntu@<IP>:/opt/maehong/data/
```

```bash
# (5) 서버에서 셋업 스크립트 실행
ssh -i ...key ubuntu@<IP>
# Windows에서 올린 파일의 CR 제거 (줄바꿈 호환)
sudo sed -i 's/\r$//' /opt/maehong/deploy/setup_oracle.sh /opt/maehong/deploy/maehong.service /opt/maehong/deploy/Caddyfile
sudo bash /opt/maehong/deploy/setup_oracle.sh
# Caddy 도메인 설정
sudo sed -i 's/YOUR_DOMAIN/maehong.duckdns.org/' /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

---
## C. 마무리

1. **Firebase 허용 도메인 추가**: Console → Authentication → Settings → 승인된 도메인 →
   `maehong.duckdns.org` 추가. (안 하면 구글 로그인 실패)
2. 접속 확인: `https://maehong.duckdns.org`
3. 수집 확인: `tail -f /var/log/maehong.log`
   → **아마란스 API가 클라우드에서 막히면** 수집 에러. 그 경우 "수집은 PC, 데이터만 서버로 동기화"로 전환.

## 운영 메모
- 앱 재시작: `sudo systemctl restart maehong` / 상태: `sudo systemctl status maehong`
- 로그: `tail -f /var/log/maehong.log`
- 코드 업데이트: 바뀐 파일 scp → `sudo systemctl restart maehong`
- 자사재고/재고일지(OneDrive)는 자동갱신 불가 → `/upload`로 수동 업로드
- DuckDNS IP는 고정 IP면 한 번만. (Oracle 인스턴스 IP는 기본 보존됨)
