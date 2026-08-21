import os
import time
import datetime
import concurrent.futures
import pandas as pd
import FinanceDataReader as fdr
import database as db
import broker.kis_client as kis
import quant_engine as quant

KST = datetime.timezone(datetime.timedelta(hours=9))

def run_signal_bot():
    db.preflight_check()
    print("🤖 [Signal Bot] Daemon Started. Monitoring markets with SIMULATION rules...")

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

                # 4. 잔고 조회 및 일일 손실률 계산 로직 동기화 (누적 평가손익 -> 당일 손익)
                b_res = kis.fetch_kis_account_balance(sys_app_key, sys_app_sec, sys_cano, sys_acnt_prdt, token, is_mock)
                if b_res.state != "SUCCESS_DATA":
                    continue
                
                c_res = kis.fetch_kis_orderable_cash(sys_app_key, sys_app_sec, sys_cano, sys_acnt_prdt, token, "", 0, "MARKET", is_mock)
                raw_cash = float(c_res.data) if c_res.state == "SUCCESS_DATA" else 0.0
                
                locked_cash, _ = db.get_locked_cash_and_qty("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value)
                usable_cash = max(0.0, raw_cash - locked_cash)
                
                summary = b_res.data.get('summary', [])
                total_eval = float(summary[0]['tot_evlu_amt']) if summary else 0.0
                
                # 🚨 패치 1: 일일 손실률 왜곡 수정 (누적 평가손익이 아닌 '당일 기준 손실률'로 재계산)
                last_principal_key = f"last_principal_{scope_key}"
                last_principal = db.get_setting(last_principal_key, total_eval)
                daily_pnl_pct = (total_eval - last_principal) / last_principal if last_principal > 0 else 0.0
                
                # 🚨 패치 2: 실거래 부스터 상시 발동 버그 수정 (시장 상황 Regime 확인)
                regime = quant.determine_market_regime(total_eval) # 엔진의 시장 판별 로직 호출
                is_bull_market = (regime == quant.MarketRegime.BULL)
                boost_addon = db.CONTRACT.get('booster_policy', {}).get('value', 0.10) if (cfg.boost and is_bull_market) else 0.0
                max_exposure_ratio = 0.90 + boost_addon
                target_buy_amt = total_eval * cfg.alloc if total_eval > 0 else 1000000.0

                # 5. 감시 대상 종목 취합
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

                db_positions = {p['ticker']: p for p in db.get_positions("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value)}
                stocks_held = {str(s.get('pdno', '')).zfill(6): int(s.get('hldg_qty', 0)) for s in b_res.data.get('holdings', [])}
                now_kst = datetime.datetime.now(KST)

                for item in eval_list:
                    tk = item['티커']
                    m_qty = db_positions[tk]['managed_qty'] if tk in db_positions else 0
                    buy_p = db_positions[tk]['buy_price'] if tk in db_positions else 0.0
                    high_p = db_positions[tk]['highest_price'] if tk in db_positions else 0.0
                    
                    # 🚨 패치 5: 보유기간 산정을 달력일(days)에서 영업일(거래세션) 기준으로 변경
                    buy_dt = pd.to_datetime(db_positions[tk]['buy_date']).tz_localize('UTC').tz_convert(KST) if tk in db_positions else now_kst
                    # Pandas의 bdate_range를 사용하여 순수 평일(영업일) 차이만 계산
                    days_held = len(pd.bdate_range(start=buy_dt.date(), end=now_kst.date())) - 1 if tk in db_positions else 0
                    
                    kis_qty = stocks_held.get(tk, 0)
                    holding_qty = max(kis_qty, m_qty)

                    p_res = kis.fetch_kis_current_price_ext(sys_app_key, sys_app_sec, tk, token, is_mock)
                    if p_res.state != "SUCCESS_DATA":
                        continue
                        
                    cp = p_res.data['price']
                    h_price = p_res.data['high']
                    l_price = p_res.data['low']
                    is_halted = p_res.data['is_halted']

                    # 🚨 패치 6: 최고가(highest_price) 갱신 로직 추가 (트레일링 스탑 정상 작동 유도)
                    if holding_qty > 0 and cp > high_p:
                        high_p = cp
                        with db.get_connection() as conn:
                            conn.execute("UPDATE positions SET highest_price=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", 
                                         (high_p, "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk))

                    # 🚨 패치 4: 상태 관리 (연패 쿨다운 체크)
                    sig_state = db.get_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk)
                    if sig_state:
                        cooldown_ts = sig_state.get('cooldown_until_session')
                        if cooldown_ts and pd.to_datetime(cooldown_ts).tz_localize('UTC').tz_convert(KST) > now_kst:
                            continue # 쿨다운 기간이면 이 종목은 건너뜀

                    cp, action, score, reason = quant.evaluate_stock_for_ui(tk, strat, cfg, buy_p, high_p, cp, h_price, l_price, is_halted, days_held)

                    # 7. 매도 시그널 적재
                    if holding_qty > 0 and ("매도" in action or "🔴" in action):
                        now_str = now_kst.strftime('%H%M%S')
                        spec = quant.OrderSpec("", f"BOT_{scope_key}_{tk}_SELL_{now_str}", "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, db.CONTRACT['strategy_version'], db.CONTRACT['contract_version'], tk, item['종목명'], "SELL", "MARKET", holding_qty, 0, cp, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_kst.strftime('%Y-%m-%d %H:%M:%S'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                        db.safe_add_order_intent(spec)
                        print(f"🔥 [SELL SIGNAL] {item['종목명']} ({tk}) - {reason}")
                        
                        # 🚨 패치 4: 확정 손절 시 loss streak 증가 및 쿨다운 갱신
                        if "손절매" in reason or "트레일링스탑" in reason:
                            curr_streak = sig_state.get('loss_streak', 0) if sig_state else 0
                            new_streak = curr_streak + 1
                            cd_days = 3 if new_streak >= 3 else 0 # 3연패 시 3영업일 휴식 (시뮬레이션과 동일 규칙)
                            cd_until = (now_kst + datetime.timedelta(days=cd_days)).strftime('%Y-%m-%d %H:%M:%S') if cd_days > 0 else None
                            db.upsert_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk, {'loss_streak': new_streak, 'cooldown_until_session': cd_until})
                        
                        # 🚨 패치 3: 매도 후 continue 대신 밖으로 빠져나와서(다음 루프) 추가 매수 로직이 씹히지 않게 함
                        # 여기서는 continue를 쓰지 않고, else나 elif로 넘깁니다.

                    # 8. 매수(신규/추가) 시그널 적재
                    elif "매수" in action or "🟢" in action:
                        allow_amt = min(usable_cash, max(0.0, target_buy_amt - (holding_qty * cp)))
                        add_qty = int(allow_amt // (cp * db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05))) if cp > 0 else 0
                        
                        # 전체 한도(Exposure) 체크 (시뮬레이션 동일 규칙 적용)
                        current_exposure = sum([float(b['prpr']) * int(b['hldg_qty']) for b in b_res.data.get('holdings', [])])
                        max_exposure = total_eval * max_exposure_ratio
                        if current_exposure + (add_qty * cp) > max_exposure:
                            add_qty = int(max(0, max_exposure - current_exposure) // cp)

                        if add_qty > 0:
                            now_str = now_kst.strftime('%H%M%S')
                            spec = quant.OrderSpec("", f"BOT_{scope_key}_{tk}_BUY_{now_str}", "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, db.CONTRACT['strategy_version'], db.CONTRACT['contract_version'], tk, item['종목명'], "BUY", "MARKET", add_qty, 0, cp, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_kst.strftime('%Y-%m-%d %H:%M:%S'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                            db.safe_add_order_intent(spec)
                            print(f"🛒 [BUY SIGNAL] {item['종목명']} ({tk}) - {add_qty}주 (사유: {reason})")
                            usable_cash -= (add_qty * cp * 1.05)
                            
                            # 매수 후 승률 추적을 위해 loss streak 초기화
                            db.upsert_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk, {'loss_streak': 0})

        except Exception as e:
            print(f"🚨 [Signal Bot] Fatal Error in loop: {e}")
        
        # CPU 과부하 방지 및 API Rate Limit 존중
        time.sleep(30)

if __name__ == "__main__":
    run_signal_bot()
