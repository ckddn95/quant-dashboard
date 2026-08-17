import time
import uuid
import os
from datetime import datetime
import database as db
import broker.kis_client as kis
import quant_engine as quant

WORKER_ID = str(uuid.uuid4())
INTENT_TTL_SECONDS = 300 
TOKEN_CACHE = {"token": "", "expires_at": 0}

# 🛑 워커 바인딩 (실행 환경변수 STRATEGY 기준 분리)
BOUND_STRATEGY = os.getenv('STRATEGY', 'CORE') 

def get_valid_token(api_key, api_sec, is_mock):
    now = time.time()
    if TOKEN_CACHE["token"] and TOKEN_CACHE["expires_at"] > now + 300: return TOKEN_CACHE["token"]
    token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
    if token: TOKEN_CACHE["token"], TOKEN_CACHE["expires_at"] = token, now + 43200
    return token

def reconcile_active_orders(api_key, api_sec, cano, acnt_prdt, env_str, port_id, strat_id, token, is_mock):
    active_orders = db.get_orders_by_status_and_env(['ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED'], "KIS", env_str, cano, port_id)
    if not active_orders: return
    executions = kis.fetch_kis_order_executions(api_key, api_sec, cano, acnt_prdt, token, is_mock)
    for order in active_orders:
        oid, odno = order['id'], order['broker_order_id']
        if odno in executions:
            if order['status'] == 'UNKNOWN': db.transition_order_status(oid, 'UNKNOWN', 'ACKNOWLEDGED')
            db.apply_fill_delta_exactly_once(oid, order['ticker'], order['side'], "KIS", env_str, cano, port_id, strat_id, executions[odno]['cum_qty'], executions[odno]['avg_price'])

def cancel_all_open_orders(api_key, api_sec, cano, acnt_prdt, env_str, port_id, token, is_mock):
    active_orders = db.get_orders_by_status_and_env(['ACKNOWLEDGED', 'PARTIALLY_FILLED'], "KIS", env_str, cano, port_id)
    for order in active_orders:
        if not order['broker_order_id'] or not order['branch_no']: continue
        status, msg = kis.cancel_kis_order(api_key, api_sec, cano, acnt_prdt, token, order['broker_order_id'], order['branch_no'], is_mock)
        if status == "CANCEL_ACKNOWLEDGED":
            db.transition_order_status(order['id'], order['status'], 'CANCEL_REQUESTED', code="KILL_CANCEL_REQ")

def main_loop():
    print(f"🤖 {BOUND_STRATEGY} Quant Worker Started... (Worker ID: {WORKER_ID})")
    api_key, api_sec, cano = os.getenv('KIS_APP_KEY'), os.getenv('KIS_APP_SECRET'), os.getenv('KIS_CANO')
    acnt_prdt, is_mock = os.getenv('KIS_ACNT_PRDT', '01'), os.getenv('KIS_IS_MOCK', 'True').lower() == 'true'
    env_str = "MOCK" if is_mock else "REAL"

    if not api_key or not api_sec or not cano: print("🚨 Fatal: Missing Config."); return

    while True:
        try:
            token = get_valid_token(api_key, api_sec, is_mock)
            if not token: time.sleep(10); continue
            
            port_id = strat_id = BOUND_STRATEGY
            sys_status = db.get_system_status("KIS", env_str, cano, port_id)
            
            if sys_status["contract_version"] != db.CONTRACT["contract_version"]: print("🚨 Version Mismatch."); time.sleep(10); continue
            if sys_status["kill_switch"]: 
                cancel_all_open_orders(api_key, api_sec, cano, acnt_prdt, env_str, port_id, token, is_mock)
                time.sleep(10); continue
            if not sys_status["auto_trade"]: time.sleep(5); continue
            
            lease_ok, lease_token = db.acquire_worker_lease("KIS", env_str, cano, port_id, WORKER_ID)
            if not lease_ok: time.sleep(10); continue

            rd = db.get_setting('last_real_data', {'eval': 0, 'pnl': 0, 'cash': 0})
            daily_pnl = (rd['pnl'] / rd['eval']) if rd['eval'] > 0 else 0.0

            reconcile_active_orders(api_key, api_sec, cano, acnt_prdt, env_str, port_id, strat_id, token, is_mock)

            while True:
                order = db.claim_next_order("KIS", env_str, cano, port_id, WORKER_ID, lease_token)
                if not order: break 
                oid, tk = order['id'], order['ticker']
                
                # 🛑 Atomic Gate: POST 직전 재검증
                check_sys = db.get_system_status("KIS", env_str, cano, port_id)
                if check_sys["kill_switch"] or not check_sys["auto_trade"]:
                    db.transition_order_status(oid, 'CLAIMED', 'CANCELED', code="SYS_BLOCKED"); break
                    
                if (datetime.now() - datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S')).total_seconds() > INTENT_TTL_SECONDS:
                    db.transition_order_status(oid, 'CLAIMED', 'EXPIRED', code="TTL_EXPIRED"); continue

                cur_p, high_p, low_p, is_halted, rsn = kis.fetch_kis_current_price_ext(api_key, api_sec, tk, token, is_mock)
                snap = quant.StockSnapshot(ticker=tk, current_price=cur_p, high_price=high_p, low_price=low_p, ma20=0, ma60=0, ma200=0, m60_up=False, as_of=datetime.now(), source="KIS", is_valid=(rsn=="OK"), is_complete_bar=False, reason=rsn, executable=True)
                snap.validate(is_halted)
                
                locked_cash, locked_sell_qty = db.get_locked_cash_and_qty("KIS", env_str, cano, port_id, tk)
                spec = quant.OrderSpec(correlation_id=order['correlation_id'], idempotency_key=order['idempotency_key'], broker="KIS", environment=env_str, account_id=cano, account_product_code=acnt_prdt, portfolio_id=port_id, strategy_id=strat_id, strategy_version=order['strategy_version'], ticker=tk, stock_name=order['stock_name'], side=order['side'], order_kind=order['order_kind'], quantity=order['qty'], limit_price=order['limit_price'], intent_created_at=order['created_at'])
                ctx = quant.RiskContext(account_id=cano, env=env_str, usable_cash=rd['cash'] - locked_cash, locked_buy_cash=locked_cash, managed_sell_qty=locked_sell_qty, current_exposure=0.0, max_exposure=float('inf'), daily_pnl_pct=daily_pnl, is_kill_switch_on=False, is_auto_trade_on=True)
                
                is_ok, reason = quant.pre_flight_risk_check(spec, snap, ctx)
                if not is_ok: db.transition_order_status(oid, 'CLAIMED', 'RISK_REJECTED', code=reason); continue

                if not db.transition_order_status(oid, 'CLAIMED', 'SUBMITTING'): continue
                
                status, msg, odno, branch, code = kis.execute_kis_order(api_key, api_sec, cano, acnt_prdt, token, tk, spec.side=="BUY", order['qty'], 0, is_mock)
                
                if status == "ACKNOWLEDGED": db.transition_order_status(oid, 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=branch, code=code)
                else: db.transition_order_status(oid, 'SUBMITTING', status, code=code)

            time.sleep(3) 
        except Exception as e: print(f"Bot Error: {e}"); time.sleep(10)

if __name__ == "__main__": main_loop()
