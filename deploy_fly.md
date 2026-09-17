# Fly.io 배포 절차 (매홍 L&F 대시보드)

PC가 꺼져도 24시간 유지되도록 Fly.io 컨테이너로 배포한다.
코드는 이미 클라우드 대응 완료(BASE_DIR/DATA_DIR/PORT/Firebase 환경변수화).

## 0. 사전 준비 (1회)
- Fly 계정 생성: https://fly.io  (신용카드 등록 필요 — 소액 종량제)
- flyctl 설치 (PowerShell):
  ```powershell
  pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
  ```
  설치 후 새 터미널에서 `fly version` 확인.
- 로그인: `fly auth login`  (브라우저 인증)

## 1. 앱 생성 (배포는 아직)
프로젝트 폴더(C:/Users/jgkim/maehong-JG)에서:
```powershell
fly launch --no-deploy --copy-config --name maehong-dashboard --region nrt
```
- 이름(maehong-dashboard)이 이미 쓰이면 다른 이름으로. (fly.toml의 app 값도 함께 변경)
- 기존 fly.toml/Dockerfile을 그대로 쓰겠냐고 물으면 Yes.

## 2. 데이터 볼륨 생성
```powershell
fly volumes create maehong_data --region nrt --size 3
```
(3GB. fly.toml의 [[mounts]] source 이름과 동일해야 함)

## 3. 시크릿 등록 (.env + Firebase 키)
```powershell
# .env 값들
fly secrets set OPENAI_API_KEY="..." AMARANTH_ACCESS_TOKEN="..." AMARANTH_HASH_KEY="..." AMARANTH_GROUP_SEQ="gcmsAmaranth38463" AMARANTH_CALLER_NAME="API_gcmsAmaranth38463" AMARANTH_CO_CD="1000" MONDAY_API_KEy="..."

# Firebase 서비스계정 JSON 전체를 한 줄로 주입
fly secrets set FIREBASE_SERVICE_ACCOUNT="$(Get-Content maehong-scm-firebase-adminsdk-fbsvc-8cae1845b3.json -Raw)"
```
(NGROK_AUTHTOKEN은 Fly에선 불필요)

## 4. 배포
```powershell
fly deploy
```
빌드(원격) → 컨테이너 기동. 끝나면 URL: https://<app이름>.fly.dev

## 5. 데이터 시드 (첫 배포 후 1회)
앱은 빈 볼륨으로 시작하므로 최신 CSV를 올린다.
```powershell
python seed_latest.py          # _seed_data/ 에 최신 CSV 모음(약 60MB) 생성
fly ssh sftp shell
  # sftp 프롬프트에서:
  put _seed_data/20260618_monday.csv /data/20260618_monday.csv
  put _seed_data/20260618_발주정보.csv /data/20260618_발주정보.csv
  ... (_seed_data 안의 모든 파일 반복)
  quit
fly apps restart maehong-dashboard      # 메모리에 로드
```
또는 헤더의 "메모리 리로드" 버튼 / `/api/reload_dfs` 호출.

## 6. Firebase 로그인 허용 도메인 추가
Firebase Console → Authentication → Settings → 승인된 도메인 →
`<app이름>.fly.dev` 추가. (안 하면 구글 로그인 실패)

## 7. 확인
- https://<app이름>.fly.dev 접속 → 대시보드 표시, 구글 로그인 동작
- `fly logs` 로 아마란스/Monday 수집 로그 확인
  → **아마란스 API가 클라우드에서 막히면** 수집 0건/에러. 이 경우 대안:
    PC에서 fetch만 계속 돌리고, 생성된 CSV를 주기적으로 Fly 볼륨에 동기화.

## 운영 메모
- 자동 수집: Monday 30분, 아마란스 60분 (앱 내장 스케줄러, 항상 가동이라 동작)
- 자사재고/재고일지: OneDrive 의존 → 클라우드 자동갱신 불가. /upload 로 수동 업로드.
- 비용 절감: fly.toml memory 2gb→1gb 가능(모니터링 후). `fly scale memory 1024`
- 로그: `fly logs` / 상태: `fly status` / 접속: `fly ssh console`
