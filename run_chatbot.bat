@echo off
chcp 65001 > nul
cd /d C:\Users\jgkim\maehong-JG

echo ================================================
echo   매홍 L&F 통합 재고 관리 챗봇 시작
echo ================================================
echo.

REM 기존 프로세스 종료
taskkill /F /IM ngrok.exe /T >nul 2>&1

REM CSV 파일 최신화 (엑셀 → CSV 변환)
echo [1/5] 재고 엑셀 데이터를 CSV로 변환중...
python convert_excel.py
echo.

echo [2/5] 단가 엑셀 데이터를 CSV로 변환중...
python convert_price.py
echo.

echo [3/5] 부자재 규격 데이터를 CSV로 변환중...
python convert_spec.py
echo.

echo [4/5] 자사 부자재 재고 데이터를 CSV로 변환중...
python convert_jasa.py
echo.

REM ngrok 외부 접속 터널 시작 (고정 도메인)
echo [5/5] 외부 접속 터널 시작중...
start /B ngrok http 5000 --url fashionable-failingly-tammie.ngrok-free.dev
echo.

REM Flask 서버 시작
echo ================================================
echo   서버 시작 완료!
echo.
echo   사내: http://localhost:5000
echo   외부: https://fashionable-failingly-tammie.ngrok-free.dev
echo   관리자: https://fashionable-failingly-tammie.ngrok-free.dev/admin
echo.
echo   종료하려면 Ctrl+C 를 누르세요
echo ================================================
echo.
start "" "http://localhost:5000"
python app.py

pause
