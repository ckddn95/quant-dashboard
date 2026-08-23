import os
import re

def safe_patch_database():
    if not os.path.exists("database.py"):
        return
        
    with open("database.py", "r", encoding="utf-8") as f:
        content = f.read()

    # 1. PIT(watchlist_events) 테이블 스키마 주입 (V17 마이그레이터)
    pit_table_sql = """
                    CREATE TABLE IF NOT EXISTS watchlist_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        source TEXT,
                        event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
"""
    if "watchlist_events" not in content:
        content = re.sub(
            r"(CREATE TABLE IF NOT EXISTS watchlists[^;]+;)",
            r"\1\n" + pit_table_sql,
            content
        )

    # 2. Kill Switch 취소 함수 주입
    kill_switch_func = """
def request_cancel_for_system_orders(account_fp, strategy):
    \"\"\"[P0-3] Kill Switch: 시스템 주문 안전 취소 요청\"\"\"
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE order_intents 
            SET status = 'CANCEL_REQUESTED', updated_at = CURRENT_TIMESTAMP
            WHERE account_fp = ? AND strategy = ? AND status IN ('ACKNOWLEDGED', 'PARTIALLY_FILLED', 'PENDING')
        ''', (account_fp, strategy))
        conn.commit()
        return cursor.rowcount
"""
    if "request_cancel_for_system_orders" not in content:
        content += "\n" + kill_switch_func

    with open("database.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ database.py: 원본 코드를 보존하며 안전하게 V17 PIT 테이블과 Kill Switch가 주입되었습니다.")

if __name__ == "__main__":
    safe_patch_database()