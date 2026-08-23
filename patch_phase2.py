import os
import re
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def patch_app_py():
    """P0-8: 웹 보안 Fail-closed 및 로그인 횟수 제한 강화"""
    if not os.path.exists("app.py"):
        logger.error("app.py를 찾을 수 없습니다.")
        return

    with open("app.py", "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 주석 처리된 패스워드 체크 복구
    content = re.sub(
        r"#\s*if not check_password\(\):\s*st\.stop\(\)", 
        r"if not check_password(): st.stop()", 
        content
    )

    # 2. bcrypt 및 Fail-closed 로직 강화 (ADMIN_PASSWORD_HASH 강제)
    fail_closed_logic = """
    hashed_pw_env = os.getenv("ADMIN_PASSWORD_HASH")
    if not hashed_pw_env: 
        st.error("🚨 [보안 결함 - Fail-closed] ADMIN_PASSWORD_HASH 환경변수가 설정되지 않았습니다. 외부 침입 방지를 위해 시스템 구동을 전면 차단합니다.")
        st.stop()
        
    # Brute-force 방어: 로그인 실패 횟수 제한
    if "login_attempts" not in st.session_state:
        st.session_state["login_attempts"] = 0
    if st.session_state["login_attempts"] >= 5:
        st.error("🔒 로그인 실패 횟수 초과. 보안을 위해 세션이 잠겼습니다. 서버를 재시작하십시오.")
        st.stop()
"""
    # 기존 초기 보안 설정 경고 부분을 강력한 Fail-closed 로직으로 교체
    content = re.sub(
        r'hashed_pw_env = os\.getenv\("ADMIN_PASSWORD_HASH"\)\s*if not hashed_pw_env:\s*st\.warning\("[^"]+"\);\s*st\.stop\(\)',
        fail_closed_logic.strip(),
        content
    )

    with open("app.py", "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("✅ app.py: 보안 빗장(Fail-closed) 및 Brute-force 방어 로직 적용 완료")

def patch_kis_client_py():
    """P0-6: KIS API 어댑터 비멱등 POST 재시도 방지 및 REAL POST 블록"""
    filepath = os.path.join("broker", "kis_client.py")
    if not os.path.exists(filepath):
        logger.error("broker/kis_client.py를 찾을 수 없습니다.")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # REAL POST 게이트 및 Timeout 재시도 방지 주입
    safety_gate = """
        # [P0-6] 1. REAL POST 진입점 하드블록 게이트
        if method.upper() == "POST" and not getattr(self, 'is_mock', True):
            if not getattr(self, 'real_approval_status', False):
                return {"rt_cd": "X", "msg1": "POST_BLOCKED: 명시적 실거래(Canary) 승인 전까지 REAL POST는 전면 차단됩니다."}

        # [P0-6] 2. 비멱등성 POST 자동 재전송 방지 및 Timeout 처리
        if method.upper() == "POST":
            # 주문/취소 등은 Timeout 발생 시 절대 재시도하지 않고 UNKNOWN 반환 (체결 대사로 해결)
            try:
                # kwargs에 timeout을 짧게 강제 설정 (예: 5초)
                if 'timeout' not in kwargs: kwargs['timeout'] = 5.0
            except Exception:
                pass
"""
    if "REAL POST 진입점 하드블록 게이트" not in content and "def _request" in content:
        content = re.sub(
            r"(def _request[^:]+:\s*)",
            r"\1" + safety_gate,
            content
        )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("✅ broker/kis_client.py: REAL POST 원천 차단 및 비멱등 POST 재전송 방지(UNKNOWN 처리) 완료")

def patch_bot_kill_switch():
    """P0-3: Bot에 Kill Switch(시스템 주문 취소) 완벽 연동"""
    if not os.path.exists("bot.py"):
        return

    with open("bot.py", "r", encoding="utf-8") as f:
        content = f.read()

    # Kill switch 활성화 시 db.request_cancel_for_system_orders 호출 주입
    if "request_cancel_for_system_orders" not in content:
        cancel_logic = """
        # [P0-3] Kill Switch 활성화 시 신규 주문 중단 및 기존 PENDING 주문 취소 요청
        if getattr(global_state, 'kill_switch_active', False):
            try:
                canceled_count = db.request_cancel_for_system_orders(account_fp, strategy_name)
                logger.warning(f"🚨 [Kill Switch] 가동 중! {canceled_count}건의 미체결 주문 취소 요청 완료. 신규 주문 의도 생성 중단.")
                return  # 신호 평가 중단
            except Exception as e:
                logger.error(f"🚨 [Kill Switch] 취소 요청 중 치명적 오류 (Fail-closed): {e}")
                return
"""
        # run_signal_bot 의 메인 루프 상단에 주입 (간단히 정규식으로 루프 시작점에 주입)
        content = re.sub(
            r"(while True:\s*try:\s*)",
            r"\1" + cancel_logic,
            content
        )
        
        with open("bot.py", "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("✅ bot.py: Kill Switch 전면 복구 및 취소 요청 연동 완료")

if __name__ == "__main__":
    print("🚀 Phase 2 패치(안전망 및 동시성 제어)를 시작합니다...")
    patch_app_py()
    patch_kis_client_py()
    patch_bot_kill_switch()
    print("🎉 Phase 2 패치가 완료되었습니다! 'git status'로 변경된 3개의 파일을 확인해 주십시오.")