import sqlite3
import json
import os
import yaml
import hashlib
import hmac
import uuid
import traceback
import math
import pandas as pd
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quant_system.db")
CONTRACT_PATH = os.path.join(BASE_DIR, "system_contract.yaml")
KST = timezone(timedelta(hours=9))

def load_contract():
    if not os.path.exists(CONTRACT_PATH):
        return {}
    with open(CONTRACT_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

CONTRACT = load_contract()
SCHEMA_VERSION = int(CONTRACT.get('schema_version', 17))

ALLOWED_TRANSITIONS = CONTRACT.get('allowed_state_transitions', {})
ALLOWED_TRANSITIONS.setdefault('CANCEL_REQUESTED', []).extend(['CANCEL_CLAIMED', 'CANCEL_SUBMITTING', 'CANCELED'])
ALLOWED_TRANSITIONS['CANCEL_CLAIMED'] = ['CANCEL_SUBMITTING', 'CANCEL_REQUESTED']
ALLOWED_TRANSITIONS['CANCEL_SUBMITTING'] = ['CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN', 'REJECTED', 'CANCELED']

def get_connection():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    conn.execute('PRAGMA foreign_keys=ON;')
    conn.row_factory = sqlite3.Row
    return conn

def backup_db() -> str:
    if not os.path.exists(DB_PATH):
        return ""
    timestamp = datetime.now(KST).strftime('%Y%m%d_%H%M%S')
    backup_path = f"{DB_PATH}.{timestamp}.bak"
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as src, sqlite3.connect(backup_path) as dst:
            src.backup(dst)
        return backup_path
    except Exception as e:
        raise RuntimeError(f"Backup failed: {e}")

def _get_db_metrics(conn):
    def table_exists(t_name):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t_name,)).fetchone() is not None
    m = {}
    m['oi_count'] = conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] if table_exists('order_intents') else 0
    m['pos_count'] = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] if table_exists('positions') else 0
    m['oi_qty'] = conn.execute("SELECT SUM(qty) FROM order_intents").fetchone()[0] or 0 if table_exists('order_intents') else 0
    m['oi_cum'] = conn.execute("SELECT SUM(cum_filled_qty) FROM order_intents").fetchone()[0] or 0 if table_exists('order_intents') else 0
    m['pos_qty'] = conn.execute("SELECT SUM(managed_qty + manual_qty) FROM positions").fetchone()[0] or 0 if table_exists('positions') else 0
    return m

def _validate_schema(conn) -> bool:
    req_tables = ['settings', 'watchlist', 'positions', 'worker_leases', 'order_intents', 'fills', 'watchlist_events', 'cash_flows', 'daily_account_equity', 'order_events', 'signal_states', 'reconciliation_events']
    existing = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if not all(t in existing for t in req_tables):
        return False
    return True

