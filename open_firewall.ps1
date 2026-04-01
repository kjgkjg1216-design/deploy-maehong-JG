# 포트 5000 방화벽 인바운드 규칙 추가
New-NetFirewallRule -DisplayName "Flask 챗봇 5000" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow -Profile Any -Description "완제품 재고 챗봇 서버 포트 5000 허용"
Write-Host "방화벽 규칙 추가 완료: 포트 5000 인바운드 허용" -ForegroundColor Green
Write-Host "팀원이 http://192.168.0.5:5000 으로 접속 가능합니다."
pause
