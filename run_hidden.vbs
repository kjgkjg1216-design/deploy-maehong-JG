' 인자로 받은 PowerShell 스크립트를 '창 없이' 실행 (작업 스케줄러 깜빡임 방지)
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & WScript.Arguments(0) & """", 0, False