def bootstrap_db():
    conn = get_connection()
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, source TEXT, provenance TEXT, added_at TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS positions (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0, managed_buy_price REAL DEFAULT 0.0, manual_buy_price REAL DEFAULT 0.0, buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS worker_leases (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, worker_id TEXT, expires_at TIMESTAMP, token INTEGER, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS order_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, strategy_version TEXT, contract_version TEXT, ticker TEXT, stock_name TEXT, side TEXT, order_kind TEXT, qty INTEGER, limit_price REAL, reference_price REAL, exchange TEXT, time_in_force TEXT, signal_id TEXT, signal_source TEXT, signal_cutoff TEXT, quote_id TEXT, quote_source TEXT, quote_timestamp TEXT, intent_ttl INTEGER, cost_model_version TEXT, status TEXT DEFAULT 'INTENT_CREATED', broker_order_id TEXT, branch_no TEXT, cum_filled_qty INTEGER DEFAULT 0, avg_fill_price REAL DEFAULT 0.0, resp_code TEXT, fencing_token INTEGER, created_at TIMESTAMP, updated_at TIMESTAMP, tot_ccld_qty INTEGER DEFAULT 0, tot_ccld_amt REAL DEFAULT 0.0, avg_prvs REAL DEFAULT 0.0, rmn_qty INTEGER DEFAULT 0, cncl_yn TEXT DEFAULT 'N', rjct_qty INTEGER DEFAULT 0, orgno TEXT, ord_tmd TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, order_intent_id INTEGER, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, delta_qty INTEGER, cum_qty INTEGER, fill_price REAL, delta_amt REAL, cum_amt REAL, fee REAL, tax REAL, slippage REAL, fill_timestamp TIMESTAMP, received_at TIMESTAMP, is_reconciled BOOLEAN, tot_ccld_qty INTEGER DEFAULT 0, tot_ccld_amt REAL DEFAULT 0.0, avg_prvs REAL DEFAULT 0.0, rmn_qty INTEGER DEFAULT 0, cncl_yn TEXT DEFAULT 'N', rjct_qty INTEGER DEFAULT 0, orgno TEXT, ord_tmd TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_events (id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, event_type TEXT, effective_at TIMESTAMP, recorded_at TIMESTAMP, source TEXT, provenance TEXT, idempotency_key TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS cash_flows (id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, amount REAL, timestamp TIMESTAMP, description TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS daily_account_equity (date TEXT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, equity REAL, cash REAL, PRIMARY KEY(date, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY AUTOINCREMENT, order_intent_id INTEGER, correlation_id TEXT, event_type TEXT, previous_status TEXT, new_status TEXT, worker_id TEXT, fencing_token INTEGER, reason TEXT, timestamp TIMESTAMP, details TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS signal_states (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, regime_id TEXT, current_signal TEXT, consecutive_count INTEGER DEFAULT 0, loss_streak INTEGER DEFAULT 0, cooldown_until_session TIMESTAMP, rearm_state BOOLEAN DEFAULT 1, highest_price REAL DEFAULT 0.0, trailing_armed BOOLEAN DEFAULT 0, last_updated TIMESTAMP, last_distinct_bar_timestamp TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS reconciliation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, order_intent_id INTEGER, event_type TEXT, timestamp TIMESTAMP, details TEXT)''')
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise RuntimeError(f"Database bootstrap failed: {e}")
    finally:
        conn.close()

def preflight_check() -> bool:
    if os.getenv("CI_TEST_MODE") == "true":
        return True
    if not os.path.exists(DB_PATH):
        bootstrap_db()
        return True
    with get_connection() as conn:
        curr_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if curr_ver < SCHEMA_VERSION:
            bootstrap_db()
    return True

def generate_account_fingerprint(cano: str, secret_salt: str) -> str:
    if cano == "MOCK_ACCOUNT":
        return "MOCK_ACCOUNT"
    return hmac.new(secret_salt.encode('utf-8'), cano.encode('utf-8'), hashlib.sha256).hexdigest()[:16]

def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row['value']) if row else default

def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn.execute("COMMIT")

def get_system_status(broker, env, acc_fp, prdt_cd, port_id, strat_id):
    master_ks = bool(get_setting('master_kill_switch', False))
    acc_ks_key = f"kill_switch_{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}"
    acc_ks = bool(get_setting(acc_ks_key, False))
    acc_at_key = f"auto_trade_{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}"
    acc_ap_key = f"auto_pilot_{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}"
    return {
        "auto_trade": bool(get_setting(acc_at_key, False)),
        "auto_pilot": bool(get_setting(acc_ap_key, False)),
        "kill_switch": master_ks or acc_ks,
        "contract_version": CONTRACT.get('contract_version', '1.0.0'),
        "real_approval_status": CONTRACT.get('execution_rules', {}).get('real_approval_status', 'POST_BLOCKED')
    }

def get_watchlist(broker, env, acc_fp, prdt_cd, port_id, strat_id):
    with get_connection() as conn: 
        return [{'티커': r['ticker'], '종목명': r['name'], 'source': r['source'], 'provenance': r['provenance']} for r in conn.execute("SELECT ticker, name, source, provenance FROM watchlist WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchall()]

def clear_and_update_watchlist(broker, env, acc_fp, prdt_cd, port_id, strat_id, new_items, source="SYSTEM", provenance="UNKNOWN"):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            curr_rows = conn.execute("SELECT ticker FROM watchlist WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchall()
            curr_tickers = set([r['ticker'] for r in curr_rows])
            new_tickers_dict = {str(item['티커']).zfill(6): item['종목명'] for item in new_items}
            new_tickers = set(new_tickers_dict.keys())
            
            added = new_tickers - curr_tickers
            removed = curr_tickers - new_tickers
            
            for tk in removed:
                conn.execute("DELETE FROM watchlist WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk))
            for tk in added:
                conn.execute("INSERT OR REPLACE INTO watchlist (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, name, source, provenance, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk, new_tickers_dict[tk], source, provenance, now_str))
            conn.execute("COMMIT")
            return True, len(added) + len(removed)
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in watchlist update: {e}")

def record_daily_account_equity(broker, env, acc_fp, prdt_cd, port_id, strat_id, equity, cash):
    date_str = datetime.now(KST).strftime('%Y-%m-%d')
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO daily_account_equity (date, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, equity, cash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (date_str, broker, env, acc_fp, prdt_cd, port_id, strat_id, equity, cash))

def record_cash_flow(broker, env, acc_fp, prdt_cd, port_id, strat_id, amount, description):
    if amount == 0: return
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        conn.execute("INSERT INTO cash_flows (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, amount, timestamp, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, amount, now_str, description))

def get_cash_flows_by_date(broker, env, acc_fp, prdt_cd, port_id, strat_id, start_date, end_date):
    with get_connection() as conn:
        rows = conn.execute("SELECT substring(timestamp, 1, 10) as dt, SUM(amount) as amt FROM cash_flows WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND timestamp >= ? AND timestamp <= ? GROUP BY dt", (broker, env, acc_fp, prdt_cd, port_id, strat_id, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d 23:59:59'))).fetchall()
        return {r['dt']: r['amt'] for r in rows}

def get_positions(broker, env, acc_fp, prdt_cd, port_id, strat_id):
    with get_connection() as conn: 
        return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchall()]

def sync_positions_from_broker(broker, env, acc_fp, prdt_cd, port_id, strat_id, kis_stocks):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = set([r['ticker'] for r in conn.execute("SELECT ticker FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchall()])
            kis_tk = set([s['ticker'] for s in kis_stocks])
            for stock in kis_stocks:
                tk, b_qty, buy_p = stock['ticker'], stock['qty'], stock.get('buy_price', 0.0)
                row = conn.execute("SELECT managed_qty, manual_qty, unknown_quarantined_qty FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk)).fetchone()
                if row:
                    diff = b_qty - (row['managed_qty'] + row['manual_qty'])
                    conn.execute("UPDATE positions SET broker_qty=?, unknown_quarantined_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (b_qty, diff, buy_p, broker, env, acc_fp, prdt_cd, port_id, strat_id, tk))
                else:
                    conn.execute("INSERT INTO positions (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, managed_buy_price, manual_buy_price, buy_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk, b_qty, 0, b_qty, 0.0, buy_p, buy_p, datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')))
            for tk in (existing - kis_tk):
                conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk))
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in sync_positions: {e}")

def get_locked_cash_and_qty(broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker=None):
    with get_connection() as conn:
        open_states = "('INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'CANCEL_CLAIMED', 'CANCEL_SUBMITTING', 'CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN')"
        buffer_multi = CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)
        r1 = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * reference_price * ?) as locked_cash FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND side='BUY' AND status IN {open_states}", (buffer_multi, broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
        locked_cash = float(r1['locked_cash']) if r1['locked_cash'] else 0.0
        locked_sell_qty = 0
        if ticker:
            r2 = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as locked_qty FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=? AND side='SELL' AND status IN {open_states}", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker)).fetchone()
            locked_sell_qty = int(r2['locked_qty']) if r2['locked_qty'] else 0
        return locked_cash, locked_sell_qty

def get_signal_state(broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM signal_states WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker)).fetchone()
        return dict(row) if row else None

def upsert_signal_state(broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker, update_fields: dict):
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    update_fields['last_updated'] = now_str
    keys = ['broker', 'environment', 'account_fingerprint', 'product_code', 'portfolio_id', 'strategy_id', 'ticker']
    key_vals = [broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker]
    fields = list(update_fields.keys())
    vals = list(update_fields.values())
    all_cols = keys + fields
    all_vals = key_vals + vals
    placeholders = ", ".join(["?"] * len(all_cols))
    col_names = ", ".join(all_cols)
    update_clause = ", ".join([f"{f}=EXCLUDED.{f}" for f in fields])
    query = f"INSERT INTO signal_states ({col_names}) VALUES ({placeholders}) ON CONFLICT(broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker) DO UPDATE SET {update_clause}"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(query, all_vals)
        conn.execute("COMMIT")

def acquire_worker_lease(broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, ttl=30):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_kst = datetime.now(KST)
            now_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
            expire_str = (now_kst + timedelta(seconds=ttl)).strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute("SELECT worker_id, token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            if not row or row['expires_at'] < now_str:
                nt = (row['token'] + 1) if row else 1
                conn.execute("INSERT OR REPLACE INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, expire_str, nt))
                conn.execute("COMMIT"); return True, nt
            elif row['worker_id'] == worker_id:
                conn.execute("UPDATE worker_leases SET expires_at=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (expire_str, broker, env, acc_fp, prdt_cd, port_id, strat_id))
                conn.execute("COMMIT"); return True, row['token']
            conn.execute("ROLLBACK"); return False, 0
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in acquire_worker_lease: {e}")

def renew_worker_lease(broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, token, extend_seconds=10):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            expire_str = (datetime.now(KST) + timedelta(seconds=extend_seconds)).strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute("SELECT token FROM worker_leases WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND worker_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id)).fetchone()
            if row and row['token'] == token:
                conn.execute("UPDATE worker_leases SET expires_at=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND worker_id=? AND token=?", (expire_str, broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, token))
                conn.execute("COMMIT"); return True
            conn.execute("ROLLBACK"); return False
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in renew_worker_lease: {e}")

def safe_add_order_intent(spec):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            open_states = "('INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'CANCEL_CLAIMED', 'CANCEL_SUBMITTING', 'CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN')"
            check_query = f"SELECT id, status FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=? AND status IN {open_states}"
            existing = conn.execute(check_query, (spec.broker, spec.environment, spec.account_fingerprint, spec.account_product_code, spec.portfolio_id, spec.strategy_id, spec.ticker)).fetchone()
            if existing:
                conn.execute("ROLLBACK")
                return False, f"Blocked: Open intent {existing['id']} ({existing['status']}) already exists for {spec.ticker}"
            corr_id = spec.correlation_id if spec.correlation_id else f"{spec.broker}_{spec.environment}_{spec.account_fingerprint}_{spec.account_product_code}_{spec.portfolio_id}_{spec.strategy_id}_{spec.ticker}_{spec.side}_{spec.intent_created_at}"
            corr_id = hashlib.sha256(corr_id.encode()).hexdigest()[:16]
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            cur = conn.execute("""INSERT INTO order_intents 
                (correlation_id, idempotency_key, broker, environment, account_fingerprint, product_code, portfolio_id, 
                 strategy_id, strategy_version, contract_version, ticker, stock_name, side, order_kind, 
                 qty, limit_price, reference_price, exchange, time_in_force, signal_id, signal_source, signal_cutoff, quote_id, quote_source, quote_timestamp, 
                 intent_ttl, cost_model_version, status, created_at, updated_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_CREATED', ?, ?)""", 
                (corr_id, spec.idempotency_key, spec.broker, spec.environment, spec.account_fingerprint, spec.account_product_code, spec.portfolio_id, 
                 spec.strategy_id, spec.strategy_version, CONTRACT.get('contract_version','1.0.0'), spec.ticker, spec.stock_name, spec.side, spec.order_kind, 
                 spec.quantity, spec.limit_price, spec.reference_price, spec.exchange, spec.time_in_force, spec.signal_id, spec.signal_source, spec.signal_cutoff, spec.quote_id, spec.quote_source, spec.quote_timestamp,
                 spec.intent_ttl, CONTRACT.get('cost_model_version', '1.0.0'), spec.intent_created_at, spec.intent_created_at))
            conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                         (cur.lastrowid, corr_id, "STATUS_CHANGE", None, "INTENT_CREATED", "SIGNAL_FIRED", now_str, "Intent Created"))
            conn.execute("COMMIT")
            return True, "OK"
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in safe_add_order_intent: {e}")

def get_orders_by_status_and_env(statuses, broker, env, acc_fp, prdt_cd, port_id, strat_id):
    with get_connection() as conn:
        query = f"SELECT * FROM order_intents WHERE status IN ({','.join(['?']*len(statuses))}) AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?"
        return [dict(r) for r in conn.execute(query, statuses + [broker, env, acc_fp, prdt_cd, port_id, strat_id]).fetchall()]

def insert_reconciliation_event(broker, env, acc_fp, prdt_cd, port_id, strat_id, order_intent_id, event_type, details):
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        conn.execute("INSERT INTO reconciliation_events (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, order_intent_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, order_intent_id, event_type, now_str, details))

def apply_broker_receipt(order_id, ticker, order_type, broker, env, acc_fp, prdt_cd, port_id, strat_id, broker_state):
    import quant_engine as quant
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            o_row = conn.execute("SELECT qty, correlation_id, cum_filled_qty, tot_ccld_qty, tot_ccld_amt, avg_fill_price, status, signal_source FROM order_intents WHERE id=? AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (order_id, broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            if not o_row:
                conn.execute("ROLLBACK")
                return False
            new_cum_qty = broker_state['tot_ccld_qty']
            new_cum_amt = broker_state['tot_ccld_amt']
            delta_qty = new_cum_qty - o_row['cum_filled_qty']
            delta_amt = new_cum_amt - o_row['tot_ccld_amt']
            now_kst = datetime.now(KST)
            now_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')
            if delta_qty > 0 or delta_amt > 0:
                delta_fill_price = delta_amt / delta_qty if delta_qty > 0 else 0
                is_manual = (o_row['signal_source'] != 'SYSTEM')
                delta_managed = delta_qty if not is_manual else 0
                delta_manual = delta_qty if is_manual else 0
                market = "KOSPI" if ticker.startswith('0') else "KOSDAQ"
                fee, slip, tax = quant.CostModel.calculate_cost(now_kst.date(), market, order_type, delta_fill_price, delta_qty, False)
                new_status = 'FILLED' if new_cum_qty >= o_row['qty'] else 'PARTIALLY_FILLED'
                if o_row['status'] in ['CANCEL_REQUESTED', 'CANCEL_CLAIMED', 'CANCEL_SUBMITTING', 'CANCEL_ACKNOWLEDGED', 'CANCELED']:
                    new_status = o_row['status']
                if new_cum_qty >= o_row['qty']:
                    new_status = 'FILLED'
                new_cum_avg_price = new_cum_amt / new_cum_qty if new_cum_qty > 0 else 0
                fill_id = f"{order_id}_{now_kst.timestamp()}"
                conn.execute("INSERT INTO fills (fill_id, order_intent_id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, delta_qty, cum_qty, fill_price, delta_amt, cum_amt, fee, tax, slippage, fill_timestamp, received_at, is_reconciled, tot_ccld_qty, tot_ccld_amt, avg_prvs, rmn_qty, cncl_yn, rjct_qty, orgno, ord_tmd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (fill_id, order_id, broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker, delta_qty, new_cum_qty, delta_fill_price, delta_amt, new_cum_amt, fee, tax, slip, now_str, now_str, 1, broker_state['tot_ccld_qty'], broker_state['tot_ccld_amt'], broker_state['avg_prvs'], broker_state['rmn_qty'], broker_state['cncl_yn'], broker_state['rjct_qty'], broker_state['orgno'], broker_state['ord_tmd']))
                p_row = conn.execute("SELECT managed_qty, manual_qty, managed_buy_price, manual_buy_price, buy_price FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker)).fetchone()
                p_qty = p_row['managed_qty'] if p_row else 0
                p_manual_qty = p_row['manual_qty'] if p_row else 0
                p_managed_buy = p_row['managed_buy_price'] if p_row and 'managed_buy_price' in p_row else (p_row['buy_price'] if p_row else 0.0)
                p_manual_buy = p_row['manual_buy_price'] if p_row and 'manual_buy_price' in p_row else (p_row['buy_price'] if p_row else 0.0)
                if "BUY" in order_type.upper():
                    if is_manual:
                        new_manual_qty = p_manual_qty + delta_qty
                        new_managed_qty = p_qty
                        new_manual_buy = ((p_manual_qty * p_manual_buy) + (delta_qty * delta_fill_price)) / new_manual_qty if new_manual_qty > 0 else 0
                        new_managed_buy = p_managed_buy
                    else:
                        new_managed_qty = p_qty + delta_qty
                        new_manual_qty = p_manual_qty
                        new_managed_buy = ((p_qty * p_managed_buy) + (delta_qty * delta_fill_price)) / new_managed_qty if new_managed_qty > 0 else 0
                        new_manual_buy = p_manual_buy
                    total_new_qty = new_managed_qty + new_manual_qty
                    composite_buy_price = ((new_managed_qty * new_managed_buy) + (new_manual_qty * new_manual_buy)) / total_new_qty if total_new_qty > 0 else 0
                    if p_row:
                        conn.execute("UPDATE positions SET managed_qty=?, manual_qty=?, managed_buy_price=?, manual_buy_price=?, buy_price=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_managed_qty, new_manual_qty, new_managed_buy, new_manual_buy, composite_buy_price, broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker))
                    else:
                        conn.execute("INSERT INTO positions (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, managed_buy_price, manual_buy_price, buy_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker, delta_qty, new_managed_qty, new_manual_qty, new_managed_buy if not is_manual else 0.0, new_manual_buy if is_manual else 0.0, delta_fill_price, now_str))
                else:
                    if is_manual:
                        new_manual_qty = max(0, p_manual_qty - delta_qty)
                        new_managed_qty = p_qty
                    else:
                        new_managed_qty = max(0, p_qty - delta_qty)
                        new_manual_qty = p_manual_qty
                    total_new_qty = new_managed_qty + new_manual_qty
                    composite_buy_price = ((new_managed_qty * p_managed_buy) + (new_manual_qty * p_manual_buy)) / total_new_qty if total_new_qty > 0 else 0
                    if total_new_qty == 0:
                        conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker))
                    else:
                        conn.execute("UPDATE positions SET managed_qty=?, manual_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_managed_qty, new_manual_qty, composite_buy_price, broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker))
                conn.execute("UPDATE order_intents SET cum_filled_qty=?, avg_fill_price=?, status=?, updated_at=?, tot_ccld_qty=?, tot_ccld_amt=?, avg_prvs=?, rmn_qty=?, cncl_yn=?, rjct_qty=?, orgno=?, ord_tmd=? WHERE id=?", (new_cum_qty, new_cum_avg_price, new_status, now_str, broker_state['tot_ccld_qty'], broker_state['tot_ccld_amt'], broker_state['avg_prvs'], broker_state['rmn_qty'], broker_state['cncl_yn'], broker_state['rjct_qty'], broker_state['orgno'], broker_state['ord_tmd'], order_id))
            else:
                conn.execute("UPDATE order_intents SET tot_ccld_qty=?, tot_ccld_amt=?, avg_prvs=?, rmn_qty=?, cncl_yn=?, rjct_qty=?, orgno=?, ord_tmd=?, updated_at=? WHERE id=?", (broker_state['tot_ccld_qty'], broker_state['tot_ccld_amt'], broker_state['avg_prvs'], broker_state['rmn_qty'], broker_state['cncl_yn'], broker_state['rjct_qty'], broker_state['orgno'], broker_state['ord_tmd'], now_str, order_id))
            conn.execute("COMMIT")
            return True
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in apply_broker_receipt: {e}")

def transition_order_status(order_id, current_status, new_status, broker_id="", branch="", broker_order_time="", code="", worker_id=None, fencing_token=None, reason=""):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []):
        return False
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            if broker_id and branch:
                conn.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, broker_order_time=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, broker_id, branch, broker_order_time, code, now_str, order_id, current_status))
            else:
                conn.execute("UPDATE order_intents SET status=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, code, now_str, order_id, current_status))
            rows = conn.execute("SELECT changes()").fetchone()[0]
            if rows > 0:
                c_row = conn.execute("SELECT correlation_id FROM order_intents WHERE id=?", (order_id,)).fetchone()
                corr_id = c_row['correlation_id'] if c_row else None
                conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, worker_id, fencing_token, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                             (order_id, corr_id, "STATUS_CHANGE", current_status, new_status, worker_id, fencing_token, reason, now_str, f"{current_status} -> {new_status} ({code})"))
            conn.execute("COMMIT")
            return rows > 0
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in transition_order_status: {e}")

def claim_cancel_intent(broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            lease = conn.execute("SELECT token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND worker_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id)).fetchone()
            if not lease or lease['expires_at'] < now_str:
                conn.execute("ROLLBACK"); return None, "Expired or No Lease"
            order = conn.execute("SELECT * FROM order_intents WHERE status='CANCEL_REQUESTED' AND broker_order_id IS NOT NULL AND broker_order_id != '' AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? ORDER BY id ASC LIMIT 1", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            if not order:
                conn.execute("ROLLBACK"); return None, "No Pending Cancels"
            conn.execute("UPDATE order_intents SET status='CANCEL_CLAIMED', fencing_token=?, updated_at=? WHERE id=?", (lease['token'], now_str, order['id']))
            conn.execute("COMMIT")
            return dict(order), "OK"
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in claim_cancel_intent: {e}")

def authorize_cancel_order(order_id, worker_id, fencing_token):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            res = conn.execute("UPDATE order_intents SET status='CANCEL_SUBMITTING', updated_at=? WHERE id=? AND status='CANCEL_CLAIMED' AND fencing_token=?", (now_str, order_id, fencing_token))
            if res.rowcount == 0:
                conn.execute("ROLLBACK")
                return False, "Atomic CAS Failed"
            conn.execute("COMMIT")
            return True, "Passed"
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in authorize_cancel_order: {e}")

def revert_stale_claims(broker, env, acc_fp, prdt_cd, port_id, strat_id):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            query = "SELECT id, correlation_id, fencing_token, status FROM order_intents WHERE status IN ('CLAIMED', 'CANCEL_CLAIMED', 'CANCEL_SUBMITTING') AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?"
            claimed = conn.execute(query, (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchall()
            for c in claimed:
                lease = conn.execute("SELECT expires_at FROM worker_leases WHERE token=?", (c['fencing_token'],)).fetchone()
                if not lease or lease['expires_at'] < now_str:
                    new_status = 'INTENT_CREATED' if c['status'] == 'CLAIMED' else 'CANCEL_REQUESTED'
                    conn.execute("UPDATE order_intents SET status=?, updated_at=? WHERE id=?", (new_status, now_str, c['id']))
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in revert_stale_claims: {e}")

def claim_intent(broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            lease = conn.execute("SELECT token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND worker_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id)).fetchone()
            if not lease or lease['expires_at'] < now_str:
                conn.execute("ROLLBACK"); return None, "Expired or No Lease"
            order = conn.execute("SELECT * FROM order_intents WHERE status='INTENT_CREATED' AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? ORDER BY id ASC LIMIT 1", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            if not order:
                conn.execute("ROLLBACK"); return None, "No Pending Intents"
            conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=?, updated_at=? WHERE id=?", (lease['token'], now_str, order['id']))
            conn.execute("COMMIT")
            return dict(order), "OK"
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in claim_intent: {e}")

def authorize_claimed_order(order_id, broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, actual_cash, current_price, is_halted, daily_pnl_pct, current_exposure, max_exposure):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            now_kst = datetime.now(KST)

            order = conn.execute("SELECT * FROM order_intents WHERE id=?", (order_id,)).fetchone()
            if not order or order['status'] != 'CLAIMED':
                conn.execute("ROLLBACK"); return None, False, "Order not in CLAIMED state"
            if order['broker'] != broker or order['environment'] != env or order['account_fingerprint'] != acc_fp or order['product_code'] != prdt_cd or order['portfolio_id'] != port_id or order['strategy_id'] != strat_id:
                conn.execute("ROLLBACK"); return dict(order), False, "Scope Mismatch"

            def reject(new_status, reason, details):
                conn.execute("UPDATE order_intents SET status=?, updated_at=? WHERE id=?", (new_status, now_str, order_id))
                conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, worker_id, fencing_token, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (order_id, order['correlation_id'], "STATUS_CHANGE", "CLAIMED", new_status, worker_id, order['fencing_token'], reason, now_str, details))
                conn.execute("COMMIT")
                return dict(order), False, details

            lease = conn.execute("SELECT token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND worker_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id)).fetchone()
            if not lease or lease['expires_at'] < now_str or lease['token'] != order['fencing_token']:
                return reject('QUARANTINED', 'INVALID_LEASE', 'Invalid fencing token or lease expired')

            m_ks = conn.execute("SELECT value FROM settings WHERE key='master_kill_switch'").fetchone()
            a_ks = conn.execute("SELECT value FROM settings WHERE key=?", (f"kill_switch_{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}",)).fetchone()
            if (m_ks and json.loads(m_ks['value'])) or (a_ks and json.loads(a_ks['value'])):
                return reject('RISK_REJECTED', 'KILL_SWITCH', 'Kill Switch ON')

            if env == "REAL":
                real_status = CONTRACT.get('execution_rules', {}).get('real_approval_status', 'POST_BLOCKED')
                if real_status != 'APPROVED':
                    return reject('RISK_REJECTED', 'POST_BLOCKED', 'REAL POST Strictly Blocked')

            if order['signal_source'] == 'SYSTEM':
                ap = conn.execute("SELECT value FROM settings WHERE key=?", (f"auto_pilot_{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}",)).fetchone()
                if not ap or not json.loads(ap['value']):
                    return reject('RISK_REJECTED', 'AUTO_PILOT_OFF', 'Auto Pilot is OFF')
                    
            at = conn.execute("SELECT value FROM settings WHERE key=?", (f"auto_trade_{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}",)).fetchone()
            if order['signal_source'] == 'SYSTEM' and env == "REAL" and (not at or not json.loads(at['value'])):
                return reject('RISK_REJECTED', 'AUTO_TRADE_OFF', 'Auto Trade is OFF for REAL')

            real_status = CONTRACT.get('execution_rules', {}).get('real_approval_status', 'POST_BLOCKED')
            if env == "REAL" and real_status != "APPROVED" and order['signal_source'] == 'SYSTEM':
                return reject('RISK_REJECTED', 'REAL_BLOCKED', 'REAL Blocked by System Contract')

            intent_created = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
            if (now_kst - intent_created).total_seconds() > order['intent_ttl']:
                return reject('EXPIRED', 'TTL_EXCEEDED', 'Intent TTL Exceeded')

            sys_strat_ver = CONTRACT.get('strategy_version', '1.0.0')
            sys_cont_ver = CONTRACT.get('contract_version', '1.0.0')
            sys_cost_ver = CONTRACT.get('cost_model_version', '2.2.0')
            
            if str(order['strategy_version']) != str(sys_strat_ver) or \
               str(order['contract_version']) != str(sys_cont_ver) or \
               str(order['cost_model_version']) != str(sys_cost_ver):
                return reject('QUARANTINED', 'VERSION_MISMATCH', f'Version mismatch: strat({order["strategy_version"]} vs {sys_strat_ver}), cont({order["contract_version"]} vs {sys_cont_ver}), cost({order["cost_model_version"]} vs {sys_cost_ver})')

            if order['side'] not in ('BUY', 'SELL') or order['qty'] <= 0 or order['order_kind'] not in ('MARKET', 'LIMIT') or order['reference_price'] <= 0:
                return reject('RISK_REJECTED', 'INVALID_SPEC', 'Invalid Spec (Side, Qty, Kind, Price)')

            if is_halted:
                return reject('RISK_REJECTED', 'MARKET_HALTED', 'Market Halted')
            try:
                quote_ts = datetime.strptime(order['quote_timestamp'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
                if (now_kst - quote_ts).total_seconds() > CONTRACT['execution_rules']['quote_freshness_ttl_sec']:
                    return reject('EXPIRED', 'STALE_QUOTE', 'Quote Freshness TTL Exceeded')
            except ValueError:
                return reject('QUARANTINED', 'INVALID_QUOTE_TS', 'Unparseable quote timestamp')

            if abs(current_price - order['reference_price']) / order['reference_price'] > 0.05:
                return reject('RISK_REJECTED', 'PRICE_DEVIATION', f"Price deviated >5% (Ref:{order['reference_price']}, Cur:{current_price})")

            if daily_pnl_pct < -0.05 and order['side'] == 'BUY':
                return reject('RISK_REJECTED', 'DAILY_LOSS_LIMIT', 'Daily PnL < -5%')

            open_states = "('CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED')"
            if order['side'] == 'BUY':
                buffer_multi = CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)
                req_val = order['qty'] * (order['reference_price'] * buffer_multi if order['order_kind'] == 'MARKET' else order['limit_price'])
                
                reserved_exposure_row = conn.execute(f"""
                    SELECT SUM((qty - cum_filled_qty) * (CASE WHEN order_kind='MARKET' THEN reference_price * ? ELSE limit_price END)) as res_exp 
                    FROM order_intents 
                    WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? 
                    AND side='BUY' AND status IN {open_states} AND id != ?
                """, (buffer_multi, broker, env, acc_fp, prdt_cd, port_id, strat_id, order_id)).fetchone()
                
                reserved_exposure = float(reserved_exposure_row['res_exp']) if reserved_exposure_row['res_exp'] else 0.0
                
                if current_exposure + reserved_exposure + req_val > max_exposure:
                    return reject('RISK_REJECTED', 'EXPOSURE_LIMIT_WITH_PENDING', f'Exceeds Max Exposure including pending orders')
                
                reserved = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * (CASE WHEN order_kind='MARKET' THEN reference_price * ? ELSE limit_price END)) as res FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND side='BUY' AND status IN {open_states} AND id != ?", (buffer_multi, broker, env, acc_fp, prdt_cd, port_id, strat_id, order_id)).fetchone()
                res_cash = float(reserved['res']) if reserved['res'] else 0.0
                if (actual_cash - res_cash) < req_val:
                    return reject('RISK_REJECTED', 'INSUFFICIENT_CASH', f'Insufficient Cash')
            elif order['side'] == 'SELL':
                pos = conn.execute("SELECT managed_qty, manual_qty FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, order['ticker'])).fetchone()
                
                is_manual_order = (order['signal_source'] != 'SYSTEM')
                available_qty = (pos['manual_qty'] if pos else 0) if is_manual_order else (pos['managed_qty'] if pos else 0)
                
                reserved = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as r_qty FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=? AND side='SELL' AND signal_source {'!=' if is_manual_order else '='} 'SYSTEM' AND status IN {open_states} AND id != ?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, order['ticker'], order_id)).fetchone()
                r_qty = int(reserved['r_qty']) if reserved['r_qty'] else 0
                
                if (available_qty - r_qty) < order['qty']:
                    return reject('RISK_REJECTED', 'INSUFFICIENT_QTY', f'Insufficient Qty')

            res = conn.execute("UPDATE order_intents SET status='SUBMITTING', updated_at=? WHERE id=? AND status='CLAIMED' AND fencing_token=?", (now_str, order_id, order['fencing_token']))
            
            if res.rowcount == 0:
                conn.execute("ROLLBACK")
                return None, False, "Atomic CAS Failed: Token mismatch or order state altered by another worker"

            conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, worker_id, fencing_token, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (order_id, order['correlation_id'], "STATUS_CHANGE", "CLAIMED", "SUBMITTING", worker_id, order['fencing_token'], "GATE_PASSED_ALL", now_str, "Intent 11-step CAS authorized"))
            conn.execute("COMMIT")

            order_updated = conn.execute("SELECT * FROM order_intents WHERE id=?", (order_id,)).fetchone()
            return dict(order_updated), True, "Passed Atomic Gate"

        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in authorize_claimed_order: {e}")

def request_cancel_for_system_orders(account_fp, strategy):
    """[P0-3] Kill Switch: 시스템 주문 안전 취소 요청"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE order_intents 
            SET status = 'CANCEL_REQUESTED', updated_at = CURRENT_TIMESTAMP
            WHERE account_fp = ? AND strategy = ? AND status IN ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'PENDING')
        ''', (account_fp, strategy))
        conn.commit()
        return cursor.rowcount


def preflight_check():
    """[P0-2] DB 초기화 및 V17 하드 마이그레이션"""
    import sqlite3
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN EXCLUSIVE TRANSACTION")
        try:
            # 1. 필수 테이블 무조건 생성 (V17 기준)
            cursor.executescript('''
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS watchlists (
                    ticker TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source TEXT DEFAULT 'MANUAL',
                    provenance TEXT DEFAULT '',
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS watchlist_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT,
                    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS positions (
                    account_fp TEXT,
                    strategy TEXT,
                    ticker TEXT,
                    quantity INTEGER DEFAULT 0,
                    managed_quantity INTEGER DEFAULT 0,
                    manual_quantity INTEGER DEFAULT 0,
                    managed_buy_price REAL DEFAULT 0.0,
                    manual_buy_price REAL DEFAULT 0.0,
                    PRIMARY KEY (account_fp, strategy, ticker)
                );
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    account_fp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT DEFAULT 'PENDING',
                    broker_order_id TEXT,
                    broker_order_time TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            
            # 2. 기존 DB 마이그레이션 (열 추가)
            cursor.execute("PRAGMA table_info(positions)")
            pos_cols = [row['name'] for row in cursor.fetchall()]
            if 'managed_buy_price' not in pos_cols:
                cursor.execute("ALTER TABLE positions ADD COLUMN managed_buy_price REAL DEFAULT 0.0")
            if 'manual_buy_price' not in pos_cols:
                cursor.execute("ALTER TABLE positions ADD COLUMN manual_buy_price REAL DEFAULT 0.0")

            cursor.execute("PRAGMA table_info(order_intents)")
            ord_cols = [row['name'] for row in cursor.fetchall()]
            if 'broker_order_time' not in ord_cols:
                cursor.execute("ALTER TABLE order_intents ADD COLUMN broker_order_time TEXT")

            cursor.execute("PRAGMA user_version = 17")
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
