@echo off
chcp 65001 > nul
title Core-Satellite Quant System
color 0A

echo ========================================================
echo      🚀 Core-Satellite Quant System 가동을 시작합니다.
echo ========================================================
echo.

:: 가상환경 활성화 (가상환경 폴더명이 venv인 경우. 다르면 수정 필요)
if exist "venv\Scripts\activate.bat" (
    echo [INFO] 파이썬 가상환경(venv)을 활성화합니다...
    call venv\Scripts\activate.bat
) else (
    echo [WARN] 가상환경(venv) 폴더를 찾을 수 없습니다. 전역 파이썬을 사용합니다.
)

:: 1. Signal Bot 백그라운드 실행
echo [1/3] Signal Bot (시장 감시 및 시그널 생성) 구동 중...
start "Signal Bot" cmd /c "title [Quant] Signal Bot & python bot.py & pause"

:: 2. Exec Worker 백그라운드 실행
echo [2/3] Exec Worker (주문 체결 및 KIS 통신) 구동 중...
start "Exec Worker" cmd /c "title [Quant] Exec Worker & python worker.py & pause"

:: 3. Streamlit 대시보드 실행
echo [3/3] Streamlit 대시보드 (UI) 구동 중...
echo.
echo 시스템이 성공적으로 백그라운드에서 가동되었습니다!
echo 곧 브라우저 창이 열리며 대시보드가 표시됩니다.
echo.
echo ※ 봇이나 워커를 종료하려면 열려있는 검은색 터미널 창을 닫으시면 됩니다.
echo ========================================================

streamlit run app.py

pause