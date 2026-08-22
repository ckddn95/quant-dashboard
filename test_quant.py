import pytest
import sqlite3
import os
import database as db

def test_p0_db_schema_v17():
    """[P0-2] Fresh DB가 V17 스키마 필수 열을 모두 포함하는지 검증"""
    if os.path.exists(db.DB_PATH): os.remove(db.DB_PATH)
    db.preflight_check()
    with db.get_connection() as conn:
        cursor = conn.cursor()
        
        # order_intents 검증
        cursor.execute("PRAGMA table_info(order_intents)")
        cols = [r['name'] for r in cursor.fetchall()]
        assert 'broker_order_time' in cols, "V17 스키마 누락: broker_order_time"
        
        # positions 검증
        cursor.execute("PRAGMA table_info(positions)")
        p_cols = [r['name'] for r in cursor.fetchall()]
        assert 'managed_buy_price' in p_cols, "V17 스키마 누락: managed_buy_price"
        
        # PIT 테이블 검증
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='watchlist_events'")
        assert cursor.fetchone()[0] == 1, "V17 스키마 누락: watchlist_events"

def test_p0_kill_switch():
    """[P0-3] Kill Switch 발동 시 신규 주문 차단 및 상태 전이 검증"""
    db.preflight_check()
    with db.get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO order_intents (intent_id, account_fp, strategy, ticker, side, quantity, status) VALUES ('TEST_INTENT', 'CORE', 'S1', '005930', 'BUY', 10, 'ACKNOWLEDGED')")
        conn.commit()
    
    canceled = db.request_cancel_for_system_orders('CORE', 'S1')
    assert canceled == 1, "Kill Switch가 ACKNOWLEDGED 주문을 찾지 못함"
    
    with db.get_connection() as conn:
        status = conn.execute("SELECT status FROM order_intents WHERE intent_id='TEST_INTENT'").fetchone()[0]
        assert status == 'CANCEL_REQUESTED', "주문 상태가 CANCEL_REQUESTED로 전이되지 않음"
