# 매홍 대시보드 — 클라우드 데이터 자동 동기화 (업로드 전용 · 변경분만)
#   로컬 data/ 최신 CSV → (변경된 것만) VM 업로드 → 무중단 리로드
#   ※ 수집(fetch)은 로컬 대시보드 앱이 담당. 여기선 업로드만.
#   작업 스케줄러가 30분마다 호출. 수동 실행도 가능.
#
# 2026-10-14 수정:
#   ① 원격 명령(ssh/scp)마다 시간 제한 — GCP 응답이 멈추면 ssh가 몇 시간씩 남아 동기화를 20분씩 막던 문제
#   ② 원격 출력은 파일로 받아 UTF-8로 읽음 — 예약 작업 콘솔(cp949)에서 한글 파일명이 깨져
#      내용이 같은 파일도 매번 '변경'으로 판단해 거의 전 파일(51MB monday 포함)을 재업로드·리로드하던 문제
#   ③ 클라우드 파일 목록 조회에 실패하면 그 회차 업로드를 건너뜀(무작정 전체 재업로드 방지)
#   ④ 시작 시 예전 회차에서 멈춰 남은 ssh/scp·동기화 프로세스 정리
#   ⑤ 재고일지 내보내기 대기 60초 → 180초 (로컬 앱이 아마란스 수집 중이면 느림)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$PY  = "C:\Users\jgkim\AppData\Local\Programs\Python\Python312\python.exe"
$DIR = "C:\Users\jgkim\maehong-JG"
$KEY = "C:\Users\jgkim\.ssh\google_compute_engine"
$IP  = "8.235.41.127"
$URL = "https://8.235.41.127.sslip.io"
$LOG = "$DIR\update_cloud.log"
$LOCK = "$DIR\.update_cloud.lock"
$SSHOPT = "-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new"

