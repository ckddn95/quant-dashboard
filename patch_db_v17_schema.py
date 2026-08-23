import os
import re

def fix_db_schema():
    if not os.path.exists("database.py"):
        return
        
    with open("database.py", "r", encoding="utf-8") as f:
        content = f.read()

    # 기존 preflight_check 함수를 찾아서 완벽한 V17 하드 마이그레이터로 교체
    # (정규식으로 기존 함수 블록 전체를 통째로 치환합니다)
    
    new_preflight = """def preflight_check():
    \"\"\"[P0-2] DB 초기화 및 V17 하드 마이그레이션\"\"\"
    import sqlite3
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN EXCLUSIVE TRANSACTION")
        try:
            # 1. 필수 테이블 무조건 생성 (V17 기준)
            cursor.executescript('''
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
                    event_type TEXT NOT NULL,
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
            ''')
            
            # 2. 기존 DB 마이그레이션 (열 추가)
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

            cursor.execute("PRAGMA user_version = 17")
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
"""
    # 기존 def preflight_check(): 부터 그 다음 def 로 시작하는 곳 직전까지 치환
    pattern = r"def preflight_check\(\):.*?(?=\n\n?def |\Z)"
    
    if re.search(pattern, content, flags=re.DOTALL):
        content = re.sub(pattern, new_preflight.strip(), content, flags=re.DOTALL)
        with open("database.py", "w", encoding="utf-8") as f:
            f.write(content)
        print("✅ database.py: V17 스키마 마이그레이션 로직이 완벽하게 주입되었습니다!")
    else:
        # 혹시 함수를 못 찾으면 맨 뒤에 추가
        with open("database.py", "a", encoding="utf-8") as f:
            f.write("\n\n" + new_preflight)
        print("✅ database.py: V17 스키마 로직이 파일 끝에 추가되었습니다!")

if __name__ == "__main__":
    fix_db_schema()