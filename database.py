import sqlite3
import json
from datetime import datetime

DB_PATH = "quant_system.db"

def get_connection():
    # 🛑 [핵심 방어 패치] 클라우드 환경의 DB Lock(잠김) 현상을 방지하는 강력한 옵션 추가
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
    try:
        conn.execute('PRAGMA journal_mode=WAL;') # 동시 읽기/쓰기 모드 활성화
    except:
        pass
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

        for old_s, new_s in [('대형주 (Core)', 'CORE'), ('중소형주 (Satellite)', 'SATELLITE')]:
            try:
                c.execute("SELECT value FROM settings WHERE key=?", (f'params_{old_s}',))
                r = c.fetchone()
                if r:
                    p = json.loads(r['value'])
                    new_p = {
                        'ma200': bool(p.get('ma200', True)),
                        'buf': float(p.get('buf', 1.5)) / 100.0 if float(p.get('buf', 1.5)) > 1 else float(p.get('buf', 0.015)),
                        'sl': float(p.get('sl', -15)) / 100.0 if float(p.get('sl', -15)) < -1 else float(p.get('sl', -0.15)),
                        'alloc': float(p.get('alloc', 35)) / 100.0 if float(p.get('alloc', 35)) > 1 else float(p.get('alloc', 0.35)),
                        'ts_tgt': float(p.get('ts_tgt', 30)) / 100.0 if float(p.get('ts_tgt', 30)) > 1 else float(p.get('ts_tgt', 0.30)),
                        'ts_drp': float(p.get('ts_drp', -10)) / 100.0 if float(p.get('ts_drp', -10)) < -1 else float(p.get('ts_drp', -0.10)),
                        'cd': int(p.get('cd', 60)),
                        'min_h': int(p.get('min_h', 5)),
                        'boost': bool(p.get('boost', True))
                    }
                    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f'params_{new_s}', json.dumps(new_p)))
                    c.execute("DELETE FROM settings WHERE key=?", (f'params_{old_s}',))
            except: pass
        conn.commit()

def init_db():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS watchlist (ticker TEXT PRIMARY KEY, name TEXT, added_at TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS order_intents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT, order_type TEXT, qty INTEGER, price REAL, 
                        status TEXT DEFAULT 'PENDING', created_at TIMESTAMP
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS positions (
                        ticker TEXT PRIMARY KEY, qty INTEGER, buy_price REAL, 
                        highest_price REAL, buy_date TIMESTAMP
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS executions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT, order_type TEXT, qty INTEGER, price REAL, executed_at TIMESTAMP
                     )''')
        conn.commit()
    migrate_db()

def get_setting(key, default=None):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        if row:
            try: return json.loads(row['value'])
            except: return row['value']
        return default

def set_setting(key, value):
    with get_connection() as conn:
        c = conn.cursor()
        val_str = json.dumps(value) if isinstance(value, (dict, list, bool, int, float)) else str(value)
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val_str))
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
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO watchlist (ticker, name, added_at) VALUES (?, ?, ?)", (str(ticker).zfill(6), name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
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
            if tk in db_map:
                high_p = max(db_map[tk]['highest_price'], cur_p, buy_p)
                b_date = db_map[tk]['buy_date']
            else:
                high_p = max(cur_p, buy_p)
                b_date = now_str
            c.execute("INSERT INTO positions (ticker, qty, buy_price, highest_price, buy_date) VALUES (?, ?, ?, ?, ?)", (tk, qty, buy_p, high_p, b_date))
        conn.commit()

def get_positions():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM positions")
        return [dict(r) for r in c.fetchall()]

def add_order_intent(ticker, order_type, qty, price):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO order_intents (ticker, order_type, qty, price, created_at) VALUES (?, ?, ?, ?, ?)", (str(ticker).zfill(6), order_type, qty, price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()

def get_pending_orders():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM order_intents WHERE status = 'PENDING'")
        return [dict(r) for r in c.fetchall()]

def update_order_status(order_id, status):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE order_intents SET status = ? WHERE id = ?", (status, order_id))
        conn.commit()

def add_execution(ticker, order_type, qty, price):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO executions (ticker, order_type, qty, price, executed_at) VALUES (?, ?, ?, ?, ?)", (ticker, order_type, qty, price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()

init_db()
