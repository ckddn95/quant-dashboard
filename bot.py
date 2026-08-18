import time
import logging
import uuid
import datetime
import traceback
import os
try: import toml
except ImportError: import tomllib as toml # Python 3.11+ fallback
import database as db
import broker.kis_client as kis
import quant_engine as quant

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SignalBot")

def get_account_secrets(portfolio_id):
    """🚨 Streamlit 종속성을 제거한 독립적인 TOML 파서"""
    try:
        secrets_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
        with open(secrets_path, "r", encoding="utf-8") as f:
            config = toml.load(f) if hasattr(toml, 'load') else toml.loads(f.read())
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        c = config["kis_accounts"][acc_key]
        return c["app_key"], c["app_secret"], str(c["cano"]).strip(), str(c.get("is_mock", "true")).lower() == 'true', str(c.get("acnt_prdt", "01")).strip()
    except Exception as e:
        logger.error(f"Secrets Load Error: {e}")
        return None, None, None, True, "01"

def run_bot_loop():
    logger.info("📡 Signal Bot 가동 시작 (시장 감시 및 INTENT 생성 전담)")
    kis_tokens = {}

    while True:
        try:
            now_kst = datetime.datetime.now(quant.KST)
            if now_kst.weekday() >= 5 or now_kst.hour < 9 or now_kst.hour >= 16:
                time.sleep(60); continue

            for portfolio_id in ["CORE", "SATELLITE"]:
                strat = quant.Strategy(portfolio_id)
                cfg = quant.get_default_config(strat)
                
                app_key, app_sec, cano, is_mock, acnt_prdt = get_account_secrets(portfolio_id)
                if not app_key: continue
                
                env = "MOCK" if is_mock else "REAL"
                acc_fp = db.hashlib.sha256(cano.encode()).hexdigest()[:16] if cano != "MOCK_ACCOUNT" else "MOCK_ACCOUNT"
                
                sys_status = db.get_system_status("KIS", env, acc_fp, portfolio_id)
                if sys_status['kill_switch'] or (not sys_status['auto_pilot'] and env == "REAL"):
                    continue

                token_key = f"{env}_{portfolio_id}"
                if token_key not in kis_tokens or kis_tokens[token_key]['expire'] < time.time():
                    t, _ = kis.get_kis_access_token(app_key, app_sec, is_mock)
                    if t: kis_tokens[token_key] = {'token': t, 'expire': time.time() + (3600 * 12)}
                    else: continue

                bal_h, bal_s, _ = kis.fetch_kis_account_balance(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], is_mock)
                raw_cash = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, acnt_prdt, kis_tokens[token_key]['token'], "", 0, "00", is_mock)
                
                total_eval = float(bal_s[0]['tot_evlu_amt']) if bal_s else 10000000.0
                locked_cash, _ = db.get_locked_cash_and_qty("KIS", env, acc_fp, portfolio_id)
                usable_cash = max(0.0, raw_cash - locked_cash)
                
                # 강세장 부스터 계좌 한도 산출
                boost_addon = db.CONTRACT.get('booster_policy', {}).get('value', 0.10) if cfg.boost else 0.0
                target_max_exposure = total_eval * (1.0 + boost_addon)

                ctx = quant.RiskContext(
                    account_id=acc_fp, env=env, usable_cash=usable_cash, locked_buy_cash=locked_cash, managed_sell_qty=0,
                    current_exposure=sum([float(b['prpr']) * int(b['hldg_qty']) for b in bal_h]),
                    max_exposure=target_max_exposure, daily_pnl_pct=0.0, 
                    is_kill_switch_on=sys_status['kill_switch'], is_auto_trade_on=sys_status['auto_trade']
                )

                positions = db.get_positions("KIS", env, acc_fp, portfolio_id, portfolio_id)
                watchlist = db.get_watchlist("KIS", env, acc_fp, portfolio_id, portfolio_id)
                targets = list(set([p['ticker'] for p in positions] + [w['티커'] for w in watchlist]))

                for tk in targets:
                    tk = str(tk).zfill(6)
                    cp, hp, lp, halted, _ = kis.fetch_kis_current_price_ext(app_key, app_sec, tk, kis_tokens[token_key]['token'], is_mock)
                    if cp <= 0: continue

                    snap = quant.StockSnapshot(tk, cp, hp, lp, 0, 0, 0, True, now_kst, "KIS", True, False, "OK", True)
                    current_bar_ts = now_kst.replace(second=0, microsecond=0) # KIS 분봉 시간 대체

                    p_row = next((x for x in positions if x['ticker'] == tk), None)
                    sig_state = db.get_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk) or {}
                    
                    managed_qty = p_row['managed_qty'] if p_row else 0
                    buy_price = p_row['buy_price'] if p_row else 0.0
                    
                    # 최고가 갱신 및 DB 저장
                    stored_hp = sig_state.get('highest_price', 0.0)
                    highest_price = max(stored_hp, cp)
                    db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'highest_price': highest_price})

                    if managed_qty > 0:
                        ctx.managed_sell_qty = managed_qty
                        is_sell, _, reason = quant.calc_sell_signal(strat, cfg, cp, hp, lp, cp, buy_price, highest_price, 5, cp, cp)
                        
                        if is_sell:
                            if reason in [quant.ExitReason.STOP_LOSS, quant.ExitReason.TRAILING_STOP]:
                                fire = True
                            else:
                                prev_ts_str = sig_state.get('last_distinct_bar_timestamp', '')
                                prev_sig = sig_state.get('current_signal', '')
                                count = sig_state.get('consecutive_count', 0)
                                
                                # 🚨 서로 다른 1분봉인지 Timestamp 비교 검증
                                if prev_sig == reason.value and prev_ts_str and datetime.datetime.strptime(prev_ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST) < current_bar_ts:
                                    count += 1
                                elif prev_sig != reason.value:
                                    count = 1
                                    
                                db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': reason.value, 'consecutive_count': count, 'last_distinct_bar_timestamp': current_bar_ts.strftime('%Y-%m-%d %H:%M:%S')})
                                fire = (count >= 2)

                            if fire:
                                spec = quant.OrderSpec("", f"SIG_SELL_{tk}_{reason.value}_{now_kst.strftime('%H%M%S')}", "KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, "1.0", db.CONTRACT.get('contract_version', '1.0.0'), tk, "", "SELL", "MARKET", managed_qty, 0, cp, "KRX", "GTC", "BOT", "SYSTEM", now_kst.strftime('%H%M'), "Q", "KIS", now_kst.strftime('%H%M'), 300, db.CONTRACT.get('cost_model_version', '2.1.0'), now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                                if quant.pre_flight_risk_check(spec, snap, ctx)[0]:
                                    db.safe_add_order_intent(spec)
                                    db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'NONE', 'consecutive_count': 0, 'rearm_state': 0}) # 매도 후 무조건 Rearm 대기
                        continue

                    # 매수 및 추가 매수
                    is_buy, _, _ = quant.calc_buy_signal(strat, cfg, cp, cp*0.9, cp*0.8, cp*0.7, True)
                    if is_buy:
                        rearm = bool(sig_state.get('rearm_state', 1))
                        if not rearm: continue # 매수 조건이 한 번 풀리기 전까지 진입 차단

                        # 쿨다운 검사
                        cd_until = sig_state.get('cooldown_until_session', '')
                        if cd_until and datetime.datetime.strptime(cd_until, '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST) > now_kst:
                            continue

                        prev_ts_str = sig_state.get('last_distinct_bar_timestamp', '')
                        prev_sig = sig_state.get('current_signal', '')
                        count = sig_state.get('consecutive_count', 0)
                        
                        if prev_sig == "BUY" and prev_ts_str and datetime.datetime.strptime(prev_ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=quant.KST) < current_bar_ts:
                            count += 1
                        elif prev_sig != "BUY":
                            count = 1
                            
                        db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'BUY', 'consecutive_count': count, 'last_distinct_bar_timestamp': current_bar_ts.strftime('%Y-%m-%d %H:%M:%S')})

                        if count >= 2:
                            stock_limit = total_eval * cfg.alloc
                            held_val = (p_row['managed_qty'] * cp) if p_row else 0.0
                            room = max(0.0, stock_limit - held_val)
                            
                            alloc_amt = min(ctx.usable_cash, max(0, ctx.max_exposure - ctx.current_exposure), room)
                            buy_qty = int(alloc_amt // (cp * db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)))
                            
                            if buy_qty > 0:
                                spec = quant.OrderSpec("", f"SIG_BUY_{tk}_{now_kst.strftime('%H%M%S')}", "KIS", env, acc_fp, acnt_prdt, portfolio_id, portfolio_id, "1.0", db.CONTRACT.get('contract_version', '1.0.0'), tk, "", "BUY", "MARKET", buy_qty, 0, cp, "KRX", "GTC", "BOT", "SYSTEM", now_kst.strftime('%H%M'), "Q", "KIS", now_kst.strftime('%H%M'), 300, db.CONTRACT.get('cost_model_version', '2.1.0'), now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                                if quant.pre_flight_risk_check(spec, snap, ctx)[0]:
                                    db.safe_add_order_intent(spec)
                                    db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'NONE', 'consecutive_count': 0, 'rearm_state': 0}) # 주문 제출 시 재무장 대기 상태로 전환
                    else:
                        # 매수 조건 해제 시 Rearm 상태 1(True)로 복구
                        db.upsert_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, {'current_signal': 'NONE', 'consecutive_count': 0, 'rearm_state': 1})

            time.sleep(15)
        except Exception as e:
            logger.error(f"Bot Error: {e}")
            logger.error(traceback.format_exc())
            time.sleep(10)

if __name__ == "__main__":
    run_bot_loop()