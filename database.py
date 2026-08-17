import sqlite3
import json
import os
import yaml
import hashlib
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quant_system.db")
CONTRACT_PATH = os.path.join(BASE_DIR, "system_contract.yaml")

def load_contract():
    with open(CONTRACT_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

CONTRACT = load_contract()
ALLOWED_TRANSITIONS = CONTRACT['allowed_state_transitions']

def get_connection():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
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
            conn.execute("PRAGMA user_version = 1")
            
        if v < 2:
            conn.execute("DROP TABLE IF EXISTS watchlist")
            conn.execute('''CREATE TABLE watchlist (
                            broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT,
                            ticker TEXT, name TEXT, added_at TIMESTAMP,
                            PRIMARY KEY (broker, environment, account_id, portfolio_id, strategy_id, ticker))''')
            
            conn.execute("DROP TABLE IF EXISTS positions")
            conn.execute('''CREATE TABLE positions (
                            broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT,
                            ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, 
                            manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0,
                            buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP,
                            PRIMARY KEY (broker, environment, account_id, portfolio_id, strategy_id, ticker))''')
            
            conn.execute("DROP TABLE IF EXISTS order_intents")
            conn.execute('''CREATE TABLE order_intents (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, 
                            correlation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE,
                            broker TEXT, environment TEXT, account_id TEXT, product_code TEXT,
                            portfolio_id TEXT, strategy_id TEXT, strategy_version TEXT, contract_version TEXT,
                            ticker TEXT, stock_name TEXT, side TEXT, order_kind TEXT,
                            qty INTEGER, limit_price REAL, status TEXT DEFAULT 'INTENT_CREATED', 
                            broker_order_id TEXT, branch_no TEXT, cum_filled_qty INTEGER DEFAULT 0, 
                            avg_fill_price REAL DEFAULT 0.0, resp_code TEXT, fencing_token INTEGER,
                            created_at TIMESTAMP, updated_at TIMESTAMP)''')
                            
            conn.execute("DROP TABLE IF EXISTS worker_leases")
            conn.execute('''CREATE TABLE worker_leases (
                            broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, 
                            worker_id TEXT, expires_at TIMESTAMP, token INTEGER DEFAULT 0,
                            PRIMARY KEY (broker, environment, account_id, portfolio_id))''')
            
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
        if row:
            return json.loads(row['value'])
        return default

def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        conn.execute("COMMIT")

def get_system_status(broker, env, account_id, portfolio_id):
    return {
        "auto_trade": bool(get_setting('auto_trade_enabled', False)),
        "auto_pilot": bool(get_setting('auto_pilot', False)),
        "kill_switch": bool(get_setting('kill_switch', False)),
        "contract_version": CONTRACT['contract_version'],
        "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

def clear_and_update_watchlist(broker, env, account_id, portfolio_id, strategy_id, items):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM watchlist WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", 
                         (broker, env, account_id, portfolio_id, strategy_id))
            for item in items:
                conn.execute("INSERT INTO watchlist (broker, environment, account_id, portfolio_id, strategy_id, ticker, name, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                             (broker, env, account_id, portfolio_id, strategy_id, str(item['티커']).zfill(6), item['종목명'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.execute("COMMIT")
            return True, len(items)
        except Exception as e:
            conn.execute("ROLLBACK")
            return False, str(e)

def get_watchlist(broker, env, account_id, portfolio_id, strategy_id):
    with get_connection() as conn: 
        rows = conn.execute("SELECT ticker, name FROM watchlist WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()
        return [{'티커': r['ticker'], '종목명': r['name']} for r in rows]

def get_positions(broker, env, account_id, portfolio_id, strategy_id):
    with get_connection() as conn: 
        rows = conn.execute("SELECT * FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()
        return [dict(r) for r in rows]

def sync_positions_from_broker(broker, env, account_id, portfolio_id, strategy_id, kis_stocks):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            
            # 🛑 [패치] 기존에 들고 있던 종목 중, KIS에서 사라진(매도된) 종목을 찾아서 DB에서도 삭제
            existing_rows = conn.execute("SELECT ticker FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()
            existing_tickers = set([r['ticker'] for r in existing_rows])
            kis_tickers = set([s['ticker'] for s in kis_stocks])
            
            for stock in kis_stocks:
                tk = stock['ticker']
                b_qty = stock['qty']
                buy_price = stock.get('buy_price', 0.0)
                
                row = conn.execute("SELECT managed_qty, manual_qty, unknown_quarantined_qty FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", 
                                   (broker, env, account_id, portfolio_id, strategy_id, tk)).fetchone()
                if row:
                    m_qty = row['managed_qty']
                    man_qty = row['manual_qty']
                    u_qty = row['unknown_quarantined_qty']
                    diff = b_qty - (m_qty + man_qty)
                    if diff != u_qty:
                        conn.execute("UPDATE positions SET broker_qty=?, unknown_quarantined_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", 
                                     (b_qty, diff, buy_price, broker, env, account_id, portfolio_id, strategy_id, tk))
                else:
                    conn.execute("INSERT INTO positions (broker, environment, account_id, portfolio_id, strategy_id, ticker, broker_qty, manual_qty, buy_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                                 (broker, env, account_id, portfolio_id, strategy_id, tk, b_qty, b_qty, buy_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            
            # 매도되어 잔고가 0이 된 종목 삭제
            sold_tickers = existing_tickers - kis_tickers
            for tk in sold_tickers:
                conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_id, portfolio_id, strategy_id, tk))
                
            conn.execute("COMMIT")
        except Exception as e:
            print("DB Sync Error:", e)
            conn.execute("ROLLBACK")

def get_locked_cash_and_qty(broker, env, account_id, portfolio_id, ticker=None):
    with get_connection() as conn:
        open_states = "('INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCEL_UNKNOWN')"
        r1 = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * limit_price) as locked_cash FROM order_intents WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND side='BUY' AND status IN {open_states}", (broker, env, account_id, portfolio_id)).fetchone()
        locked_cash = float(r1['locked_cash']) if r1['locked_cash'] else 0.0
        locked_sell_qty = 0
        if ticker:
            r2 = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as locked_qty FROM order_intents WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND ticker=? AND side='SELL' AND status IN {open_states}", (broker, env, account_id, portfolio_id, ticker)).fetchone()
            locked_sell_qty = int(r2['locked_qty']) if r2['locked_qty'] else 0
        return locked_cash, locked_sell_qty

def acquire_worker_lease(broker, env, account_id, portfolio_id, worker_id, ttl=30):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute("SELECT worker_id, token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?", (broker, env, account_id, portfolio_id)).fetchone()
            
            if not row or row['expires_at'] < now:
                new_token = (row['token'] + 1) if row else 1
                conn.execute("INSERT OR REPLACE INTO worker_leases (broker, environment, account_id, portfolio_id, worker_id, expires_at, token) VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime', '+{} seconds'), ?)".format(ttl), (broker, env, account_id, portfolio_id, worker_id, new_token))
                conn.execute("COMMIT")
                return True, new_token
            elif row['worker_id'] == worker_id:
                conn.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds') WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?".format(ttl), (broker, env, account_id, portfolio_id))
                conn.execute("COMMIT")
                return True, row['token']
            conn.execute("ROLLBACK")
            return False, 0
        except Exception:
            conn.execute("ROLLBACK")
            return False, 0

def generate_correlation_id(spec):
    raw = f"{spec.broker}_{spec.environment}_{spec.account_id}_{spec.ticker}_{spec.side}_{spec.intent_created_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def safe_add_order_intent(spec):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            corr_id = generate_correlation_id(spec)
            conn.execute("""INSERT INTO order_intents 
                (correlation_id, idempotency_key, broker, environment, account_id, product_code, portfolio_id, strategy_id, strategy_version, contract_version, ticker, stock_name, side, order_kind, qty, limit_price, status, created_at, updated_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_CREATED', ?, ?)""", 
                (corr_id, spec.idempotency_key, spec.broker, spec.environment, spec.account_id, spec.account_product_code, spec.portfolio_id, spec.strategy_id, spec.strategy_version, CONTRACT['contract_version'],
                 spec.ticker, spec.stock_name, spec.side, spec.order_kind, spec.quantity, spec.limit_price, spec.intent_created_at, spec.intent_created_at))
            conn.execute("COMMIT")
            return True, "OK"
        except sqlite3.IntegrityError: 
            conn.execute("ROLLBACK")
            return False, "Idempotency 차단 (중복 주문)"

def claim_next_order(broker, env, account_id, portfolio_id, worker_id, fencing_token):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute("SELECT token FROM worker_leases WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND worker_id=?", (broker, env, account_id, portfolio_id, worker_id)).fetchone()
            if not lease or lease['token'] != fencing_token:
                conn.execute("ROLLBACK")
                return None

            row = conn.execute("SELECT * FROM order_intents WHERE status = 'INTENT_CREATED' AND broker=? AND environment=? AND account_id=? AND portfolio_id=? ORDER BY id ASC LIMIT 1", (broker, env, account_id, portfolio_id)).fetchone()
            if row:
                conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=?, updated_at=? WHERE id=?", (fencing_token, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['id']))
                conn.execute("COMMIT")
                return dict(row)
            conn.execute("ROLLBACK")
        except Exception:
            conn.execute("ROLLBACK")
    return None

def transition_order_status(order_id, current_status, new_status, broker_id="", branch="", code=""):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []):
        return False
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if broker_id and branch:
                conn.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, resp_code=?, updated_at=? WHERE id=? AND status=?", 
                             (new_status, broker_id, branch, code, now_str, order_id, current_status))
            else:
                conn.execute("UPDATE order_intents SET status=?, resp_code=?, updated_at=? WHERE id=? AND status=?", 
                             (new_status, code, now_str, order_id, current_status))
            rows = conn.execute("SELECT changes()").fetchone()[0]
            conn.execute("COMMIT")
            return rows > 0
        except Exception:
            conn.execute("ROLLBACK")
            return False

def apply_fill_delta_exactly_once(order_id, ticker, order_type, broker, env, account_id, portfolio_id, strategy_id, new_cum_qty, new_cum_avg_price):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            o_row = conn.execute("SELECT qty, cum_filled_qty, avg_fill_price, status FROM order_intents WHERE id=? AND broker=? AND environment=? AND account_id=? AND portfolio_id=?", (order_id, broker, env, account_id, portfolio_id)).fetchone()
            if not o_row:
                conn.execute("ROLLBACK")
                return False
            
            delta_qty = new_cum_qty - o_row['cum_filled_qty']
            if delta_qty <= 0:
                conn.execute("ROLLBACK")
                return False 
            
            new_status = 'FILLED' if new_cum_qty >= o_row['qty'] else 'PARTIALLY_FILLED'
            if o_row['status'] in ['CANCEL_REQUESTED', 'CANCEL_ACKNOWLEDGED', 'CANCELED']:
                new_status = o_row['status'] 
            conn.execute("UPDATE order_intents SET cum_filled_qty=?, avg_fill_price=?, status=?, updated_at=? WHERE id=?", 
                         (new_cum_qty, new_cum_avg_price, new_status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), order_id))

            p_row = conn.execute("SELECT managed_qty, buy_price FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_id, portfolio_id, strategy_id, ticker)).fetchone()
            p_qty = p_row['managed_qty'] if p_row else 0
            p_buy = p_row['buy_price'] if p_row else 0.0

            if "BUY" in order_type.upper():
                delta_notional = (new_cum_qty * new_cum_avg_price) - (o_row['cum_filled_qty'] * o_row['avg_fill_price'])
                delta_fill_price = delta_notional / delta_qty if delta_qty > 0 else 0
                new_p_qty = p_qty + delta_qty
                new_p_buy = ((p_qty * p_buy) + (delta_qty * delta_fill_price)) / new_p_qty if new_p_qty > 0 else 0
                if p_row:
                    conn.execute("UPDATE positions SET managed_qty=?, buy_price=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, new_p_buy, broker, env, account_id, portfolio_id, strategy_id, ticker))
                else:
                    conn.execute("INSERT INTO positions (broker, environment, account_id, portfolio_id, strategy_id, ticker, managed_qty, buy_price, highest_price, buy_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (broker, env, account_id, portfolio_id, strategy_id, ticker, new_p_qty, new_p_buy, delta_fill_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            else: 
                new_p_qty = p_qty - delta_qty
                if new_p_qty < 0: 
                    conn.execute("UPDATE order_intents SET status='RECONCILIATION_REQUIRED' WHERE id=?", (order_id,))
                    conn.execute("ROLLBACK")
                    return False 
                if new_p_qty == 0: 
                    conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_id, portfolio_id, strategy_id, ticker))
                else: 
                    conn.execute("UPDATE positions SET managed_qty=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, broker, env, account_id, portfolio_id, strategy_id, ticker))
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            return False

def get_orders_by_status_and_env(statuses, broker, env, account_id, portfolio_id):
    with get_connection() as conn:
        query = f"SELECT * FROM order_intents WHERE status IN ({','.join(['?']*len(statuses))}) AND broker=? AND environment=? AND account_id=? AND portfolio_id=?"
        rows = conn.execute(query, statuses + [broker, env, account_id, portfolio_id]).fetchall()
        return [dict(r) for r in rows]
