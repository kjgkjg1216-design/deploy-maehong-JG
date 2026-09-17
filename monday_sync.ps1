# Monday 변경 감지 → 즉시 수집·동기화 (웹훅 근실시간 반영)
#   작업 스케줄러가 1분마다 호출. dirty면 로컬앱 monday 재수집 → 클라우드 동기화.
$ErrorActionPreference = 'Continue'
$DIR  = "C:\Users\jgkim\maehong-JG"
$URL  = "https://8.235.41.127.sslip.io"
$LOG  = "$DIR\monday_sync.log"
$LOCK = "$DIR\.monday_sync.lock"
$tok  = (Get-Content "$DIR\.reload_token" -Raw).Trim()
function Log($m){ Add-Content -Path $LOG -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" -Encoding UTF8 }

# dirty 확인 (변경 없으면 즉시 종료 — 가벼움)
try {
  $st = (Invoke-WebRequest "$URL/api/monday_dirty?token=$tok" -UseBasicParsing -TimeoutSec 20).Content | ConvertFrom-Json
} catch { exit 0 }
if (-not $st.dirty) { exit 0 }

# 중복 실행 방지
if (Test-Path $LOCK) {
  $age = (Get-Date) - (Get-Item $LOCK).LastWriteTime
  if ($age.TotalMinutes -lt 15) { exit 0 }
}
New-Item -ItemType File -Path $LOCK -Force | Out-Null

try {
  Log "===== 변경 감지 → 수집 시작 ====="
  # 먼저 dirty 클리어 (수집 중 또 바뀌면 다음 사이클에 재처리)
  try { Invoke-WebRequest "$URL/api/monday_dirty/clear?token=$tok" -Method POST -UseBasicParsing -TimeoutSec 20 | Out-Null } catch {}

  # 로컬 앱에 monday 재수집 요청 (앱 내부 락으로 중복 방지)
  try { Invoke-WebRequest "http://localhost:5000/api/refresh_monday" -Method POST -UseBasicParsing -TimeoutSec 30 | Out-Null }
  catch { Log "refresh_monday 호출 실패: $($_.Exception.Message)" }

  # 수집 완료 대기 (최대 6분)
  for ($i = 0; $i -lt 72; $i++) {
    Start-Sleep -Seconds 5
    try { $s = (Invoke-WebRequest "http://localhost:5000/api/refresh_monday/status" -UseBasicParsing -TimeoutSec 15).Content | ConvertFrom-Json } catch { continue }
    if (-not $s.running) { break }
  }
  Log "수집 완료 → 클라우드 동기화 대기"

  # 30분 정기 동기화와 충돌 방지 — update_cloud 락이 비워질 때까지 대기(최대 4분)
  for ($i = 0; $i -lt 24; $i++) {
    if (-not (Test-Path "$DIR\.update_cloud.lock")) { break }
    Start-Sleep -Seconds 10
  }
  # 클라우드 동기화 — 같은 프로세스에서 실행(별도 창 안 뜸)
  & "$DIR\update_cloud.ps1"
  Log "===== 동기화 완료 ====="
}
finally {
  Remove-Item $LOCK -Force -ErrorAction SilentlyContinue
}



