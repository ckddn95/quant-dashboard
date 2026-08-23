import os
import sys
import subprocess
import time

def main():
    print("===================================================")
    print("   Institutional Quant System - Python Launcher")
    print("===================================================")

    # 1. 필수 파일 검사
    required_files = ["app.py", "bot.py", "worker.py"]
    for f in required_files:
        if not os.path.exists(f):
            print(f"❌ [에러] {f} 파일을 찾을 수 없습니다!")
            input("종료하려면 엔터를 누르세요...")
            sys.exit(1)

    # 2. 파이썬 실행 경로 (현재 실행 중인 파이썬 사용)
    python_exe = sys.executable
    print(f"✅ 사용 중인 파이썬 경로: {python_exe}")

    # 3. 프로세스 3개 독립 실행 (새 창으로 띄우기)
    try:
        print("▶️ 1. 대시보드 (app.py) 시작 중...")
        subprocess.Popen(f'start "Quant Dashboard" cmd /k "{python_exe} -m streamlit run app.py"', shell=True)
        time.sleep(1)

        print("▶️ 2. 주문 실행 워커 (worker.py) 시작 중...")
        subprocess.Popen(f'start "Quant Execution Worker" cmd /k "{python_exe} worker.py"', shell=True)
        time.sleep(1)

        print("▶️ 3. 시그널 봇 (bot.py) 시작 중...")
        subprocess.Popen(f'start "Quant Signal Bot" cmd /k "{python_exe} bot.py"', shell=True)
        
        print("\n✅ 모든 시스템이 성공적으로 가동되었습니다!")
        print("각 프로세스의 검은색 터미널 창을 확인해 주십시오.")
        
    except Exception as e:
        print(f"❌ 실행 중 오류 발생: {e}")
        
    input("\n[런처 종료] 이 창은 엔터를 눌러 닫으셔도 시스템은 계속 켜져 있습니다...")

if __name__ == "__main__":
    main()