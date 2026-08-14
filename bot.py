import time
import datetime
import database as db
import quant_engine as qe
from broker.kis_client import KISClient

KST = datetime.timezone(datetime.timedelta(hours=9))

def run_bot():
    db.init_db()
    print(f"🤖 [SQLite Engine] 무인 매매 봇 가동 시작: {datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        try:
            now = datetime.datetime.now(KST)
            is_weekend = now.weekday() >= 5
            is_market_hours = datetime.time(9, 0) <= now.time() <= datetime.time(15, 30)
            
            if is_weekend or not is_market_hours:
                time.sleep(60)
                continue

            cfg_dict = db.get_config("MAIN_PORT")
            if not cfg_dict or cfg_dict.get('kill_switch'): 
                time.sleep(60); continue

            # 1. KIS Client 검증 (P0)
            app_key, app_secret, cano, prdt = cfg_dict.get('app_key'), cfg_dict.get('app_secret'), cfg_dict.get('cano'), cfg_dict.get('prdt_cd')
            if not app_key: 
                time.sleep(60); continue
                
            try: kis = KISClient(app_key, app_secret, cano, prdt, is_mock=bool(cfg_dict.get('is_mock', 1)))
            except PermissionError as e:
                print(e); time.sleep(60); continue # 실계좌 차단

            # 2. 대기 중인 주문 의도(Intent) 집행 (UI에서 쏜 수동주문 등)
            pending_orders = db.get_pending_orders()
            for order in pending_orders:
                db.update_order_status(order['order_id'], "SUBMITTING")
                res = kis.execute_order(order['ticker'], order['intent_qty'], order['intent_price'], order['side'], is_market=True)
                db.update_order_status(order['order_id'], res.status, res.filled_qty, res.filled_price, res.msg)
                if res.status == "FILLED" and order['side'] == "SELL":
                    db.update_position(order['ticker'], order['stock_name'], 0, 0.0, 0.0, 0) # 매도 시 포지션 초기화

            # 3. 자동 스캔 및 전략 판정 (오토파일럿 켜진 경우)
            if cfg_dict.get('auto_pilot'):
                mkt_snap = qe.fetch_market_snapshot()
                strat_cfg = qe.StrategyConfig(
                    cfg_dict['strategy_name'], bool(cfg_dict['use_ma200_filter']), cfg_dict['ma_buffer_pct'], 
                    cfg_dict['stop_loss_pct'], cfg_dict['ts_target_pct'], cfg_dict['ts_drop_pct'], cfg_dict['max_alloc_pct']
                )

                # 매도 감시
                _, bal_summary = kis.fetch_account_balance()
                real_cash = float(bal_summary.get('dnca_tot_amt', 0)) if bal_summary else 0.0
                
                with sqlite3.connect(db.DB_PATH) as conn:
                    conn.row_factory = sqlite3.Row
                    watchlist = [dict(r) for r in conn.execute("SELECT * FROM watchlist WHERE port_name='MAIN_PORT'").fetchall()]
                
                for w in watchlist:
                    ticker = w['ticker']
                    snap = qe.fetch_stock_snapshot(ticker)
                    if not snap: continue
                    
                    pos_dict = db.get_position(ticker)
                    if pos_dict and pos_dict['managed_qty'] > 0:
                        # 매도 판정
                        pos = qe.PositionState(ticker, pos_dict['managed_qty'], pos_dict['avg_fill_price'], pos_dict['highest_price'], bool(pos_dict['trailing_armed']))
                        exit_cond, reason, new_pos = qe.evaluate_exit(snap, pos, strat_cfg)
                        db.update_position(ticker, w['stock_name'], new_pos.managed_qty, new_pos.avg_fill_price, new_pos.highest_price, new_pos.trailing_armed)
                        
                        if exit_cond and cfg_dict.get('auto_trade_enabled'):
                            db.log_order_intent(ticker, w['stock_name'], "SELL", new_pos.managed_qty, snap.close)
                    else:
                        # 매수 판정
                        cd_dict = db.get_position(ticker)
                        cd_until = datetime.datetime.strptime(cd_dict['cooldown_until'], '%Y-%m-%d').date() if cd_dict and cd_dict['cooldown_until'] else datetime.date(2000, 1, 1)
                        if now.date() < cd_until: continue

                        sig = qe.generate_signal(snap, mkt_snap, strat_cfg)
                        if sig['entry'] and cfg_dict.get('auto_trade_enabled'):
                            target_amt = 10000000 * strat_cfg.max_alloc_pct # 임시 투자원금 1천만
                            add_qty = int(target_amt // snap.close)
                            if add_qty > 0 and real_cash >= (add_qty * snap.close):
                                db.log_order_intent(ticker, w['stock_name'], "BUY", add_qty, snap.close)

        except Exception as e:
            print(f"[{datetime.datetime.now(KST)}] 스캔 오류 발생: {e}")
            
        time.sleep(60)

if __name__ == "__main__":
    import sqlite3
    run_bot()
