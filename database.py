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
    'FILLED': [], 'REJECTED': [], 'RISK_REJECTED': [], 'CANCELED': [], 'EXPIRED': [], 'QUARANTINED': [], 'RECONCILIATION_REQUIRED': []
}

def get_connection():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=20)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    conn.execute('PRAGMA foreign_keys=ON;')
    conn.row_factory = sqlite3.Row
    return conn

def migrate_db():
    conn = get_connection()
    try:
        conn.execute("BEGIN EXCLUSIVE")
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        
        if v < 1:
            conn.execute('''CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)''')
            conn.execute('''CREATE TABLE watchlist (ticker TEXT PRIMARY KEY, name TEXT, account_id TEXT, env TEXT, added_at TIMESTAMP)''')
            conn.execute('''CREATE TABLE order_intents (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE,
                            account_id TEXT, environment TEXT, portfolio_id TEXT, strategy_id TEXT,
                            ticker TEXT, order_type TEXT, qty INTEGER, price REAL, status TEXT DEFAULT 'INTENT_CREATED', 
                            broker_order_id TEXT, branch_no TEXT, cum_filled_qty INTEGER DEFAULT 0, avg_fill_price REAL DEFAULT 0.0,
                            resp_code TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)''')
            conn.execute('''CREATE TABLE positions (
                            account_id TEXT, environment TEXT, portfolio_id TEXT, ticker TEXT, 
                            broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0,
                            buy_price REAL, highest_price REAL, buy_date TIMESTAMP,
                            PRIMARY KEY (account_id, environment, portfolio_id, ticker))''')
            conn.execute('''CREATE TABLE worker_leases (
                            account_id TEXT, environment TEXT, worker_id TEXT, expires_at TIMESTAMP, token INTEGER DEFAULT 0,
                            PRIMARY KEY (account_id, environment))''')
            conn.execute("PRAGMA user_version = 1")
            
        if v < 2:
            conn.execute("UPDATE order_intents SET status='QUARANTINED' WHERE account_id IS NULL OR account_id='UNKNOWN'")
            conn.execute("PRAGMA user_version = 2")
            
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"🚨 Migration Failed: {e}. System Halted.")
        raise
    finally:
        conn.close()

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

def clear_and_update_watchlist(account_id, env, items):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM watchlist WHERE account_id=? AND env=?", (account_id, env))
            for item in items:
                conn.execute("INSERT INTO watchlist (ticker, name, account_id, env, added_at) VALUES (?, ?, ?, ?, ?)", 
                             (str(item['티커']).zfill(6), item['종목명'], account_id, env, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.execute("COMMIT")
            return True, len(items)
        except Exception as e:
            conn.execute("ROLLBACK")
            return False, str(e)

def acquire_worker_lease(account_id, env, worker_id, ttl=30):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute("SELECT worker_id, token, expires_at FROM worker_leases WHERE account_id=? AND environment=?", (account_id, env)).fetchone()
            
            if not row or row['expires_at'] < now:
                new_token = (row['token'] + 1) if row else 1
                conn.execute("INSERT OR REPLACE INTO worker_leases (account_id, environment, worker_id, expires_at, token) VALUES (?, ?, ?, datetime('now', 'localtime', '+{} seconds'), ?)".format(ttl), (account_id, env, worker_id, new_token))
                conn.execute("COMMIT")
                return True, new_token
            elif row['worker_id'] == worker_id:
                conn.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds') WHERE account_id=? AND environment=?".format(ttl), (account_id, env))
                conn.execute("COMMIT")
                return True, row['token']
                
            conn.execute("ROLLBACK")
            return False, 0
        except:
            conn.execute("ROLLBACK")
            return False, 0

def safe_add_order_intent(spec):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO order_intents 
                (correlation_id, idempotency_key, account_id, environment, portfolio_id, strategy_id, ticker, order_type, qty, price, status, created_at, updated_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_CREATED', ?, ?)""", 
                (spec.correlation_id, spec.idempotency_key, spec.account_id, spec.environment, spec.portfolio_id, spec.strategy_id, 
                 spec.ticker, spec.side, spec.quantity, spec.limit_price, spec.intent_created_at, spec.intent_created_at))
            conn.execute("COMMIT")
            return True, "OK"
        except sqlite3.IntegrityError: 
            conn.execute("ROLLBACK")
            return False, "Idempotency 차단 (중복 주문)"

def claim_next_order(account_id, env, worker_id, fencing_token):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT token FROM worker_leases WHERE account_id=? AND environment=? AND worker_id=?", (account_id, env, worker_id)).fetchone()
            if not lease or lease['token'] != fencing_token:
                conn.execute("ROLLBACK"); return None

            row = conn.execute("SELECT * FROM order_intents WHERE status = 'INTENT_CREATED' AND account_id=? AND environment=? ORDER BY id ASC LIMIT 1", (account_id, env)).fetchone()
            if row:
                conn.execute("UPDATE order_intents SET status='CLAIMED', updated_at=? WHERE id=?", (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['id']))
                conn.execute("COMMIT")
                return dict(row)
            conn.execute("ROLLBACK")
        except: conn.execute("ROLLBACK")
    return None

def transition_order_status(order_id, current_status, new_status, broker_id="", branch="", code=""):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []): return False
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, resp_code=?, updated_at=? WHERE id=? AND status=?", 
                         (new_status, broker_id, branch, code, now_str, order_id, current_status))
            rows = conn.execute("SELECT changes()").fetchone()[0]
            conn.execute("COMMIT")
            return rows > 0
        except:
            conn.execute("ROLLBACK")
            return False

