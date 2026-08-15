import time
import database as db
import broker.kis_client as kis

def main_loop():
    print("🤖 퀀트 오토파일럿 봇 가동 시작...")
    while True:
        try:
            status = db.get_system_status()
            if status == "HALTED_CONFIG_ERROR":
                print("🚨 [HALTED_CONFIG_ERROR] 알 수 없는 전략 설정 오류로 인해 신규 주문이 차단되었습니다.")
                time.sleep(10)
                continue
            elif status == "KILL_SWITCH":
                print("🚨 킬 스위치 작동 중! 대기...")
                time.sleep(10)
                continue
            
            auto_trade = bool(db.get_setting('auto_trade_enabled', False))
            if auto_trade:
                pending_orders = db.get_pending_orders()
                if pending_orders:
                    api_key = db.get_setting('manual_app_key')
                    api_sec = db.get_setting('manual_app_secret')
                    cano = db.get_setting('manual_cano')
                    is_mock = bool(db.get_setting('manual_is_mock', True))
                    
                    if api_key and api_sec and cano:
                        token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
                        if token:
                            for order in pending_orders:
                                ticker = order['ticker']
                                is_buy = "매수" in order['order_type']
                                qty = order['qty']
                                price = 0 
                                
                                success, msg = kis.execute_kis_order(api_key, api_sec, cano, "01", token, ticker, is_buy, qty, price, is_mock)
                                if success:
                                    print(f"✅ 주문 성공: {ticker} {order['order_type']} {qty}주")
                                    db.update_order_status(order['id'], 'COMPLETED')
                                else:
                                    print(f"❌ 주문 실패 ({ticker}): {msg}")
                                    db.update_order_status(order['id'], 'FAILED')
                    else:
                        print("⚠️ API 설정 누락.")
            time.sleep(5) 
        except Exception as e:
            print(f"봇 에러: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_loop()
