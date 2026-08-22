@echo off
echo Starting Core-Satellite Quant System...

:: 1. 봇 실행 (에러 발생 시에도 창이 안 꺼지도록 cmd /k 사용)
start "Signal Bot" cmd /k "python bot.py"

:: 2. 워커 실행
start "Exec Worker" cmd /k "python worker.py"

:: 3. 대시보드 실행
echo Starting Dashboard...
streamlit run app.py

pause