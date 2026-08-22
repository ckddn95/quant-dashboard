import os
import time
import datetime
import concurrent.futures
import pandas as pd
import math
import FinanceDataReader as fdr
import database as db
import broker.kis_client as kis
import quant_engine as quant

KST = datetime.timezone(datetime.timedelta(hours=9))

def get_last_friday_close():
    now = datetime.datetime.now(KST)
    days_since_friday = (now.weekday() - 4) % 7
    if days_since_friday == 0 and (now.hour * 100 + now.minute) < 1530:
        days_since_friday = 7
    last_fri = now - datetime.timedelta(days=days_since_friday)
    return last_fri.replace(hour=15, minute=30, second=0, microsecond=0)

def get_account_secrets(portfolio_id):
    try:
        try: import tomllib as toml
        except ImportError: import toml
        secrets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
        with open(secrets_path, "r", encoding="utf-8") as f: config = toml.loads(f.read())
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        c = config["kis_accounts"][acc_key]
        is_mock_raw = str(c.get("is_mock", "true")).strip().lower()
        if is_mock_raw not in ["true", "false"]: return None, None, None, True, "01", ""
        sys_secret = config.get("system", {}).get("hmac_secret")
        if not sys_secret or sys_secret == "fallback_default_secret" or str(sys_secret).strip() == "":
            print("🚨 [Security Alert] hmac_secret is missing!")
            return None, None, None, True, "01", ""
        return c["app_key"], c["app_secret"], str(c["cano"]).strip(), is_mock_raw == 'true', str(c.get("acnt_prdt", "01")).strip(), sys_secret
    except Exception as e:
        print(f"Secrets Error: {e}"); return None, None, None, True, "01", ""

