import time
import logging
import uuid
import datetime
import traceback
import os
import math
try: import tomllib as toml
except ImportError: import toml
import database as db
import broker.kis_client as kis
import quant_engine as quant

db.preflight_check()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ExecWorker")
WORKER_ID = f"worker_{uuid.uuid4().hex[:8]}"

def get_account_secrets(portfolio_id):
    try:
        secrets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
        with open(secrets_path, "r", encoding="utf-8") as f: config = toml.loads(f.read())
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        c = config["kis_accounts"][acc_key]
        is_mock_raw = str(c.get("is_mock", "true")).strip().lower()
        if is_mock_raw not in ["true", "false"]: return None, None, None, True, "01", ""
        sys_secret = config.get("system", {}).get("hmac_secret")
        if not sys_secret or sys_secret == "fallback_default_secret" or str(sys_secret).strip() == "":
            logger.error("🚨 [Security Alert] hmac_secret is strictly required in secrets.toml!")
            return None, None, None, True, "01", ""
        return c["app_key"], c["app_secret"], str(c["cano"]).strip(), is_mock_raw == 'true', str(c.get("acnt_prdt", "01")).strip(), sys_secret
    except Exception as e:
        logger.error(f"Secrets Error: {e}"); return None, None, None, True, "01", ""

def reconcile_executions(app_key, app_sec, cano, acnt_prdt, token, env, acc_fp, portfolio_id, is_mock):
    open_statuses = ['SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'CANCEL_CLAIMED', 'CANCEL_SUBMITTING', 'CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN']
    pending_orders = db.get_orders_by_status_and_env(open_statuses, "KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id)
    if not pending_orders: return
    
    orders_by_date = {}
    now_kst = datetime.datetime.now(quant.KST)
    for order in pending_orders:
        dt_str = order['created_at'][:10].replace('-', '')
        if dt_str not in orders_by_date: orders_by_date[dt_str] = []
        orders_by_date[dt_str].append(order)
        
    for dt_str, orders_in_date in orders_by_date.items():
        exec_res = kis.fetch_daily_executions_0081(app_key, app_sec, cano, acnt_prdt, token, is_mock, order_date=dt_str)
        if exec_res.state != "SUCCESS_DATA": continue
        executions = exec_res.data
        
        for order in orders_in_date:
            matched_execs = []
            if order['broker_order_id']:
                matched_execs = [
                    e for e in executions 
                    if e.get('odno') == order['broker_order_id']
                    and order['broker'] == "KIS" and order['environment'] == env
                    and order['account_fingerprint'] == acc_fp and order['product_code'] == acnt_prdt
                ]
            elif order['status'] in ['UNKNOWN', 'SUBMITTING']:
                side_code = '02' if order['side'] == 'BUY' else '01'
                created_dt = datetime.datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S')
                order_time_str = created_dt.strftime('%H%M%S')
                
                def time_diff_sec(t1_str, t2_str):
                    if not t1_str or not t2_str: return 9999
                    try:
                        t1 = datetime.datetime.strptime(t1_str[-6:], '%H%M%S')
                        t2 = datetime.datetime.strptime(t2_str[-6:], '%H%M%S')
                        return abs((t1 - t2).total_seconds())
                    except Exception: return 9999

                candidates = [
                    e for e in executions
                    if e.get('pdno') == order['ticker']
                    and e.get('sll_buy_dvsn_cd') == side_code
                    and e.get('ord_qty') == str(order['qty'])
                    and (order['order_kind'] == 'MARKET' or e.get('ord_unpr') == str(int(order['limit_price'])))
                    and e.get('ord_dt') == dt_str
                    and order['exchange'] == 'KRX'
                    and time_diff_sec(e.get('ord_tmd', ''), order_time_str) <= 60
                ]
                
                if len(candidates) == 1:
                    order['broker_order_id'] = candidates[0]['odno']
                    order['branch_no'] = candidates[0].get('ord_gno_brno', '') 
                    try:
                        with db.get_connection() as conn:
                            conn.execute("UPDATE order_intents SET broker_order_id=?, branch_no=? WHERE id=?", (order['broker_order_id'], order['branch_no'], order['id']))
                        matched_execs = candidates
                        
                        if order['status'] in ['SUBMITTING', 'UNKNOWN']:
                            if not db.transition_order_status(order['id'], order['status'], 'ACKNOWLEDGED', broker_id=order['broker_order_id'], branch=order['branch_no'], worker_id=WORKER_ID, fencing_token=order.get('fencing_token'), reason="RECONCILED"):
                                logger.error(f"Failed to transition {order['id']} to ACKNOWLEDGED")
                            else: order['status'] = 'ACKNOWLEDGED'
                    except Exception as e: logger.error(f"DB Error unknown recovery: {e}")
                else:
                    if order['status'] == 'UNKNOWN':
                        try: 
                            db.transition_order_status(order['id'], order['status'], 'RECONCILIATION_REQUIRED', worker_id=WORKER_ID, fencing_token=order.get('fencing_token'), reason="UNKNOWN_MATCH_FAILED")
                            db.insert_reconciliation_event("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, order['id'], "UNKNOWN_MATCH_FAILED", f"Candidates found: {len(candidates)}")
                        except Exception as e: logger.error(f"DB Error recon insert: {e}")
                    
                    created_dt = datetime.datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST)
                    if (now_kst - created_dt).total_seconds() > 600:
                        if order['status'] == 'SUBMITTING':
                            if not db.transition_order_status(order['id'], order['status'], 'UNKNOWN', worker_id=WORKER_ID, fencing_token=order.get('fencing_token'), reason="NO_EXEC_TIMEOUT_LOCK"):
                                logger.error(f"Failed to transition {order['id']} to UNKNOWN via timeout")
                            else: order['status'] = 'UNKNOWN'
                    continue
                        
            if not matched_execs:
                created_dt = datetime.datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST)
                if (now_kst - created_dt).total_seconds() > 600:
                    if order['status'] == 'SUBMITTING':
                        if not db.transition_order_status(order['id'], order['status'], 'UNKNOWN', worker_id=WORKER_ID, fencing_token=order.get('fencing_token'), reason="NO_EXEC_TIMEOUT_LOCK"):
                            logger.error(f"Failed to transition {order['id']} to UNKNOWN via timeout")
                        else: order['status'] = 'UNKNOWN'
                continue

            latest_exec = matched_execs[0]
            broker_state = {
                'tot_ccld_qty': int(latest_exec.get('tot_ccld_qty', 0)),
                'tot_ccld_amt': float(latest_exec.get('tot_ccld_amt', 0)),
                'avg_prvs': float(latest_exec.get('avg_prvs', 0)),
                'rmn_qty': int(latest_exec.get('rmn_qty', 0)),
                'cncl_yn': latest_exec.get('cncl_yn', 'N'),
                'rjct_qty': int(latest_exec.get('rjct_qty', 0)),
                'orgno': latest_exec.get('krx_fwdg_ord_orgno', latest_exec.get('ord_gno_brno', '')), 
                'ord_tmd': latest_exec.get('ord_tmd', '')
            }
            
            try:
                db.apply_broker_receipt(
                    order['id'], order['ticker'], order['side'], "KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, 
                    broker_state
                )
            except Exception as e: logger.error(f"DB Error applying receipt/fill: {e}")
                
            if broker_state['cncl_yn'] == 'Y' and order['status'] not in ['CANCELED', 'FILLED']:
                try: db.transition_order_status(order['id'], order['status'], 'CANCELED', worker_id=WORKER_ID, fencing_token=order.get('fencing_token'), reason="BROKER_CANCELED")
                except Exception as e: logger.error(f"DB Error transitioning to CANCELED: {e}")
            elif broker_state['rjct_qty'] > 0 and broker_state['tot_ccld_qty'] == 0 and order['status'] not in ['REJECTED', 'CANCELED']:
                try: db.transition_order_status(order['id'], order['status'], 'REJECTED', worker_id=WORKER_ID, fencing_token=order.get('fencing_token'), reason="BROKER_REJECTED")
                except Exception as e: logger.error(f"DB Error transitioning to REJECTED: {e}")

def process_cancellations(app_key, app_sec, cano, acnt_prdt, token, env, acc_fp, portfolio_id, is_mock):
    while True:
        try: lease_ok, lease_tok = db.acquire_worker_lease("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, ttl=10)
        except Exception as e: logger.error(f"DB Error lease: {e}"); break
        if not lease_ok: break
        
        try: order, claim_msg = db.claim_cancel_intent("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID)
        except Exception as e: logger.error(f"DB Error claim_cancel: {e}"); break
        if not order: break
        
        if not db.renew_worker_lease("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, lease_tok, 10):
            break
            
        try: passed, auth_msg = db.authorize_cancel_order(order['id'], WORKER_ID, lease_tok)
        except Exception as e: logger.error(f"DB Error auth_cancel: {e}"); break
        
        if not passed:
            logger.warning(f"⚠️ 취소 게이트 인가 거절 (ID: {order['id']}): {auth_msg}"); continue
            
        if not db.renew_worker_lease("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, lease_tok, 10):
            logger.error(f"⚠️ [Fencing] POST 직전 Lease 탈취 감지. 취소 전송을 폐기합니다. (ID: {order['id']})")
            continue
            
        status, msg = kis.cancel_kis_order_0013(app_key, app_sec, cano, acnt_prdt, token, order['broker_order_id'], order['branch_no'], order['qty'] - order['cum_filled_qty'], is_mock)
        
        try:
            if status == "CANCEL_ACKNOWLEDGED": 
                db.transition_order_status(order['id'], 'CANCEL_SUBMITTING', 'CANCEL_ACKNOWLEDGED', code=msg, worker_id=WORKER_ID, fencing_token=lease_tok, reason="BROKER_CANCEL_ACK")
            elif status == "REJECTED": 
                db.transition_order_status(order['id'], 'CANCEL_SUBMITTING', 'CANCEL_UNKNOWN', code=msg, worker_id=WORKER_ID, fencing_token=lease_tok, reason="BROKER_CANCEL_REJECT")
            else: 
                db.transition_order_status(order['id'], 'CANCEL_SUBMITTING', 'CANCEL_UNKNOWN', code=msg, worker_id=WORKER_ID, fencing_token=lease_tok, reason="NETWORK_UNKNOWN")
        except Exception as e: logger.error(f"DB Error cancelling: {e}")

def run_worker_loop():
    logger.info(f"🚀 실행 워커 가동 (ID: {WORKER_ID})")
    kis_tokens = {}
    while True:
        try:
            for portfolio_id in ["CORE", "SATELLITE"]:
                app_key, app_sec, cano, is_mock, acnt_prdt, sys_secret = get_account_secrets(portfolio_id)
                if not app_key: continue
                env = "MOCK" if is_mock else "REAL"
                acc_fp = db.generate_account_fingerprint(cano, sys_secret)
                
                try: 
                    db.set_setting(f"heartbeat_worker_KIS_{env}_{acc_fp}_{acnt_prdt}_{portfolio_id}_{portfolio_id}", datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S'))
                except Exception as e: logger.error(f"DB Error heartbeat: {e}")
                
                try: db.revert_stale_claims("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id)
                except Exception as e: logger.error(f"DB Revert Stale Claims Error: {e}")
                
                token_key = f"kis_token_KIS_{env}_{acc_fp}_{acnt_prdt}_{portfolio_id}_{portfolio_id}"
                if token_key not in kis_tokens or kis_tokens[token_key]['expire'] < time.time():
                    t, _ = kis.get_kis_access_token(app_key, app_sec, is_mock)
                    if t: kis_tokens[token_key] = {'token': t, 'expire': time.time() + 40000}
                    else: continue

                try:
                    reconcile_executions(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], env, acc_fp, portfolio_id, is_mock)
                    process_cancellations(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], env, acc_fp, portfolio_id, is_mock)
                except Exception as e:
                    logger.error(f"⚠️ [Worker] 체결 대사/취소 프로세스 에러 (진행 유지): {e}")
                
                b_res = kis.fetch_kis_account_balance(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], is_mock)
                if b_res.state == "SUCCESS_DATA":
                    bal_s = b_res.data.get('summary', [])
                    bal_h = b_res.data.get('holdings', [])
                    total_eval = float(bal_s[0]['tot_evlu_amt']) if bal_s else 0.0
                    pnl = float(bal_s[0]['evlu_pfls_smtl_amt']) if bal_s else 0.0
                    current_principal = total_eval - pnl
                    last_principal_key = f"last_principal_KIS_{env}_{acc_fp}_{acnt_prdt}_{portfolio_id}_{portfolio_id}"
                    last_principal = db.get_setting(last_principal_key, current_principal)
                    if current_principal != last_principal:
                        try:
                            db.record_cash_flow("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, current_principal - last_principal, "Auto-detected principal change via Worker")
                            db.set_setting(last_principal_key, current_principal)
                        except Exception as e: logger.error(f"DB Error record_cash_flow: {e}")
                    
                    # 🚨 30초 관측 루프 내 실시간 현재가(prpr)로 보유종목 트레일링 최고가(highest_price) 갱신
                    for h in bal_h:
                        tk = h.get('pdno')
                        cur_prpr = float(h.get('prpr', 0.0))
                        if tk and cur_prpr > 0:
                            try:
                                with db.get_connection() as conn:
                                    conn.execute("""
                                        UPDATE positions 
                                        SET highest_price = MAX(highest_price, ?) 
                                        WHERE broker='KIS' AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?
                                    """, (cur_prpr, env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, tk))
                            except Exception as e:
                                logger.error(f"DB Error updating real-time highest price for {tk}: {e}")

                    c_res = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], "", 0, "00", is_mock)
                    if c_res.state == "SUCCESS_DATA": 
                        raw_cash = float(c_res.data)
                    else:
                        logger.warning(f"⚠️ 주문가능금액 조회 실패. 가용 현금을 0으로 제한: {c_res.msg}")
                        raw_cash = 0.0
                    
                    today_str = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d')
                    baseline_key = f"baseline_equity_KIS_{env}_{acc_fp}_{acnt_prdt}_{portfolio_id}_{portfolio_id}_{today_str}"
                    
                    starting_equity = db.get_setting(baseline_key, None)
                    if starting_equity is None:
                        db.set_setting(baseline_key, total_eval)
                        starting_equity = total_eval
                    
                    today_cash_flows = db.get_cash_flows_by_date("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, datetime.datetime.now(quant.KST).date(), datetime.datetime.now(quant.KST).date())
                    net_cash_flow_today = sum(today_cash_flows.values()) if today_cash_flows else 0.0
                    
                    adjusted_start_equity = starting_equity + net_cash_flow_today
                    if adjusted_start_equity > 0:
                        daily_pnl_pct = (total_eval - adjusted_start_equity) / adjusted_start_equity
                    else:
                        daily_pnl_pct = 0.0

                    try: db.record_daily_account_equity("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, total_eval, raw_cash)
                    except Exception as e: logger.error(f"DB Error record_equity: {e}")
                    
                    strat_cfg = quant.get_default_config(quant.Strategy(portfolio_id))
                    boost_addon = db.CONTRACT.get('booster_policy', {}).get('value', 0.10) if strat_cfg.boost else 0.0
                    max_exposure = total_eval * min(1.0, 0.90 + boost_addon)
                    current_exposure = sum([float(b['prpr']) * int(b['hldg_qty']) for b in bal_h])
                else: 
                    continue
                
                try: sys_status = db.get_system_status("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id)
                except Exception as e: logger.error(f"DB Error sys_status: {e}"); continue
                    
                try: lease_ok, lease_tok = db.acquire_worker_lease("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, ttl=10)
                except Exception as e: logger.error(f"DB Error lease: {e}"); continue
                if not lease_ok: continue
                
                try: order, claim_msg = db.claim_intent("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID)
                except Exception as e: logger.error(f"DB Error claim: {e}"); continue
                if not order: continue
                
                if not db.renew_worker_lease("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, lease_tok, 10):
                    continue
                
                target_price = order['reference_price'] if order['order_kind'] == 'MARKET' else order['limit_price']
                c_res_order = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], order['ticker'], target_price, order['order_kind'], is_mock)
                if c_res_order.state != "SUCCESS_DATA":
                    logger.warning(f"⚠️ 주문건 현금 조회 실패 (ID: {order['id']}) - 0원 처리: {c_res_order.msg}")
                    actual_cash = 0.0
                else:
                    actual_cash = float(c_res_order.data)
                
                if not db.renew_worker_lease("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, lease_tok, 10):
                    continue
                
                p_res = kis.fetch_kis_current_price_ext(app_key, app_sec, order['ticker'], kis_tokens[token_key]['token'], is_mock)
                if p_res.state != "SUCCESS_DATA": continue
                quote = p_res.data
                
                try:
                    cp = float(quote.get('price', 0))
                    hp = float(quote.get('high', 0))
                    lp = float(quote.get('low', 0))
                    is_halted = quote.get('is_halted', True)
                    
                    if math.isinf(cp) or math.isnan(cp) or cp <= 0 or \
                       math.isinf(hp) or math.isnan(hp) or hp <= 0 or \
                       math.isinf(lp) or math.isnan(lp) or lp <= 0 or \
                       lp > hp or cp > hp or cp < lp:
                        logger.error(f"⚠️ [Data Integrity] 비정상 시세 감지. 주문 인가를 보류합니다: {quote}")
                        continue
                        
                    if not isinstance(is_halted, bool): is_halted = True
                    quote['price'] = cp
                    quote['is_halted'] = is_halted
                except (ValueError, TypeError):
                    logger.error(f"⚠️ [Data Integrity] 시세 파싱 실패. 주문 인가를 보류합니다: {quote}")
                    continue
                
                try:
                    auth_order, passed, auth_msg = db.authorize_claimed_order(
                        order['id'], "KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, 
                        actual_cash, quote['price'], quote['is_halted'], daily_pnl_pct, current_exposure, max_exposure
                    )
                except Exception as e:
                    logger.error(f"DB Error authorize: {e}"); continue
                    
                if not passed:
                    logger.warning(f"⚠️ 주문 게이트 인가 거절 (ID: {order['id']}): {auth_msg}"); continue

                if not db.renew_worker_lease("KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, WORKER_ID, lease_tok, 10):
                    logger.error(f"⚠️ [Fencing] POST 직전 Lease 탈취 감지. 이중 지불 방지를 위해 주문을 폐기합니다. (ID: {order['id']})")
                    continue

                status, msg, odno, krx_odno, ord_tmd, resp_code = kis.execute_kis_order_001x(
                    app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], 
                    auth_order['ticker'], auth_order['side'].upper() == "BUY", auth_order['qty'], auth_order['reference_price'] if auth_order['order_kind'] != 'MARKET' else 0, is_mock
                )
                
                try:
                    if status == "ACKNOWLEDGED": 
                        db.transition_order_status(auth_order['id'], 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=krx_odno, broker_order_time=ord_tmd, code=resp_code, worker_id=WORKER_ID, fencing_token=lease_tok, reason="BROKER_ACK")
                    elif status == "REJECTED": 
                        db.transition_order_status(auth_order['id'], 'SUBMITTING', 'REJECTED', code=resp_code, worker_id=WORKER_ID, fencing_token=lease_tok, reason="BROKER_REJECT")
                    else: 
                        db.transition_order_status(auth_order['id'], 'SUBMITTING', 'UNKNOWN', code=msg, worker_id=WORKER_ID, fencing_token=lease_tok, reason="NETWORK_UNKNOWN")
                except Exception as e: logger.error(f"DB Error transition: {e}")

            time.sleep(1)
        except Exception as e:
            logger.error(f"Worker Error: {e}"); time.sleep(5)

if __name__ == "__main__":
    run_worker_loop()