import time
import logging
import uuid
import datetime
import traceback
import os
try: import tomllib as toml
except ImportError: import toml
import database as db
import broker.kis_client as kis

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ExecWorker")

WORKER_ID = f"worker_{uuid.uuid4().hex[:8]}"

def get_account_secrets(portfolio_id):
    try:
        secrets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
        with open(secrets_path, "rb") as f: config = toml.load(f)
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        c = config["kis_accounts"][acc_key]
        is_mock_raw = str(c.get("is_mock", "true")).strip().lower()
        if is_mock_raw not in ["true", "false"]:
            logger.error(f"HALTED_CONFIG_ERROR: Invalid is_mock")
            return None, None, None, True, "01"
        return c["app_key"], c["app_secret"], str(c["cano"]).strip(), is_mock_raw == 'true', str(c.get("acnt_prdt", "01")).strip()
    except Exception as e:
        logger.error(f"Secrets Load Error: {e}")
        return None, None, None, True, "01"

def reconcile_executions(app_key, app_sec, cano, acnt_prdt, token, env, acc_fp, portfolio_id, is_mock):
    """✅ 지시사항 5.3: ODNO가 없는 UNKNOWN 주문의 복합키 대사 로직 추가"""
    open_statuses = ['CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED']
    pending_orders = db.get_orders_by_status_and_env(open_statuses, "KIS", env, acc_fp, portfolio_id)
    if not pending_orders: return
    
    executions = kis.fetch_daily_executions_0081(app_key, app_sec, cano, acnt_prdt, token, is_mock)
    
    for order in pending_orders:
        matched_execs = []
        
        # 1. 정상적으로 ODNO가 발급된 주문의 복합키 대사
        if order['broker_order_id']:
            matched_execs = [
                e for e in executions 
                if e.get('odno') == order['broker_order_id']
                and order['broker'] == "KIS"
                and order['environment'] == env
                and order['account_fingerprint'] == acc_fp
                and order['product_code'] == acnt_prdt
                and e.get('bcnc_ptno', '') == order['branch_no'] 
            ]
        
        # 🚨 2. UNKNOWN(No-ODNO) 주문의 후보 복합키 대사 복구 (지시 5.3항)
        elif not order['broker_order_id'] and order['status'] == 'UNKNOWN':
            side_code = '02' if order['side'] == 'BUY' else '01'
            candidates = [
                e for e in executions
                if e.get('pdno') == order['ticker']
                and e.get('sll_buy_dvsn_cd') == side_code
                and e.get('ord_qty') == str(order['qty'])
                # 시장가가 아니면 가격까지 대조
                and (order['order_kind'] == 'MARKET' or e.get('ord_unpr') == str(int(order['limit_price'])))
            ]
            
            if len(candidates) == 1:
                # 단일 건 매칭 성공 시 ODNO 자동 복구 후 대사 진행
                order['broker_order_id'] = candidates[0]['odno']
                order['branch_no'] = candidates[0].get('bcnc_ptno', '')
                with db.get_connection() as conn:
                    conn.execute("UPDATE order_intents SET broker_order_id=?, branch_no=? WHERE id=?", 
                                 (order['broker_order_id'], order['branch_no'], order['id']))
                logger.info(f"✅ UNKNOWN 주문(ID:{order['id']}) 대사 매칭 성공: 복구된 ODNO {order['broker_order_id']}")
                matched_execs = candidates
            else:
                # 0건이거나 2건 이상이면 자동 복구 및 재주문 금지. 운영자 확인(수동) 로그 출력.
                logger.warning(f"⚠️ UNKNOWN 주문(ID:{order['id']}) 매칭 후보 {len(candidates)}건. 자동 재주문 금지. 운영자 수동 확인 필요.")
                continue

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
    logger.info(f"🚀 실행 워커 가동 (ID: {WORKER_ID}) - KIS 001x 통신 대기 중")
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
                
                # 1. 무조건 체결/상태 대사 먼저
                reconcile_executions(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], env, acc_fp, portfolio_id, is_mock)

                # 2. Worker Lease 확보
                lease_ok, token = db.acquire_worker_lease("KIS", env, acc_fp, portfolio_id, WORKER_ID, ttl=10)
                if not lease_ok: continue
                
                # 3. 원자적 Claim 및 Gate 검증
                actual_cash = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], "", 0, "00", is_mock)
                order, passed, msg = db.claim_and_authorize_submission("KIS", env, acc_fp, acnt_prdt, portfolio_id, WORKER_ID, actual_cash)
                
                if not order: continue
                if not passed:
                    logger.warning(f"⚠️ 주문 게이트 차단 (ID: {order['id']}): {msg}")
                    continue

                # 4. KIS 001x API 발송 (단 1회)
                logger.info(f"[{portfolio_id}] 001x API 발송: {order['side']} {order['ticker']} {order['qty']}주")
                status, msg, odno, krx_odno, ord_tmd, resp_code = kis.execute_kis_order_001x(
                    app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], 
                    order['ticker'], order['side'].upper() == "BUY", order['qty'], order['reference_price'] if order['order_kind'] != 'MARKET' else 0, is_mock
                )

                # 5. 결과 상태 단방향 전이 (ORD_TMD 기록 포함)
                if status == "ACKNOWLEDGED":
                    db.transition_order_status(order['id'], 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=krx_odno, broker_order_time=ord_tmd, code=resp_code)
                elif status == "REJECTED":
                    db.transition_order_status(order['id'], 'SUBMITTING', 'REJECTED', code=resp_code)
                else:
                    db.transition_order_status(order['id'], 'SUBMITTING', 'UNKNOWN', code=msg)

            time.sleep(1) # Rate limit
            
        except Exception as e:
            logger.error(f"Worker Error: {e}")
            logger.error(traceback.format_exc())
            time.sleep(5)

if __name__ == "__main__":
    run_worker_loop()