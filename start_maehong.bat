@echo off
rem ── 매홍 대시보드 자동 가동 (Flask + ngrok) ──
cd /d C:\Users\jgkim\maehong-JG

rem 이미 5000 포트가 떠 있으면 Flask 중복 실행 방지
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto :end

start "Maehong-Flask" /min cmd /c ""C:\Users\jgkim\AppData\Local\Programs\Python\Python312\python.exe" app.py > flask.log 2>&1"

rem ngrok 외부터널 제거됨(2026-06-29): 외부접속은 GCP VM만. 옛 ngrok 사이트 차단.

:end
exit
