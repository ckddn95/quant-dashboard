import sqlite3
import pandas as pd
from datetime import datetime

DB_PATH = "quant_system.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_config (
                port_name TEXT PRIMARY KEY,
                strategy_name TEXT,
                use_ma200_filter INTEGER,
                ma_buffer_pct REAL,
                stop_loss_pct REAL,
                ts_target_pct REAL,
                ts_drop_pct REAL,
                min_liquidity REAL,
                max_alloc_pct REAL,
                cash REAL,
                is_mock INTEGER,
                kill_switch INTEGER,
                auto_trade_enabled INTEGER,
                auto_pilot INTEGER,
                app_key TEXT,
                app_secret TEXT,
                cano TEXT,
                prdt_cd TEXT
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                port_name TEXT, ticker TEXT, stock_name TEXT, UNIQUE(port_name, ticker)
            );
            CREATE TABLE IF NOT EXISTS positions (
                ticker TEXT PRIMARY KEY,
                stock_name TEXT, managed_qty INTEGER, avg_fill_price REAL,
                highest_price REAL, trailing_armed INTEGER, hold_days INTEGER,
                cooldown_until DATE
            );
            CREATE TABLE IF NOT EXISTS order_ledger (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, stock_name TEXT, side TEXT, intent_qty INTEGER, intent_price REAL,
                status TEXT, filled_qty INTEGER, filled_price REAL, msg TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

def get_config(port_name: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM strategy_config WHERE port_name=?", (port_name,)).fetchone()
        return dict(row) if row else {}

def save_config(port_name: str, cfg: dict):
    with sqlite3.connect(DB_PATH) as conn:
        cols = ', '.join(cfg.keys())
        places = ', '.join(['?'] * len(cfg))
        updates = ', '.join([f"{k}=excluded.{k}" for k in cfg.keys()])
        sql = f"INSERT INTO strategy_config (port_name, {cols}) VALUES (?, {places}) ON CONFLICT(port_name) DO UPDATE SET {updates}"
        conn.execute(sql, [port_name] + list(cfg.values()))

def log_order_intent(ticker, name, side, qty, price):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO order_ledger (ticker, stock_name, side, intent_qty, intent_price, status) VALUES (?, ?, ?, ?, ?, ?)",
                     (ticker, name, side, qty, price, 'INTENT_CREATED'))

def get_pending_orders():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM order_ledger WHERE status='INTENT_CREATED'").fetchall()]

def update_order_status(order_id, status, filled_qty=0, filled_price=0.0, msg=""):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE order_ledger SET status=?, filled_qty=?, filled_price=?, msg=? WHERE order_id=?", 
                     (status, filled_qty, filled_price, msg, order_id))

def get_position(ticker):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
        return dict(row) if row else None

def update_position(ticker, name, qty, price, highest, armed, cd_until='2000-01-01'):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO positions (ticker, stock_name, managed_qty, avg_fill_price, highest_price, trailing_armed, hold_days, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(ticker) DO UPDATE SET 
            managed_qty=excluded.managed_qty, avg_fill_price=excluded.avg_fill_price, highest_price=excluded.highest_price, trailing_armed=excluded.trailing_armed, cooldown_until=excluded.cooldown_until
        """, (ticker, name, qty, price, highest, int(armed), cd_until))
