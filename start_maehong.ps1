# 매홍 대시보드 런처 (Flask only) — Start-Process 직접 실행(안정적)
# ngrok 외부터널 제거됨(2026-06-29): 운영은 호스트+GCP VM만. 옛 ngrok 사이트 차단.
# 작업 스케줄러(로그인 시) 또는 수동 실행용. 백틱 줄바꿈 미사용(파싱 안정).
$ErrorActionPreference = 'SilentlyContinue'
$root = 'C:\Users\jgkim\maehong-JG'
$py   = 'C:\Users\jgkim\AppData\Local\Programs\Python\Python312\python.exe'

function Test-PortUp($port) { return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) }

function Test-AppRunning { return [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*app.py*' }) }

# Flask: 5000 포트가 안 떠 있고 기동 중인 프로세스도 없을 때만 (기동에 몇 분 걸릴 수 있어 포트만 보면 중복 실행됨)
# -u, faulthandler, 직전 로그 보관, flask_restart.log = 조용히 꺼졌을 때 원인 추적용 (2026-11-09)
if (-not (Test-PortUp 5000) -and -not (Test-AppRunning)) {
  Copy-Item "$root\flask.log" "$root\flask.prev.log" -Force; Copy-Item "$root\flask.err.log" "$root\flask.err.prev.log" -Force
  Add-Content "$root\flask_restart.log" "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') start"
  Start-Process -FilePath $py -ArgumentList '-u','-X','faulthandler','app.py' -WorkingDirectory $root -RedirectStandardOutput "$root\flask.log" -RedirectStandardError "$root\flask.err.log" -WindowStyle Hidden
}

# Flask 기동(포트 LISTENING) 대기 — 최대 40초
for ($i = 0; $i -lt 20; $i++) { if (Test-PortUp 5000) { break }; Start-Sleep -Seconds 2 }

# ngrok 외부터널 비활성화 — 외부 접속은 GCP VM(https://8.235.41.127.sslip.io)으로만.

exit 0
