import time
import database as db
import broker.kis_client as kis

def main_loop():
    print("🤖 퀀트 오토파일럿 봇 가동 시작...")
    while True:
        try:
            # 1. 킬 스위치 감시
            kill_switch = db.get_setting('kill_switch', False)
            if kill_switch:
                print("🚨 킬 스위치 작동 중! 모든 봇 활동 정지.")
                time.sleep(10)
                continue
            
            # 2. 자동주문 활성화 확인 및 큐 처리
            auto_trade = db.get_setting('auto_trade_enabled', False)
            if auto_trade:
                pending_orders = db.get_pending_orders()
                if pending_orders:
                    api_key = db.get_setting('manual_app_key')
                    api_sec = db.get_setting('manual_app_secret')
                    cano = db.get_setting('manual_cano')
                    is_mock = db.get_setting('manual_is_mock', True)
                    
                    if api_key and api_sec and cano:
                        token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
                        if token:
                            for order in pending_orders:
                                ticker = order['ticker']
                                is_buy = "매수" in order['order_type']
                                qty = order['qty']
                                price = 0 # 시장가 기준
                                
                                success, msg = kis.execute_kis_order(api_key, api_sec, cano, "01", token, ticker, is_buy, qty, price, is_mock)
                                if success:
                                    print(f"✅ 주문 성공: {ticker} {order['order_type']} {qty}주")
                                    db.update_order_status(order['id'], 'COMPLETED')
                                else:
                                    print(f"❌ 주문 실패 ({ticker}): {msg}")
                                    db.update_order_status(order['id'], 'FAILED')
                    else:
                        print("⚠️ API 설정 누락으로 주문을 실행할 수 없습니다.")
            
            time.sleep(5) # 5초 주기로 DB 폴링
        except Exception as e:
            print(f"봇 에러 발생: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_loop()
