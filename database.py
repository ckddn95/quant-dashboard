import sqlite3
import json
import os
import yaml
import hashlib
import shutil
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quant_system.db")
CONTRACT_PATH = os.path.join(BASE_DIR, "system_contract.yaml")

def load_contract():
    with open(CONTRACT_PATH, 'r', encoding='utf-8') as f: return yaml.safe_load(f)

CONTRACT = load_contract()
ALLOWED_TRANSITIONS = CONTRACT['allowed_state_transitions']

def get_connection():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    conn.execute('PRAGMA foreign_keys=ON;')
    conn.row_factory = sqlite3.Row
    return conn

def backup_db():
    if os.path.exists(DB_PATH):
        backup_path = f"{DB_PATH}.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        shutil.copy2(DB_PATH, backup_path)
        print(f"DB Backup created: {backup_path}")

def migrate_db():
    conn = get_connection()
    try:
        conn.execute("BEGIN EXCLUSIVE")
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        
        if v < 1:
            conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
            conn.execute("PRAGMA user_version = 1")
        if v < 2: conn.execute("PRAGMA user_version = 2")
        if v < 3: conn.execute("PRAGMA user_version = 3")
        if v < 4: conn.execute("PRAGMA user_version = 4")
        
        if v < 5:
            backup_db()
            print("Migrating DB to v5 (Signal Regime States)...")
            # 🛑 [Step 2 패치] 2연속 1분봉 확인 및 재진입(Rearm)을 추적하기 위한 테이블 신설
            conn.execute('''CREATE TABLE IF NOT EXISTS signal_states (
                            broker TEXT, environment TEXT, account_fingerprint TEXT, portfolio_id TEXT, strategy_id TEXT,
                            ticker TEXT, regime_id TEXT, current_signal TEXT, consecutive_count INTEGER DEFAULT 0,
                            last_updated TIMESTAMP,
                            PRIMARY KEY (broker, environment, account_fingerprint, portfolio_id, strategy_id, ticker))''')
            conn.execute("PRAGMA user_version = 5")
            
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"🚨 Migration Failed: {e}. System Halted.")
        raise
    finally: conn.close()

migrate_db()

def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row['value']) if row else default

def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn.execute("COMMIT")

