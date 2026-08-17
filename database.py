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
            
        if v < 4:
            backup_db()
            print("Migrating DB to v4 (Account Isolation & OrderSpec Expansion)...")
            
            # 🛑 1. 기존 평문 계좌번호를 해시 핑거프린트로 전환
            for table in ['watchlist', 'positions', 'worker_leases', 'order_intents']:
                try:
                    rows = conn.execute(f"SELECT DISTINCT account_id FROM {table}").fetchall()
                    for r in rows:
                        plain_acc = r['account_id']
                        if plain_acc and not plain_acc.startswith("MOCK_"):
                            fp = hashlib.sha256(plain_acc.encode()).hexdigest()[:16]
                            conn.execute(f"UPDATE {table} SET account_id=? WHERE account_id=?", (fp, plain_acc))
                except sqlite3.OperationalError: pass
            
            # 🛑 2. Order Intent 테이블 확장 재구성 (27개 컬럼)
            conn.execute('''CREATE TABLE IF NOT EXISTS order_intents_v4 (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, 
                            correlation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE,
                            broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT,
                            portfolio_id TEXT, strategy_id TEXT, strategy_version TEXT, contract_version TEXT,
                            ticker TEXT, stock_name TEXT, side TEXT, order_kind TEXT,
                            qty INTEGER, limit_price REAL, reference_price REAL, exchange TEXT, time_in_force TEXT,
                            signal_id TEXT, signal_source TEXT, signal_cutoff TEXT,
                            quote_id TEXT, quote_source TEXT, quote_timestamp TEXT,
                            intent_ttl INTEGER, cost_model_version TEXT,
                            status TEXT DEFAULT 'INTENT_CREATED', 
                            broker_order_id TEXT, branch_no TEXT, cum_filled_qty INTEGER DEFAULT 0, 
                            avg_fill_price REAL DEFAULT 0.0, resp_code TEXT, fencing_token INTEGER,
                            created_at TIMESTAMP, updated_at TIMESTAMP)''')
            
            try:
                # 기존 v3 컬럼 매핑 후 이동
                conn.execute('''INSERT INTO order_intents_v4 (
                                id, correlation_id, idempotency_key, broker, environment, account_fingerprint, product_code,
                                portfolio_id, strategy_id, strategy_version, contract_version, ticker, stock_name, side, order_kind,
                                qty, limit_price, status, broker_order_id, branch_no, cum_filled_qty, avg_fill_price, resp_code, fencing_token,
                                created_at, updated_at) 
                                SELECT id, correlation_id, idempotency_key, broker, environment, account_id, product_code,
                                portfolio_id, strategy_id, strategy_version, contract_version, ticker, stock_name, side, order_kind,
                                qty, limit_price, status, broker_order_id, branch_no, cum_filled_qty, avg_fill_price, resp_code, fencing_token,
                                created_at, updated_at FROM order_intents''')
                conn.execute("DROP TABLE order_intents")
            except sqlite3.OperationalError: pass
                
            conn.execute("ALTER TABLE order_intents_v4 RENAME TO order_intents")
            conn.execute("PRAGMA user_version = 4")
            
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

# 🛑 [패치] 계좌별 완전 격리를 위한 전역/로컬 상태 동시 조회
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

# 🛑 [패치] 음수 체결 롤백 금지 룰 반영 (Reconciliation Required로 감사 추적)
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
                    # 🛑 Rollback 금지: 음수로 둔 채 RECONCILIATION_REQUIRED 기록
                    conn.execute("UPDATE order_intents SET status='RECONCILIATION_REQUIRED' WHERE id=?", (order_id,))
                    conn.execute("UPDATE positions SET managed_qty=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, broker, env, account_fingerprint, portfolio_id, strategy_id, ticker))
                    conn.execute("COMMIT"); return False 
                if new_p_qty == 0: conn.execute("DELETE FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (broker, env, account_fingerprint, portfolio_id, strategy_id, ticker))
                else: conn.execute("UPDATE positions SET managed_qty=? WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=? AND ticker=?", (new_p_qty, broker, env, account_fingerprint, portfolio_id, strategy_id, ticker))
            conn.execute("COMMIT"); return True
        except: conn.execute("ROLLBACK"); return False

# (get_watchlist, sync_positions 등 기존 복합키 조회 함수들은 파라미터명만 account_fingerprint로 매핑하여 유지 - 생략)
