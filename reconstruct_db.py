import os
import re

def reconstruct_database_file():
    filepath = "database.py"
    if not os.path.exists(filepath):
        return
        
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 과거의 잘못된 패치들이 만든 중복 함수들(축약 스키마용)을 정규식으로 모조리 삭제
    # preflight_check, bootstrap_db, request_cancel_for_system_orders 등을 전부 날립니다.
    content = re.sub(r"^def preflight_check\(.*?\n(?:[ \t]+.*?\n)*", "", content, flags=re.MULTILINE)
    content = re.sub(r"^def bootstrap_db\(.*?\n(?:[ \t]+.*?\n)*", "", content, flags=re.MULTILINE)
    content = re.sub(r"^def request_cancel_for_system_orders\(.*?\n(?:[ \t]+.*?\n)*", "", content, flags=re.MULTILINE)
    
    # 빈 줄 정리
    content = re.sub(r'\n{3,}', '\n\n', content)

    # 2. 오직 1개만 존재해야 하는 캐노니컬(정통) 스키마 기반의 V17 마이그레이터 생성
    canonical_migration = """
def preflight_check():
    \"\"\"[P0-B, P0-C] 정통(Canonical) 스키마 기반 원자적 V17 마이그레이션 및 DB 초기화\"\"\"
    import sqlite3
    import logging
    logger = logging.getLogger(__name__)
    
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN EXCLUSIVE TRANSACTION")
        try:
            # 1. 정통 캐노니컬 스키마 필수 테이블 구축 (축약형 account_fp 등 사용 금지)
            cursor.executescript('''
                CREATE TABLE IF NOT EXISTS watchlist_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source TEXT,
                    event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            
            # 2. V17 마이그레이션 (안전한 열 추가)
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

            # 3. 무결성 최종 확인 후 버전 업데이트
            cursor.execute("PRAGMA integrity_check")
            if cursor.fetchone()[0].lower() != "ok":
                raise RuntimeError("DB Integrity Check Failed during migration!")
                
            cursor.execute("PRAGMA user_version = 17")
            conn.commit()
            logger.info("✅ V17 캐노니컬 마이그레이션 원자적 완료.")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌ 마이그레이션 실패 및 롤백됨: {e}")
            raise

def request_cancel_for_system_orders(broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id):
    \"\"\"[P0-E] Kill Switch: 6인자 단일 인터페이스를 통한 시스템 주문 안전 취소 요청\"\"\"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE order_intents 
            SET status = 'CANCEL_REQUESTED', updated_at = CURRENT_TIMESTAMP
            WHERE broker = ? AND environment = ? AND account_fingerprint = ? 
              AND product_code = ? AND portfolio_id = ? AND strategy_id = ? 
              AND status IN ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'PENDING')
        ''', (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id))
        conn.commit()
        return cursor.rowcount
"""
    # 3. 파일의 맨 아래에 정통 함수들을 예쁘게 붙여넣음
    content += "\n\n" + canonical_migration.strip() + "\n"

    with open("database.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ database.py: 중복 함수 싹쓸이 및 6인자 기반 정통 V17 스키마 마이그레이터 이식 완료.")

if __name__ == "__main__":
    print("🧹 [Phase 1-B] DB 찌꺼기 청소 및 정통 스키마 복구를 시작합니다...")
    reconstruct_database_file()