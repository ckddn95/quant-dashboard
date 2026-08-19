import os
import time
import datetime
import concurrent.futures
import pandas as pd
import FinanceDataReader as fdr  # 🚨 패치 2: fdr 모듈 누락 해결
import database as db
import broker.kis_client as kis
import quant_engine as quant

KST = datetime.timezone(datetime.timedelta(hours=9))

def run_signal_bot():
    db.preflight_check()
    print("🤖 [Signal Bot] Daemon Started. Monitoring markets...")

    while True:
        try:
            # 1. 킬스위치 확인 (전역)
            if db.get_setting('master_kill_switch', False):
                print("🚨 [Signal Bot] Master Kill Switch is ON. Paused.")
                time.sleep(10)
                continue

            # 시스템에 등록된 모든 계좌/전략을 순회 (현재는 CORE, SATELLITE 2개 가정)
            for strat in [quant.Strategy.CORE, quant.Strategy.SATELLITE]:
                account_key = "core" if strat == quant.Strategy.CORE else "satellite"
                try:
                    acc_config = os.environ.get(f"KIS_APP_KEY_{account_key.upper()}") 
                    # Streamlit secrets 대신 환경변수나 로컬 설정으로 불러오는 봇 전용 로직
                    # (간소화를 위해 DB에 저장된 세팅값을 이용하거나 MOCK 모드로 폴백)
                    sys_app_key = db.get_setting(f'kis_app_key_{account_key}', None)
                    sys_app_sec = db.get_setting(f'kis_app_sec_{account_key}', None)
                    sys_cano = db.get_setting(f'kis_cano_{account_key}', 'MOCK_ACCOUNT')
                    sys_acnt_prdt = db.get_setting(f'kis_prdt_{account_key}', '01')
                    is_mock = db.get_setting(f'kis_is_mock_{account_key}', True)
                except KeyError:
                    continue

                if not sys_app_key:
                    continue # 키가 없으면 스킵

                env_str = "MOCK" if is_mock else "REAL"
                acc_fp = db.generate_account_fingerprint(sys_cano, "fallback_default_secret")
                
                # 2. 오토파일럿(자동매매) 켜져 있는지 확인
                scope_key = f"KIS_{env_str}_{acc_fp}_{sys_acnt_prdt}_{strat.value}_{strat.value}"
                if not db.get_setting(f"auto_pilot_{scope_key}", False):
                    continue

                print(f"🔍 [Signal Bot] Scanning {strat.value} ({env_str}) ...")
                
                cfg = quant.get_default_config(strat)
                
                # 3. KIS 토큰 발급
                token, err = kis.get_kis_access_token(sys_app_key, sys_app_sec, is_mock)
                if not token:
                    print(f"⚠️ [Signal Bot] Token Error: {err}")
                    continue

                # 4. 잔고 조회 및 raw_cash 확보 (🚨 패치 1: raw_cash NameError 해결)
                b_res = kis.fetch_kis_account_balance(sys_app_key, sys_app_sec, sys_cano, sys_acnt_prdt, token, is_mock)
                if b_res.state != "SUCCESS_DATA":
                    continue
                
                c_res = kis.fetch_kis_orderable_cash(sys_app_key, sys_app_sec, sys_cano, sys_acnt_prdt, token, "", 0, "MARKET", is_mock)
                raw_cash = float(c_res.data) if c_res.state == "SUCCESS_DATA" else 0.0
                
                # 락 잡힌 현금 제외한 순수 가용 현금
                locked_cash, _ = db.get_locked_cash_and_qty("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value)
                usable_cash = max(0.0, raw_cash - locked_cash)
                
                summary = b_res.data.get('summary', [])
                total_eval = float(summary[0]['tot_evlu_amt']) if summary else 0.0
                target_buy_amt = total_eval * cfg.alloc if total_eval > 0 else 1000000.0

                # 5. 감시 대상 종목 취합 (Watchlist + 보유 종목)
                eval_tickers = set()
                eval_list = []
                for w in db.get_watchlist("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value):
                    tk = str(w['티커']).zfill(6)
                    eval_tickers.add(tk)
                    eval_list.append({'티커': tk, '종목명': w['종목명']})
                    
                for p in db.get_positions("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value):
                    tk = str(p['ticker']).zfill(6)
                    if tk not in eval_tickers:
                        eval_tickers.add(tk)
                        eval_list.append({'티커': tk, '종목명': tk})

                # 6. 종목별 평가 로직
                db_positions = {p['ticker']: p for p in db.get_positions("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value)}
                stocks_held = {str(s.get('pdno', '')).zfill(6): int(s.get('hldg_qty', 0)) for s in b_res.data.get('holdings', [])}

                for item in eval_list:
                    tk = item['티커']
                    m_qty = db_positions[tk]['managed_qty'] if tk in db_positions else 0
                    buy_p = db_positions[tk]['buy_price'] if tk in db_positions else 0.0
                    high_p = db_positions[tk]['highest_price'] if tk in db_positions else 0.0
                    days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[tk]['buy_date'])).days if tk in db_positions else 0
                    kis_qty = stocks_held.get(tk, 0)
                    holding_qty = max(kis_qty, m_qty)

                    # 현재가 틱 조회
                    p_res = kis.fetch_kis_current_price_ext(sys_app_key, sys_app_sec, tk, token, is_mock)
                    if p_res.state != "SUCCESS_DATA":
                        continue
                        
                    cp = p_res.data['price']
                    h_price = p_res.data['high']
                    l_price = p_res.data['low']
                    is_halted = p_res.data['is_halted']

                    # 🚨 패치 3: 억지 1분봉 확정 로직 제거, 실시간 틱(Tick)을 그대로 엔진에 넘겨 즉각 타격 판정
                    cp, action, score, reason = quant.evaluate_stock_for_ui(tk, strat, cfg, buy_p, high_p, cp, h_price, l_price, is_halted, days_held)

                    # 7. 매도/매수 시그널에 따른 Intent DB 적재
                    if holding_qty > 0 and ("매도" in action or "🔴" in action):
                        now_str = datetime.datetime.now(KST).strftime('%H%M%S')
                        spec = quant.OrderSpec("", f"BOT_{scope_key}_{tk}_SELL_{now_str}", "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, db.CONTRACT['strategy_version'], db.CONTRACT['contract_version'], tk, item['종목명'], "SELL", "MARKET", holding_qty, 0, cp, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'))
                        db.safe_add_order_intent(spec)
                        print(f"🔥 [SELL SIGNAL] {item['종목명']} ({tk}) - {reason}")
                        
                    elif "매수" in action or "🟢" in action:
                        allow_amt = min(usable_cash, max(0.0, target_buy_amt - (holding_qty * cp)))
                        add_qty = int(allow_amt // (cp * db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05))) if cp > 0 else 0
                        
                        if add_qty > 0:
                            now_str = datetime.datetime.now(KST).strftime('%H%M%S')
                            spec = quant.OrderSpec("", f"BOT_{scope_key}_{tk}_BUY_{now_str}", "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, db.CONTRACT['strategy_version'], db.CONTRACT['contract_version'], tk, item['종목명'], "BUY", "MARKET", add_qty, 0, cp, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'))
                            db.safe_add_order_intent(spec)
                            print(f"🛒 [BUY SIGNAL] {item['종목명']} ({tk}) - {add_qty}주 (사유: {reason})")
                            # 매수 후 가용현금 즉각 차감 (연속 매수 방지)
                            usable_cash -= (add_qty * cp * 1.05)

        except Exception as e:
            print(f"🚨 [Signal Bot] Fatal Error in loop: {e}")
        
        # CPU 과부하 방지 및 API Rate Limit 존중
        time.sleep(30)

if __name__ == "__main__":
    run_signal_bot()
