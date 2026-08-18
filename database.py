import sqlite3
import json
import os
import yaml
import hashlib
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "quant_system.db")
CONTRACT_PATH = os.path.join(BASE_DIR, "system_contract.yaml")
KST = timezone(timedelta(hours=9))

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
        curr_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, name TEXT, added_at TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS positions (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, strategy_id TEXT, ticker TEXT, broker_qty INTEGER DEFAULT 0, managed_qty INTEGER DEFAULT 0, manual_qty INTEGER DEFAULT 0, unknown_quarantined_qty INTEGER DEFAULT 0, buy_price REAL DEFAULT 0.0, highest_price REAL DEFAULT 0.0, buy_date TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS worker_leases (broker TEXT, environment TEXT, account_id TEXT, portfolio_id TEXT, worker_id TEXT, expires_at TIMESTAMP, token INTEGER, PRIMARY KEY (broker, environment, account_id, portfolio_id))''')
        
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
        
        conn.execute('''CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, order_intent_id INTEGER, ticker TEXT, fill_qty INTEGER, fill_price REAL, fill_timestamp TIMESTAMP, fee REAL, tax REAL, is_reconciled BOOLEAN)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS watchlist_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, event_type TEXT, timestamp TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS cash_flows (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, environment TEXT, amount REAL, timestamp TIMESTAMP, description TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS daily_account_equity (date TEXT, account_id TEXT, environment TEXT, equity REAL, cash REAL, PRIMARY KEY(date, account_id, environment))''')
        
        # V8: 신호 상태 테이블 확장 (UPSERT 용)
        conn.execute('''CREATE TABLE IF NOT EXISTS signal_states (
                        broker TEXT, environment TEXT, account_fingerprint TEXT, portfolio_id TEXT, strategy_id TEXT,
                        ticker TEXT, regime_id TEXT, current_signal TEXT, consecutive_count INTEGER DEFAULT 0,
                        last_distinct_bar_timestamp TIMESTAMP, loss_streak INTEGER DEFAULT 0, 
                        cooldown_until_session TIMESTAMP, rearm_state BOOLEAN DEFAULT 1,
                        highest_price REAL DEFAULT 0.0, trailing_armed BOOLEAN DEFAULT 0, last_updated TIMESTAMP,
                        PRIMARY KEY (broker, environment, account_fingerprint, portfolio_id, strategy_id, ticker))''')

        if curr_ver < 8:
            # 기존 V7 이하일 경우 컬럼 추가 로직 (생략 대비 안정성 부여)
            try: conn.execute('ALTER TABLE signal_states ADD COLUMN last_distinct_bar_timestamp TIMESTAMP')
            except sqlite3.OperationalError: pass
            conn.execute("PRAGMA user_version = 8")
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise RuntimeError(f"🚨 Migration V8 Failed: {e}")
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
        "contract_version": CONTRACT.get('contract_version', '1.0.0'),
        "real_approval_status": CONTRACT.get('execution_rules', {}).get('real_approval_status', 'BLOCKED')
    }

def upsert_signal_state(broker, env, acc_fp, port_id, strat_id, ticker, update_fields: dict):
    """지시사항 10항: UPSERT를 사용하여 지정된 필드만 보존 및 업데이트"""
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    update_fields['last_updated'] = now_str
    
    keys = ['broker', 'environment', 'account_fingerprint', 'portfolio_id', 'strategy_id', 'ticker']
    key_vals = [broker, env, acc_fp, port_id, strat_id, ticker]
    
    fields = list(update_fields.keys())
    vals = list(update_fields.values())
    
    all_cols = keys + fields
    all_vals = key_vals + vals
    
    placeholders = ", ".join(["?"] * len(all_cols))
    col_names = ", ".join(all_cols)
    
    update_clause = ", ".join([f"{f}=EXCLUDED.{f}" for f in fields])
    
    query = f"""
        INSERT INTO signal_states ({col_names})
        VALUES ({placeholders})
        ON CONFLICT(broker, environment, account_fingerprint, portfolio_id, strategy_id, ticker)
        DO UPDATE SET {update_clause}
    """
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(query, all_vals)
        conn.execute("COMMIT")

def claim_and_authorize_submission(broker, env, account_fp, product_code, portfolio_id, worker_id, actual_cash):
    """🚨 지시사항 5항: 비원자적 claim과 pre_flight를 통합한 단일 트랜잭션 안전 게이트"""
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            
            # 1. Lease 만료 및 권한 검사
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            lease = conn.execute("SELECT token, expires_at FROM worker_leases WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=?", (broker, env, account_fp, portfolio_id)).fetchone()
            if not lease or lease['expires_at'] < now_str or lease['worker_id'] != worker_id:
                conn.execute("ROLLBACK"); return None, False, "Invalid or Expired Lease"
            fencing_token = lease['token']

            # 2. 주문 선점 (가장 오래된 INTENT_CREATED)
            order = conn.execute("SELECT * FROM order_intents WHERE status='INTENT_CREATED' AND broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? ORDER BY id ASC LIMIT 1", (broker, env, account_fp, product_code, portfolio_id)).fetchone()
            if not order:
                conn.execute("ROLLBACK"); return None, False, "No Pending Intents"

            order_id = order['id']

            # 3. REAL 활성화 안전장치 검사 (MOCK이 아닌데 REAL이 BLOCKED이면 거절)
            real_status = CONTRACT.get('execution_rules', {}).get('real_approval_status', 'BLOCKED')
            if env == "REAL" and real_status != "APPROVED":
                conn.execute("UPDATE order_intents SET status='RISK_REJECTED', resp_code='REAL Execution Blocked by Contract' WHERE id=?", (order_id,))
                conn.execute("COMMIT"); return dict(order), False, "REAL Execution Blocked"

            # 4. Kill Switch 및 Auto Trade 검사
            m_ks = conn.execute("SELECT value FROM settings WHERE key='master_kill_switch'").fetchone()
            a_ks = conn.execute("SELECT value FROM settings WHERE key=?", (f"kill_switch_{broker}_{env}_{account_fp}_{portfolio_id}",)).fetchone()
            if (m_ks and json.loads(m_ks['value'])) or (a_ks and json.loads(a_ks['value'])):
                conn.execute("UPDATE order_intents SET status='RISK_REJECTED', resp_code='Kill Switch ON' WHERE id=?", (order_id,))
                conn.execute("COMMIT"); return dict(order), False, "Kill Switch is ON"
            
            at = conn.execute("SELECT value FROM settings WHERE key=?", (f"auto_trade_{broker}_{env}_{account_fp}_{portfolio_id}",)).fetchone()
            if env == "REAL" and (not at or not json.loads(at['value'])):
                conn.execute("UPDATE order_intents SET status='RISK_REJECTED', resp_code='Auto Trade OFF' WHERE id=?", (order_id,))
                conn.execute("COMMIT"); return dict(order), False, "Auto Trade is OFF for REAL"

            # 5. Contract / Strategy Version 검사
            if order['contract_version'] != CONTRACT.get('contract_version'):
                conn.execute("UPDATE order_intents SET status='QUARANTINED', resp_code='Contract Version Mismatch' WHERE id=?", (order_id,))
                conn.execute("COMMIT"); return dict(order), False, "Version Mismatch (Quarantined)"

            # 6. Intent TTL 및 Freshness 검사
            intent_created = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=KST)
            if (datetime.now(KST) - intent_created).total_seconds() > order['intent_ttl']:
                conn.execute("UPDATE order_intents SET status='EXPIRED', resp_code='Intent TTL Exceeded' WHERE id=?", (order_id,))
                conn.execute("COMMIT"); return dict(order), False, "Intent TTL Exceeded"

            # 7. 현금 / 수량 예약(Reservation) 이중 검사
            open_states = "('CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED')"
            if order['side'] == 'BUY':
                buffer_multi = CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)
                req_cash = order['qty'] * order['reference_price'] * (buffer_multi if order['order_kind'] == 'MARKET' else 1.0)
                
                # 본 주문을 제외한 다른 미체결 BUY 예약금
                reserved = conn.execute(f"SELECT SUM((qty - cum_filled_qty) * reference_price * ?) as res FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? AND side='BUY' AND status IN {open_states} AND id != ?", (buffer_multi, broker, env, account_fp, portfolio_id, order_id)).fetchone()
                res_cash = float(reserved['res']) if reserved['res'] else 0.0
                
                if (actual_cash - res_cash) < req_cash:
                    conn.execute("UPDATE order_intents SET status='RISK_REJECTED', resp_code='Insufficient Cash' WHERE id=?", (order_id,))
                    conn.execute("COMMIT"); return dict(order), False, "Insufficient Cash after Reservations"
            
            elif order['side'] == 'SELL':
                pos = conn.execute("SELECT managed_qty FROM positions WHERE broker=? AND environment=? AND account_id=? AND portfolio_id=? AND ticker=?", (broker, env, account_fp, portfolio_id, order['ticker'])).fetchone()
                m_qty = pos['managed_qty'] if pos else 0
                res = conn.execute(f"SELECT SUM(qty - cum_filled_qty) as r_qty FROM order_intents WHERE broker=? AND environment=? AND account_fingerprint=? AND portfolio_id=? AND ticker=? AND side='SELL' AND status IN {open_states} AND id != ?", (broker, env, account_fp, portfolio_id, order['ticker'], order_id)).fetchone()
                r_qty = int(res['r_qty']) if res['r_qty'] else 0
                
                if (m_qty - r_qty) < order['qty']:
                    conn.execute("UPDATE order_intents SET status='RISK_REJECTED', resp_code='Insufficient Qty' WHERE id=?", (order_id,))
                    conn.execute("COMMIT"); return dict(order), False, "Insufficient Managed Quantity after Reservations"

            # 모든 게이트 통과: 원자적 SUBMITTING 전이
            conn.execute("UPDATE order_intents SET status='SUBMITTING', fencing_token=?, updated_at=? WHERE id=?", (fencing_token, now_str, order_id))
            conn.execute("COMMIT")
            
            # SUBMITTING 반영 후 데이터 리턴
            order_updated = conn.execute("SELECT * FROM order_intents WHERE id=?", (order_id,)).fetchone()
            return dict(order_updated), True, "Passed Atomic Gate"

        except Exception as e:
            conn.execute("ROLLBACK")
            return None, False, f"DB Error: {str(e)}"

def transition_order_status(order_id, current_status, new_status, broker_id="", branch="", code=""):
    if new_status not in ALLOWED_TRANSITIONS.get(current_status, []): return False
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
            if broker_id and branch:
                conn.execute("UPDATE order_intents SET status=?, broker_order_id=?, branch_no=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, broker_id, branch, code, now_str, order_id, current_status))
            else:
                conn.execute("UPDATE order_intents SET status=?, resp_code=?, updated_at=? WHERE id=? AND status=?", (new_status, code, now_str, order_id, current_status))
            rows = conn.execute("SELECT changes()").fetchone()[0]
            conn.execute("COMMIT")
            return rows > 0
        except Exception: 
            conn.execute("ROLLBACK")
            return False

# (나머지 읽기 전용 함수들: get_watchlist, get_positions 등은 그대로 유지됨)