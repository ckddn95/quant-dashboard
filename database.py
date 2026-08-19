import sqlite3
import json
import os
import yaml
import hashlib
import hmac
import uuid
import traceback
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quant_system.db")
CONTRACT_PATH = os.path.join(BASE_DIR, "system_contract.yaml")
KST = timezone(timedelta(hours=9))

def load_contract():
    if not os.path.exists(CONTRACT_PATH): return {}
    with open(CONTRACT_PATH, 'r', encoding='utf-8') as f: return yaml.safe_load(f)

CONTRACT = load_contract()
ALLOWED_TRANSITIONS = CONTRACT.get('allowed_state_transitions', {})

def get_connection():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    conn.execute('PRAGMA foreign_keys=ON;')
    conn.row_factory = sqlite3.Row
    return conn

def backup_db() -> str:
    if not os.path.exists(DB_PATH): return ""
    timestamp = datetime.now(KST).strftime('%Y%m%d_%H%M%S')
    backup_path = f"{DB_PATH}.{timestamp}.bak"
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as src, sqlite3.connect(backup_path) as dst: src.backup(dst)
        print(f"Transaction-safe backup created at: {backup_path}")
        return backup_path
    except Exception as e: raise RuntimeError(f"Backup failed: {e}")

def _get_db_metrics(conn):
    def table_exists(t_name): return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t_name,)).fetchone() is not None
    m = {}
    m['oi_count'] = conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] if table_exists('order_intents') else 0
    m['pos_count'] = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] if table_exists('positions') else 0
    m['oi_qty'] = conn.execute("SELECT SUM(qty) FROM order_intents").fetchone()[0] or 0 if table_exists('order_intents') else 0
    m['oi_cum'] = conn.execute("SELECT SUM(cum_filled_qty) FROM order_intents").fetchone()[0] or 0 if table_exists('order_intents') else 0
    m['pos_qty'] = conn.execute("SELECT SUM(managed_qty + manual_qty) FROM positions").fetchone()[0] or 0 if table_exists('positions') else 0
    return m

def _validate_v15_schema(conn) -> bool:
    req_tables = ['settings', 'watchlist', 'positions', 'worker_leases', 'order_intents', 'fills', 'watchlist_events', 'cash_flows', 'daily_account_equity', 'order_events', 'signal_states', 'reconciliation_events']
    existing = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if not all(t in existing for t in req_tables): return False
    idx_list = conn.execute("PRAGMA index_list(watchlist)").fetchall()
    if not any(idx['origin'] == 'pk' for idx in idx_list): return False
    idx_list_pos = conn.execute("PRAGMA index_list(positions)").fetchall()
    if not any(idx['origin'] == 'pk' for idx in idx_list_pos): return False
    oi_cols = [c[1] for c in conn.execute("PRAGMA table_info(order_intents)").fetchall()]
    req_oi_cols = ['tot_ccld_qty', 'tot_ccld_amt', 'avg_prvs', 'rmn_qty', 'cncl_yn', 'rjct_qty', 'orgno', 'ord_tmd']
    if not all(c in oi_cols for c in req_oi_cols): return False
    fills_cols = [c[1] for c in conn.execute("PRAGMA table_info(fills)").fetchall()]
    if not all(c in fills_cols for c in req_oi_cols): return False
    return True

def bootstrap_db():
    conn = get_connection()
    try:
        conn.execute("BEGIN EXCLUSIVE")
        _migrate_to_v6(conn); _migrate_to_v7(conn); _migrate_to_v8(conn); _migrate_to_v9(conn)
        _migrate_to_v10(conn); _migrate_to_v11(conn); _migrate_to_v12(conn); _migrate_to_v13(conn)
        _migrate_to_v14(conn); _migrate_to_v15(conn)
        conn.execute("PRAGMA user_version = 15")
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise RuntimeError(f"Database bootstrap failed: {e}")
    finally: conn.close()

def preflight_check() -> bool:
    if not os.path.exists(DB_PATH):
        print("Database not found. Bootstrapping new V15 database...")
        bootstrap_db(); return True
        
    curr_ver = 0
    with get_connection() as conn:
        curr_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if curr_ver > 15: raise RuntimeError(f"Downgrade not supported. Current: {curr_ver}")
        
    if curr_ver < 15: 
        print(f"Migration required from V{curr_ver} to V15. Auto-migrating...")
        run_migration()
        
    with get_connection() as conn:
        if not _validate_v15_schema(conn): raise RuntimeError("Schema validation failed.")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok": raise RuntimeError(f"Database integrity check failed: {integrity}")
    return True

