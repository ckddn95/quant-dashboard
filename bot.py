import sqlite3 # 🛑 [보안/런타임] sqlite3를 상단에 명시적 임포트
import time
import uuid
import os      # 🛑 [보안] 환경변수 연동
from datetime import datetime
import database as db
import broker.kis_client as kis
import quant_engine as quant

WORKER_ID = str(uuid.uuid4())
INTENT_TTL_SECONDS = 300 

# 🛑 [보강 패치] 토큰 캐싱 시스템 (초당 API 폭격 및 차단 방지)
TOKEN_CACHE = {"token": None, "expires_at": 0}

def get_valid_token(api_key, api_sec, is_mock):
    now = time.time()
    if TOKEN_CACHE["token"] and TOKEN_CACHE["expires_at"] > now + 300:
        return TOKEN_CACHE["token"]
    
    token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
    if token:
        TOKEN_CACHE["token"] = token
        TOKEN_CACHE["expires_at"] = now + 43200
    return token

def cancel_all_open_orders(api_key, api_sec, cano, acnt_prdt, token, is_mock):
    active_orders = db.get_orders_by_status(['ACKNOWLEDGED', 'PARTIALLY_FILLED'])
    for order in active_orders:
        if not order['broker_order_id'] or not order['branch_no']: continue
        success, msg = kis.cancel_kis_order(api_key, api_sec, cano, acnt_prdt, token, order['broker_order_id'], order['branch_no'], is_mock)
        if success:
            db.transition_order_status(order['id'], order['status'], 'CANCEL_REQUESTED', code="KILL_CANCEL_REQ")
            print(f"🛑 [KILL SWITCH] 미체결 주문 강제 취소 요청 완료: {order['ticker']}")

def main_loop():
    print(f"🤖 퀀트 오토파일럿 리스크 방어 봇 가동 시작... (Worker ID: {WORKER_ID})")
    
    api_key = os.getenv('KIS_APP_KEY')
    api_sec = os.getenv('KIS_APP_SECRET')
    cano = os.getenv('KIS_CANO')
    acnt_prdt = os.getenv('KIS_ACNT_PRDT', '01') # 🛑 "01" 하드코딩 제거
    is_mock = os.getenv('KIS_IS_MOCK', 'True').lower() == 'true'

    if not api_key or not api_sec or not cano:
        print("🚨 OS 환경변수에 KIS_APP_KEY/SECRET/CANO가 누락되었습니다. 봇을 정지합니다.")
        return

    while True:
        try:
            token = get_valid_token(api_key, api_sec, is_mock)
            if not token: 
                print("⚠️ 토큰 발급에 실패했습니다. 10초 후 재시도합니다."); time.sleep(10); continue

            status = db.get_system_status()
            if status == "HALTED_CONFIG_ERROR": time.sleep(10); continue
            elif status == "KILL_SWITCH":
                print("🚨 킬 스위치 작동 중! 신규 진입 차단 및 미체결 취소 시도...")
                cancel_all_open_orders(api_key, api_sec, cano, acnt_prdt, token, is_mock)
                time.sleep(10); continue
            
            if not bool(db.get_setting('auto_trade_enabled', False)): time.sleep(5); continue
            if not db.acquire_worker_lease(cano, WORKER_ID): time.sleep(10); continue

            rd = db.get_setting('last_real_data', {'eval': 0, 'pnl': 0, 'cash': 0})
            daily_pnl_pct = (rd['pnl'] / rd['eval']) if rd['eval'] > 0 else 0.0

            # 1. 미체결 주문 대사 (Reconciliation)
            active_orders = db.get_orders_by_status(['ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED'])
            if active_orders:
                executions = kis.fetch_kis_order_executions(api_key, api_sec, cano, acnt_prdt, token, is_mock)
                for order in active_orders:
                    oid, odno = order['id'], order['broker_order_id']
                    if odno in executions:
                        exec_data = executions[odno]
                        if order['status'] == 'UNKNOWN': db.transition_order_status(oid, 'UNKNOWN', 'ACKNOWLEDGED')
                        delta_qty = exec_data['cum_qty'] - order['cum_filled_qty']
                        if delta_qty > 0: db.process_fill_event(oid, order['ticker'], order['order_type'], delta_qty, exec_data['avg_price'])

            # 2. 신규 주문 처리
            while True:
                order = db.claim_next_order()
                if not order: break 
                oid, tk = order['id'], order['ticker']
                
                if db.get_setting('kill_switch', False):
                    db.transition_order_status(oid, 'CLAIMED', 'CANCELED', code="KILL_SWITCH")
                    break 
                if not db.get_setting('auto_trade_enabled', False):
                    db.transition_order_status(oid, 'CLAIMED', 'CANCELED', code="AUTO_TRADE_OFF")
                    break
                    
                created_at = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S')
                if (datetime.now() - created_at).total_seconds() > INTENT_TTL_SECONDS:
                    db.transition_order_status(oid, 'CLAIMED', 'EXPIRED', code="TTL_EXPIRED")
                    print(f"⌛ 주문 만료 (TTL 초과 폐기): {tk}")
                    continue

                # 🛑 [보강 패치] RiskContext 빌드 및 사전 리스크 검사
                cur_p, high_p, low_p, is_halted = kis.fetch_kis_current_price_ext(api_key, api_sec, tk, token, is_mock)
                snap = quant.StockSnapshot(
                    ticker=tk, current_price=cur_p, high_price=high_p, low_price=low_p,
                    ma20=0, ma60=0, ma200=0, m60_up=False,
                    as_of=datetime.now(), source="KIS", is_valid=True, is_complete_bar=False, reason="OK"
                )
                snap.validate(is_halted)
                
                locked_cash, locked_sell_qty = db.get_locked_cash_and_qty(tk)
                ctx = quant.RiskContext(
                    account_id=cano, env="MOCK" if is_mock else "REAL",
                    usable_cash=rd['cash'] - locked_cash, locked_buy_cash=locked_cash,
                    managed_sell_qty=locked_sell_qty, current_exposure=0.0, max_exposure=float('inf'),
                    daily_pnl_pct=daily_pnl_pct, is_kill_switch_on=db.get_setting('kill_switch', False),
                    is_auto_trade_on=db.get_setting('auto_trade_enabled', False)
                )
                
                is_ok, reason = quant.pre_flight_risk_check(order['order_type'], order['price'], snap, ctx, is_mock)
                if not is_ok:
                    db.transition_order_status(oid, 'CLAIMED', 'REJECTED', code=reason)
                    print(f"🛑 주문 차단 (리스크 통제): {tk} - {reason}")
                    continue

                # 🛑 [보강 패치] POST 전송 직전에만 SUBMITTING으로 전환 (영구 고착 방지)
                if not db.transition_order_status(oid, 'CLAIMED', 'SUBMITTING'): continue
                
                is_buy = order['order_type'].upper() == "BUY" or "매수" in order['order_type']
                success, msg, odno, branch, code = kis.execute_kis_order(api_key, api_sec, cano, acnt_prdt, token, tk, is_buy, order['qty'], 0, is_mock)
                
                if success and odno: 
                    db.transition_order_status(oid, 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=branch, code=code)
                else: 
                    db.transition_order_status(oid, 'SUBMITTING', 'UNKNOWN' if code=="EXCEPTION" else 'REJECTED', code=code)

            time.sleep(3) 
        except Exception as e: print(f"봇 에러: {e}"); time.sleep(10)

if __name__ == "__main__":
    main_loop()
