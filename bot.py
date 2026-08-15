import time
import database as db
import broker.kis_client as kis

def main_loop():
    print("🤖 퀀트 오토파일럿 대사(Reconciliation) 봇 가동 시작...")
    while True:
        try:
            status = db.get_system_status()
            if status == "HALTED_CONFIG_ERROR":
                print("🚨 [HALTED_CONFIG_ERROR] 알 수 없는 설정 오류로 매매 정지.")
                time.sleep(10); continue
            elif status == "KILL_SWITCH":
                print("🚨 킬 스위치 작동 중! 대기..."); time.sleep(10); continue
            
            auto_trade = bool(db.get_setting('auto_trade_enabled', False))
            if not auto_trade:
                time.sleep(5); continue

            api_key = db.get_setting('manual_app_key')
            api_sec = db.get_setting('manual_app_secret')
            cano = db.get_setting('manual_cano')
            is_mock = bool(db.get_setting('manual_is_mock', True))
            
            if not (api_key and api_sec and cano):
                print("⚠️ API 설정 누락."); time.sleep(10); continue

            token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
            if not token:
                print("⚠️ KIS 토큰 발급 실패."); time.sleep(10); continue

            # 🛑 PHASE 1: 신규 의도(INTENT) 발송 상태 머신
            intents = db.get_orders_by_status(['INTENT_CREATED'])
            for order in intents:
                oid = order['id']
                if not db.transition_order_status(oid, 'INTENT_CREATED', 'CLAIMED'): continue
                if not db.transition_order_status(oid, 'CLAIMED', 'SUBMITTING'): continue
                
                is_buy = "매수" in order['order_type']
                success, msg, odno, branch, code = kis.execute_kis_order(api_key, api_sec, cano, "01", token, order['ticker'], is_buy, order['qty'], 0, is_mock)
                
                if success and odno:
                    # KIS 접수 성공은 ACKNOWLEDGED (체결 아님)
                    db.transition_order_status(oid, 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=branch, code=code)
                    print(f"📡 KIS 접수 완료 (ACK): {order['ticker']} {order['order_type']} {order['qty']}주 [주문번호: {odno}]")
                else:
                    if code == "EXCEPTION":
                        # UNKNOWN: 망분리/타임아웃. 절대 재전송 금지 (대사 봇이 폴링으로 찾음)
                        db.transition_order_status(oid, 'SUBMITTING', 'UNKNOWN', code=code)
                        print(f"⚠️ 주문 타임아웃(UNKNOWN). 대사 대기: {order['ticker']}")
                    else:
                        db.transition_order_status(oid, 'SUBMITTING', 'REJECTED', code=code)
                        print(f"❌ KIS 승인 거절 (REJECTED): {msg}")

            # 🛑 PHASE 2: 주문 대사 (Reconciliation) - KIS 서버와 DB 상태 일치화
            active_orders = db.get_orders_by_status(['ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED'])
            if active_orders:
                executions = kis.fetch_kis_order_executions(api_key, api_sec, cano, "01", token, is_mock)
                for order in active_orders:
                    oid = order['id']
                    odno = order['broker_order_id']
                    
                    if odno in executions:
                        exec_data = executions[odno]
                        
                        # UNKNOWN이었는데 서버에 살아있다면 ACK로 복구
                        if order['status'] == 'UNKNOWN':
                            db.transition_order_status(oid, 'UNKNOWN', 'ACKNOWLEDGED')
                            
                        cum_qty_server = exec_data['cum_qty']
                        cum_qty_db = order['cum_filled_qty']
                        delta_qty = cum_qty_server - cum_qty_db
                        
                        # 델타 체결량 발생 시 원장에 정확히 1번 반영
                        if delta_qty > 0:
                            if db.process_fill_event(oid, order['ticker'], order['order_type'], delta_qty, exec_data['avg_price']):
                                print(f"✅ 체결 업데이트: {order['ticker']} +{delta_qty}주 (평단 {exec_data['avg_price']})")
                    else:
                        # UNKNOWN인데 장 마감까지 서버에 없다면 거절/만료 처리
                        if order['status'] == 'UNKNOWN' and datetime.now().hour >= 16:
                            db.transition_order_status(oid, 'UNKNOWN', 'EXPIRED')

            time.sleep(3) # 3초 주기로 대사 폴링
        except Exception as e:
            print(f"봇 에러: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_loop()
