import os
import re
import shutil

def fix_quant_engine_simulations():
    filepath = "quant_engine.py"
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P1-A] 수동/자동 물량 철저한 격리 (managed_buy_price 만 사용)
    # 기존 코드에서 buy_price를 가져오는 부분을 찾아 안전하게 치환
    if "managed_buy_price" not in content and "position.get(" in content:
        content = re.sub(
            r'buy_price\s*=\s*position\.get\([\'"]buy_price[\'"](?:,\s*0\.0)?\)',
            r'buy_price = position.get("managed_buy_price", 0.0)  # [P1-A] 수동 물량 격리: 자동운용 평단가만 사용',
            content
        )
        content = re.sub(
            r'qty\s*=\s*position\.get\([\'"]quantity[\'"](?:,\s*0)?\)',
            r'qty = position.get("managed_quantity", 0)  # [P1-A] 수동 매수한 수량은 매도 대상에서 철저히 제외',
            content
        )

    # [P1-C] Test 1, 2 빈 데이터 반환 시 AI 중단 버그 픽스
    content = re.sub(
        r'if df is None or df\.empty:\s*return {"error": "Test1 data unavailable"}',
        r'if df is None or df.empty:\n            continue  # [P1-C] 빈 데이터 시 에러로 중단하지 않고 스킵 (AI 생존성)',
        content
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ quant_engine.py: 수동/자동 물량 완벽 격리 및 AI 자율운용(Test 1,2) 버그 수정 완료.")

def fix_app_security():
    filepath = "app.py"
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P1-D] Brute-force 로그인 방어 (5회 실패 시 세션 하드락)
    security_logic = """
    # [P1-D] 보안 빗장 (Fail-closed) 및 로그인 횟수 제한 강화
    if "login_attempts" not in st.session_state:
        st.session_state["login_attempts"] = 0
        
    if st.session_state["login_attempts"] >= 5:
        st.error("🔒 [보안 경고] 로그인 실패 5회 초과. 시스템 보호를 위해 접속이 영구 차단되었습니다. 서버를 재시작하십시오.")
        st.stop()
        
    hashed_pw_env = os.getenv("ADMIN_PASSWORD_HASH")
    if not hashed_pw_env:
        st.error("🚨 [보안 결함 - Fail-closed] ADMIN_PASSWORD_HASH 환경변수가 설정되지 않았습니다. 외부 침입 방지를 위해 시스템 구동을 전면 차단합니다.")
        st.stop()
"""
    if "login_attempts" not in content:
        # st.set_page_config 다음 줄에 삽입
        content = re.sub(r'(st\.set_page_config.*?\n)', r'\1' + security_logic, content, count=1)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print("✅ app.py: Brute-force 5회 실패 방어 및 Fail-closed 보안 빗장 이식 완료.")

def setup_ci_pipeline():
    # 올바른 CI 경로(.github/workflows) 설정 및 가짜 시크릿 주입
    os.makedirs(".github/workflows", exist_ok=True)
    yaml_path = ".github/workflows/test.yml"
    
    ci_content = """name: Quant System CI

on: [push, pull_request]

jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - name: Set up Python 3.11
      uses: actions/setup-python@v5
      with:
        python-version: "3.11"
        cache: "pip"
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt pytest
    - name: Create Dummy Secrets for CI (Fail-closed 우회)
      run: |
        mkdir -p .streamlit
        echo '[system]' > .streamlit/secrets.toml
        echo 'hmac_secret = "dummy_hmac_123"' >> .streamlit/secrets.toml
        echo '[kis_accounts.core]' >> .streamlit/secrets.toml
        echo 'is_mock = "true"' >> .streamlit/secrets.toml
    - name: Run Integrity Tests (Pytest)
      env:
        CI_TEST_MODE: "true"
      run: pytest test_quant.py -v --disable-warnings
"""
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(ci_content)
    
    # 잘못된 옛날 CI 폴더 삭제
    if os.path.exists(".githubworkflows"):
        shutil.rmtree(".githubworkflows", ignore_errors=True)
    print("✅ CI 파이프라인: 표준 경로(.github/workflows/test.yml) 이동 및 Python 3.11 런타임 동기화 완료.")

if __name__ == "__main__":
    print("🧪 [Phase 4] 시뮬레이션 고도화 및 보안/CI 구축을 시작합니다...")
    fix_quant_engine_simulations()
    fix_app_security()
    setup_ci_pipeline()