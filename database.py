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

def migrate_db():
    """V7 무손실 마이그레이션 및 영속 원장 추가"""
    conn = get_connection()
    try:
        conn.execute("BEGIN EXCLUSIVE")
        curr_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        
        # 기본 테이블
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, added_at TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS positions (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0, buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS worker_leases (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, worker_id TEXT, expires_at TIMESTAMP, token INTEGER, PRIMARY KEY (broker, environment, account_id, portfolio_id))''')
        
        # 상세 필드가 추가된 V7 인텐트 테이블
        conn.execute('''CREATE TABLE IF NOT EXISTS order_intents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE,
                        broker TEXT, environment TEXT, account_fingerprint TEXT, product_code TEXT,
                        portfolio_id TEXT, strategy_id TEXT, strategy_version TEXT, contract_version TEXT,
                        ticker TEXT, stock_name TEXT, side TEXT, order_kind TEXT,
                        qty INTEGER, limit_price REAL, reference_price REAL, exchange TEXT, time_in_force TEXT,
                        signal_id TEXT, signal_source TEXT, signal_cutoff TEXT, quote_id TEXT, quote_source TEXT, quote_timestamp TEXT,
                        intent_ttl INTEGER, cost_model_version TEXT, status TEXT DEFAULT 'INTENT_CREATED', 
                        broker_order_id TEXT, branch_no TEXT, cum_filled_qty INTEGER DEFAULT 0, 
                        avg_fill_price REAL DEFAULT 0.0, resp_code TEXT, fencing_token INTEGER,
                        created_at TIMESTAMP, updated_at TIMESTAMP)''')
        
        # 신규 추가 영속 원장 (V7)
        conn.execute('''CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, order_intent_id INTEGER, ticker TEXT, fill_qty INTEGER, fill_price REAL, fill_timestamp TIMESTAMP, fee REAL, tax REAL, is_reconciled BOOLEAN)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, event_type TEXT, timestamp TIMESTAMP)''')
        
        # 쿨다운 및 재무장 관리를 위한 테이블
        conn.execute('''CREATE TABLE IF NOT EXISTS signal_states (
                        broker TEXT, environment TEXT, account_fingerprint TEXT, portfolio_id TEXT, strategy_id TEXT,
                        ticker TEXT, regime_id TEXT, current_signal TEXT, consecutive_count INTEGER DEFAULT 0,
                        loss_streak INTEGER DEFAULT 0, cooldown_until_session TIMESTAMP, rearm_state BOOLEAN DEFAULT 1,
                        highest_price REAL DEFAULT 0.0, trailing_armed BOOLEAN DEFAULT 0, last_updated TIMESTAMP,
                        PRIMARY KEY (broker, environment, account_fingerprint, portfolio_id, strategy_id, ticker))''')

        if curr_ver < 7:
            conn.execute("PRAGMA user_version = 7")
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"🚨 Migration V7 Failed: {e}. System Halted.")
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

def acquire_worker_lease(broker, env, account_id, portfolio_id, worker_id, ttl=30):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute("SELECT worker_id, token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?", (broker, env, account_id, portfolio_id)).fetchone()
            if not row or row['expires_at'] < now:
                nt = (row['token'] + 1) if row else 1
                conn.execute("INSERT OR REPLACE INTO worker_leases (broker, environment, account_id, portfolio_id, worker_id, expires_at, token) VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime', '+{} seconds'), ?)".format(ttl), (broker, env, account_id, portfolio_id, worker_id, nt))
                conn.execute("COMMIT")
                return True, nt
            elif row['worker_id'] == worker_id:
                conn.execute("UPDATE worker_leases SET expires_at=datetime('now', 'localtime', '+{} seconds') WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?".format(ttl), (broker, env, account_id, portfolio_id))
                conn.execute("COMMIT")
                return True, row['token']
            conn.execute("ROLLBACK")
            return False, 0
        except Exception:
            conn.execute("ROLLBACK"); return False, 0

def atomic_preflight_and_claim(broker, env, account_fp, portfolio_id, worker_id, fencing_token, order_id, actual_cash):
    """🚨 [중요] SUBMITTING 전이 직전, DB 트랜잭션 내에서 킬스위치, 환경, 잔고를 모두 확인하는 최종 안전 게이트"""
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            
            # 1. Lease Token 확인
            lease = conn.execute("SELECT token FROM worker_leases WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND worker_id=?", (broker, env, account_fp, portfolio_id, worker_id)).fetchone()
            if not lease or lease['token'] != fencing_token:
                conn.execute("ROLLBACK"); return False, "Fencing Token Mismatch"

            # 2. 전역 및 개별 킬 스위치 & 자동매매 확인
            m_ks = conn.execute("SELECT value FROM settings WHERE key='master_kill_switch'").fetchone()
            a_ks = conn.execute("SELECT value FROM settings WHERE key=?", (f"kill_switch_{broker}_{env}_{account_fp}_{portfolio_id}",)).fetchone()
            if (m_ks and json.loads(m_ks['value'])) or (a_ks and json.loads(a_ks['value'])):
                conn.execute("ROLLBACK"); return False, "Kill Switch is ON"
            
            at = conn.execute("SELECT value FROM settings WHERE key=?", (f"auto_trade_{broker}_{env}_{account_fp}_{portfolio_id}",)).fetchone()
            if env == "REAL" and (not at or not json.loads(at['value'])):
                conn.execute("ROLLBACK"); return False, "Auto Trade is OFF for REAL"

            # 3. 주문 유효성 및 상태 확인
            order = conn.execute("SELECT * FROM order_intents WHERE id=? AND status='INTENT_CREATED'", (order_id,)).fetchone()
            if not order:
                conn.execute("ROLLBACK"); return False, "Order not in INTENT_CREATED"

            # 4. 현금 한도 이중 확인 (매수 시)
            if order['side'] == 'BUY':
                cost_multi = 1.0 + CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)
                open_states = "('CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED')"
                reserved = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * reference_price * ?) as res FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? AND side='BUY' AND status IN {open_states}", (cost_multi, broker, env, account_fp, portfolio_id)).fetchone()
                res_cash = float(reserved['res']) if reserved['res'] else 0.0
                req_cash = order['qty'] * order['reference_price'] * cost_multi
                
                if (actual_cash - res_cash) < req_cash:
                    conn.execute("ROLLBACK"); return False, "Insufficient Cash after Reservations"

            # 5. 매도 수량 이중 확인 (매도 시)
            if order['side'] == 'SELL':
                pos = conn.execute("SELECT managed_qty FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND ticker=?", (broker, env, account_fp, portfolio_id, order['ticker'])).fetchone()
                m_qty = pos['managed_qty'] if pos else 0
                res = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as r_qty FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? AND ticker=? AND side='SELL' AND status IN {open_states}", (broker, env, account_fp, portfolio_id, order['ticker'])).fetchone()
                r_qty = int(res['r_qty']) if res['r_qty'] else 0
                
                if (m_qty - r_qty) < order['qty']:
                    conn.execute("ROLLBACK"); return False, "Insufficient Managed Quantity after Reservations"

            # 6. 통과 시 SUBMITTING으로 전이
            conn.execute("UPDATE order_intents SET status='SUBMITTING', fencing_token=?, updated_at=? WHERE id=?", (fencing_token, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), order_id))
            conn.execute("COMMIT")
            return True, "Passed Atomic Gate"
        except Exception as e:
            conn.execute("ROLLBACK"); return False, f"DB Error: {str(e)}"

# 기타 기존 DB 함수 유지 (생략 없이 UI에서 쓰이는 함수들 보존)
def get_watchlist(broker, env, account_id, portfolio_id, strategy_id):
    with get_connection() as conn: 
        return [{'티커': r['ticker'], '종목명': r['name']} for r in conn.execute("SELECT ticker, name FROM watchlist WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()]
def get_positions(broker, env, account_id, portfolio_id, strategy_id):
    with get_connection() as conn: 
        return [dict(r) for r in conn.execute("SELECT * FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND strategy_id=?", (broker, env, account_id, portfolio_id, strategy_id)).fetchall()]
def safe_add_order_intent(spec):
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            corr_id = spec.correlation_id if spec.correlation_id else f"{spec.broker}_{spec.environment}_{spec.account_fingerprint}_{spec.portfolio_id}_{spec.strategy_id}_{spec.ticker}_{spec.side}_{spec.intent_created_at}"
            corr_id = hashlib.sha256(corr_id.encode()).hexdigest()[:16]
            conn.execute("""INSERT INTO order_intents 
                (correlation_id, idempotency_key, broker, environment, account_fingerprint, product_code, portfolio_id, 
                 strategy_id, strategy_version, contract_version, ticker, stock_name, side, order_kind, 
                 qty, limit_price, reference_price, exchange, time_in_force, signal_id, signal_source, signal_cutoff, quote_id, quote_source, quote_timestamp, 
                 intent_ttl, cost_model_version, status, created_at, updated_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_CREATED', ?, ?)""", 
                (corr_id, spec.idempotency_key, spec.broker, spec.environment, spec.account_fingerprint, spec.account_product_code, spec.portfolio_id, 
                 spec.strategy_id, spec.strategy_version, CONTRACT['contract_version'], spec.ticker, spec.stock_name, spec.side, spec.order_kind, 
                 spec.quantity, spec.limit_price, spec.reference_price, spec.exchange, spec.time_in_force, spec.signal_id, spec.signal_source, spec.signal_cutoff, spec.quote_id, spec.quote_source, spec.quote_timestamp,
                 spec.intent_ttl, CONTRACT.get('cost_model_version', '2.1.0'), spec.intent_created_at, spec.intent_created_at))
            conn.execute("COMMIT"); return True, "OK"
        except sqlite3.IntegrityError: 
            conn.execute("ROLLBACK"); return False, "Idempotency Blocked"