import time
import logging
import uuid
import sys
import datetime
import traceback
import database as db
import broker.kis_client as kis

# --- 로깅 설정 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("worker_execution.log", encoding='utf-8')
    ]
)
logger = logging.getLogger("QuantWorker")

# --- 워커 고유 ID 발급 ---
WORKER_ID = f"worker_{uuid.uuid4().hex[:8]}"

def get_account_secrets(portfolio_id):
    """system_contract에 맞춰 secrets를 로드합니다 (실제 환경에서는 .env나 보안 볼륨 사용)"""
    import streamlit as st
    try:
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        config = st.secrets["kis_accounts"][acc_key]
        app_key = config["app_key"]
        app_sec = config["app_secret"]
        cano = str(config["cano"]).strip()
        is_mock = str(config.get("is_mock", "True")).lower() == 'true'
        acnt_prdt = str(config.get("acnt_prdt", "01")).strip()
        return app_key, app_sec, cano, acnt_prdt, is_mock
    except Exception as e:
        logger.error(f"계좌 정보 로드 실패 (portfolio: {portfolio_id}): {e}")
        return None, None, None, True, None

def run_worker_loop():
    logger.info(f"🚀 워커 봇 시작됨 (ID: {WORKER_ID}) - 16단계 상태머신 기반 KIS 001x 통신 대기 중...")
    
    # KIS 토큰 캐싱용
    kis_tokens = {}

    while True:
        try:
            # 1. 운용 중인 전략 순회 (CORE, SATELLITE)
            for portfolio_id in ["CORE", "SATELLITE"]:
                app_key, app_secret, cano, acnt_prdt, is_mock = get_account_secrets(portfolio_id)
                if not app_key:
                    continue
                
                env = "MOCK" if is_mock else "REAL"
                acc_fp = db.hashlib.sha256(cano.encode()).hexdigest()[:16] if cano != "MOCK_ACCOUNT" else "MOCK_ACCOUNT"
                
                # 2. 시스템 상태 점검 (Kill Switch, Auto Trade)
                sys_status = db.get_system_status("KIS", env, acc_fp, portfolio_id)
                if sys_status['kill_switch']:
                    # 킬스위치 켜지면 봇은 쉰다
                    continue
                    
                if not sys_status['auto_trade'] and env == "REAL":
                    # 실전인데 자동매매가 꺼져있으면 쉰다
                    continue

                # 3. Worker Lease 획득 (다중 워커 충돌 방지)
                lease_ok, token = db.acquire_worker_lease("KIS", env, acc_fp, portfolio_id, WORKER_ID, ttl=10)
                if not lease_ok:
                    continue # 다른 워커가 선점 중
                
                # 4. API Token 확인 및 갱신
                token_key = f"{env}_{portfolio_id}"
                if token_key not in kis_tokens or kis_tokens[token_key]['expire'] < time.time():
                    new_token, msg = kis.get_kis_access_token(app_key, app_secret, is_mock)
                    if new_token:
                        kis_tokens[token_key] = {'token': new_token, 'expire': time.time() + (3600 * 12)} # 대략 12시간 유효
                        logger.info(f"[{portfolio_id}] KIS API 토큰 갱신 완료")
                    else:
                        logger.error(f"[{portfolio_id}] 토큰 발급 실패: {msg}")
                        continue

                # 5. 주문 대기열(INTENT_CREATED) 확인 및 Claim (원자적)
                order = db.claim_next_order("KIS", env, acc_fp, portfolio_id, WORKER_ID, token)
                if not order:
                    continue # 처리할 주문 없음

                logger.info(f"[{portfolio_id}] 주문 획득 (ID: {order['id']}, {order['side']} {order['ticker']} {order['qty']}주)")

                # 6. SUBMITTING 전이
                db.transition_order_status(order['id'], 'CLAIMED', 'SUBMITTING')

                # 7. KIS 001x API 발송 (최신 KRX 공식 어댑터 사용)
                status, msg, odno, krx_odno, resp_code = kis.execute_kis_order_current_001x(
                    app_key, app_secret, cano, acnt_prdt, kis_tokens[token_key]['token'], 
                    order['ticker'], order['side'].upper() == "BUY", order['qty'], order['limit_price'], is_mock
                )

                # 8. 결과에 따른 상태 전이
                if status == "ACKNOWLEDGED":
                    logger.info(f"✅ 주문 접수 완료: {odno} ({msg})")
                    db.transition_order_status(order['id'], 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=krx_odno, code=resp_code)
                elif status == "REJECTED":
                    logger.warning(f"❌ 주문 거절됨: {msg}")
                    db.transition_order_status(order['id'], 'SUBMITTING', 'REJECTED', code=resp_code)
                else:
                    logger.error(f"⚠️ 주문 상태 알 수 없음 (UNKNOWN): {msg}")
                    db.transition_order_status(order['id'], 'SUBMITTING', 'UNKNOWN', code=resp_code)

            # 초당 호출 제한(Rate Limit) 방지 및 DB 부하 조절을 위한 기본 휴식
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"워커 루프 에러: {e}")
            logger.error(traceback.format_exc())
            time.sleep(5) # 크래시 방지용 휴식

if __name__ == "__main__":
    run_worker_loop()