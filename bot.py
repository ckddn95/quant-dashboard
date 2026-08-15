import time
import uuid
from datetime import datetime
import database as db
import broker.kis_client as kis

WORKER_ID = str(uuid.uuid4())

def main_loop():
    print(f"🤖 퀀트 오토파일럿 봇 가동 시작... (Worker ID: {WORKER_ID})")
    while True:
        try:
            status = db.get_system_status()
            if status == "HALTED_CONFIG_ERROR":
                print("🚨 [HALTED_CONFIG_ERROR] 알 수 없는 설정 오류로 매매 정지."); time.sleep(10); continue
            elif status == "KILL_SWITCH":
                print("🚨 킬 스위치 작동 중! 대기..."); time.sleep(10); continue
            
            auto_trade = bool(db.get_setting('auto_trade_enabled', False))
            if not auto_trade:
                time.sleep(5); continue

            api_key, api_sec, cano = db.get_setting('manual_app_key'), db.get_setting('manual_app_secret'), db.get_setting('manual_cano')
            is_mock = bool(db.get_setting('manual_is_mock', True))
            if not (api_key and api_sec and cano):
                print("⚠️ API 설정 누락."); time.sleep(10); continue

            # 🛑 [핵심 패치 5] 계좌별 단일 worker lease 획득
            if not db.acquire_worker_lease(cano, WORKER_ID):
                print("⚠️ 타 봇 인스턴스가 해당 계좌 제어 중. 락(Lock) 해제 대기..."); time.sleep(10); continue

            token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
            if not token:
                print("⚠️ KIS 토큰 발급 실패."); time.sleep(10); continue

            # 🛑 [핵심 패치 6] 재시작 및 루프 시 신규 신호보다 브로커 주문·체결 대사(Reconciliation)를 무조건 선행
            active_orders = db.get_orders_by_status(['ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED'])
            if active_orders:
                executions = kis.fetch_kis_order_executions(api_key, api_sec, cano, "01", token, is_mock)
                for order in active_orders:
                    oid, odno = order['id'], order['broker_order_id']
                    if odno in executions:
                        exec_data = executions[odno]
                        if order['status'] == 'UNKNOWN': db.transition_order_status(oid, 'UNKNOWN', 'ACKNOWLEDGED')
                            
                        delta_qty = exec_data['cum_qty'] - order['cum_filled_qty']
                        if delta_qty > 0:
                            if db.process_fill_event(oid, order['ticker'], order['order_type'], delta_qty, exec_data['avg_price']):
                                print(f"✅ 체결 업데이트: {order['ticker']} +{delta_qty}주 (평단 {exec_data['avg_price']})")
                    else:
                        if order['status'] == 'UNKNOWN' and datetime.now().hour >= 16:
                            db.transition_order_status(oid, 'UNKNOWN', 'EXPIRED')

            # 🛑 신규 의도(INTENT) 큐 Claim 및 발송
            while True:
                order = db.claim_next_order()
                if not order: break 
                
                oid = order['id']
                if not db.transition_order_status(oid, 'CLAIMED', 'SUBMITTING'): continue
                
                is_buy = "매수" in order['order_type']
                success, msg, odno, branch, code = kis.execute_kis_order(api_key, api_sec, cano, "01", token, order['ticker'], is_buy, order['qty'], 0, is_mock)
                
                if success and odno:
                    db.transition_order_status(oid, 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=branch, code=code)
                    print(f"📡 KIS 접수 완료: {order['ticker']} {order['order_type']} {order['qty']}주 [번호: {odno}]")
                else:
                    if code == "EXCEPTION":
                        db.transition_order_status(oid, 'SUBMITTING', 'UNKNOWN', code=code)
                        print(f"⚠️ 주문 타임아웃(UNKNOWN). 자동 재전송 금지 및 대사 대기: {order['ticker']}")
                    else:
                        db.transition_order_status(oid, 'SUBMITTING', 'REJECTED', code=code)
                        print(f"❌ KIS 승인 거절: {msg}")

            time.sleep(3) 
        except Exception as e:
            print(f"봇 에러: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_loop()
