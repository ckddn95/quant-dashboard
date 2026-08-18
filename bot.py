import time
import logging
import sys
import uuid
import datetime
import traceback
import database as db
import broker.kis_client as kis
import quant_engine as quant

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("SignalBot")

KST = datetime.timezone(datetime.timedelta(hours=9))

def get_account_secrets(portfolio_id):
    import streamlit as st
    try:
        acc_key = "core" if portfolio_id == "CORE" else "satellite"
        config = st.secrets["kis_accounts"][acc_key]
        return config["app_key"], config["app_secret"], str(config["cano"]).strip(), str(config.get("is_mock", "True")).lower() == 'true'
    except Exception: return None, None, None, True

def run_bot_loop():
    logger.info("📡 Signal Bot 가동 시작 (시장 감시 및 INTENT 생성 전담)")
    kis_tokens = {}

    while True:
        try:
            now_kst = datetime.datetime.now(KST)
            # 시장 시간 체크 (보수적)
            if now_kst.weekday() >= 5 or now_kst.hour < 9 or now_kst.hour >= 16:
                time.sleep(60); continue

            for portfolio_id in ["CORE", "SATELLITE"]:
                strat = quant.Strategy(portfolio_id)
                cfg = quant.get_default_config(strat)
                
                app_key, app_sec, cano, is_mock = get_account_secrets(portfolio_id)
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

                # 리스크 컨텍스트 생성 (자산/현금 조회)
                bal_h, bal_s, _ = kis.fetch_kis_account_balance(app_key, app_sec, cano, "01", kis_tokens[token_key]['token'], is_mock)
                raw_cash = kis.fetch_kis_orderable_cash(app_key, app_sec, cano, "01", kis_tokens[token_key]['token'], is_mock)
                
                total_eval = float(bal_s[0]['tot_evlu_amt']) if bal_s else 10000000.0
                locked_cash, _ = db.get_locked_cash_and_qty("KIS", env, acc_fp, portfolio_id)
                usable_cash = max(0.0, raw_cash - locked_cash)
                
                # 강세장 부스터 (ABSOLUTE_ADDITION +10%p)
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

                    # 스냅샷 생성
                    snap = quant.StockSnapshot(tk, cp, hp, lp, 0, 0, 0, True, now_kst, "KIS", True, False, "OK", True)
                    # 1분봉 대사용 가상 Timestamp
                    current_bar_ts = now_kst.replace(second=0, microsecond=0)

                    p_row = next((x for x in positions if x['ticker'] == tk), None)
                    sig_state = db.get_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk) or {}
                    
                    managed_qty = p_row['managed_qty'] if p_row else 0
                    buy_price = p_row['buy_price'] if p_row else 0.0
                    highest_price = max(p_row['highest_price'], cp) if p_row else cp

                    # (1) 보유 중: 매도 평가
                    if managed_qty > 0:
                        ctx.managed_sell_qty = managed_qty
                        is_sell, _, reason = quant.calc_sell_signal(strat, cfg, cp, hp, lp, cp, buy_price, highest_price, 5, cp, cp) # MA는 근사치 처리 (Intraday 엔진 한계상 임시 조치)
                        
                        if is_sell:
                            # 즉각 판정 (손절, 트레일링) vs 버퍼 검증 (추세이탈)
                            if reason in [quant.ExitReason.STOP_LOSS, quant.ExitReason.TRAILING_STOP]:
                                fire = True
                            else:
                                # TREND_EXIT: 2연속 분봉 확인
                                prev_ts_str = sig_state.get('last_updated', '')
                                prev_sig = sig_state.get('current_signal', '')
                                count = sig_state.get('consecutive_count', 0)
                                
                                if prev_sig == reason.value and prev_ts_str and datetime.datetime.strptime(prev_ts_str, '%Y-%m-%d %H:%M:%S') < current_bar_ts:
                                    count += 1
                                else: count = 1
                                db.update_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, "REGIME", reason.value, count)
                                fire = (count >= 2)

                            if fire:
                                spec = quant.OrderSpec("", f"SIG_SELL_{tk}_{now_kst.strftime('%H%M')}", "KIS", env, acc_fp, "01", portfolio_id, portfolio_id, "1.0", db.CONTRACT['contract_version'], tk, "", "SELL", "MARKET", managed_qty, 0, cp, "KRX", "GTC", "BOT", "SYSTEM", now_kst.strftime('%H%M'), "Q", "KIS", now_kst.strftime('%H%M'), 300, db.CONTRACT['cost_model_version'], now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                                if quant.pre_flight_risk_check(spec, snap, ctx)[0]:
                                    db.safe_add_order_intent(spec)
                                    db.update_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, "REGIME", "NONE", 0) # 상태 초기화
                        continue

                    # (2) 미보유 중: 매수 평가
                    is_buy, _, _ = quant.calc_buy_signal(strat, cfg, cp, cp*0.9, cp*0.8, cp*0.7, True) # 임시 지표
                    if is_buy:
                        rearm = bool(sig_state.get('rearm_state', 1))
                        if not rearm: continue # 조건이 한 번 이탈될 때까지 매수 불가

                        prev_ts_str = sig_state.get('last_updated', '')
                        prev_sig = sig_state.get('current_signal', '')
                        count = sig_state.get('consecutive_count', 0)
                        
                        if prev_sig == "BUY" and prev_ts_str and datetime.datetime.strptime(prev_ts_str, '%Y-%m-%d %H:%M:%S') < current_bar_ts:
                            count += 1
                        else: count = 1
                        db.update_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, "REGIME", "BUY", count)

                        if count >= 2:
                            # 추가 매수 / 신규 매수 한도 및 수량 산출
                            target_amt = total_eval * cfg.alloc
                            buy_qty = int((target_amt) // (cp * 1.05))
                            
                            if buy_qty > 0:
                                spec = quant.OrderSpec("", f"SIG_BUY_{tk}_{now_kst.strftime('%H%M')}", "KIS", env, acc_fp, "01", portfolio_id, portfolio_id, "1.0", db.CONTRACT['contract_version'], tk, "", "BUY", "MARKET", buy_qty, 0, cp, "KRX", "GTC", "BOT", "SYSTEM", now_kst.strftime('%H%M'), "Q", "KIS", now_kst.strftime('%H%M'), 300, db.CONTRACT['cost_model_version'], now_kst.strftime('%Y-%m-%d %H:%M:%S'))
                                if quant.pre_flight_risk_check(spec, snap, ctx)[0]:
                                    db.safe_add_order_intent(spec)
                                    db.update_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, "REGIME", "NONE", 0)
                    else:
                        # 매수 조건 해제 시 Rearm 초기화
                        db.update_signal_state("KIS", env, acc_fp, portfolio_id, portfolio_id, tk, "REGIME", "NONE", 0)

            time.sleep(30)
        except Exception as e:
            logger.error(f"Bot Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_bot_loop()