import os

DATABASE_PY_CONTENT = '''"""
Core-Satellite Quant System - Database Layer (Phase 1 Patched)
V17 스키마 기반 무손실 마이그레이터, WAL 백업, PIT 관심종목 이력 포함.
"""
import sqlite3
import os
import shutil
import time
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

DB_PATH = "quant_system.db"
CURRENT_SCHEMA_VERSION = 17

def _checkpoint_and_backup():
    if not os.path.exists(DB_PATH):
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        logger.warning(f"WAL Checkpoint failed: {e}")
        
    backup_path = f"backup_v16_to_v17_{int(time.time())}.db"
    shutil.copy2(DB_PATH, backup_path)
    return backup_path

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def preflight_check():
    """DB 초기화 및 하드 마이그레이션 (P0-2)"""
    backup_path = _checkpoint_and_backup()
    
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN EXCLUSIVE TRANSACTION")
        
        try:
            version = cursor.execute("PRAGMA user_version").fetchone()[0]
            
            if version == 0:
                # Fresh Install (V17)
                cursor.executescript("""
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
                        event_type TEXT NOT NULL, -- 'ADD', 'REMOVE'
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
                """)
                cursor.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                
            elif version < CURRENT_SCHEMA_VERSION:
                # V16 -> V17 Migration
                if backup_path:
                    logger.info(f"DB Backup created at {backup_path} prior to migration.")
                
                # 1. watchlist_events 테이블 추가 (PIT 시뮬레이션용)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS watchlist_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        source TEXT,
                        event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 2. positions 테이블 마이그레이션 (managed_buy_price, manual_buy_price 추가)
                cursor.execute("PRAGMA table_info(positions)")
                pos_cols = [row['name'] for row in cursor.fetchall()]
                if 'managed_buy_price' not in pos_cols:
                    cursor.execute("ALTER TABLE positions ADD COLUMN managed_buy_price REAL DEFAULT 0.0")
                if 'manual_buy_price' not in pos_cols:
                    cursor.execute("ALTER TABLE positions ADD COLUMN manual_buy_price REAL DEFAULT 0.0")

                # 3. order_intents 테이블 마이그레이션 (broker_order_time 추가)
                cursor.execute("PRAGMA table_info(order_intents)")
                ord_cols = [row['name'] for row in cursor.fetchall()]
                if 'broker_order_time' not in ord_cols:
                    cursor.execute("ALTER TABLE order_intents ADD COLUMN broker_order_time TEXT")

                # 4. 스키마 검증기 (Schema Validator)
                cursor.execute("PRAGMA table_info(order_intents)")
                final_ord_cols = [row['name'] for row in cursor.fetchall()]
                assert 'broker_order_time' in final_ord_cols, "Migration Failed: broker_order_time missing"
                
                cursor.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                
            conn.commit()
            logger.info(f"Database preflight check complete. Version: {CURRENT_SCHEMA_VERSION}")
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Migration failed and rolled back: {e}")
            raise

def transition_order_status(intent_id, new_status, broker_order_id=None, broker_order_time=None, expected_current_status=None):
    """주문 상태 전이 및 ACK 검증 (P0-1, P0-5)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # P0-1: ACK 저장 시 유효성 검사
        if new_status == 'ACKNOWLEDGED':
            if not broker_order_id or not str(broker_order_id).strip():
                new_status = 'RECONCILIATION_REQUIRED'
                logger.error(f"ACK received but broker_order_id is missing for {intent_id}. Esculated to RECONCILIATION_REQUIRED.")

        query = "UPDATE order_intents SET status = ?, updated_at = CURRENT_TIMESTAMP"
        params = [new_status]
        
        if broker_order_id:
            query += ", broker_order_id = ?"
            params.append(broker_order_id)
        if broker_order_time:
            query += ", broker_order_time = ?"
            params.append(broker_order_time)
            
        query += " WHERE intent_id = ?"
        params.append(intent_id)
        
        if expected_current_status:
            query += " AND status = ?"
            params.append(expected_current_status)
            
        cursor.execute(query, params)
        conn.commit()
        return cursor.rowcount > 0

def add_to_watchlist(ticker, name, source):
    """관심종목 추가 및 PIT 이벤트 기록"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION")
        try:
            cursor.execute("INSERT OR REPLACE INTO watchlists (ticker, name, source) VALUES (?, ?, ?)", (ticker, name, source))
            cursor.execute("INSERT INTO watchlist_events (ticker, event_type, source) VALUES (?, 'ADD', ?)", (ticker, source))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

def remove_from_watchlist(ticker):
    """관심종목 제거 및 PIT 이벤트 기록"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION")
        try:
            cursor.execute("DELETE FROM watchlists WHERE ticker = ?", (ticker,))
            cursor.execute("INSERT INTO watchlist_events (ticker, event_type, source) VALUES (?, 'REMOVE', 'MANUAL')", (ticker,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

def request_cancel_for_system_orders(account_fp, strategy):
    """Kill Switch: 시스템 주문 안전 취소 요청 (P0-3)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        # ACKNOWLEDGED, PARTIALLY_FILLED 등 취소 가능한 상태만 대상
        cursor.execute("""
            UPDATE order_intents 
            SET status = 'CANCEL_REQUESTED', updated_at = CURRENT_TIMESTAMP
            WHERE account_fp = ? AND strategy = ? AND status IN ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'PENDING')
        """, (account_fp, strategy))
        conn.commit()
        return cursor.rowcount
'''

WORKER_PY_PATCH = '''
# worker.py 의 ACK 검증 부분 패치 (P0-1)
import re

def patch_worker_file():
    if not os.path.exists("worker.py"): return
    with open("worker.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # ACK 저장 시 broker_order_time 파싱 및 전달 로직 주입
    if "transition_order_status(intent['intent_id'], 'ACKNOWLEDGED'" in content:
        content = re.sub(
            r"transition_order_status\(intent\['intent_id'\],\s*'ACKNOWLEDGED',\s*broker_order_id=([^\)]+)\)",
            r"transition_order_status(intent['intent_id'], 'ACKNOWLEDGED', broker_order_id=\\1, broker_order_time=res.get('ord_tmd', ''))",
            content
        )
        with open("worker.py", "w", encoding="utf-8") as f:
            f.write(content)
        print("✅ worker.py ACK 처리 로직 패치 완료")
'''

def apply_patch():
    print("🚀 Phase 1 패치(V17 마이그레이터)를 시작합니다...")
    
    with open("database.py", "w", encoding="utf-8") as f:
        f.write(DATABASE_PY_CONTENT)
    print("✅ database.py 전면 재작성 완료 (V17 스키마, WAL, PIT 테이블, Kill Switch 함수 적용)")
    
    exec(WORKER_PY_PATCH)
    patch_worker_file()
    
    print("🎉 Phase 1 패치가 완료되었습니다. 'python database.py' 를 실행하여 문법 오류가 없는지 확인하거나, 바로 'git commit'을 진행하십시오.")

if __name__ == "__main__":
    apply_patch()