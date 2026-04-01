@echo off
echo ================================================
echo   포트 5000 방화벽 허용 (관리자 권한 필요)
echo ================================================
echo.

netsh advfirewall firewall add rule name="Flask 챗봇 5000" dir=in action=allow protocol=TCP localport=5000

if %errorlevel% == 0 (
    echo.
    echo [성공] 포트 5000 방화벽 허용 완료!
    echo 팀원들이 아래 주소로 접속할 수 있습니다:
    echo   http://192.168.0.51:5000
) else (
    echo.
    echo [실패] 관리자 권한으로 다시 실행해주세요.
    echo 이 파일을 우클릭 ^> "관리자 권한으로 실행" 하세요.
)

echo.
pause
