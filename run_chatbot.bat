@echo off
chcp 65001 > nul
cd /d C:\Users\jgkim\maehong-JG

echo ================================================
echo   완제품 재고 챗봇 시작
echo ================================================
echo.

REM CSV 파일 최신화 (엑셀 → CSV 변환)
echo [1/4] 재고 엑셀 데이터를 CSV로 변환중...
python convert_excel.py
echo.

echo [2/4] 단가 엑셀 데이터를 CSV로 변환중...
python convert_price.py
echo.

echo [3/4] 부자재 규격 데이터를 CSV로 변환중...
python convert_spec.py
echo.

echo [4/5] 자사 부자재 재고 데이터를 CSV로 변환중...
python convert_jasa.py
echo.

REM Flask 서버 시작
echo [5/5] 챗봇 서버 시작중...
echo.
echo  브라우저에서 http://localhost:5000 으로 접속하세요
echo  종료하려면 Ctrl+C 를 누르세요
echo.
start "" "http://localhost:5000"
python app.py

pause
