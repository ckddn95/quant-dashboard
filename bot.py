import time
import logging
import uuid
import datetime
import traceback
import os
import pandas as pd
try: import tomllib as toml
except ImportError: import toml
import database as db
import broker.kis_client as kis
import quant_engine as quant

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SignalBot")

def get_account_secrets(portfolio_id):
    """🚨 Streamlit 종속성을 완벽히 제거한 독립적인 strict TOML 파서"""
    try:
        secrets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
        with open(secrets_path, "rb") as f: config = toml.load(f)
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        c = config["kis_accounts"][acc_key]
        
        is_mock_raw = str(c.get("is_mock", "true")).strip().lower()
        if is_mock_raw not in ["true", "false"]:
            logger.error(f"HALTED_CONFIG_ERROR: Invalid is_mock value '{is_mock_raw}'")
            return None, None, None, True, "01"
            
        return c["app_key"], c["app_secret"], str(c["cano"]).strip(), is_mock_raw == 'true', str(c.get("acnt_prdt", "01")).strip()
    except Exception as e:
        logger.error(f"Secrets Load Error: {e}")
        return None, None, None, True, "01"

def auto_update_watchlist(env, acc_fp, portfolio_id, strat, cfg):
    """✅ 지시사항 6.3: 완전 무인 종목 발굴 (매주 유니버스 스캔 후 Watchlist 갱신, 보유종목은 강제매도 안함)"""
    now_kst = datetime.datetime.now(quant.KST)
    last_scan_week = db.get_setting(f"last_scan_week_{env}_{portfolio_id}")
    current_week = f"{now_kst.year}-W{now_kst.isocalendar()[1]}"
    
    if last_scan_week == current_week: return

    logger.info(f"[{portfolio_id}] 주간 완전 무인 유니버스 스캔 시작 ({current_week})")
    krx = quant.load_krx_universe()
    if krx.empty: return
    
    cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(200) if strat == quant.Strategy.CORE else krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].sort_values('Marcap', ascending=False).head(150)
    
    new_items = [{'티커': str(r['Code']).strip().zfill(6), '종목명': r['Name']} for _, r in cands.iterrows()]
    db.clear_and_update_watchlist("KIS", env, acc_fp, portfolio_id, portfolio_id, new_items)
    db.set_setting(f"last_scan_week_{env}_{portfolio_id}", current_week)
    logger.info(f"[{portfolio_id}] 유니버스 자동 갱신 완료 (총 {len(new_items)}개 편입)")

