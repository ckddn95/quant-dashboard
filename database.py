import sqlite3
import json
import uuid
import os
from datetime import datetime
from quant_engine import OrderSpec

DB_PATH = os.path.abspath("quant_system.db")

ALLOWED_TRANSITIONS = {
    'INTENT_CREATED': ['CLAIMED', 'CANCELED', 'QUARANTINED'],
    'CLAIMED': ['SUBMITTING', 'RISK_REJECTED', 'CANCELED', 'EXPIRED'],
    'SUBMITTING': ['ACKNOWLEDGED', 'UNKNOWN', 'REJECTED'],
    'ACKNOWLEDGED': ['PARTIALLY_FILLED', 'FILLED', 'CANCEL_REQUESTED', 'EXPIRED'],
    'UNKNOWN': ['ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'REJECTED', 'CANCEL_REQUESTED', 'EXPIRED', 'RECONCILIATION_REQUIRED'],
    'PARTIALLY_FILLED': ['FILLED', 'CANCEL_REQUESTED', 'EXPIRED'],
    'CANCEL_REQUESTED': ['CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN'],
    'CANCEL_ACKNOWLEDGED': ['CANCELED', 'PARTIALLY_FILLED', 'FILLED'],
    'FILLED': [], 'REJECTED': [], 'RISK_REJECTED': [], 'CANCELED': [], 'EXPIRED': [], 'QUARANTINED': []
}

def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA busy_timeout=5000;')
        conn.execute('PRAGMA foreign_keys=ON;')
    except: pass
    conn.row_factory = sqlite3.Row
    return conn

def migrate_db():
    with get_connection() as conn:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v < 1:
            conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (ticker TEXT PRIMARY KEY, name TEXT, added_at TIMESTAMP)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS order_intents (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE,
                            ticker TEXT, order_type TEXT, qty INTEGER, price REAL, status TEXT DEFAULT 'INTENT_CREATED', 
                            broker_order_id TEXT, branch_no TEXT, cum_filled_qty INTEGER DEFAULT 0, avg_fill_price REAL DEFAULT 0.0,
                            resp_code TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS positions (
                            ticker TEXT PRIMARY KEY, qty INTEGER, buy_price REAL, highest_price REAL, buy_date TIMESTAMP)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS worker_leases (
                            account_id TEXT PRIMARY KEY, worker_id TEXT, expires_at TIMESTAMP, token INTEGER DEFAULT 0)''')
            conn.execute("PRAGMA user_version = 1")
        if v < 2:
            c = conn.cursor()
            try:
                c.execute("ALTER TABLE order_intents ADD COLUMN account_id TEXT DEFAULT 'UNKNOWN'")
                c.execute("ALTER TABLE order_intents ADD COLUMN environment TEXT DEFAULT 'UNKNOWN'")
                c.execute("ALTER TABLE order_intents ADD COLUMN portfolio_id TEXT DEFAULT 'DEFAULT'")
                c.execute("ALTER TABLE order_intents ADD COLUMN strategy_id TEXT DEFAULT 'UNKNOWN'")
                c.execute("ALTER TABLE positions ADD COLUMN managed_qty INTEGER DEFAULT 0")
                c.execute("ALTER TABLE positions ADD COLUMN manual_qty INTEGER DEFAULT 0")
                c.execute("UPDATE order_intents SET status='QUARANTINED' WHERE status IN ('INTENT_CREATED', 'CLAIMED') AND account_id='UNKNOWN'")
            except: pass
            c.execute("DELETE FROM settings WHERE key IN ('manual_app_key', 'manual_app_secret', 'manual_cano', 'manual_is_mock')")
            conn.execute("PRAGMA user_version = 2")
        conn.commit()
migrate_db()

def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row['value']) if row else default

def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn.commit()

def get_system_status():
    if get_setting('halted_config_error', False): return "HALTED_CONFIG_ERROR"
    if get_setting('kill_switch', False): return "KILL_SWITCH"
    return "OK"

def acquire_worker_lease(account_id, worker_id, ttl_seconds=30):
    with get_connection() as conn:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute("DELETE FROM worker_leases WHERE expires_at < ?", (now,))
        try:
            c.execute("INSERT INTO worker_leases (account_id, worker_id, expires_at, token) VALUES (?, ?, datetime('now', 'localtime', '+{} seconds'), 1)".format(ttl_seconds), (account_id, worker_id))
            conn.commit()
            return True, 1
        except sqlite3.IntegrityError:
            row = c.execute("SELECT worker_id, token FROM worker_leases WHERE account_id=?", (account_id,)).fetchone()
            if row and row['worker_id'] == worker_id:
                new_token = row['token'] + 1
                c.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds'), token=? WHERE account_id=? AND worker_id=?".format(ttl_seconds), (new_token, account_id, worker_id))
                conn.commit()
                return True, new_token
            return False, 0

def safe_add_order_intent(spec: OrderSpec) -> tuple[bool, str]:
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO order_intents 
                (correlation_id, idempotency_key, account_id, environment, portfolio_id, strategy_id, ticker, order_type, qty, price, status, created_at, updated_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_CREATED', ?, ?)""", 
                (str(uuid.uuid4()), spec.idempotency_key, spec.account_id, spec.environment, spec.portfolio_id, spec.strategy_id, 
                 str(spec.ticker).zfill(6), spec.side, spec.quantity, spec.limit_price, spec.intent_created_at, spec.intent_created_at))
            conn.commit()
            return True, "OK"
    except sqlite3.IntegrityError: return False, "Idempotency 차단 (중복 주문 방어)"
    except Exception as e: return False, str(e)

def claim_next_order(account_id, env):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM order_intents WHERE status = 'INTENT_CREATED' AND account_id=? AND environment=? ORDER BY id ASC LIMIT 1", (account_id, env)).fetchone()
        if row:
            conn.execute("UPDATE order_intents SET status='CLAIMED', updated_at=? WHERE id=?", (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['id']))
            conn.commit()
            return dict(row)
        conn.commit()
    except sqlite3.Error: conn.rollback()
    finally: conn.close()
    return None

def transition_order_status(order_id, current_status, new_status, broker_id=None, branch=None, code=None):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []): raise ValueError(f"Invalid state transition: {current_status} -> {new_status}")
    with get_connection() as conn:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if broker_id and branch: 
            conn.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, broker_id, branch, code, now_str, order_id, current_status))
        else: 
            conn.execute("UPDATE order_intents SET status=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, code, now_str, order_id, current_status))
        conn.commit()
        return conn.total_changes > 0

def apply_fill_delta_exactly_once(order_id, ticker, order_type, new_cum_qty, new_cum_avg_price):
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        o_row = conn.execute("SELECT qty, cum_filled_qty, avg_fill_price, status FROM order_intents WHERE id=?", (order_id,)).fetchone()
        if not o_row: return False
        
        delta_qty = new_cum_qty - o_row['cum_filled_qty']
        if delta_qty <= 0: return False 
        
        new_status = 'FILLED' if new_cum_qty >= o_row['qty'] else 'PARTIALLY_FILLED'
        if o_row['status'] in ['CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCELED']: new_status = o_row['status'] 
        conn.execute("UPDATE order_intents SET cum_filled_qty=?, avg_fill_price=?, status=?, updated_at=? WHERE id=?", (new_cum_qty, new_cum_avg_price, new_status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), order_id))

        p_row = conn.execute("SELECT managed_qty, buy_price FROM positions WHERE ticker=?", (ticker,)).fetchone()
        p_qty, p_buy = p_row['managed_qty'] if p_row else 0, p_row['buy_price'] if p_row else 0.0

        if "BUY" in order_type.upper():
            delta_notional = (new_cum_qty * new_cum_avg_price) - (o_row['cum_filled_qty'] * o_row['avg_fill_price'])
            delta_fill_price = delta_notional / delta_qty if delta_qty > 0 else 0
            new_p_qty = p_qty + delta_qty
            new_p_buy = ((p_qty * p_buy) + (delta_qty * delta_fill_price)) / new_p_qty if new_p_qty > 0 else 0
            if p_row: conn.execute("UPDATE positions SET qty=qty+?, managed_qty=?, buy_price=? WHERE ticker=?", (delta_qty, new_p_qty, new_p_buy, ticker))
            else: conn.execute("INSERT INTO positions (ticker, qty, managed_qty, buy_price, highest_price, buy_date) VALUES (?, ?, ?, ?, ?, ?)", (ticker, delta_qty, new_p_qty, new_p_buy, delta_fill_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        else: 
            new_p_qty = p_qty - delta_qty
            if new_p_qty <= 0: conn.execute("DELETE FROM positions WHERE ticker=? AND manual_qty=0", (ticker,))
            else: conn.execute("UPDATE positions SET qty=qty-?, managed_qty=? WHERE ticker=?", (delta_qty, new_p_qty, ticker))
        conn.commit()
        return True

def get_orders_by_status_and_env(statuses, account_id, env):
    with get_connection() as conn:
        query = f"SELECT * FROM order_intents WHERE status IN ({','.join(['?']*len(statuses))}) AND account_id=? AND environment=?"
        return [dict(r) for r in conn.execute(query, statuses + [account_id, env]).fetchall()]

def get_watchlist():
    with get_connection() as conn: return [{'티커': r['ticker'], '종목명': r['name']} for r in conn.execute("SELECT ticker, name FROM watchlist").fetchall()]
def add_to_watchlist(ticker, name):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO watchlist (ticker, name, added_at) VALUES (?, ?, ?)", (str(ticker).zfill(6), name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
def get_positions():
    with get_connection() as conn: return [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]
def get_locked_cash_and_qty(account_id, env, ticker=None):
    with get_connection() as conn:
        open_states = "('INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED')"
        r1 = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * price) as locked_cash FROM order_intents WHERE account_id=? AND environment=? AND order_type='BUY' AND status IN {open_states}", (account_id, env)).fetchone()
        locked_cash = float(r1['locked_cash']) if r1['locked_cash'] else 0.0
        locked_sell_qty = 0
        if ticker:
            r2 = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as locked_qty FROM order_intents WHERE account_id=? AND environment=? AND ticker=? AND order_type='SELL' AND status IN {open_states}", (account_id, env, ticker)).fetchone()
            locked_sell_qty = int(r2['locked_qty']) if r2['locked_qty'] else 0
        return locked_cash, locked_sell_qty

def sync_positions_from_broker(kis_stocks):
    pass
