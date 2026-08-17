import time
import uuid
import os
import hashlib
from datetime import datetime, timedelta
import pandas as pd
import database as db
import broker.kis_client as kis
import quant_engine as quant

WORKER_ID = str(uuid.uuid4())
TOKEN_CACHE = {"token": "", "expires_at": 0}
BOUND_STRATEGY = os.getenv('STRATEGY', 'CORE') 

def get_valid_token(api_key, api_sec, is_mock):
    now = time.time()
    if TOKEN_CACHE["token"] and TOKEN_CACHE["expires_at"] > now + 300: return TOKEN_CACHE["token"]
    token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
    if token: TOKEN_CACHE["token"], TOKEN_CACHE["expires_at"] = token, now + 43200
    return token

def load_t_minus_1_data(ticker):
    # 🛑 [Step 2 패치] 무조건 어제(T-1)까지의 데이터만 로드하여 Look-ahead 편향 원천 봉쇄
    start_d = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    end_d = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        df = quant.fdr.DataReader(str(ticker).zfill(6), start=start_d, end=end_d)
        if not df.empty:
            ma20 = df['Close'].rolling(20).mean().iloc[-1]
            ma60 = df['Close'].rolling(60).mean().iloc[-1]
            ma200 = df['Close'].rolling(200).mean().iloc[-1]
            m60_up = True if len(df) < 60 else (ma60 > df['Close'].rolling(60).mean().iloc[-11])
            return ma20, ma60, ma200, m60_up
    except: pass
    return 0, 0, 0, False