function Log($m){ $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"; Add-Content -Path $LOG -Value $line -Encoding UTF8; Write-Host $line }

# 원격 명령 실행 (시간 제한 + 출력은 UTF-8 파일로). 반환 @{ ok; timeout; lines }
function Invoke-Remote([string]$Exe, [string]$ArgLine, [int]$TimeoutSec) {
  $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
  $res = @{ ok = $false; timeout = $false; lines = @() }
  try {
    $p = Start-Process -FilePath $Exe -ArgumentList $ArgLine -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $p.Handle   # PS 5.1: 핸들을 잡아 둬야 종료 코드가 남음
    if ($p.WaitForExit($TimeoutSec * 1000)) { $res.ok = ($p.ExitCode -eq 0) }
    else { try { $p.Kill() } catch {}; $res.timeout = $true }
    $res.lines = @(Get-Content $out -Encoding UTF8 -ErrorAction SilentlyContinue)
  } catch { Log "  $Exe 실행 오류: $($_.Exception.Message)" }
  finally { Remove-Item $out, $err -Force -ErrorAction SilentlyContinue }
  return $res
}

# ④ 예전 회차에서 멈춰 남은 프로세스 정리 (이 VM으로 가는 ssh/scp 10분 초과, 동기화 스크립트 25분 초과)
try {
  $now = Get-Date
  Get-CimInstance Win32_Process -Filter "Name='ssh.exe' OR Name='scp.exe'" |
    Where-Object { $_.CommandLine -like "*google_compute_engine*" -and $_.CommandLine -like "*$IP*" -and ($now - $_.CreationDate).TotalMinutes -gt 10 } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Log "  멈춘 $($_.Name) 정리 (PID $($_.ProcessId), $([int]($now - $_.CreationDate).TotalMinutes)분)" }
  Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*update_cloud.ps1*" -and $_.ProcessId -ne $PID -and ($now - $_.CreationDate).TotalMinutes -gt 25 } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Log "  멈춘 이전 동기화 정리 (PID $($_.ProcessId), $([int]($now - $_.CreationDate).TotalMinutes)분)" }
} catch { Log "  프로세스 정리 오류: $($_.Exception.Message)" }

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
    $ex = Invoke-WebRequest -Uri "http://localhost:5000/api/export_source_csv" -Method POST -TimeoutSec 180 -UseBasicParsing
    Log "  export(OneDrive→CSV): $($ex.Content)"
  } catch { Log "  export 실패(로컬앱 미가동?): $($_.Exception.Message)" }

  # ① 타입별 최신 CSV 추림 → _seed_data/
  & $PY "$DIR\seed_latest.py" *> "$DIR\_last_seed.log"

  $uploaded = 0

  # ② 클라우드 현재 파일 md5 조회 (변경분 판별용 — 크기 아닌 내용 비교)
  #    크기만 비교하면 monday처럼 "내용 바뀌었는데 바이트 수 같은" 드리프트를 놓침.
  $r = Invoke-Remote 'ssh' "-i `"$KEY`" $SSHOPT jgkim@$IP `"timeout 60 md5sum /opt/maehong/data/*.csv`"" 90
  if (-not $r.ok -or $r.lines.Count -eq 0) {
    Log "  ⚠ 클라우드 data 목록 조회 실패(시간 초과=$($r.timeout)) → 이번 회차 data 업로드 건너뜀"
  } else {
    $cloudMap = @{}
    foreach($line in $r.lines){ if($line -match '^([0-9a-f]{32})\s+\S*/([^/]+\.csv)$'){ $cloudMap[$Matches[2]] = $Matches[1] } }

    # ③ 내용이 다른/신규 파일만 업로드 (md5 비교)
    Get-ChildItem "$DIR\_seed_data" -File -Filter *.csv -ErrorAction SilentlyContinue | ForEach-Object {
      $cloudHash = $cloudMap[$_.Name]
      $localHash = (Get-FileHash -Path $_.FullName -Algorithm MD5).Hash.ToLower()
      if (($null -eq $cloudHash) -or ($cloudHash -ne $localHash)) {
        $u = Invoke-Remote 'scp' "-i `"$KEY`" $SSHOPT `"$($_.FullName)`" jgkim@${IP}:/opt/maehong/data/" 300
        if ($u.ok) { $uploaded++ } else { Log "  ⚠ 업로드 실패: $($_.Name) (시간 초과=$($u.timeout))" }
      }
    }
  }
  Log "  업로드 $uploaded 파일 (내용 변경분)"

  # ③-1 알림 기록(🔔 알림 센터) — 점검 엔진은 PC에서만 돌므로 기록을 GCP로 복사 (2026-10-23, GCP는 9/2 기록에 멈춰 있었음)
  #     앱이 요청 때마다 파일을 읽으므로 리로드 불필요
  $an = Invoke-Remote 'ssh' "-i `"$KEY`" $SSHOPT jgkim@$IP `"timeout 30 sh -c 'md5sum /opt/maehong/data/alerts_log.jsonl /opt/maehong/data/_alert_state.json 2>/dev/null; true'`"" 60
  if ($an.ok) {
    $am = @{}
    foreach($line in $an.lines){ if($line -match '^([0-9a-f]{32})\s+\S*/([^/]+)$'){ $am[$Matches[2]] = $Matches[1] } }
    $aup = 0
    foreach($n in @('alerts_log.jsonl', '_alert_state.json')) {
      $lf = "$DIR\data\$n"
      if (-not (Test-Path $lf)) { continue }
      if ($am[$n] -ne (Get-FileHash -Path $lf -Algorithm MD5).Hash.ToLower()) {
        $u = Invoke-Remote 'scp' "-i `"$KEY`" $SSHOPT `"$lf`" jgkim@${IP}:/opt/maehong/data/" 60
        if ($u.ok) { $aup++ } else { Log "  ⚠ 알림 기록 업로드 실패: $n" }
      }
    }
    if ($aup) { Log "  알림 기록 업로드 $aup 파일" }
  } else { Log "  ⚠ 알림 기록 확인 실패(시간 초과=$($an.timeout))" }

  # ③-2 프로젝트 루트의 수동 CSV(판매 CSV·SKU매핑)도 동기화 (2026-09-17: 예전엔 scp 수작업)
  #     → /opt/maehong/ 루트로. -p로 수정시각 보존(로더가 mtime 최신 판매 파일을 고르므로 순서가 바뀌면 안 됨)
  $r2 = Invoke-Remote 'ssh' "-i `"$KEY`" $SSHOPT jgkim@$IP `"timeout 60 md5sum /opt/maehong/*.csv`"" 90
  if (-not $r2.ok -or $r2.lines.Count -eq 0) {
    Log "  ⚠ 클라우드 루트 목록 조회 실패(시간 초과=$($r2.timeout)) → 루트 CSV 업로드 건너뜀"
  } else {
    $rootMap = @{}
    foreach($line in $r2.lines){ if($line -match '^([0-9a-f]{32})\s+\S*/([^/]+\.csv)$'){ $rootMap[$Matches[2]] = $Matches[1] } }
    $rootUp = 0
    Get-ChildItem $DIR -File -Filter *.csv -ErrorAction SilentlyContinue |
      Where-Object { ($_.Name -like '*판매수량*') -or ($_.Name -like '*공급가*') -or ($_.Name -eq 'SKU매핑_확정.csv') -or ($_.Name -eq '상품매입_업체조건.csv') } |
      ForEach-Object {
        $localHash = (Get-FileHash -Path $_.FullName -Algorithm MD5).Hash.ToLower()
        if (($null -eq $rootMap[$_.Name]) -or ($rootMap[$_.Name] -ne $localHash)) {
          $u = Invoke-Remote 'scp' "-p -i `"$KEY`" $SSHOPT `"$($_.FullName)`" jgkim@${IP}:/opt/maehong/" 120
          if ($u.ok) { $rootUp++; Log "    루트 CSV 업로드: $($_.Name)" } else { Log "  ⚠ 루트 CSV 업로드 실패: $($_.Name) (시간 초과=$($u.timeout))" }
        }
      }
    if ($rootUp -gt 0) { $uploaded += $rootUp }
  }

  # ④ 업로드 있었으면 무중단 리로드
  if ($uploaded -gt 0) {
    $tok = (Get-Content "$DIR\.reload_token" -Raw).Trim()
    try {
      $r = Invoke-WebRequest -Uri "$URL/api/reload_dfs?token=$tok" -Method POST -TimeoutSec 180 -UseBasicParsing
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
  $pr = Invoke-Remote 'ssh' "-i `"$KEY`" $SSHOPT jgkim@$IP `"timeout 60 /opt/maehong/prune_data.sh $KEEP`"" 90
  if ($pr.ok) { Log "  VM data 정리 요청(최신 $KEEP 유지)" } else { Log "  ⚠ VM 정리 실패(시간 초과=$($pr.timeout))" }

  Log "===== 동기화 완료 ====="
}
finally {
  Remove-Item $LOCK -Force -ErrorAction SilentlyContinue
}
