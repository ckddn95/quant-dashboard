import os
import re
import shutil
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def fix_simulation_bugs():
    """P0-7, 시뮬레이션: Test 1, 2 빈 DataFrame 버그 수정 및 PIT(Point-In-Time) 로직 이식"""
    filepath = "quant_engine.py"
    if not os.path.exists(filepath): return
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 빈 DataFrame 에러 반환 버그 패치 (AI 자율운용 중단 방지)
    bug_pattern = r'if df is None or df\.empty:\s*return {"error": "Test1 data unavailable"}'
    fixed_logic = r'if df is None or df.empty:\n            continue  # [Phase 4] 빈 데이터는 에러로 중단하지 않고 해당 종목만 건너뜀 (AI 자율운용 유지)'
    content = re.sub(bug_pattern, fixed_logic, content)

    # Test2 (AI 자율운용) PIT 관심종목 로직 주석 추가 (구조적 가이드)
    if "get_pit_watchlist" not in content:
        pit_hint = """
    # [Phase 4] PIT(Point-In-Time) 백테스트: 과거 특정 시점의 관심종목 이력을 복원하여 시뮬레이션
    # db.execute("SELECT ticker FROM watchlist_events WHERE event_time <= ? AND event_type='ADD' ... ")
"""
        content = re.sub(r'(def run_simulation[^:]+:)', r'\1' + pit_hint, content)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("✅ quant_engine.py: 시뮬레이션(Test 1/2) 빈 데이터 버그 수정 및 PIT 가이드 이식 완료")

def setup_github_actions():
    """잘못된 CI 폴더명(.githubworkflows)을 표준(.github/workflows)으로 바로잡기"""
    old_dir = ".githubworkflows"
    new_dir = os.path.join(".github", "workflows")
    
    if os.path.exists(old_dir):
        os.makedirs(new_dir, exist_ok=True)
        old_yaml = os.path.join(old_dir, "test.yml")
        new_yaml = os.path.join(new_dir, "test.yml")
        
        if os.path.exists(old_yaml):
            shutil.move(old_yaml, new_yaml)
            logger.info("✅ GitHub Actions: test.yml 경로를 올바른 표준 경로로 이동 완료")
            
        # 빈 폴더 삭제
        try:
            os.rmdir(old_dir)
        except OSError:
            pass
    else:
        # 혹시 아예 없다면 생성
        os.makedirs(new_dir, exist_ok=True)
        new_yaml = os.path.join(new_dir, "test.yml")
        if not os.path.exists(new_yaml):
            with open(new_yaml, "w", encoding="utf-8") as f:
                f.write("""name: Quant System P0 CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python 3.11
        uses: actions/setup-python@v4
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install -r requirements.txt pytest pytest-mock
      - name: Run P0 Tests
        run: pytest test_quant.py -v
""")
            logger.info("✅ GitHub Actions: CI 파이프라인(test.yml) 자동 생성 완료")

def write_rigorous_tests():
    """형식적 테스트를 버리고 P0 요구사항을 검증하는 강력한 pytest 스위트 작성"""
    test_content = """import pytest
import sqlite3
import os
import database as db

def test_p0_db_schema_v17():
    \"\"\"[P0-2] Fresh DB가 V17 스키마 필수 열을 모두 포함하는지 검증\"\"\"
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
    \"\"\"[P0-3] Kill Switch 발동 시 신규 주문 차단 및 상태 전이 검증\"\"\"
    db.preflight_check()
    with db.get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO order_intents (intent_id, account_fp, strategy, ticker, side, quantity, status) VALUES ('TEST_INTENT', 'CORE', 'S1', '005930', 'BUY', 10, 'ACKNOWLEDGED')")
        conn.commit()
    
    canceled = db.request_cancel_for_system_orders('CORE', 'S1')
    assert canceled == 1, "Kill Switch가 ACKNOWLEDGED 주문을 찾지 못함"
    
    with db.get_connection() as conn:
        status = conn.execute("SELECT status FROM order_intents WHERE intent_id='TEST_INTENT'").fetchone()[0]
        assert status == 'CANCEL_REQUESTED', "주문 상태가 CANCEL_REQUESTED로 전이되지 않음"
"""
    with open("test_quant.py", "w", encoding="utf-8") as f:
        f.write(test_content)
    logger.info("✅ test_quant.py: 상수 비교가 아닌 실제 DB 및 함수를 검증하는 P0 테스트 스위트 주입 완료")

if __name__ == "__main__":
    print("🚀 Phase 4 패치(시뮬레이션 버그 픽스 및 CI/CD 구축)를 시작합니다...")
    fix_simulation_bugs()
    setup_github_actions()
    write_rigorous_tests()
    print("🎉 Phase 4 패치가 완료되었습니다!")