def main_loop():
    print(f"🤖 {BOUND_STRATEGY} Quant Worker Started... (Worker ID: {WORKER_ID})")
    api_key, api_sec, cano = os.getenv('KIS_APP_KEY'), os.getenv('KIS_APP_SECRET'), os.getenv('KIS_CANO')
    acnt_prdt, is_mock = os.getenv('KIS_ACNT_PRDT', '01'), os.getenv('KIS_IS_MOCK', 'True').lower() == 'true'
    env_str = "MOCK" if is_mock else "REAL"

    if not api_key or not api_sec or not cano: print("🚨 Fatal: Missing Config."); return
    acc_fp = hashlib.sha256(cano.encode()).hexdigest()[:16]
    
    cfg = quant.get_default_config(quant.Strategy(BOUND_STRATEGY))

    while True:
        try:
            token = get_valid_token(api_key, api_sec, is_mock)
            if not token: time.sleep(10); continue
            
            sys_status = db.get_system_status("KIS", env_str, acc_fp, BOUND_STRATEGY)
            if sys_status["kill_switch"] or not sys_status["auto_pilot"]: time.sleep(5); continue
            
            lease_ok, lease_token = db.acquire_worker_lease("KIS", env_str, acc_fp, BOUND_STRATEGY, WORKER_ID)
            if not lease_ok: time.sleep(10); continue

            watchlist = db.get_watchlist("KIS", env_str, acc_fp, BOUND_STRATEGY, BOUND_STRATEGY)
            positions = {p['ticker']: p for p in db.get_positions("KIS", env_str, acc_fp, BOUND_STRATEGY, BOUND_STRATEGY)}
            eval_tickers = set([w['티커'] for w in watchlist] + list(positions.keys()))

            # 🛑 1분봉 실시간 스캔 (Regime Engine)
            for tk in eval_tickers:
                tk = str(tk).zfill(6)
                
                # 1. 고정된 T-1 지표 로드
                ma20, ma60, ma200, m60_up = load_t_minus_1_data(tk)
                if ma200 == 0: continue
                
                # 2. 실시간 현재가(1분봉 종가), 고가, 저가 로드
                c_price, h_price, l_price, is_halted, rsn = kis.fetch_kis_current_price_ext(api_key, api_sec, tk, token, is_mock)
                if c_price <= 0 or is_halted: continue
                
                # 3. 상태 테이블 조회
                regime = db.get_signal_state("KIS", env_str, acc_fp, BOUND_STRATEGY, BOUND_STRATEGY, tk)
                count = regime['consecutive_count'] if regime else 0
                curr_sig = regime['current_signal'] if regime else "NONE"
                
                pos = positions.get(tk)
                new_signal = "NONE"
                reason_str = ""
                
                # 4. 실시간 판단
                if pos:
                    days_held = (datetime.now() - pd.to_datetime(pos['buy_date'])).days
                    is_sell, s_price, s_reason = quant.calc_sell_signal(quant.Strategy(BOUND_STRATEGY), cfg, c_price, h_price, l_price, c_price, pos['buy_price'], max(pos['highest_price'], h_price), days_held, ma20, ma60)
                    
                    if is_sell:
                        if "즉각" in s_reason or "손절" in s_reason or "트레일링" in s_reason:
                            new_signal = "SELL_IMMEDIATE" # 즉시 관통
                            reason_str = s_reason
                        else:
                            new_signal = "SELL_TREND" # 2분 검증 필요
                            reason_str = s_reason
                else:
                    is_buy, score, b_reason = quant.calc_buy_signal(quant.Strategy(BOUND_STRATEGY), cfg, c_price, ma20, ma60, ma200, m60_up)
                    if is_buy:
                        new_signal = "BUY"
                        reason_str = b_reason
                
                # 5. Signal Regime 2분속 확인기 (State Machine)
                if new_signal == "NONE":
                    if count > 0: db.update_signal_state("KIS", env_str, acc_fp, BOUND_STRATEGY, BOUND_STRATEGY, tk, "NONE", "NONE", 0)
                    continue
                
                if new_signal == "SELL_IMMEDIATE":
                    # 즉시 실행 (Delay 없음)
                    now_str = datetime.now(KST).strftime('%Y%m%d_%H%M%S')
                    spec = quant.OrderSpec("", f"BOT_{tk}_SELL_{now_str}", "KIS", env_str, acc_fp, acnt_prdt, BOUND_STRATEGY, BOUND_STRATEGY, "1.0", db.CONTRACT['contract_version'], tk, tk, "SELL", "MARKET", pos['managed_qty'], 0, c_price, "KRX", "GTC", f"SIG_{now_str}", "BOT", now_str, "", "KIS", now_str, 300, db.CONTRACT.get('cost_model_version', '1.0.0'), now_str)
                    db.safe_add_order_intent(spec)
                    db.update_signal_state("KIS", env_str, acc_fp, BOUND_STRATEGY, BOUND_STRATEGY, tk, "NONE", "NONE", 0) # Rearm
                    print(f"⚡ [IMMEDIATE SELL] {tk}: {reason_str}")
                
                elif new_signal in ["BUY", "SELL_TREND"]:
                    if curr_sig == new_signal: count += 1
                    else: count = 1
                    
                    db.update_signal_state("KIS", env_str, acc_fp, BOUND_STRATEGY, BOUND_STRATEGY, tk, str(uuid.uuid4())[:8], new_signal, count)
                    
                    # 🛑 2분(2연속 틱) 확인 시 주문 발생
                    if count == db.CONTRACT['execution_rules']['signal_confirmation_candles']:
                        print(f"🎯 [REGIME CONFIRMED] {tk} {new_signal} (Count: {count})")
                        qty = 10 # 임시 수량 (실제로는 목표 예산 산식 적용 필요)
                        side = "BUY" if new_signal == "BUY" else "SELL"
                        if side == "SELL": qty = pos['managed_qty']
                        
                        now_str = datetime.now(KST).strftime('%Y%m%d_%H%M%S')
                        spec = quant.OrderSpec("", f"BOT_{tk}_{side}_{now_str}", "KIS", env_str, acc_fp, acnt_prdt, BOUND_STRATEGY, BOUND_STRATEGY, "1.0", db.CONTRACT['contract_version'], tk, tk, side, "MARKET", qty, 0, c_price, "KRX", "GTC", f"SIG_{now_str}", "BOT", now_str, "", "KIS", now_str, 300, db.CONTRACT.get('cost_model_version', '1.0.0'), now_str)
                        db.safe_add_order_intent(spec)
                        # 제출 후 쿨다운 대기 상태로 변경 (추가 중복 발주 방지)
                        db.update_signal_state("KIS", env_str, acc_fp, BOUND_STRATEGY, BOUND_STRATEGY, tk, "WAIT_REARM", "WAIT", 0)

            time.sleep(60) # 1분마다 스캔 (실제는 초단위 루프로 1분봉 API 찌르도록 고도화 가능)
        except Exception as e: print(f"Bot Error: {e}"); time.sleep(10)

if __name__ == "__main__": main_loop()
