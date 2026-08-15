import sqlite3
import json
from datetime import datetime

DB_PATH = "quant_system.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

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
        conn.commit()

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

def get_watchlist():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT ticker, name FROM watchlist")
        return [{'티커': r['ticker'], '종목명': r['name']} for r in c.fetchall()]

def add_to_watchlist(ticker, name):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO watchlist (ticker, name, added_at) VALUES (?, ?, ?)",
                  (str(ticker).zfill(6), name, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()

def clear_and_update_watchlist(keep_list):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM watchlist")
        for item in keep_list:
            c.execute("INSERT INTO watchlist (ticker, name, added_at) VALUES (?, ?, ?)",
                      (str(item['티커']).zfill(6), item['종목명'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()

def add_order_intent(ticker, order_type, qty, price):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO order_intents (ticker, order_type, qty, price, created_at) VALUES (?, ?, ?, ?, ?)",
                  (str(ticker).zfill(6), order_type, qty, price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
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

init_db()
