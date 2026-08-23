import pytest
import os
import sqlite3

# [P0-A] 테스트 환경 강제 격리 (운영 DB 접근 원천 차단)
os.environ["CI_TEST_MODE"] = "true"
os.environ["QUANT_DB_PATH"] = ":memory:" # 디폴트로 메모리 DB 사용 (파일 찌꺼기 방지)

import database as db

# 운영 DB 삭제 방어 게이트
if db.DB_PATH == "quant_system.db":
    raise RuntimeError("🚨 [치명적 오류] 테스트가 운영 DB(quant_system.db)를 참조하고 있습니다! Fail-closed!")

def test_p0_db_isolation_and_schema_v17(tmp_path):
    """운영 DB와 완벽히 격리된 임시 폴더에서 V17 스키마 무결성 검증"""
    temp_db = tmp_path / "test_quant.db"
    db.DB_PATH = str(temp_db)
    
    # 1. Fresh DB 생성
    db.preflight_check()
    
    with sqlite3.connect(db.DB_PATH) as conn:
        cursor = conn.cursor()
        
        # 2. 필수 테이블 검증 (운영 정통 캐노니컬 열 이름 검증)
        cursor.execute("PRAGMA table_info(order_intents)")
        cols = [r[1] for r in cursor.fetchall()]
        assert 'account_fingerprint' in cols, "축약 스키마(account_fp)가 아닌 정통 스키마여야 함"
        assert 'strategy_id' in cols, "정통 스키마(strategy_id) 누락"
        assert 'broker_order_time' in cols, "V17 마이그레이션 필수 열 누락"
        
        cursor.execute("PRAGMA table_info(positions)")
        p_cols = [r[1] for r in cursor.fetchall()]
        assert 'managed_buy_price' in p_cols, "V17 포지션 열 누락"
        assert 'manual_buy_price' in p_cols, "V17 포지션 열 누락"

def test_p0_no_duplicate_functions():
    """[P0-B] database.py 내 중복 함수 및 덮어쓰기 방어 검증"""
    with open("database.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # 함수 정의가 1번만 나타나야 함
    assert content.count("def preflight_check():") == 1
    assert content.count("def request_cancel_for_system_orders(") == 1