def get_system_status(broker, env, account_fingerprint, portfolio_id):
    master_ks = bool(get_setting('master_kill_switch', False))
    acc_ks_key = f"kill_switch_{broker}_{env}_{account_fingerprint}_{portfolio_id}"
    acc_ks = bool(get_setting(acc_ks_key, False))
    acc_at_key = f"auto_trade_{broker}_{env}_{account_fingerprint}_{portfolio_id}"
    acc_ap_key = f"auto_pilot_{broker}_{env}_{account_fingerprint}_{portfolio_id}"
    
    return {
        "auto_trade": bool(get_setting(acc_at_key, False)),
        "auto_pilot": bool(get_setting(acc_ap_key, False)),
        "kill_switch": master_ks or acc_ks,
        "contract_version": CONTRACT['contract_version'],
        "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

# --- Watchlist, Positions, Order intents 등 기존 100% 동일 유지 ---
def get_watchlist(broker, env, account_id, portfolio_id, strategy_id):
    with get_connection() as conn: 
        return [{'티커': r['ticker'], '종목명': r['name']} for r in conn.execute("SELECT ticker, name FROM watchlist WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()]

def clear_and_update_watchlist(broker, env, account_id, portfolio_id, strategy_id, items):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM watchlist WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id))
            for item in items:
                conn.execute("INSERT INTO watchlist (broker, environment, account_id, portfolio_id, strategy_id, ticker, name, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                             (broker, env, account_id, portfolio_id, strategy_id, str(item['티커']).zfill(6), item['종목명'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.execute("COMMIT")
            return True, len(items)
        except Exception as e:
            conn.execute("ROLLBACK"); return False, str(e)

def get_positions(broker, env, account_id, portfolio_id, strategy_id):
    with get_connection() as conn: 
        return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()]

def sync_positions_from_broker(broker, env, account_id, portfolio_id, strategy_id, kis_stocks):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = set([r['ticker'] for r in conn.execute("SELECT ticker FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()])
            kis_tk = set([s['ticker'] for s in kis_stocks])
            
            for stock in kis_stocks:
                tk, b_qty, buy_p = stock['ticker'], stock['qty'], stock.get('buy_price', 0.0)
                row = conn.execute("SELECT managed_qty, manual_qty, unknown_quarantined_qty FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_id, portfolio_id, strategy_id, tk)).fetchone()
                if row:
                    diff = b_qty - (row['managed_qty'] + row['manual_qty'])
                    if diff != row['unknown_quarantined_qty']:
                        conn.execute("UPDATE positions SET broker_qty=?, unknown_quarantined_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (b_qty, diff, buy_p, broker, env, account_id, portfolio_id, strategy_id, tk))
                else:
                    conn.execute("INSERT INTO positions (broker, environment, account_id, portfolio_id, strategy_id, ticker, broker_qty, manual_qty, buy_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, account_id, portfolio_id, strategy_id, tk, b_qty, b_qty, buy_p, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            for tk in (existing - kis_tk):
                conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_id, portfolio_id, strategy_id, tk))
            conn.execute("COMMIT")
        except: conn.execute("ROLLBACK")

def get_locked_cash_and_qty(broker, env, account_id, portfolio_id, ticker=None):
    with get_connection() as conn:
        open_states = "('INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN')"
        cost_multiplier = 1.0 + CONTRACT['simulation_rules']['assumed_cost_pct_per_side']
        r1 = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * limit_price * ?) as locked_cash FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? AND side='BUY' AND status IN {open_states}", (cost_multiplier, broker, env, account_id, portfolio_id)).fetchone()
        locked_cash = float(r1['locked_cash']) if r1['locked_cash'] else 0.0
        locked_sell_qty = 0
        if ticker:
            r2 = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as locked_qty FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? AND ticker=? AND side='SELL' AND status IN {open_states}", (broker, env, account_id, portfolio_id, ticker)).fetchone()
            locked_sell_qty = int(r2['locked_qty']) if r2['locked_qty'] else 0
        return locked_cash, locked_sell_qty

def get_portfolio_creation_date(broker, env, account_id, portfolio_id):
    with get_connection() as conn:
        try:
            r1 = conn.execute("SELECT MIN(created_at) as d FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=?", (broker, env, account_id, portfolio_id)).fetchone()
            r2 = conn.execute("SELECT MIN(added_at) as d FROM watchlist WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?", (broker, env, account_id, portfolio_id)).fetchone()
            r3 = conn.execute("SELECT MIN(buy_date) as d FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?", (broker, env, account_id, portfolio_id)).fetchone()
            dates = [d for d in [r1['d'] if r1 else None, r2['d'] if r2 else None, r3['d'] if r3 else None] if d]
            if dates: return datetime.strptime(min(dates)[:10], '%Y-%m-%d').date()
        except: pass
    return None

def acquire_worker_lease(broker, env, account_id, portfolio_id, worker_id, ttl=30):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute("SELECT worker_id, token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?", (broker, env, account_id, portfolio_id)).fetchone()
            if not row or row['expires_at'] < now:
                nt = (row['token'] + 1) if row else 1
                conn.execute("INSERT OR REPLACE INTO worker_leases (broker, environment, account_id, portfolio_id, worker_id, expires_at, token) VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime', '+{} seconds'), ?)".format(ttl), (broker, env, account_id, portfolio_id, worker_id, nt))
                conn.execute("COMMIT"); return True, nt
            elif row['worker_id'] == worker_id:
                conn.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds') WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?".format(ttl), (broker, env, account_id, portfolio_id))
                conn.execute("COMMIT"); return True, row['token']
            conn.execute("ROLLBACK"); return False, 0
        except: conn.execute("ROLLBACK"); return False, 0

def safe_add_order_intent(spec):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            corr_id = spec.correlation_id if spec.correlation_id else f"{spec.broker}_{spec.environment}_{spec.account_fingerprint}_{spec.portfolio_id}_{spec.strategy_id}_{spec.ticker}_{spec.side}_{spec.intent_created_at}"
            corr_id = hashlib.sha256(corr_id.encode()).hexdigest()[:16]
            
            conn.execute("""INSERT INTO order_intents 
                (correlation_id, idempotency_key, broker, environment, account_fingerprint, product_code, portfolio_id, 
                 strategy_id, strategy_version, contract_version, ticker, stock_name, side, order_kind, 
                 qty, limit_price, reference_price, exchange, time_in_force, 
                 signal_id, signal_source, signal_cutoff, quote_id, quote_source, quote_timestamp, 
                 intent_ttl, cost_model_version, status, created_at, updated_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_CREATED', ?, ?)""", 
                (corr_id, spec.idempotency_key, spec.broker, spec.environment, spec.account_fingerprint, spec.account_product_code, spec.portfolio_id, 
                 spec.strategy_id, spec.strategy_version, CONTRACT['contract_version'], spec.ticker, spec.stock_name, spec.side, spec.order_kind, 
                 spec.quantity, spec.limit_price, spec.reference_price, spec.exchange, spec.time_in_force,
                 spec.signal_id, spec.signal_source, spec.signal_cutoff, spec.quote_id, spec.quote_source, spec.quote_timestamp,
                 spec.intent_ttl, CONTRACT.get('cost_model_version', '1.0.0'), spec.intent_created_at, spec.intent_created_at))
            conn.execute("COMMIT")
            return True, "OK"
        except sqlite3.IntegrityError: 
            conn.execute("ROLLBACK"); return False, "Idempotency Blocked"

def claim_next_order(broker, env, account_id, portfolio_id, worker_id, fencing_token):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT token FROM worker_leases WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND worker_id=?", (broker, env, account_id, portfolio_id, worker_id)).fetchone()
            if not lease or lease['token'] != fencing_token: conn.execute("ROLLBACK"); return None
            row = conn.execute("SELECT * FROM order_intents WHERE status = 'INTENT_CREATED' AND broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? ORDER BY id ASC LIMIT 1", (broker, env, account_id, portfolio_id)).fetchone()
            if row:
                conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=?, updated_at=? WHERE id=?", (fencing_token, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['id']))
                conn.execute("COMMIT"); return dict(row)
            conn.execute("ROLLBACK")
        except: conn.execute("ROLLBACK")
    return None

def transition_order_status(order_id, current_status, new_status, broker_id="", branch="", code=""):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []): return False
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if broker_id and branch:
                conn.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, broker_id, branch, code, now_str, order_id, current_status))
            else:
                conn.execute("UPDATE order_intents SET status=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, code, now_str, order_id, current_status))
            rows = conn.execute("SELECT changes()").fetchone()[0]
            conn.execute("COMMIT"); return rows > 0
        except: conn.execute("ROLLBACK"); return False

def get_orders_by_status_and_env(statuses, broker, env, account_id, portfolio_id):
    with get_connection() as conn:
        query = f"SELECT * FROM order_intents WHERE status IN ({','.join(['?']*len(statuses))}) AND broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=?"
        return [dict(r) for r in conn.execute(query, statuses + [broker, env, account_id, portfolio_id]).fetchall()]

def apply_fill_delta_exactly_once(order_id, ticker, order_type, broker, env, account_fingerprint, portfolio_id, strategy_id, new_cum_qty, new_cum_avg_price):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            o_row = conn.execute("SELECT qty, cum_filled_qty, avg_fill_price, status FROM order_intents WHERE id=? AND broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=?", (order_id, broker, env, account_fingerprint, portfolio_id)).fetchone()
            if not o_row: conn.execute("ROLLBACK"); return False
            
            delta_qty = new_cum_qty - o_row['cum_filled_qty']
            if delta_qty <= 0: conn.execute("ROLLBACK"); return False 
            
            new_status = 'FILLED' if new_cum_qty >= o_row['qty'] else 'PARTIALLY_FILLED'
            if o_row['status'] in ['CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCELED']: new_status = o_row['status'] 
            conn.execute("UPDATE order_intents SET cum_filled_qty=?, avg_fill_price=?, status=?, updated_at=? WHERE id=?", (new_cum_qty, new_cum_avg_price, new_status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), order_id))

            p_row = conn.execute("SELECT managed_qty, buy_price FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_fingerprint, portfolio_id, strategy_id, ticker)).fetchone()
            p_qty = p_row['managed_qty'] if p_row else 0
            p_buy = p_row['buy_price'] if p_row else 0.0

            if "BUY" in order_type.upper():
                delta_notional = (new_cum_qty * new_cum_avg_price) - (o_row['cum_filled_qty'] * o_row['avg_fill_price'])
                delta_fill_price = delta_notional / delta_qty if delta_qty > 0 else 0
                new_p_qty = p_qty + delta_qty
                new_p_buy = ((p_qty * p_buy) + (delta_qty * delta_fill_price)) / new_p_qty if new_p_qty > 0 else 0
                if p_row: conn.execute("UPDATE positions SET managed_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, new_p_buy, broker, env, account_fingerprint, portfolio_id, strategy_id, ticker))
                else: conn.execute("INSERT INTO positions (broker, environment, account_id, portfolio_id, strategy_id, ticker, managed_qty, buy_price, highest_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, account_fingerprint, portfolio_id, strategy_id, ticker, new_p_qty, new_p_buy, delta_fill_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            else: 
                new_p_qty = p_qty - delta_qty
                if new_p_qty < 0: 
                    conn.execute("UPDATE order_intents SET status='RECONCILIATION_REQUIRED' WHERE id=?", (order_id,))
                    conn.execute("UPDATE positions SET managed_qty=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, broker, env, account_fingerprint, portfolio_id, strategy_id, ticker))
                    conn.execute("COMMIT"); return False 
                if new_p_qty == 0: conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_fingerprint, portfolio_id, strategy_id, ticker))
                else: conn.execute("UPDATE positions SET managed_qty=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, broker, env, account_fingerprint, portfolio_id, strategy_id, ticker))
            conn.execute("COMMIT"); return True
        except: conn.execute("ROLLBACK"); return False

# 🛑 [Step 2 패치] Signal Regime DB 핸들러 추가
def get_signal_state(broker, env, acc_fp, port_id, strat_id, ticker):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM signal_states WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? AND strategy_id=? AND ticker=?", 
                           (broker, env, acc_fp, port_id, strat_id, ticker)).fetchone()
        return dict(row) if row else None

def update_signal_state(broker, env, acc_fp, port_id, strat_id, ticker, regime_id, signal, count):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO signal_states (broker, environment, account_fingerprint, portfolio_id, strategy_id, ticker, regime_id, current_signal, consecutive_count, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                     (broker, env, acc_fp, port_id, strat_id, ticker, regime_id, signal, count, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
