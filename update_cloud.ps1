# 매홍 대시보드 — 클라우드 데이터 자동 동기화 (업로드 전용 · 변경분만)
#   로컬 data/ 최신 CSV → (변경된 것만) VM 업로드 → 무중단 리로드
#   ※ 수집(fetch)은 로컬 대시보드 앱이 담당. 여기선 업로드만.
#   작업 스케줄러가 30분마다 호출. 수동 실행도 가능.

$ErrorActionPreference = 'Continue'
$PY  = "C:\Users\jgkim\AppData\Local\Programs\Python\Python312\python.exe"
$DIR = "C:\Users\jgkim\maehong-JG"
$KEY = "C:\Users\jgkim\.ssh\google_compute_engine"
$IP  = "8.235.41.127"
$URL = "https://8.235.41.127.sslip.io"
$LOG = "$DIR\update_cloud.log"
$LOCK = "$DIR\.update_cloud.lock"

function Log($m){ $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"; Add-Content -Path $LOG -Value $line -Encoding UTF8; Write-Host $line }

if (Test-Path $LOCK) {
  $age = (Get-Date) - (Get-Item $LOCK).LastWriteTime
  if ($age.TotalMinutes -lt 20) { $mins = [int]$age.TotalMinutes; Log "이전 실행 진행 중($mins 분) → 건너뜀"; exit 0 }
}
New-Item -ItemType File -Path $LOCK -Force | Out-Null

try {
  Log "===== 동기화 시작 ====="
  Set-Location $DIR

  # ⓪ OneDrive 기반 재고일지·자사재고를 오늘자 CSV로 내보내기 (로컬 앱 메모리 → CSV)
  try {
    $ex = Invoke-WebRequest -Uri "http://localhost:5000/api/export_source_csv" -Method POST -TimeoutSec 60 -UseBasicParsing
    Log "  export(OneDrive→CSV): $($ex.Content)"
  } catch { Log "  export 실패(로컬앱 미가동?): $($_.Exception.Message)" }

  # ① 타입별 최신 CSV 추림 → _seed_data/
  & $PY "$DIR\seed_latest.py" *> "$DIR\_last_seed.log"

  # ② 클라우드 현재 파일 md5 조회 (변경분 판별용 — 크기 아닌 내용 비교)
  #    크기만 비교하면 monday처럼 "내용 바뀌었는데 바이트 수 같은" 드리프트를 놓침.
  $cloudMap = @{}
  $cloudHashes = & ssh -i $KEY -o StrictHostKeyChecking=accept-new "jgkim@${IP}" "md5sum /opt/maehong/data/*.csv" 2>$null
  foreach($line in $cloudHashes){ if($line -match '^([0-9a-f]{32})\s+\S*/([^/]+\.csv)$'){ $cloudMap[$Matches[2]] = $Matches[1] } }

  # ③ 내용이 다른/신규 파일만 업로드 (md5 비교)
  $uploaded = 0
  Get-ChildItem "$DIR\_seed_data" -File -Filter *.csv -ErrorAction SilentlyContinue | ForEach-Object {
    $cloudHash = $cloudMap[$_.Name]
    $localHash = (Get-FileHash -Path $_.FullName -Algorithm MD5).Hash.ToLower()
    $needUpload = ($null -eq $cloudHash) -or ($cloudHash -ne $localHash)
    if ($needUpload) {
      & scp -i $KEY -o StrictHostKeyChecking=accept-new $_.FullName "jgkim@${IP}:/opt/maehong/data/" 2>$null
      if ($LASTEXITCODE -eq 0) { $uploaded++ }
    }
  }
  Log "  업로드 $uploaded 파일 (내용 변경분)"

  # ③-2 프로젝트 루트의 수동 CSV(판매 CSV·SKU매핑)도 동기화 (2026-09-17: 예전엔 scp 수작업)
  #     → /opt/maehong/ 루트로. -p로 수정시각 보존(로더가 mtime 최신 판매 파일을 고르므로 순서가 바뀌면 안 됨)
  $rootMap = @{}
  $rootHashes = & ssh -i $KEY -o StrictHostKeyChecking=accept-new "jgkim@${IP}" "md5sum /opt/maehong/*.csv" 2>$null
  foreach($line in $rootHashes){ if($line -match '^([0-9a-f]{32})\s+\S*/([^/]+\.csv)$'){ $rootMap[$Matches[2]] = $Matches[1] } }
  $rootUp = 0
  Get-ChildItem $DIR -File -Filter *.csv -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like '*판매수량*') -or ($_.Name -like '*공급가*') -or ($_.Name -eq 'SKU매핑_확정.csv') -or ($_.Name -eq '상품매입_업체조건.csv') } |
    ForEach-Object {
      $localHash = (Get-FileHash -Path $_.FullName -Algorithm MD5).Hash.ToLower()
      if (($null -eq $rootMap[$_.Name]) -or ($rootMap[$_.Name] -ne $localHash)) {
        & scp -p -i $KEY -o StrictHostKeyChecking=accept-new $_.FullName "jgkim@${IP}:/opt/maehong/" 2>$null
        if ($LASTEXITCODE -eq 0) { $rootUp++; Log "    루트 CSV 업로드: $($_.Name)" }
      }
    }
  if ($rootUp -gt 0) { $uploaded += $rootUp }

  # ④ 업로드 있었으면 무중단 리로드
  if ($uploaded -gt 0) {
    $tok = (Get-Content "$DIR\.reload_token" -Raw).Trim()
    try {
      $r = Invoke-WebRequest -Uri "$URL/api/reload_dfs?token=$tok" -Method POST -TimeoutSec 120 -UseBasicParsing
      Log "  리로드 완료: $($r.Content)"
    } catch { Log "  리로드 실패: $($_.Exception.Message)" }
  } else {
    Log "  변경 없음 → 리로드 생략"
  }

  # ⑤ 오래된 날짜 CSV 정리 (타입별 최신 KEEP개만 유지) — 로컬 + VM
  $KEEP = 3
  try {
    $before = (Get-ChildItem "$DIR\data" -Filter '*.csv').Count
    Get-ChildItem "$DIR\data" -Filter '*.csv' |
      Where-Object { $_.Name -match '^\d{8}_(.+\.csv)$' } |
      Group-Object { [regex]::Match($_.Name, '^\d{8}_(.+\.csv)$').Groups[1].Value } |
      ForEach-Object {
        $_.Group | Sort-Object Name -Descending | Select-Object -Skip $KEEP |
          Remove-Item -Force -ErrorAction SilentlyContinue
      }
    $after = (Get-ChildItem "$DIR\data" -Filter '*.csv').Count
    Log "  로컬 data 정리: $before → $after (타입별 최신 $KEEP 유지)"
  } catch { Log "  로컬 정리 실패: $($_.Exception.Message)" }
  try {
    & ssh -i $KEY -o StrictHostKeyChecking=accept-new "jgkim@${IP}" "/opt/maehong/prune_data.sh $KEEP" 2>$null
    Log "  VM data 정리 요청(최신 $KEEP 유지)"
  } catch { Log "  VM 정리 실패: $($_.Exception.Message)" }

  Log "===== 동기화 완료 ====="
}
finally {
  Remove-Item $LOCK -Force -ErrorAction SilentlyContinue
}


