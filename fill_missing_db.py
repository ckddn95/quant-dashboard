import os
import sqlite3

def final_backfill_positions():
    db_path = "quant_system.db"
    if not os.path.exists(db_path): return

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN EXCLUSIVE TRANSACTION")
        try:
            # 1. 자동운용(managed) 물량만 있는 경우
            # (managed_qty나 broker_qty가 있고, manual_qty가 없는 경우)
            cursor.execute("""
                UPDATE positions
                SET managed_buy_price = buy_price
                WHERE (managed_qty > 0 OR broker_qty > 0) 
                  AND manual_qty = 0 
                  AND managed_buy_price = 0.0
            """)
            updated_managed = cursor.rowcount
            
            # 2. 수동운용(manual) 물량만 있는 경우
            cursor.execute("""
                UPDATE positions
                SET manual_buy_price = buy_price
                WHERE manual_qty > 0 
                  AND (managed_qty = 0 AND broker_qty = 0)
                  AND manual_buy_price = 0.0
            """)
            updated_manual = cursor.rowcount
            
            conn.commit()
            print(f"✅ DB Backfill 최종 성공! 자동물량 {updated_managed}건, 수동물량 {updated_manual}건 평단가 안전 복구.")
                
        except Exception as e:
            conn.rollback()
            print(f"❌ 최종 Backfill 중 오류 발생: {e}")

if __name__ == "__main__":
    final_backfill_positions()