def run_bot_loop():
    logger.info("📡 Signal Bot 가동 시작 (시장 감시 및 INTENT 생성 전담)")
    kis_tokens = {}

    while True:
        try:
            now_kst = datetime.datetime.now(quant.KST)
            # 15:30 이후 신규 진입/청산 락
            if now_kst.weekday() >= 5 or now_kst.hour < 9 or now_kst.hour >= 15 or (now_kst.hour == 15 and now_kst.minute >= 30):
                time.sleep(60); continue 

            for portfolio_id in ["CORE", "SATELLITE"]:
                strat = quant.Strategy(portfolio_id)
                cfg = quant.get_default_config(strat)
                
                app_key, app_sec, cano, is_mock, acnt_prdt = get_account_secrets(portfolio_id)
                if not app_key: continue
                
                env = "MOCK" if is_mock else "REAL"
                acc_fp = db.hashlib.sha256(cano.encode()).hexdigest()[:16] if cano != "MOCK_ACCOUNT" else "MOCK_ACCOUNT"
                
                sys_status = db.get_system_status("KIS", env, acc_fp, portfolio_id)
                if sys_status['kill_switch'] or (not sys_status['auto_pilot'] and env == "REAL"): continue

                auto_update_watchlist(env, acc_fp, portfolio_id, strat, cfg)

                token_key = f"{env}_{portfolio_id}"
                if token_key not in kis_tokens or kis_tokens[token_key]['expire'] < time.time():
                    t, _ = kis.get_kis_access_token(app_key, app_sec, is_mock)
                    if t: kis_tokens[token_key] = {'token': t, 'expire': time.time() + (3600 * 12)}
                    else: continue

                b_res = kis.fetch_kis_account_balance(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], is_mock)
                if b_res['status'] != "SUCCESS": continue
                bal_h, bal_s = b_res['holdings'], b_res['summary']
                
                raw_cash = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], "", 0, "00", is_mock)
                
                total_eval = float(bal_s[0]['tot_evlu_amt']) if bal_s else 0.0
                if total_eval <= 0: continue
                
                locked_cash, _ = db.get_locked_cash_and_qty("KIS", env, acc_fp, portfolio_id)
                usable_cash = max(0.0, raw_cash - locked_cash)
                
                daily_pnl_pct = float(bal_s[0]['evlu_pfls_smtl_amt']) / (total_eval - float(bal_s[0]['evlu_pfls_smtl_amt'])) if total_eval > float(bal_s[0]['evlu_pfls_smtl_amt']) else 0.0

                boost_addon = db.CONTRACT.get('booster_policy', {}).get('value', 0.10) if cfg.boost else 0.0
                target_max_exposure = total_eval * (1.0 + boost_addon)

                ctx = quant.RiskContext(
                    account_id=acc_fp, env=env, usable_cash=usable_cash, locked_buy_cash=locked_cash, managed_sell_qty=0,
                    current_exposure=sum([float(b['prpr']) * int(b['hldg_qty']) for b in bal_h]),
                    max_exposure=target_max_exposure, daily_pnl_pct=daily_pnl_pct, 
                    is_kill_switch_on=sys_status['kill_switch'], is_auto_trade_on=sys_status['auto_trade']
                )

                positions = db.get_positions("KIS", env, acc_fp, portfolio_id, portfolio_id)
                watchlist = db.get_watchlist("KIS", env, acc_fp, portfolio_id, portfolio_id)
                targets = list(set([p['ticker'] for p in positions] + [w['티커'] for w in watchlist]))

                for tk in targets:
                    tk = str(tk).zfill(6)
                    p_res = kis.fetch_kis_current_price_ext(app_key, app_sec, tk, kis_tokens[token_key]['token'], is_mock)
                    if p_res['status'] != "SUCCESS": continue
                    cp, hp, lp, is_halted = p_res['price'], p_res['high'], p_res['low'], p_res['is_halted']

                    snap = quant.StockSnapshot(tk, cp, hp, lp, 0, 0, 0, True, p_res['received_at'], "KIS", True, False, "OK", p_res['executable'])
                    snap.validate(is_halted, max_ttl_sec=db.CONTRACT['execution_rules']['quote_freshness_ttl_sec'])
                    if not snap.is_valid: continue
                    
                    current_bar_ts = p_res['broker_time'].replace(second=0, microsecond=0)

                    # ✅ 지시사항 6.1: 가짜 지표 완벽 소거. FDR 기반 T-1 완료 일봉 지표 획득
                    ma20, ma60, ma200, m60_up = 0.0, 0.0, 0.0, True
                    try:
                        start_d = (now_kst - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
                        end_d = (now_kst - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
                        cache_key = f"{tk}_{end_d}"
                        if cache_key in quant._fdr_cache: df = quant._fdr_cache[cache_key]
                        else: 
                            df = fdr.DataReader(tk, start=start_d, end=end_d)
                            quant._fdr_cache[cache_key] = df
                        if not df.empty and len(df) >= 200:
                            ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
                            ma60 = float(df['Close'].rolling(60).mean().iloc[-1])
                            ma200 = float(df['Close'].rolling(200).mean().iloc[-1])
                            m60_up = float(ma60) > float(df['Close'].rolling(60).mean().iloc[-11])
                        else: continue # 완료 일봉 데이터 없으면 신규 신호 차단
                    except Exception as e: continue 

                    p_row = next((x for x in positions if x['ticker'] == tk), None)
                    sig_state = db.get_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk) or {}
                    
                    managed_qty = p_row['managed_qty'] if p_row else 0
                    buy_price = p_row['buy_price'] if p_row else 0.0
                    
                    stored_hp = sig_state.get('highest_price', 0.0)
                    highest_price = max(stored_hp, cp)
                    db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'highest_price': highest_price})

                    if managed_qty > 0:
                        ctx.managed_sell_qty = managed_qty
                        days_held = (now_kst.date() - datetime.datetime.strptime(p_row['buy_date'], '%Y-%m-%d %H:%M:%S').date()).days
                        is_sell, _, reason = quant.calc_sell_signal(strat, cfg, cp, hp, lp, cp, buy_price, highest_price, days_held, ma20, ma60)
                        
                        if is_sell:
                            if reason in [quant.ExitReason.STOP_LOSS, quant.ExitReason.TRAILING_STOP]:
                                fire = True # 즉각 발동
                            else:
                                prev_ts_str = sig_state.get('last_distinct_bar_timestamp', '')
                                prev_sig = sig_state.get('current_signal', '')
                                count = sig_state.get('consecutive_count', 0)
                                
                                # 서로 다른 1분봉인지 Timestamp 비교 검증
                                if prev_sig == reason.value and prev_ts_str and datetime.datetime.strptime(prev_ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST) < current_bar_ts:
                                    count += 1
                                elif prev_sig != reason.value: count = 1
                                    
                                db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': reason.value, 'consecutive_count': count, 'last_distinct_bar_timestamp': current_bar_ts.strftime('%Y-%m-%d %H:%M:%S')})
                                fire = (count >= 2)

                            if fire:
                                event_id = str(uuid.uuid4().hex[:8])
                                # ✅ 지시사항 3.1: 멱등성 키 강화
                                idem_key = f"SIG_{env}_{acc_fp}_{portfolio_id}_{tk}_SELL_{reason.value}_{event_id}"
                                spec = quant.OrderSpec("", idem_key, "KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, "1.0", db.CONTRACT.get('contract_version', '1.0.0'), tk, "", "SELL", "MARKET", managed_qty, 0, cp, "KRX", "GTC", event_id, "SYSTEM", now_kst.strftime('%H%M'), "Q", "KIS", now_kst.strftime('%H%M'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT.get('cost_model_version', '2.2.0'), now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                                if quant.pre_flight_risk_check(spec, snap, ctx)[0]:
                                    db.safe_add_order_intent(spec)
                                    db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'NONE', 'consecutive_count': 0, 'rearm_state': 0})
                        continue

                    # 미보유 중 (매수 및 추가매수 검사)
                    is_buy, _, _ = quant.calc_buy_signal(strat, cfg, cp, ma20, ma60, ma200, m60_up)
                    if is_buy:
                        rearm = bool(sig_state.get('rearm_state', 1))
                        if not rearm: continue # 조건 풀리기 전 진입/추가매수 차단 (독립 재무장 필수)

                        cd_until = sig_state.get('cooldown_until_session', '')
                        if cd_until and datetime.datetime.strptime(cd_until, '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST) > now_kst:
                            continue

                        prev_ts_str = sig_state.get('last_distinct_bar_timestamp', '')
                        prev_sig = sig_state.get('current_signal', '')
                        count = sig_state.get('consecutive_count', 0)
                        
                        if prev_sig == "BUY" and prev_ts_str and datetime.datetime.strptime(prev_ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST) < current_bar_ts:
                            count += 1
                        elif prev_sig != "BUY": count = 1
                            
                        db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'BUY', 'consecutive_count': count, 'last_distinct_bar_timestamp': current_bar_ts.strftime('%Y-%m-%d %H:%M:%S')})

                        if count >= 2:
                            stock_limit = total_eval * cfg.alloc
                            held_val = (p_row['managed_qty'] * cp) if p_row else 0.0
                            room = max(0.0, stock_limit - held_val) # 한도 내 추가매수 계산
                            
                            alloc_amt = min(ctx.usable_cash, max(0, ctx.max_exposure - ctx.current_exposure), room)
                            buy_qty = int(alloc_amt // (cp * db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)))
                            
                            if buy_qty > 0:
                                event_id = str(uuid.uuid4().hex[:8])
                                idem_key = f"SIG_{env}_{acc_fp}_{portfolio_id}_{tk}_BUY_REGIME_{event_id}"
                                spec = quant.OrderSpec("", idem_key, "KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, "1.0", db.CONTRACT.get('contract_version', '1.0.0'), tk, "", "BUY", "MARKET", buy_qty, 0, cp, "KRX", "GTC", event_id, "SYSTEM", now_kst.strftime('%H%M'), "Q", "KIS", now_kst.strftime('%H%M'), db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT.get('cost_model_version', '2.2.0'), now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                                if quant.pre_flight_risk_check(spec, snap, ctx)[0]:
                                    db.safe_add_order_intent(spec)
                                    db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'NONE', 'consecutive_count': 0, 'rearm_state': 0})
                    else:
                        db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'NONE', 'consecutive_count': 0, 'rearm_state': 1})

            time.sleep(15)
        except Exception as e:
            logger.error(f"Bot Error: {e}")
            logger.error(traceback.format_exc())
            time.sleep(10)

if __name__ == "__main__":
    run_bot_loop()