def run_signal_bot():
    db.preflight_check()
    print("🤖 [Signal Bot] Daemon Started. Monitoring markets with DETERMINISTIC rules...")

    while True:
        try:
            now_kst = datetime.datetime.now(KST)
            hm = now_kst.hour * 100 + now_kst.minute

            if db.get_setting('master_kill_switch', False):
                print("🚨 [Signal Bot] Master Kill Switch is ON. Paused.")
                for strat_ks in [quant.Strategy.CORE, quant.Strategy.SATELLITE]:
                    app_k, _, c_ano, is_m, p_cd, sec = get_account_secrets(strat_ks.value)
                    if app_k:
                        fp_ks = db.generate_account_fingerprint(c_ano, sec)
                        db.request_cancel_for_system_orders("KIS", "MOCK" if is_m else "REAL", fp_ks, p_cd, strat_ks.value, strat_ks.value)
                time.sleep(10)
                continue

            for strat in [quant.Strategy.CORE, quant.Strategy.SATELLITE]:
                portfolio_id = strat.value
                sys_app_key, sys_app_sec, sys_cano, is_mock, sys_acnt_prdt, sys_secret = get_account_secrets(portfolio_id)
                
                if not sys_app_key:
                    continue

                env_str = "MOCK" if is_mock else "REAL"
                acc_fp = db.generate_account_fingerprint(sys_cano, sys_secret) 
                scope_key = f"KIS_{env_str}_{acc_fp}_{sys_acnt_prdt}_{strat.value}_{strat.value}"
                
                db.set_setting(f"heartbeat_bot_{scope_key}", now_kst.strftime('%Y-%m-%d %H:%M:%S'))

                if db.get_setting(f"kill_switch_{scope_key}", False):
                    db.request_cancel_for_system_orders("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value)

                if not db.get_setting(f"auto_pilot_{scope_key}", False):
                    continue

                cfg = quant.get_default_config(strat)
                
                last_fri_dt = get_last_friday_close()
                last_scan_key = f"last_auto_scan_{scope_key}"
                last_scan_str = db.get_setting(last_scan_key, "1970-01-01 00:00:00")
                last_scan_dt = datetime.datetime.strptime(last_scan_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
                
                if last_scan_dt < last_fri_dt and now_kst >= last_fri_dt:
                    print(f"🔄 [Signal Bot] Running Weekly Auto-Scan for {strat.value} (Friday Close)...")
                    scan_df = quant.run_scanner_safe(strat, cfg)
                    if not scan_df.empty:
                        new_items = [{'티커': str(r['티커']).zfill(6), '종목명': r['종목명']} for _, r in scan_df.iterrows()]
                        db.clear_and_update_watchlist("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, new_items, source="SYSTEM", provenance="AUTO_WEEKLY_SCAN")
                    db.set_setting(last_scan_key, now_kst.strftime('%Y-%m-%d %H:%M:%S'))

                if env_str == "REAL" and not (900 <= hm <= 1530):
                    continue 

                token, err = kis.get_kis_access_token(sys_app_key, sys_app_sec, is_mock)
                if not token:
                    continue

                b_res = kis.fetch_kis_account_balance(sys_app_key, sys_app_sec, sys_cano, sys_acnt_prdt, token, is_mock)
                if b_res.state != "SUCCESS_DATA": continue
                
                c_res = kis.fetch_kis_orderable_cash(sys_app_key, sys_app_sec, sys_cano, sys_acnt_prdt, token, "", 0, "MARKET", is_mock)
                raw_cash = float(c_res.data) if c_res.state == "SUCCESS_DATA" else 0.0
                
                locked_cash, _ = db.get_locked_cash_and_qty("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value)
                usable_cash = max(0.0, raw_cash - locked_cash)
                
                summary = b_res.data.get('summary', [])
                total_eval = float(summary[0]['tot_evlu_amt']) if summary else 0.0
                
                last_principal_key = f"last_principal_{scope_key}"
                last_principal = db.get_setting(last_principal_key, total_eval)
                daily_pnl_pct = (total_eval - last_principal) / last_principal if last_principal > 0 else 0.0
                
                is_bull_market = False
                if cfg.boost:
                    try:
                        idx_tk = 'KS11' if strat == quant.Strategy.CORE else 'KQ11'
                        start_d = (now_kst - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
                        market_df = fdr.DataReader(idx_tk, start=start_d)
                        if not market_df.empty and len(market_df) >= 200:
                            ma200 = market_df['Close'].rolling(200).mean().iloc[-1]
                            is_bull_market = market_df['Close'].iloc[-1] > ma200
                    except Exception as e:
                        print(f"⚠️ [Signal Bot] Market Regime Check Error: {e}")

                boost_addon = db.CONTRACT.get('booster_policy', {}).get('value', 0.10) if (cfg.boost and is_bull_market) else 0.0
                max_exposure_ratio = 0.90 + boost_addon
                target_buy_amt = total_eval * cfg.alloc if total_eval > 0 else 1000000.0

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
                evaluations = []

                for item in eval_list:
                    time.sleep(0.07)
                    
                    tk = item['티커']
                    m_qty = db_positions[tk]['managed_qty'] if tk in db_positions else 0
                    manual_qty = db_positions[tk]['manual_qty'] if tk in db_positions else 0
                    buy_p = db_positions[tk]['buy_price'] if tk in db_positions else 0.0
                    high_p = db_positions[tk]['highest_price'] if tk in db_positions else 0.0
                    
                    buy_dt = pd.to_datetime(db_positions[tk]['buy_date']).tz_localize('UTC').tz_convert(KST) if tk in db_positions else now_kst
                    days_held = len(pd.bdate_range(start=buy_dt.date(), end=now_kst.date())) - 1 if tk in db_positions else 0
                    
                    kis_qty = stocks_held.get(tk, 0)
                    
                    if m_qty > 0 or manual_qty > 0:
                        if kis_qty != (m_qty + manual_qty):
                            print(f"⚠️ [Reconciliation Alert] {item['종목명']} ({tk}) 브로커수량({kis_qty}) != DB수량({m_qty}+{manual_qty}). 매매 보류.")
                            try:
                                db.insert_reconciliation_event("KIS", env_str, acc_fp, sys_acnt_prdt, portfolio_id, portfolio_id, 0, "POSITION_MISMATCH", f"KIS: {kis_qty}, DB(m:{m_qty}+u:{manual_qty})")
                            except: pass
                            continue
                            
                    bot_sell_qty = m_qty

                    p_res = kis.fetch_kis_current_price_ext(sys_app_key, sys_app_sec, tk, token, is_mock)
                    if p_res.state != "SUCCESS_DATA": continue
                        
                    try:
                        cp = float(p_res.data['price'])
                        h_price = float(p_res.data['high'])
                        l_price = float(p_res.data['low'])
                        is_halted = p_res.data['is_halted']
                        
                        if math.isinf(cp) or math.isnan(cp) or cp <= 0: continue
                        if math.isinf(h_price) or math.isnan(h_price) or h_price <= 0: continue
                        if math.isinf(l_price) or math.isnan(l_price) or l_price <= 0: continue
                        if l_price > h_price or cp > h_price or cp < l_price: continue
                        if not isinstance(is_halted, bool): is_halted = True
                    except (ValueError, TypeError, KeyError):
                        continue 

                    if bot_sell_qty > 0 and cp > high_p:
                        high_p = cp
                        with db.get_connection() as conn:
                            conn.execute("UPDATE positions SET highest_price=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", 
                                         (high_p, "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk))

                    sig_state = db.get_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk)
                    is_cooldown = False
                    if sig_state:
                        cooldown_ts = sig_state.get('cooldown_until_session')
                        if cooldown_ts and pd.to_datetime(cooldown_ts).tz_localize('UTC').tz_convert(KST) > now_kst:
                            is_cooldown = True

                    cp, action, score, reason = quant.evaluate_stock_for_ui(tk, strat, cfg, buy_p, high_p, cp, h_price, l_price, is_halted, days_held)
                    
                    if is_cooldown and ("매수" in action or "🟢" in action):
                        action = "🟡 쿨다운 기간 (매수 보류)"
                        score = 0.0
                        reason = "최근 2연속 손실로 인한 거래 휴식"

                    is_sl_or_ts = "STOP_LOSS" in action or "TRAILING_STOP" in action
                    is_trend_exit = "TREND_EXIT" in action
                    is_buy_signal = "매수" in action or "🟢" in action

                    fire_order = False
                    target_side = None

                    # 🚨 패치 7-B: 손절 및 트레일링스탑은 1분봉 2개 확인 없이 즉시(Immediate) 타격
                    if is_sl_or_ts and bot_sell_qty > 0:
                        fire_order = True
                        target_side = "SELL"
                    elif (is_trend_exit or is_buy_signal) and (bot_sell_qty > 0 or is_buy_signal):
                        # 🚨 실제 KIS 1분봉 바(Bar) API를 조회하여 closed=True인 완성된 봉 2개의 bar_end_time을 검증
                        try:
                            chart_res = kis.fetch_kis_minute_chart(sys_app_key, sys_app_sec, tk, token, is_mock)
                            if chart_res.state == "SUCCESS_DATA" and len(chart_res.data) >= 2:
                                bars = chart_res.data
                                bar1, bar2 = bars[-2], bars[-1] # 최근 완료된 두 개의 1분봉
                                
                                t1_str = f"{bar1.get('stck_bsop_date', now_kst.strftime('%Y%m%d'))} {bar1.get('stck_cntg_hour', '000000')}"
                                t2_str = f"{bar2.get('stck_bsop_date', now_kst.strftime('%Y%m%d'))} {bar2.get('stck_cntg_hour', '000000')}"
                                
                                bar_time_1 = datetime.datetime.strptime(t1_str, '%Y%m%d %H%M%S').replace(tzinfo=KST)
                                bar_time_2 = datetime.datetime.strptime(t2_str, '%Y%m%d %H%M%S').replace(tzinfo=KST)
                                
                                # 당일 거래일 기준 검증 및 두 완성봉의 시간 간격이 1분 이상 벌어져 있는지(중복 틱/조작 방지) 엄격히 검증
                                same_trade_date = (bar_time_1.date() == now_kst.date() and bar_time_2.date() == now_kst.date())
                                distinct_bars = (bar_time_2 > bar_time_1) and ((bar_time_2 - bar_time_1).total_seconds() >= 60)
                                
                                target_signal_type = 'BUY' if is_buy_signal else 'TREND_EXIT'
                                last_recorded_bar = sig_state.get('last_distinct_bar_timestamp') if sig_state else None
                                
                                if same_trade_date and distinct_bars:
                                    if last_recorded_bar != bar_time_2.strftime('%Y-%m-%d %H:%M:%S'):
                                        fire_order = True
                                        target_side = "BUY" if is_buy_signal else "SELL"
                                        db.upsert_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk, {
                                            'current_signal': target_signal_type, 
                                            'consecutive_count': 2, 
                                            'last_distinct_bar_timestamp': bar_time_2.strftime('%Y-%m-%d %H:%M:%S')
                                        })
                                    else:
                                        print(f"⏳ [Bar Already Processed] {item['종목명']} ({tk}) 이미 처리된 1분봉입니다.")
                                else:
                                    print(f"⏳ [Waiting for 2 Distinct Completed Bars] {item['종목명']} ({tk}) 조건 미충족 (봉 간격 부족 또는 날짜 상이)")
                            else:
                                print(f"⚠️ [Bar Fetch Warning] {item['종목명']} ({tk}) 1분봉 데이터 조회 실패 또는 개수 부족")
                        except Exception as ex:
                            print(f"⚠️ [Bar Validation Error] {ex}")
                    else:
                        if sig_state and (sig_state.get('current_signal') != 'NONE'):
                            db.upsert_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk, {'current_signal': 'NONE', 'consecutive_count': 0})

                    if fire_order:
                        evaluations.append({
                            'tk': tk, 'name': item['종목명'], 'bot_sell_qty': bot_sell_qty, 'total_qty': kis_qty, 'cp': cp,
                            'target_side': target_side, 'score': score, 'reason': reason, 'sig_state': sig_state
                        })

                def sort_priority(x):
                    is_sell = 1 if x['target_side'] == "SELL" else 0
                    is_buy = 1 if x['target_side'] == "BUY" else 0
                    return (-is_sell, -is_buy, -x['score'], x['tk'])
                    
                evaluations.sort(key=sort_priority)

                for ev in evaluations:
                    tk, name, bot_sell_qty, total_qty, cp = ev['tk'], ev['name'], ev['bot_sell_qty'], ev['total_qty'], ev['cp']
                    target_side, reason, sig_state = ev['target_side'], ev['reason'], ev['sig_state']
                    
                    if target_side == "SELL":
                        now_str = now_kst.strftime('%H%M%S')
                        spec = quant.OrderSpec("", f"BOT_{scope_key}_{tk}_SELL_{now_str}", "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, db.CONTRACT['strategy_version'], db.CONTRACT['contract_version'], tk, name, "SELL", "MARKET", bot_sell_qty, 0, cp, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_kst.strftime('%Y-%m-%d %H:%M:%S'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                        db.safe_add_order_intent(spec)
                        print(f"🔥 [SELL SIGNAL] {name} ({tk}) - {reason}")
                        
                        db.upsert_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk, {'current_signal': 'NONE', 'consecutive_count': 0})
                            
                    elif target_side == "BUY":
                        allow_amt = min(usable_cash, max(0.0, target_buy_amt - (total_qty * cp)))
                        add_qty = int(allow_amt // (cp * db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05))) if cp > 0 else 0
                        
                        current_exposure = sum([float(b['prpr']) * int(b['hldg_qty']) for b in b_res.data.get('holdings', [])])
                        max_exposure = total_eval * max_exposure_ratio
                        if current_exposure + (add_qty * cp) > max_exposure:
                            add_qty = int(max(0, max_exposure - current_exposure) // cp)

                        if add_qty > 0:
                            now_str = now_kst.strftime('%H%M%S')
                            spec = quant.OrderSpec("", f"BOT_{scope_key}_{tk}_BUY_{now_str}", "KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, db.CONTRACT['strategy_version'], db.CONTRACT['contract_version'], tk, name, "BUY", "MARKET", add_qty, 0, cp, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_kst.strftime('%Y-%m-%d %H:%M:%S'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                            db.safe_add_order_intent(spec)
                            print(f"🛒 [BUY SIGNAL] {name} ({tk}) - {add_qty}주 (사유: {reason})")
                            
                            usable_cash -= (add_qty * cp * 1.05)
                            db.upsert_signal_state("KIS", env_str, acc_fp, sys_acnt_prdt, strat.value, strat.value, tk, {'current_signal': 'NONE', 'consecutive_count': 0})

        except Exception as e:
            print(f"🚨 [Signal Bot] Fatal Error in loop: {e}")
        
        time.sleep(30)

if __name__ == "__main__":
    run_signal_bot()