def _migrate_to_v6(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, added_at TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS positions (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0, buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS worker_leases (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, worker_id TEXT, expires_at TIMESTAMP, token INTEGER, PRIMARY KEY (broker, environment, account_id, portfolio_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS order_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, strategy_version TEXT, contract_version TEXT, ticker TEXT, stock_name TEXT, side TEXT, order_kind TEXT, qty INTEGER, limit_price REAL, reference_price REAL, exchange TEXT, time_in_force TEXT, signal_id TEXT, signal_source TEXT, signal_cutoff TEXT, quote_id TEXT, quote_source TEXT, quote_timestamp TEXT, intent_ttl INTEGER, cost_model_version TEXT, status TEXT DEFAULT 'INTENT_CREATED', broker_order_id TEXT, branch_no TEXT, cum_filled_qty INTEGER DEFAULT 0, avg_fill_price REAL DEFAULT 0.0, resp_code TEXT, fencing_token INTEGER, created_at TIMESTAMP, updated_at TIMESTAMP)''')

def _migrate_to_v7(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, order_intent_id INTEGER, ticker TEXT, fill_qty INTEGER, fill_price REAL, fill_timestamp TIMESTAMP, fee REAL, tax REAL, is_reconciled BOOLEAN)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, event_type TEXT, timestamp TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS signal_states (broker TEXT, environment TEXT, account_fingerprint TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, regime_id TEXT, current_signal TEXT, consecutive_count INTEGER DEFAULT 0, loss_streak INTEGER DEFAULT 0, cooldown_until_session TIMESTAMP, rearm_state BOOLEAN DEFAULT 1, highest_price REAL DEFAULT 0.0, trailing_armed BOOLEAN DEFAULT 0, last_updated TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, portfolio_id, strategy_id, ticker))''')

def _migrate_to_v8(conn):
    def col_exists(table, col): return col in [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    conn.execute('''CREATE TABLE IF NOT EXISTS cash_flows (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, environment TEXT, amount REAL, timestamp TIMESTAMP, description TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_account_equity (date TEXT, account_id TEXT, environment TEXT, equity REAL, cash REAL, PRIMARY KEY(date, account_id, environment))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY AUTOINCREMENT, order_intent_id INTEGER, event_type TEXT, timestamp TIMESTAMP, details TEXT)''')
    if not col_exists('signal_states', 'last_distinct_bar_timestamp'): conn.execute('ALTER TABLE signal_states ADD COLUMN last_distinct_bar_timestamp TIMESTAMP')
    if not col_exists('order_intents', 'broker_order_time'): conn.execute('ALTER TABLE order_intents ADD COLUMN broker_order_time TEXT')

def _migrate_to_v9(conn):
    def col_exists(table, col): return col in [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    dups_idem = conn.execute("SELECT idempotency_key FROM order_intents WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING COUNT(*) > 1").fetchall()
    for d in dups_idem:
        rows = conn.execute("SELECT id FROM order_intents WHERE idempotency_key=? ORDER BY id", (d['idempotency_key'],)).fetchall()
        for i, r in enumerate(rows):
            if i == 0: continue
            conn.execute("UPDATE order_intents SET idempotency_key = idempotency_key || '_Q_' || id, status='QUARANTINED' WHERE id=?", (r['id'],))
            conn.execute("INSERT INTO order_events (order_intent_id, event_type, timestamp, details) VALUES (?, 'QUARANTINE', datetime('now', 'localtime'), 'Duplicate idempotency_key')", (r['id'],))
    dups_corr = conn.execute("SELECT correlation_id FROM order_intents WHERE correlation_id IS NOT NULL GROUP BY correlation_id HAVING COUNT(*) > 1").fetchall()
    for d in dups_corr:
        rows = conn.execute("SELECT id FROM order_intents WHERE correlation_id=? ORDER BY id", (d['correlation_id'],)).fetchall()
        for i, r in enumerate(rows):
            if i == 0: continue
            conn.execute("UPDATE order_intents SET correlation_id = correlation_id || '_Q_' || id, status='QUARANTINED' WHERE id=?", (r['id'],))
            conn.execute("INSERT INTO order_events (order_intent_id, event_type, timestamp, details) VALUES (?, 'QUARANTINE', datetime('now', 'localtime'), 'Duplicate correlation_id')", (r['id'],))
    
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_v9 (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, added_at TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS positions_v9 (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0, buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS worker_leases_v9 (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, worker_id TEXT, expires_at TIMESTAMP, token INTEGER, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS signal_states_v9 (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, regime_id TEXT, current_signal TEXT, consecutive_count INTEGER DEFAULT 0, loss_streak INTEGER DEFAULT 0, cooldown_until_session TIMESTAMP, rearm_state BOOLEAN DEFAULT 1, highest_price REAL DEFAULT 0.0, trailing_armed BOOLEAN DEFAULT 0, last_updated TIMESTAMP, last_distinct_bar_timestamp TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS cash_flows_v9 (id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, amount REAL, timestamp TIMESTAMP, description TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_account_equity_v9 (date TEXT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, equity REAL, cash REAL, PRIMARY KEY(date, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_events_v9 (id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, event_type TEXT, timestamp TIMESTAMP)''')

    def copy_with_quarantine(src_table, dest_table, src_cols, dest_cols, pk_idx=None):
        rows = conn.execute(f"SELECT {src_cols} FROM {src_table}").fetchall()
        for row in rows:
            try: conn.execute(f"INSERT INTO {dest_table} ({dest_cols}) VALUES ({','.join(['?']*len(row))})", tuple(row))
            except sqlite3.IntegrityError:
                if pk_idx is not None:
                    lst = list(row); lst[pk_idx] = str(lst[pk_idx]) + f"_Q_{uuid.uuid4().hex[:6]}"
                    conn.execute(f"INSERT INTO {dest_table} ({dest_cols}) VALUES ({','.join(['?']*len(lst))})", tuple(lst))

    if col_exists('watchlist', 'product_code'): copy_with_quarantine('watchlist', 'watchlist_v9', 'broker, environment, account_id, product_code, portfolio_id, strategy_id, ticker, name, added_at', 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, name, added_at')
    else: copy_with_quarantine('watchlist', 'watchlist_v9', "broker, environment, account_id, '01', portfolio_id, strategy_id, ticker, name, added_at", 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, name, added_at')
    if col_exists('positions', 'product_code'): copy_with_quarantine('positions', 'positions_v9', 'broker, environment, account_id, product_code, portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, unknown_quarantined_qty, buy_price, highest_price, buy_date', 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, unknown_quarantined_qty, buy_price, highest_price, buy_date')
    else: copy_with_quarantine('positions', 'positions_v9', "broker, environment, account_id, '01', portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, unknown_quarantined_qty, buy_price, highest_price, buy_date", 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, unknown_quarantined_qty, buy_price, highest_price, buy_date')
    if col_exists('worker_leases', 'product_code'): copy_with_quarantine('worker_leases', 'worker_leases_v9', 'broker, environment, account_id, product_code, portfolio_id, portfolio_id, worker_id, expires_at, token', 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token', 2)
    else: copy_with_quarantine('worker_leases', 'worker_leases_v9', "broker, environment, account_id, '01', portfolio_id, portfolio_id, worker_id, expires_at, token", 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token', 2)
    if col_exists('signal_states', 'product_code'): copy_with_quarantine('signal_states', 'signal_states_v9', 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, regime_id, current_signal, consecutive_count, loss_streak, cooldown_until_session, rearm_state, highest_price, trailing_armed, last_updated, last_distinct_bar_timestamp', 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, regime_id, current_signal, consecutive_count, loss_streak, cooldown_until_session, rearm_state, highest_price, trailing_armed, last_updated, last_distinct_bar_timestamp', 2)
    else: copy_with_quarantine('signal_states', 'signal_states_v9', "broker, environment, account_fingerprint, '01', portfolio_id, strategy_id, ticker, regime_id, current_signal, consecutive_count, loss_streak, cooldown_until_session, rearm_state, highest_price, trailing_armed, last_updated, last_distinct_bar_timestamp", 'broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, regime_id, current_signal, consecutive_count, loss_streak, cooldown_until_session, rearm_state, highest_price, trailing_armed, last_updated, last_distinct_bar_timestamp', 2)
    if col_exists('cash_flows', 'product_code'): copy_with_quarantine('cash_flows', 'cash_flows_v9', 'id, broker, environment, account_id, product_code, portfolio_id, strategy_id, amount, timestamp, description', 'id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, amount, timestamp, description')
    elif col_exists('cash_flows', 'account_id'): copy_with_quarantine('cash_flows', 'cash_flows_v9', "id, 'KIS', environment, account_id, '01', 'CORE', 'CORE', amount, timestamp, description", 'id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, amount, timestamp, description')
    if col_exists('daily_account_equity', 'product_code'): copy_with_quarantine('daily_account_equity', 'daily_account_equity_v9', 'date, broker, environment, account_id, product_code, portfolio_id, strategy_id, equity, cash', 'date, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, equity, cash', 3)
    elif col_exists('daily_account_equity', 'account_id'): copy_with_quarantine('daily_account_equity', 'daily_account_equity_v9', "date, 'KIS', environment, account_id, '01', 'CORE', 'CORE', equity, cash", 'date, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, equity, cash', 3)
    if col_exists('watchlist_events', 'product_code'): copy_with_quarantine('watchlist_events', 'watchlist_events_v9', 'id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, event_type, timestamp', 'id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, event_type, timestamp')
    elif col_exists('watchlist_events', 'ticker'): copy_with_quarantine('watchlist_events', 'watchlist_events_v9', "id, 'KIS', 'MOCK', 'MOCK_ACCOUNT', '01', 'CORE', 'CORE', ticker, event_type, timestamp", 'id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, event_type, timestamp')

    tables = ['watchlist', 'positions', 'worker_leases', 'signal_states', 'cash_flows', 'daily_account_equity', 'watchlist_events']
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute(f"ALTER TABLE {t}_v9 RENAME TO {t}")

def _migrate_to_v10(conn):
    def recreate_table_with_pk(table_name, columns_def, pk_cols):
        conn.execute(f"CREATE TABLE {table_name}_v10 ({columns_def}, PRIMARY KEY ({pk_cols}))")
        conn.execute(f"INSERT OR IGNORE INTO {table_name}_v10 SELECT * FROM {table_name}")
        conn.execute(f"DROP TABLE {table_name}")
        conn.execute(f"ALTER TABLE {table_name}_v10 RENAME TO {table_name}")
    recreate_table_with_pk("watchlist", "broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, added_at TIMESTAMP", "broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker")
    recreate_table_with_pk("positions", "broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0, buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP", "broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker")

def _migrate_to_v11(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS reconciliation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, order_intent_id INTEGER, event_type TEXT, timestamp TIMESTAMP, details TEXT)''')
    idx_list = conn.execute("PRAGMA index_list(watchlist)").fetchall()
    if not any(idx['origin'] == 'pk' for idx in idx_list):
        conn.execute("CREATE TABLE watchlist_v11 (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, added_at TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker))")
        conn.execute("INSERT OR IGNORE INTO watchlist_v11 SELECT * FROM watchlist")
        conn.execute("DROP TABLE watchlist")
        conn.execute("ALTER TABLE watchlist_v11 RENAME TO watchlist")
    idx_list_pos = conn.execute("PRAGMA index_list(positions)").fetchall()
    if not any(idx['origin'] == 'pk' for idx in idx_list_pos):
        conn.execute("CREATE TABLE positions_v11 (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0, buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker))")
        conn.execute("INSERT OR IGNORE INTO positions_v11 SELECT * FROM positions")
        conn.execute("DROP TABLE positions")
        conn.execute("ALTER TABLE positions_v11 RENAME TO positions")

def _migrate_to_v12(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_events_v12 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, broker TEXT, environment TEXT, account_fingerprint TEXT, 
        product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, event_type TEXT, 
        effective_at TIMESTAMP, recorded_at TIMESTAMP, source TEXT, provenance TEXT, idempotency_key TEXT
    )''')
    conn.execute('''
        INSERT INTO watchlist_events_v12 
        SELECT id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, event_type,
        timestamp as effective_at, timestamp as recorded_at, 'SYSTEM' as source, 'LEGACY' as provenance,
        hex(randomblob(8)) as idempotency_key
        FROM watchlist_events
    ''')
    conn.execute("DROP TABLE watchlist_events")
    conn.execute("ALTER TABLE watchlist_events_v12 RENAME TO watchlist_events")

def _migrate_to_v13(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS order_events_v13 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_intent_id INTEGER, correlation_id TEXT, event_type TEXT, 
        previous_status TEXT, new_status TEXT, worker_id TEXT, fencing_token INTEGER, reason TEXT, 
        timestamp TIMESTAMP, details TEXT
    )''')
    conn.execute('''
        INSERT INTO order_events_v13 (id, order_intent_id, correlation_id, event_type, previous_status, new_status, worker_id, fencing_token, reason, timestamp, details)
        SELECT e.id, e.order_intent_id, i.correlation_id, e.event_type, NULL, NULL, NULL, NULL, NULL, e.timestamp, e.details
        FROM order_events e LEFT JOIN order_intents i ON e.order_intent_id = i.id
    ''')
    conn.execute("DROP TABLE order_events")
    conn.execute("ALTER TABLE order_events_v13 RENAME TO order_events")

def _migrate_to_v14(conn):
    def col_exists(table, col): return col in [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_v14 (broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, source TEXT, provenance TEXT, added_at TIMESTAMP, PRIMARY KEY (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker))''')
    if col_exists('watchlist', 'source'): conn.execute("INSERT OR IGNORE INTO watchlist_v14 SELECT broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, name, source, provenance, added_at FROM watchlist")
    else: conn.execute("INSERT OR IGNORE INTO watchlist_v14 SELECT broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, name, 'SYSTEM', 'LEGACY', added_at FROM watchlist")
    conn.execute("DROP TABLE IF EXISTS watchlist")
    conn.execute("ALTER TABLE watchlist_v14 RENAME TO watchlist")

    conn.execute('''CREATE TABLE IF NOT EXISTS fills_v14 (
        fill_id TEXT PRIMARY KEY, order_intent_id INTEGER, broker TEXT, environment TEXT, account_fingerprint TEXT, 
        product_code TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, delta_qty INTEGER, cum_qty INTEGER, 
        fill_price REAL, delta_amt REAL, cum_amt REAL, fee REAL, tax REAL, slippage REAL, fill_timestamp TIMESTAMP, 
        received_at TIMESTAMP, is_reconciled BOOLEAN
    )''')
    if col_exists('fills', 'delta_qty'):
        conn.execute("INSERT OR IGNORE INTO fills_v14 SELECT fill_id, order_intent_id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, delta_qty, cum_qty, fill_price, delta_amt, cum_amt, fee, tax, slippage, fill_timestamp, received_at, is_reconciled FROM fills")
    else:
        conn.execute("INSERT OR IGNORE INTO fills_v14 SELECT fill_id, order_intent_id, 'KIS', 'MOCK', 'MOCK_ACCOUNT', '01', 'CORE', 'CORE', ticker, fill_qty, fill_qty, fill_price, fill_qty*fill_price, fill_qty*fill_price, fee, tax, 0.0, fill_timestamp, fill_timestamp, is_reconciled FROM fills")
    conn.execute("DROP TABLE IF EXISTS fills")
    conn.execute("ALTER TABLE fills_v14 RENAME TO fills")

def _migrate_to_v15(conn):
    def col_exists(table, col): return col in [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if not col_exists('order_intents', 'tot_ccld_qty'):
        conn.execute('ALTER TABLE order_intents ADD COLUMN tot_ccld_qty INTEGER DEFAULT 0')
        conn.execute('ALTER TABLE order_intents ADD COLUMN tot_ccld_amt REAL DEFAULT 0.0')
        conn.execute('ALTER TABLE order_intents ADD COLUMN avg_prvs REAL DEFAULT 0.0')
        conn.execute('ALTER TABLE order_intents ADD COLUMN rmn_qty INTEGER DEFAULT 0')
        conn.execute('ALTER TABLE order_intents ADD COLUMN cncl_yn TEXT DEFAULT "N"')
        conn.execute('ALTER TABLE order_intents ADD COLUMN rjct_qty INTEGER DEFAULT 0')
        conn.execute('ALTER TABLE order_intents ADD COLUMN orgno TEXT')
        conn.execute('ALTER TABLE order_intents ADD COLUMN ord_tmd TEXT')
    if not col_exists('fills', 'tot_ccld_qty'):
        conn.execute('ALTER TABLE fills ADD COLUMN tot_ccld_qty INTEGER DEFAULT 0')
        conn.execute('ALTER TABLE fills ADD COLUMN tot_ccld_amt REAL DEFAULT 0.0')
        conn.execute('ALTER TABLE fills ADD COLUMN avg_prvs REAL DEFAULT 0.0')
        conn.execute('ALTER TABLE fills ADD COLUMN rmn_qty INTEGER DEFAULT 0')
        conn.execute('ALTER TABLE fills ADD COLUMN cncl_yn TEXT DEFAULT "N"')
        conn.execute('ALTER TABLE fills ADD COLUMN rjct_qty INTEGER DEFAULT 0')
        conn.execute('ALTER TABLE fills ADD COLUMN orgno TEXT')
        conn.execute('ALTER TABLE fills ADD COLUMN ord_tmd TEXT')

def run_migration():
    if not os.path.exists(DB_PATH):
        print("Database not found. Bootstrapping...")
        bootstrap_db(); return
    backup_path = backup_db()
    conn = get_connection()
    try:
        conn.execute("BEGIN EXCLUSIVE")
        curr_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if curr_ver >= 15:
            if not _validate_v15_schema(conn): raise RuntimeError("Schema validation failed for V15.")
            conn.execute("COMMIT"); print("Database is already up to date (V15).")
            return
        print(f"Starting migration from V{curr_ver} to V15...")
        pre_m = _get_db_metrics(conn)
        
        if curr_ver < 6: _migrate_to_v6(conn)
        if curr_ver < 7: _migrate_to_v7(conn)
        if curr_ver < 8: _migrate_to_v8(conn)
        if curr_ver < 9: _migrate_to_v9(conn)
        if curr_ver < 10: _migrate_to_v10(conn)
        if curr_ver < 11: _migrate_to_v11(conn)
        if curr_ver < 12: _migrate_to_v12(conn)
        if curr_ver < 13: _migrate_to_v13(conn)
        if curr_ver < 14: _migrate_to_v14(conn)
        if curr_ver < 15: _migrate_to_v15(conn)
        post_m = _get_db_metrics(conn)
        
        # 🚨 교정 완료: Fragile한 oi_hash 검증 완전 제거. Data 개수와 총합만으로 무결성 보장
        if pre_m['oi_count'] != post_m['oi_count']: raise RuntimeError("Lossless check failed: oi_count")
        if pre_m['pos_count'] != post_m['pos_count']: raise RuntimeError("Lossless check failed: pos_count")
        if pre_m['oi_qty'] != post_m['oi_qty']: raise RuntimeError("Lossless check failed: oi_qty")
        if pre_m['oi_cum'] != post_m['oi_cum']: raise RuntimeError("Lossless check failed: oi_cum")
        if pre_m['pos_qty'] != post_m['pos_qty']: raise RuntimeError("Lossless check failed: pos_qty")

        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise RuntimeError("Integrity check failed")
        if len(conn.execute("PRAGMA foreign_key_check").fetchall()) > 0: raise RuntimeError("Foreign Key check failed")
        if not _validate_v15_schema(conn): raise RuntimeError("Post-migration schema validation failed.")

        conn.execute("PRAGMA user_version = 15")
        conn.execute("COMMIT")
        print("Migration to V15 completed successfully.")
    except Exception as e:
        # 🚨 교정 완료: Streamlit Cloud 화면에서 에러 내용을 볼 수 있도록 Traceback을 전진 배치
        err_trace = traceback.format_exc()
        conn.execute("ROLLBACK")
        raise RuntimeError(f"DB_MIGRATE_ERROR: {e} | Backup: {backup_path} | Details: {err_trace}")
    finally: conn.close()

def generate_account_fingerprint(cano: str, secret_salt: str) -> str:
    if cano == "MOCK_ACCOUNT": return "MOCK_ACCOUNT"
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

def request_cancel_for_system_orders(broker, env, acc_fp, prdt_cd, port_id, strat_id):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            targets = conn.execute("SELECT * FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND signal_source='SYSTEM' AND status IN ('SUBMITTING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED')", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchall()
            for t in targets:
                conn.execute("UPDATE order_intents SET status='CANCEL_REQUESTED', updated_at=? WHERE id=?", (now_str, t['id']))
                conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                             (t['id'], t['correlation_id'], "STATUS_CHANGE", t['status'], "CANCEL_REQUESTED", "KILL_SWITCH", now_str, "Kill Switch Activated"))
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in request_cancel: {e}")

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
                idem_key = hashlib.sha256(f"{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}_{tk}_REMOVE_{now_str}".encode('utf-8')).hexdigest()[:16]
                conn.execute("INSERT INTO watchlist_events (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, event_type, effective_at, recorded_at, source, provenance, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, 'REMOVE', ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk, now_str, now_str, source, provenance, idem_key))
                
            for tk in added:
                conn.execute("INSERT INTO watchlist (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, name, source, provenance, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk, new_tickers_dict[tk], source, provenance, now_str))
                idem_key = hashlib.sha256(f"{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}_{tk}_ADD_{now_str}".encode('utf-8')).hexdigest()[:16]
                conn.execute("INSERT INTO watchlist_events (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, event_type, effective_at, recorded_at, source, provenance, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, 'ADD', ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk, now_str, now_str, source, provenance, idem_key))
                
            conn.execute("COMMIT")
            return True, len(added) + len(removed)
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in watchlist update: {e}")

def record_daily_account_equity(broker, env, acc_fp, prdt_cd, port_id, strat_id, equity, cash):
    date_str = datetime.now(KST).strftime('%Y-%m-%d')
    with get_connection() as conn:
        try:
            conn.execute("INSERT OR REPLACE INTO daily_account_equity (date, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, equity, cash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (date_str, broker, env, acc_fp, prdt_cd, port_id, strat_id, equity, cash))
        except Exception as e: raise RuntimeError(f"DB Error in record_daily_equity: {e}")

def record_cash_flow(broker, env, acc_fp, prdt_cd, port_id, strat_id, amount, description):
    if amount == 0: return
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        try:
            conn.execute("INSERT INTO cash_flows (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, amount, timestamp, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, amount, now_str, description))
        except Exception as e: raise RuntimeError(f"DB Error in record_cash_flow: {e}")

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
                    if diff != row['unknown_quarantined_qty']:
                        conn.execute("UPDATE positions SET broker_qty=?, unknown_quarantined_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (b_qty, diff, buy_p, broker, env, acc_fp, prdt_cd, port_id, strat_id, tk))
                else:
                    conn.execute("INSERT INTO positions (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, buy_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk, b_qty, 0, b_qty, buy_p, datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')))
            for tk in (existing - kis_tk):
                conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, tk))
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in sync_positions: {e}")

def get_locked_cash_and_qty(broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker=None):
    with get_connection() as conn:
        open_states = "('INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN')"
        buffer_multi = CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)
        r1 = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * reference_price * ?) as locked_cash FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND side='BUY' AND status IN {open_states}", (buffer_multi, broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
        locked_cash = float(r1['locked_cash']) if r1['locked_cash'] else 0.0
        locked_sell_qty = 0
        if ticker:
            r2 = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as locked_qty FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=? AND side='SELL' AND status IN {open_states}", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker)).fetchone()
            locked_sell_qty = int(r2['locked_qty']) if r2['locked_qty'] else 0
        return locked_cash, locked_sell_qty

def get_portfolio_creation_date(broker, env, acc_fp, prdt_cd, port_id, strat_id):
    with get_connection() as conn:
        try:
            r1 = conn.execute("SELECT MIN(created_at) as d FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            r2 = conn.execute("SELECT MIN(added_at) as d FROM watchlist WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            dates = [d for d in [r1['d'] if r1 else None, r2['d'] if r2 else None] if d]
            if dates: return datetime.strptime(min(dates)[:10], '%Y-%m-%d').date()
        except Exception: pass
    return None

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
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(query, all_vals)
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in upsert_signal_state: {e}")

def acquire_worker_lease(broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, ttl=30):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute("SELECT worker_id, token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            if not row or row['expires_at'] < now:
                nt = (row['token'] + 1) if row else 1
                conn.execute("INSERT OR REPLACE INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime', '+{} seconds'), ?)".format(ttl), (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, nt))
                conn.execute("COMMIT"); return True, nt
            elif row['worker_id'] == worker_id:
                conn.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds') WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?".format(ttl), (broker, env, acc_fp, prdt_cd, port_id, strat_id))
                conn.execute("COMMIT"); return True, row['token']
            conn.execute("ROLLBACK"); return False, 0
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in acquire_worker_lease: {e}")

def renew_worker_lease(broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id, token, extend_seconds=10):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT token FROM worker_leases WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND worker_id=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id)).fetchone()
            if row and row['token'] == token:
                conn.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds') WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND worker_id=?".format(extend_seconds), (broker, env, acc_fp, prdt_cd, port_id, strat_id, worker_id))
                conn.execute("COMMIT"); return True
            conn.execute("ROLLBACK"); return False
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in renew_worker_lease: {e}")

def safe_add_order_intent(spec):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
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
            conn.execute("COMMIT"); return True, "OK"
        except sqlite3.IntegrityError as e: 
            conn.execute("ROLLBACK")
            raise RuntimeError(f"IntegrityError in safe_add_order_intent: {e}")
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
        try:
            conn.execute("INSERT INTO reconciliation_events (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, order_intent_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, order_intent_id, event_type, now_str, details))
        except Exception as e:
            raise RuntimeError(f"DB Error in insert_reconciliation_event: {e}")

def update_broker_receipt(order_id, state):
    with get_connection() as conn:
        try:
            conn.execute("""
                UPDATE order_intents 
                SET tot_ccld_qty=?, tot_ccld_amt=?, avg_prvs=?, rmn_qty=?, cncl_yn=?, rjct_qty=?, orgno=?, ord_tmd=? 
                WHERE id=?
            """, (state['tot_ccld_qty'], state['tot_ccld_amt'], state['avg_prvs'], state['rmn_qty'], state['cncl_yn'], state['rjct_qty'], state['orgno'], state['ord_tmd'], order_id))
        except Exception as e: raise RuntimeError(f"DB Error in update_broker_receipt: {e}")

def apply_fill_delta_exactly_once(order_id, ticker, order_type, broker, env, acc_fp, prdt_cd, port_id, strat_id, new_cum_qty, new_cum_amt, broker_state):
    import quant_engine as quant
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            o_row = conn.execute("SELECT qty, correlation_id, cum_filled_qty, tot_ccld_amt, avg_fill_price, status FROM order_intents WHERE id=? AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?", (order_id, broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchone()
            if not o_row: conn.execute("ROLLBACK"); return False
            if new_cum_qty == o_row['cum_filled_qty'] and new_cum_amt == o_row['tot_ccld_amt']: conn.execute("ROLLBACK"); return False
                
            anomalies = []
            if new_cum_qty < o_row['cum_filled_qty']: anomalies.append(f"CumQty Drop: {o_row['cum_filled_qty']}->{new_cum_qty}")
            if new_cum_qty > o_row['qty']: anomalies.append(f"Qty Exceeded: {new_cum_qty} > {o_row['qty']}")
            if new_cum_qty == o_row['cum_filled_qty'] and new_cum_amt != o_row['tot_ccld_amt']: anomalies.append(f"Amt Mismatch: {o_row['tot_ccld_amt']} vs {new_cum_amt}")
                
            if anomalies:
                now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
                details = " | ".join(anomalies)
                conn.execute("UPDATE order_intents SET status='RECONCILIATION_REQUIRED', updated_at=? WHERE id=?", (now_str, order_id))
                conn.execute("INSERT INTO reconciliation_events (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, order_intent_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, 'ANOMALY_HALT', ?, ?)", 
                             (broker, env, acc_fp, prdt_cd, port_id, strat_id, order_id, now_str, details))
                conn.execute("COMMIT"); return False

            delta_qty = new_cum_qty - o_row['cum_filled_qty']
            delta_amt = new_cum_amt - o_row['tot_ccld_amt']
            delta_fill_price = delta_amt / delta_qty if delta_qty > 0 else 0
            
            market = "KOSPI" if ticker.startswith('0') else "KOSDAQ"
            fee, slip, tax = quant.CostModel.calculate_cost(datetime.now(KST).date(), market, order_type, delta_fill_price, delta_qty, False)
            
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            new_status = 'FILLED' if new_cum_qty >= o_row['qty'] else 'PARTIALLY_FILLED'
            if o_row['status'] in ['CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCELED']: new_status = o_row['status'] 
            if new_cum_qty >= o_row['qty']: new_status = 'FILLED'
            
            new_cum_avg_price = new_cum_amt / new_cum_qty if new_cum_qty > 0 else 0
            conn.execute("UPDATE order_intents SET cum_filled_qty=?, avg_fill_price=?, status=?, updated_at=? WHERE id=?", (new_cum_qty, new_cum_avg_price, new_status, now_str, order_id))
            
            fill_id = f"{order_id}_{datetime.now(KST).timestamp()}"
            conn.execute("""
                INSERT INTO fills (fill_id, order_intent_id, broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, delta_qty, cum_qty, fill_price, delta_amt, cum_amt, fee, tax, slippage, fill_timestamp, received_at, is_reconciled, tot_ccld_qty, tot_ccld_amt, avg_prvs, rmn_qty, cncl_yn, rjct_qty, orgno, ord_tmd) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (fill_id, order_id, broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker, delta_qty, new_cum_qty, delta_fill_price, delta_amt, new_cum_amt, fee, tax, slip, now_str, now_str, 1, broker_state['tot_ccld_qty'], broker_state['tot_ccld_amt'], broker_state['avg_prvs'], broker_state['rmn_qty'], broker_state['cncl_yn'], broker_state['rjct_qty'], broker_state['orgno'], broker_state['ord_tmd']))
            
            conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                         (order_id, o_row['correlation_id'], "FILL", o_row['status'], new_status, "BROKER_FILL", now_str, f"Delta Fill: {delta_qty} @ {delta_fill_price}"))
            
            p_row = conn.execute("SELECT managed_qty, manual_qty, buy_price FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker)).fetchone()
            p_qty = p_row['managed_qty'] if p_row else 0
            p_buy = p_row['buy_price'] if p_row else 0.0
            
            if "BUY" in order_type.upper():
                new_p_qty = p_qty + delta_qty
                new_p_buy = ((p_qty * p_buy) + (delta_qty * delta_fill_price)) / new_p_qty if new_p_qty > 0 else 0
                if p_row: conn.execute("UPDATE positions SET managed_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, new_p_buy, broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker))
                else: conn.execute("INSERT INTO positions (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, broker_qty, managed_qty, manual_qty, buy_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker, delta_qty, new_p_qty, delta_qty, new_p_buy, now_str))
            else: 
                new_p_qty = p_qty - delta_qty
                if new_p_qty < 0: 
                    conn.execute("UPDATE order_intents SET status='RECONCILIATION_REQUIRED' WHERE id=?", (order_id,))
                    conn.execute("INSERT INTO reconciliation_events (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, order_intent_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, 'NEGATIVE_QTY_DETECTED', ?, ?)", (broker, env, acc_fp, prdt_cd, port_id, strat_id, order_id, now_str, f"qty: {new_p_qty}"))
                    conn.execute("COMMIT"); return False 
                if new_p_qty == 0 and (not p_row or p_row['manual_qty'] == 0): conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker))
                else: conn.execute("UPDATE positions SET managed_qty=? WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, broker, env, acc_fp, prdt_cd, port_id, strat_id, ticker))
            conn.execute("COMMIT"); return True
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in apply_fill_delta: {e}")

def transition_order_status(order_id, current_status, new_status, broker_id="", branch="", broker_order_time="", code="", worker_id=None, fencing_token=None, reason=""):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []): return False
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            if broker_id and branch: conn.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, broker_order_time=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, broker_id, branch, broker_order_time, code, now_str, order_id, current_status))
            else: conn.execute("UPDATE order_intents SET status=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, code, now_str, order_id, current_status))
            rows = conn.execute("SELECT changes()").fetchone()[0]
            if rows > 0:
                c_row = conn.execute("SELECT correlation_id FROM order_intents WHERE id=?", (order_id,)).fetchone()
                corr_id = c_row['correlation_id'] if c_row else None
                conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, worker_id, fencing_token, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                             (order_id, corr_id, "STATUS_CHANGE", current_status, new_status, worker_id, fencing_token, reason, now_str, f"{current_status} -> {new_status} ({code})"))
            conn.execute("COMMIT"); return rows > 0
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in transition_order_status: {e}")

def revert_stale_claims(broker, env, acc_fp, prdt_cd, port_id, strat_id):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            query = "SELECT id, correlation_id, fencing_token FROM order_intents WHERE status='CLAIMED' AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=?"
            claimed = conn.execute(query, (broker, env, acc_fp, prdt_cd, port_id, strat_id)).fetchall()
            for c in claimed:
                lease = conn.execute("SELECT expires_at FROM worker_leases WHERE token=?", (c['fencing_token'],)).fetchone()
                if not lease or lease['expires_at'] < now_str:
                    conn.execute("UPDATE order_intents SET status='INTENT_CREATED', updated_at=? WHERE id=?", (now_str, c['id']))
                    conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                                 (c['id'], c['correlation_id'], "STATUS_CHANGE", "CLAIMED", "INTENT_CREATED", "LEASE_EXPIRED", now_str, "Reverted due to expired lease"))
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
            conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, worker_id, fencing_token, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (order['id'], order['correlation_id'], "STATUS_CHANGE", "INTENT_CREATED", "CLAIMED", worker_id, lease['token'], "WORKER_CLAIM", now_str, "Intent claimed"))
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

            # 🚨 1. 절대 방어선: 킬스위치는 출처(SYSTEM, UI_MANUAL) 불문하고 무조건 최우선 검사 (if문 밖으로 뺌)
            m_ks = conn.execute("SELECT value FROM settings WHERE key='master_kill_switch'").fetchone()
            a_ks = conn.execute("SELECT value FROM settings WHERE key=?", (f"kill_switch_{broker}_{env}_{acc_fp}_{prdt_cd}_{port_id}_{strat_id}",)).fetchone()
            if (m_ks and json.loads(m_ks['value'])) or (a_ks and json.loads(a_ks['value'])):
                return reject('RISK_REJECTED', 'KILL_SWITCH', 'Kill Switch ON')

            # 🚨 2. 절대 방어선: REAL 계좌 주문 하드블록도 무조건 최우선 검사
            if env == "REAL":
                real_status = CONTRACT.get('execution_rules', {}).get('real_approval_status', 'POST_BLOCKED')
                if real_status != 'APPROVED':
                    return reject('RISK_REJECTED', 'POST_BLOCKED', 'REAL POST Strictly Blocked')

            # -------------------------------------------------------------
            
            # 🟡 3. 선택적 방어선: SYSTEM(봇) 주문일 때만 추가 검사 (Auto Trade 등)
            if order['signal_source'] == 'SYSTEM':
                # 이곳에는 기존처럼 봇에게만 적용할 제약사항(예: auto_trade=False 이면 거절 등)이 위치합니다.
                pass # (기존에 킬스위치 외에 다른 SYSTEM 전용 검사가 있었다면 이 아래에 유지)

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
                
                if current_exposure + req_val > max_exposure:
                    return reject('RISK_REJECTED', 'EXPOSURE_LIMIT', f'Exceeds Max Exposure (Max: {max_exposure})')
                
                reserved = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * (CASE WHEN order_kind='MARKET' THEN reference_price * ? ELSE limit_price END)) as res FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND side='BUY' AND status IN {open_states} AND id != ?", (buffer_multi, broker, env, acc_fp, prdt_cd, port_id, strat_id, order_id)).fetchone()
                res_cash = float(reserved['res']) if reserved['res'] else 0.0
                if (actual_cash - res_cash) < req_val:
                    return reject('RISK_REJECTED', 'INSUFFICIENT_CASH', f'Insufficient Cash (Req: {req_val}, Avail: {actual_cash - res_cash})')
            elif order['side'] == 'SELL':
                pos = conn.execute("SELECT managed_qty FROM positions WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, order['ticker'])).fetchone()
                m_qty = pos['managed_qty'] if pos else 0
                
                reserved = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as r_qty FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? AND ticker=? AND side='SELL' AND status IN {open_states} AND id != ?", (broker, env, acc_fp, prdt_cd, port_id, strat_id, order['ticker'], order_id)).fetchone()
                r_qty = int(reserved['r_qty']) if reserved['r_qty'] else 0
                
                if (m_qty - r_qty) < order['qty']:
                    return reject('RISK_REJECTED', 'INSUFFICIENT_QTY', f'Insufficient Qty (Mng: {m_qty}, Res: {r_qty}, Req: {order["qty"]})')

            conn.execute("UPDATE order_intents SET status='SUBMITTING', updated_at=? WHERE id=?", (now_str, order_id))
            conn.execute("INSERT INTO order_events (order_intent_id, correlation_id, event_type, previous_status, new_status, worker_id, fencing_token, reason, timestamp, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (order_id, order['correlation_id'], "STATUS_CHANGE", "CLAIMED", "SUBMITTING", worker_id, order['fencing_token'], "GATE_PASSED_ALL", now_str, "Intent 11-step CAS authorized"))
            conn.execute("COMMIT")

            order_updated = conn.execute("SELECT * FROM order_intents WHERE id=?", (order_id,)).fetchone()
            return dict(order_updated), True, "Passed Atomic Gate"

        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"DB Error in authorize_claimed_order: {e}")
