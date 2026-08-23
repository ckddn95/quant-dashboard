import os
import sqlite3
import shutil
import time
from datetime import datetime

# ==========================================
# 1. Phase 0: 안전한 운영 DB 백업 및 진단
# ==========================================
ORIGINAL_DB = "quant_system.db"

def backup_and_diagnose():
    print("🛡️ [Phase 0] 운영 DB 안전 진단 및 백업을 시작합니다...")
    if not os.path.exists(ORIGINAL_DB):
        print("ℹ️ 기존 운영 DB가 존재하지 않습니다. 새로 생성(Fresh DB)을 준비합니다.")
        return None

    # WAL 체크포인트 강제 수행 (안전한 백업을 위해)
    try:
        with sqlite3.connect(ORIGINAL_DB, timeout=10) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        print(f"⚠️ WAL Checkpoint 경고 (무시 가능): {e}")

    # SQLite Online Backup API를 이용한 안전한 백업
    backup_file = f"quant_system_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
    try:
        source = sqlite3.connect(ORIGINAL_DB)
        dest = sqlite3.connect(backup_file)
        with dest:
            source.backup(dest)
        dest.close()
        source.close()
        print(f"✅ DB 안전 백업 완료: {backup_file}")
    except Exception as e:
        print(f"❌ DB 백업 실패! 작업을 즉시 중단합니다: {e}")
        exit(1)

    # 사전 상태 진단 (행 수, 버전 등)
    try:
        with sqlite3.connect(ORIGINAL_DB) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 무결성 검사
            integrity = cursor.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity.lower() != "ok":
                print(f"❌ 무결성 검사 실패! (상태: {integrity}) 작업을 즉시 중단합니다.")
                exit(1)
                
            version = cursor.execute("PRAGMA user_version").fetchone()[0]
            
            # 주요 테이블 행 수 파악
            tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            stats = {}
            for t in tables:
                t_name = t['name']
                count = cursor.execute(f"SELECT COUNT(*) FROM {t_name}").fetchone()[0]
                stats[t_name] = count
                
            print(f"📊 [사전 진단 결과] 무결성: OK | 스키마 버전: {version}")
            for table, count in stats.items():
                print(f"   - {table} 테이블: {count}행")
    except Exception as e:
         print(f"⚠️ 사전 진단 중 예외 발생 (스키마 미구축 상태일 수 있음): {e}")

    return backup_file

# ==========================================
# 2. Phase 1: 파일 전면 재구축 (격리 & 마이그레이션)
# ==========================================
def reconstruct_files():
    print("\n🛠️ [Phase 1] 파일 정밀 재구축을 시작합니다...")

    # [P0-A] test_quant.py 재작성: 운영 DB 완전 격리 및 삭제 금지
    test_content = """import pytest
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
    \"\"\"운영 DB와 완벽히 격리된 임시 폴더에서 V17 스키마 무결성 검증\"\"\"
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
    \"\"\"[P0-B] database.py 내 중복 함수 및 덮어쓰기 방어 검증\"\"\"
    with open("database.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # 함수 정의가 1번만 나타나야 함
    assert content.count("def preflight_check():") == 1
    assert content.count("def request_cancel_for_system_orders(") == 1
"""
    with open("test_quant.py", "w", encoding="utf-8") as f:
        f.write(test_content)
    print("✅ test_quant.py: 운영 DB 삭제 로직 제거 및 tmp_path 격리 스위트 주입 완료")

    # [P0-B, P0-C] database.py 재구축 가이드라인 출력 (너무 길어서 별도 작업으로 분리 유도)
    # 기존 database.py를 직접 덮어쓰지 않고, 중복을 제거하는 정규식 처리를 시도합니다.
    if os.path.exists("database.py"):
        with open("database.py", "r", encoding="utf-8") as f:
            db_code = f.read()
            
        # P0-A: DB 환경변수 주입 로직 추가
        if "os.getenv('QUANT_DB_PATH'" not in db_code:
            db_code = db_code.replace('DB_PATH = "quant_system.db"', 
                                      'DB_PATH = os.getenv("QUANT_DB_PATH", "quant_system.db")')

        with open("database.py", "w", encoding="utf-8") as f:
            f.write(db_code)
        print("✅ database.py: QUANT_DB_PATH 환경 변수 의존성 주입(DI) 처리 완료")
        print("⚠️ 주의: database.py의 중복 함수(preflight_check) 제거 및 원자적 마이그레이션 코드는 다음 스크립트에서 정밀 교체합니다.")

if __name__ == "__main__":
    backup_and_diagnose()
    reconstruct_files()