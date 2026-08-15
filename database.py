import sqlite3
import json
import uuid
from datetime import datetime

DB_PATH = "quant_system.db"

ALLOWED_TRANSITIONS = {
    'INTENT_CREATED': ['CLAIMED', 'CANCELED'],
    'CLAIMED': ['SUBMITTING', 'CANCELED'],
    'SUBMITTING': ['ACKNOWLEDGED', 'UNKNOWN', 'REJECTED'],
    'ACKNOWLEDGED': ['PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED'],
    'UNKNOWN': ['ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'REJECTED', 'CANCELED', 'EXPIRED'],
    'PARTIALLY_FILLED': ['FILLED', 'CANCELED', 'EXPIRED'],
    'FILLED': [], 'REJECTED': [], 'CANCELED': [], 'EXPIRED': []
}

def get_connection():
    # 🛑 [핵심 패치 1] SQLite WAL 모드 및 busy_timeout 5초(5000ms) 적용
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA busy_timeout=5000;')
    except: pass
    conn.row_factory = sqlite3.Row
    return conn

def migrate_db():
    with get_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("SELECT value FROM settings WHERE key='strategy'")
            row = c.fetchone()
            if row:
                val = row['value'].strip('"')
                if val == '대형주 (Core)': c.execute("UPDATE settings SET value='\"CORE\"' WHERE key='strategy'")
                elif val == '중소형주 (Satellite)': c.execute("UPDATE settings SET value='\"SATELLITE\"' WHERE key='strategy'")
        except: pass
        
        try: c.execute("ALTER TABLE order_intents ADD COLUMN idempotency_key TEXT UNIQUE")
        except: pass
        conn.commit()

def init_db():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS watchlist (ticker TEXT PRIMARY KEY, name TEXT, added_at TIMESTAMP)''')
        # 🛑 [핵심 패치 2] idempotency_key UNIQUE 제약 추가
        c.execute('''CREATE TABLE IF NOT EXISTS order_intents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        correlation_id TEXT UNIQUE,
                        idempotency_key TEXT UNIQUE,
                        ticker TEXT, order_type TEXT, qty INTEGER, price REAL, 
                        status TEXT DEFAULT 'INTENT_CREATED', 
                        broker_order_id TEXT, branch_no TEXT,
                        cum_filled_qty INTEGER DEFAULT 0, avg_fill_price REAL DEFAULT 0.0,
                        resp_code TEXT, created_at TIMESTAMP, updated_at TIMESTAMP
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS positions (
                        ticker TEXT PRIMARY KEY, qty INTEGER, buy_price REAL, 
                        highest_price REAL, buy_date TIMESTAMP
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS fills (
                        fill_id TEXT PRIMARY KEY, order_id INTEGER,
                        ticker TEXT, fill_qty INTEGER, fill_price REAL, executed_at TIMESTAMP
                     )''')
        # 🛑 [핵심 패치 3] 계좌별 단일 worker lease 테이블 생성
        c.execute('''CREATE TABLE IF NOT EXISTS worker_leases (
                        account_id TEXT PRIMARY KEY, worker_id TEXT, expires_at TIMESTAMP
                     )''')
        conn.commit()
    migrate_db()

def acquire_worker_lease(account_id, worker_id, ttl_seconds=30):
    with get_connection() as conn:
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute("DELETE FROM worker_leases WHERE expires_at < ?", (now,))
        try:
            c.execute("INSERT INTO worker_leases (account_id, worker_id, expires_at) VALUES (?, ?, datetime('now', 'localtime', '+{} seconds'))".format(ttl_seconds), (account_id, worker_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            c.execute("SELECT worker_id FROM worker_leases WHERE account_id=?", (account_id,))
            row = c.fetchone()
            if row and row['worker_id'] == worker_id:
                c.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds') WHERE account_id=? AND worker_id=?".format(ttl_seconds), (account_id, worker_id))
                conn.commit()
                return True
            return False

def get_setting(key, default=None):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        return json.loads(row['value']) if row else default

def set_setting(key, value):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn.commit()

def get_system_status():
    if get_setting('halted_config_error', False): return "HALTED_CONFIG_ERROR"
    if get_setting('kill_switch', False): return "KILL_SWITCH"
    return "OK"

def get_watchlist():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT ticker, name FROM watchlist")
        return [{'티커': r['ticker'], '종목명': r['name']} for r in c.fetchall()]

def add_to_watchlist(ticker, name):
    with get_connection() as conn:
        conn.cursor().execute("INSERT OR REPLACE INTO watchlist (ticker, name, added_at) VALUES (?, ?, ?)", (str(ticker).zfill(6), name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()

def clear_and_update_watchlist(keep_list):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM watchlist")
        for item in keep_list:
            c.execute("INSERT INTO watchlist (ticker, name, added_at) VALUES (?, ?, ?)", (str(item['티커']).zfill(6), item['종목명'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()

def sync_positions_from_broker(broker_positions):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT ticker, highest_price, buy_date FROM positions")
        db_map = {row['ticker']: dict(row) for row in c.fetchall()}
        c.execute("DELETE FROM positions")
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for bp in broker_positions:
            tk, qty, buy_p, cur_p = bp['ticker'], bp['qty'], bp['buy_price'], bp['current_price']
            high_p = max(db_map[tk]['highest_price'], cur_p, buy_p) if tk in db_map else max(cur_p, buy_p)
            b_date = db_map[tk]['buy_date'] if tk in db_map else now_str
            c.execute("INSERT INTO positions (ticker, qty, buy_price, highest_price, buy_date) VALUES (?, ?, ?, ?, ?)", (tk, qty, buy_p, high_p, b_date))
        conn.commit()

def get_positions():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM positions")
        return [dict(r) for r in c.fetchall()]

def has_open_order(ticker, side):
    with get_connection() as conn:
        c = conn.cursor()
        open_statuses = ['INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED']
        like_side = "%매수%" if side == "BUY" else "%매도%"
        c.execute(f"SELECT 1 FROM order_intents WHERE ticker=? AND order_type LIKE ? AND status IN ({','.join(['?']*len(open_statuses))})", [ticker, like_side] + open_statuses)
        return c.fetchone() is not None

def add_order_intent(ticker, order_type, qty, price, idem_key):
    corr_id = str(uuid.uuid4())
    try:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO order_intents (correlation_id, idempotency_key, ticker, order_type, qty, price, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'INTENT_CREATED', ?, ?)", 
                      (corr_id, idem_key, str(ticker).zfill(6), order_type, qty, price, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False

# 🛑 [핵심 패치 4] BEGIN IMMEDIATE 트랜잭션으로 원자적(Atomic) 큐 점유
def claim_next_order():
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()
        c.execute("SELECT * FROM order_intents WHERE status = 'INTENT_CREATED' ORDER BY id ASC LIMIT 1")
        row = c.fetchone()
        if row:
            c.execute("UPDATE order_intents SET status='CLAIMED', updated_at=? WHERE id=?", (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['id']))
            conn.commit()
            return dict(row)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
    finally:
        conn.close()
    return None

def get_orders_by_status(statuses):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(f"SELECT * FROM order_intents WHERE status IN ({','.join(['?']*len(statuses))})", statuses)
        return [dict(r) for r in c.fetchall()]

def transition_order_status(order_id, current_status, new_status, broker_id=None, branch=None, code=None):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []): raise ValueError(f"Invalid state transition: {current_status} -> {new_status}")
    with get_connection() as conn:
        c = conn.cursor()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if broker_id and branch: c.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, broker_id, branch, code, now_str, order_id, current_status))
        else: c.execute("UPDATE order_intents SET status=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, code, now_str, order_id, current_status))
        conn.commit()
        return c.rowcount > 0

def process_fill_event(order_id, ticker, order_type, fill_qty, fill_price):
    fill_id = f"{order_id}_{fill_qty}_{int(datetime.now().timestamp()*1000)}"
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT fill_id FROM fills WHERE fill_id=?", (fill_id,))
        if c.fetchone(): return False

        c.execute("INSERT INTO fills (fill_id, order_id, ticker, fill_qty, fill_price, executed_at) VALUES (?, ?, ?, ?, ?, ?)", (fill_id, order_id, ticker, fill_qty, fill_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        c.execute("SELECT qty, cum_filled_qty, avg_fill_price FROM order_intents WHERE id=?", (order_id,))
        o_row = c.fetchone()
        if not o_row: return False
        
        req_qty, old_cum, old_avg = o_row['qty'], o_row['cum_filled_qty'], o_row['avg_fill_price']
        new_cum = old_cum + fill_qty
        new_avg = ((old_cum * old_avg) + (fill_qty * fill_price)) / new_cum if new_cum > 0 else 0.0
        new_status = 'FILLED' if new_cum >= req_qty else 'PARTIALLY_FILLED'
        c.execute("UPDATE order_intents SET cum_filled_qty=?, avg_fill_price=?, status=?, updated_at=? WHERE id=?", (new_cum, new_avg, new_status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), order_id))

        c.execute("SELECT value FROM settings WHERE key='virtual_cash'")
        cash_row = c.fetchone()
        cash = float(json.loads(cash_row['value'])) if cash_row else 10000000.0

        c.execute("SELECT qty, buy_price FROM positions WHERE ticker=?", (ticker,))
        p_row = c.fetchone()
        p_qty, p_buy = p_row['qty'] if p_row else 0, p_row['buy_price'] if p_row else 0.0

        if "매수" in order_type:
            cash -= (fill_qty * fill_price * 1.0025)
            new_p_qty = p_qty + fill_qty
            new_p_buy = ((p_qty * p_buy) + (fill_qty * fill_price)) / new_p_qty
            if p_row: c.execute("UPDATE positions SET qty=?, buy_price=? WHERE ticker=?", (new_p_qty, new_p_buy, ticker))
            else: c.execute("INSERT INTO positions (ticker, qty, buy_price, highest_price, buy_date) VALUES (?, ?, ?, ?, ?)", (ticker, new_p_qty, new_p_buy, fill_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        else: 
            cash += (fill_qty * fill_price * 0.9975)
            new_p_qty = p_qty - fill_qty
            if new_p_qty <= 0: c.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
            else: c.execute("UPDATE positions SET qty=? WHERE ticker=?", (new_p_qty, ticker))
                
        c.execute("UPDATE settings SET value=? WHERE key='virtual_cash'", (json.dumps(cash),))
        conn.commit()
        return True

init_db()
