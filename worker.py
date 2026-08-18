import time
import logging
import uuid
import sys
import datetime
import traceback
import database as db
import broker.kis_client as kis

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ExecWorker")

WORKER_ID = f"worker_{uuid.uuid4().hex[:8]}"

def get_account_secrets(portfolio_id):
    import streamlit as st
    try:
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        config = st.secrets["kis_accounts"][acc_key]
        return config["app_key"], config["app_secret"], str(config["cano"]).strip(), str(config.get("is_mock", "True")).lower() == 'true', str(config.get("acnt_prdt", "01")).strip()
    except Exception: return None, None, None, True, "01"

def reconcile_executions(app_key, app_sec, cano, acnt_prdt, token, env, acc_fp, portfolio_id, is_mock):
    """✅ 0081R 기반 체결 대사 (Delta 반영)"""
    open_statuses = ['CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED']
    pending_orders = db.get_orders_by_status_and_env(open_statuses, "KIS", env, acc_fp, portfolio_id)
    if not pending_orders: return
    
    executions = kis.fetch_daily_executions(app_key, app_sec, cano, acnt_prdt, token, is_mock)
    
    for order in pending_orders:
        if not order['broker_order_id']: continue
        
        # ODNO(주문번호)가 일치하는 체결 내역 필터링
        matched_execs = [e for e in executions if e.get('odno') == order['broker_order_id']]
        if not matched_execs: continue
        
        # 증권사 누적 체결 수량 및 단가 산출
        broker_cum_qty = sum(int(e.get('ccld_qty', 0)) for e in matched_execs)
        if broker_cum_qty > order['cum_filled_qty']:
            broker_cum_amt = sum(int(e.get('ccld_qty', 0)) * float(e.get('ccld_unpr', 0)) for e in matched_execs)
            broker_avg_price = broker_cum_amt / broker_cum_qty if broker_cum_qty > 0 else 0
            
            # Delta 원자적 갱신
            db.apply_fill_delta_exactly_once(
                order['id'], order['ticker'], order['side'], "KIS", env, acc_fp, portfolio_id, 
                order['strategy_id'], broker_cum_qty, broker_avg_price
            )
            logger.info(f"✅ 체결 대사 완료: {order['ticker']} 누적 {broker_cum_qty}주")

def run_worker_loop():
    logger.info(f"🚀 실행 워커 가동 (ID: {WORKER_ID}) - KIS 001x 통신 준비 완료")
    kis_tokens = {}

    while True:
        try:
            for portfolio_id in ["CORE", "SATELLITE"]:
                app_key, app_sec, cano, is_mock, acnt_prdt = get_account_secrets(portfolio_id)
                if not app_key: continue
                
                env = "MOCK" if is_mock else "REAL"
                acc_fp = db.hashlib.sha256(cano.encode()).hexdigest()[:16] if cano != "MOCK_ACCOUNT" else "MOCK_ACCOUNT"
                
                # Token 관리
                token_key = f"{env}_{portfolio_id}"
                if token_key not in kis_tokens or kis_tokens[token_key]['expire'] < time.time():
                    t, _ = kis.get_kis_access_token(app_key, app_sec, is_mock)
                    if t: kis_tokens[token_key] = {'token': t, 'expire': time.time() + 40000}
                    else: continue
                
                # 1. 체결 대사 (Reconciliation) 우선 수행
                reconcile_executions(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], env, acc_fp, portfolio_id, is_mock)

                # 2. Worker Lease 확보
                lease_ok, token = db.acquire_worker_lease("KIS", env, acc_fp, portfolio_id, WORKER_ID, ttl=10)
                if not lease_ok: continue
                
                # 3. 신규 의도 Claim
                order = db.claim_next_order("KIS", env, acc_fp, portfolio_id, WORKER_ID, token)
                if not order: continue

                # 4. Atomic Pre-flight Gate (가장 엄격한 제출 전 최종 검사)
                actual_cash = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], is_mock)
                passed, msg = db.atomic_preflight_and_claim("KIS", env, acc_fp, portfolio_id, WORKER_ID, token, order['id'], actual_cash)
                
                if not passed:
                    logger.warning(f"⚠️ 주문 게이트 차단 (ID: {order['id']}): {msg}")
                    db.transition_order_status(order['id'], 'CLAIMED', 'CANCELED', code=msg)
                    continue

                # 5. KIS 001x 전송 (POST 1회 제한)
                logger.info(f"[{portfolio_id}] 001x API 발송: {order['side']} {order['ticker']} {order['qty']}주")
                status, msg, odno, krx_odno, resp_code = kis.execute_kis_order_current_001x(
                    app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], 
                    order['ticker'], order['side'].upper() == "BUY", order['qty'], order['limit_price'], is_mock
                )

                if status == "ACKNOWLEDGED":
                    db.transition_order_status(order['id'], 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=krx_odno, code=resp_code)
                elif status == "REJECTED":
                    db.transition_order_status(order['id'], 'SUBMITTING', 'REJECTED', code=resp_code)
                else:
                    db.transition_order_status(order['id'], 'SUBMITTING', 'UNKNOWN', code=msg)

            time.sleep(1) # Rate limit 방어
            
        except Exception as e:
            logger.error(f"Worker Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_worker_loop()