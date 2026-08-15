import time
import uuid
from datetime import datetime
import database as db
import broker.kis_client as kis
import quant_engine as quant

WORKER_ID = str(uuid.uuid4())

def main_loop():
    print(f"🤖 퀀트 오토파일럿 리스크 방어 봇 가동 시작... (Worker ID: {WORKER_ID})")
    while True:
        try:
            status = db.get_system_status()
            if status == "HALTED_CONFIG_ERROR": time.sleep(10); continue
            elif status == "KILL_SWITCH": print("🚨 킬 스위치 작동 중!"); time.sleep(10); continue
            
            if not bool(db.get_setting('auto_trade_enabled', False)): time.sleep(5); continue

            api_key, api_sec, cano = db.get_setting('manual_app_key'), db.get_setting('manual_app_secret'), db.get_setting('manual_cano')
            is_mock = bool(db.get_setting('manual_is_mock', True))
            if not (api_key and api_sec and cano): time.sleep(10); continue
            if not db.acquire_worker_lease(cano, WORKER_ID): time.sleep(10); continue

            token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
            if not token: time.sleep(10); continue

            rd = db.get_setting('last_real_data', {'eval': 0, 'pnl': 0, 'cash': 0})
            daily_pnl_pct = (rd['pnl'] / rd['eval']) if rd['eval'] > 0 else 0.0

            # 대사(Reconciliation) 수행
            active_orders = db.get_orders_by_status(['ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED'])
            if active_orders:
                executions = kis.fetch_kis_order_executions(api_key, api_sec, cano, "01", token, is_mock)
                for order in active_orders:
                    oid, odno = order['id'], order['broker_order_id']
                    if odno in executions:
                        exec_data = executions[odno]
                        if order['status'] == 'UNKNOWN': db.transition_order_status(oid, 'UNKNOWN', 'ACKNOWLEDGED')
                        delta_qty = exec_data['cum_qty'] - order['cum_filled_qty']
                        if delta_qty > 0: db.process_fill_event(oid, order['ticker'], order['order_type'], delta_qty, exec_data['avg_price'])
                    else:
                        if order['status'] == 'UNKNOWN' and datetime.now().hour >= 16: db.transition_order_status(oid, 'UNKNOWN', 'EXPIRED')

            # 🛑 [핵심 패치 4] Pre-flight 검증 후 주문 발송
            while True:
                order = db.claim_next_order()
                if not order: break 
                oid, tk = order['id'], order['ticker']
                if not db.transition_order_status(oid, 'CLAIMED', 'SUBMITTING'): continue
                
                # 시세 신선도 및 정지 여부
                cur_p, is_halted = kis.fetch_kis_current_price_ext(api_key, api_sec, tk, token, is_mock)
                is_ok, reason = quant.pre_flight_risk_check(order['order_type'], order['price'], cur_p, is_halted, daily_pnl_pct, is_mock)
                
                if not is_ok:
                    db.transition_order_status(oid, 'SUBMITTING', 'REJECTED', code=reason)
                    print(f"🛑 주문 차단 (리스크 관리): {tk} - {reason}")
                    continue

                is_buy = "매수" in order['order_type']
                success, msg, odno, branch, code = kis.execute_kis_order(api_key, api_sec, cano, "01", token, tk, is_buy, order['qty'], 0, is_mock)
                
                if success and odno: db.transition_order_status(oid, 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=branch, code=code)
                else: db.transition_order_status(oid, 'SUBMITTING', 'UNKNOWN' if code=="EXCEPTION" else 'REJECTED', code=code)

            time.sleep(3) 
        except Exception as e: print(f"봇 에러: {e}"); time.sleep(10)

if __name__ == "__main__":
    main_loop()
