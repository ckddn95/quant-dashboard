import time
import logging
import uuid
import datetime
import traceback
import os
try: import toml
except ImportError: import tomllib as toml
import database as db
import broker.kis_client as kis

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ExecWorker")

WORKER_ID = f"worker_{uuid.uuid4().hex[:8]}"

def get_account_secrets(portfolio_id):
    """🚨 Streamlit 종속성을 제거한 독립적인 TOML 파서"""
    try:
        secrets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
        with open(secrets_path, "r", encoding="utf-8") as f:
            config = toml.load(f) if hasattr(toml, 'load') else toml.loads(f.read())
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        c = config["kis_accounts"][acc_key]
        return c["app_key"], c["app_secret"], str(c["cano"]).strip(), str(c.get("is_mock", "true")).lower() == 'true', str(c.get("acnt_prdt", "01")).strip()
    except Exception as e:
        logger.error(f"Secrets Load Error: {e}")
        return None, None, None, True, "01"

def reconcile_executions(app_key, app_sec, cano, acnt_prdt, token, env, acc_fp, portfolio_id, is_mock):
    open_statuses = ['CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED']
    pending_orders = db.get_orders_by_status_and_env(open_statuses, "KIS", env, acc_fp, portfolio_id)
    if not pending_orders: return
    
    executions = kis.fetch_daily_executions_0081(app_key, app_sec, cano, acnt_prdt, token, is_mock)
    
    for order in pending_orders:
        if not order['broker_order_id']: continue
        matched_execs = [e for e in executions if e.get('odno') == order['broker_order_id']]
        if not matched_execs: continue
        
        broker_cum_qty = sum(int(e.get('ccld_qty', 0)) for e in matched_execs)
        if broker_cum_qty > order['cum_filled_qty']:
            broker_cum_amt = sum(int(e.get('ccld_qty', 0)) * float(e.get('ccld_unpr', 0)) for e in matched_execs)
            broker_avg_price = broker_cum_amt / broker_cum_qty if broker_cum_qty > 0 else 0
            
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
                
                token_key = f"{env}_{portfolio_id}"
                if token_key not in kis_tokens or kis_tokens[token_key]['expire'] < time.time():
                    t, _ = kis.get_kis_access_token(app_key, app_sec, is_mock)
                    if t: kis_tokens[token_key] = {'token': t, 'expire': time.time() + 40000}
                    else: continue
                
                reconcile_executions(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], env, acc_fp, portfolio_id, is_mock)

                lease_ok, token = db.acquire_worker_lease("KIS", env, acc_fp, portfolio_id, WORKER_ID, ttl=10)
                if not lease_ok: continue
                
                order = db.claim_next_order("KIS", env, acc_fp, portfolio_id, WORKER_ID, token)
                if not order: continue

                actual_cash = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], order['ticker'], order['reference_price'], order['order_kind'], is_mock)
                order_updated, passed, msg = db.claim_and_authorize_submission("KIS", env, acc_fp, acnt_prdt, portfolio_id, WORKER_ID, actual_cash)
                
                if not passed:
                    logger.warning(f"⚠️ 주문 게이트 차단 (ID: {order['id']}): {msg}")
                    continue

                logger.info(f"[{portfolio_id}] 001x API 발송: {order['side']} {order['ticker']} {order['qty']}주")
                status, msg, odno, krx_odno, resp_code = kis.execute_kis_order_001x(
                    app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], 
                    order['ticker'], order['side'].upper() == "BUY", order['qty'], order['limit_price'], is_mock
                )

                if status == "ACKNOWLEDGED":
                    db.transition_order_status(order['id'], 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=krx_odno, code=resp_code)
                elif status == "REJECTED":
                    db.transition_order_status(order['id'], 'SUBMITTING', 'REJECTED', code=resp_code)
                else:
                    db.transition_order_status(order['id'], 'SUBMITTING', 'UNKNOWN', code=msg)

            time.sleep(1) 
            
        except Exception as e:
            logger.error(f"Worker Error: {e}")
            logger.error(traceback.format_exc())
            time.sleep(5)

if __name__ == "__main__":
    run_worker_loop()