def apply_fill_delta_exactly_once(order_id, ticker, order_type, account_id, env, portfolio_id, new_cum_qty, new_cum_avg_price):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            o_row = conn.execute("SELECT qty, cum_filled_qty, avg_fill_price, status FROM order_intents WHERE id=? AND account_id=? AND environment=?", (order_id, account_id, env)).fetchone()
            if not o_row: conn.execute("ROLLBACK"); return False
            
            delta_qty = new_cum_qty - o_row['cum_filled_qty']
            if delta_qty <= 0: conn.execute("ROLLBACK"); return False 
            
            new_status = 'FILLED' if new_cum_qty >= o_row['qty'] else 'PARTIALLY_FILLED'
            if o_row['status'] in ['CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCELED']: new_status = o_row['status'] 
            conn.execute("UPDATE order_intents SET cum_filled_qty=?, avg_fill_price=?, status=?, updated_at=? WHERE id=?", 
                         (new_cum_qty, new_cum_avg_price, new_status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), order_id))

            p_row = conn.execute("SELECT managed_qty, manual_qty, buy_price FROM positions WHERE account_id=? AND environment=? AND portfolio_id=? AND ticker=?", (account_id, env, portfolio_id, ticker)).fetchone()
            p_qty = p_row['managed_qty'] if p_row else 0
            p_buy = p_row['buy_price'] if p_row else 0.0

            if "BUY" in order_type.upper():
                delta_notional = (new_cum_qty * new_cum_avg_price) - (o_row['cum_filled_qty'] * o_row['avg_fill_price'])
                delta_fill_price = delta_notional / delta_qty if delta_qty > 0 else 0
                new_p_qty = p_qty + delta_qty
                new_p_buy = ((p_qty * p_buy) + (delta_qty * delta_fill_price)) / new_p_qty if new_p_qty > 0 else 0
                if p_row: conn.execute("UPDATE positions SET managed_qty=?, buy_price=? WHERE account_id=? AND environment=? AND portfolio_id=? AND ticker=?", (new_p_qty, new_p_buy, account_id, env, portfolio_id, ticker))
                else: conn.execute("INSERT INTO positions (account_id, environment, portfolio_id, ticker, managed_qty, buy_price, highest_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (account_id, env, portfolio_id, ticker, new_p_qty, new_p_buy, delta_fill_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            else: 
                new_p_qty = p_qty - delta_qty
                if new_p_qty < 0: 
                    conn.execute("UPDATE order_intents SET status='RECONCILIATION_REQUIRED' WHERE id=?", (order_id,))
                    conn.execute("ROLLBACK"); return False 
                if new_p_qty == 0 and (not p_row or p_row['manual_qty'] == 0): 
                    conn.execute("DELETE FROM positions WHERE account_id=? AND environment=? AND portfolio_id=? AND ticker=?", (account_id, env, portfolio_id, ticker))
                else: 
                    conn.execute("UPDATE positions SET managed_qty=? WHERE account_id=? AND environment=? AND portfolio_id=? AND ticker=?", (new_p_qty, account_id, env, portfolio_id, ticker))
            conn.execute("COMMIT")
            return True
        except:
            conn.execute("ROLLBACK"); return False

def get_orders_by_status_and_env(statuses, account_id, env):
    with get_connection() as conn:
        query = f"SELECT * FROM order_intents WHERE status IN ({','.join(['?']*len(statuses))}) AND account_id=? AND environment=?"
        return [dict(r) for r in conn.execute(query, statuses + [account_id, env]).fetchall()]
        
# 🛑 [수정 사항] UI가 뻗지 않도록 무조건 '티커', '종목명' 한글 키값으로 반환
def get_watchlist(account_id, env):
    with get_connection() as conn: 
        return [{'티커': r['ticker'], '종목명': r['name']} for r in conn.execute("SELECT ticker, name FROM watchlist WHERE account_id=? AND env=?", (account_id, env)).fetchall()]

def get_positions(account_id, env, portfolio_id):
    with get_connection() as conn: return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE account_id=? AND environment=? AND portfolio_id=?", (account_id, env, portfolio_id)).fetchall()]

def get_locked_cash_and_qty(account_id, env, portfolio_id, ticker=None):
    with get_connection() as conn:
        open_states = "('INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED')"
        r1 = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * price) as locked_cash FROM order_intents WHERE account_id=? AND environment=? AND portfolio_id=? AND order_type='BUY' AND status IN {open_states}", (account_id, env, portfolio_id)).fetchone()
        locked_cash = float(r1['locked_cash']) if r1['locked_cash'] else 0.0
        locked_sell_qty = 0
        if ticker:
            r2 = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as locked_qty FROM order_intents WHERE account_id=? AND environment=? AND portfolio_id=? AND ticker=? AND order_type='SELL' AND status IN {open_states}", (account_id, env, portfolio_id, ticker)).fetchone()
            locked_sell_qty = int(r2['locked_qty']) if r2['locked_qty'] else 0
        return locked_cash, locked_sell_qty
