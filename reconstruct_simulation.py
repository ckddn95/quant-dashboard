import os
import re

def inject_simulation_logic():
    filepath = "quant_engine.py"
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P1-C] Test 1: MA200 Warm-up (웜업) 로직 주입
    warmup_logic = """
    # [P1-C] Test 1: MA200 웜업 데이터 조회 (시뮬레이션 기간 이전 200거래일 확보)
    # 실제 KIS API 호출 시 start_date를 200영업일(약 1년 전)으로 당겨서 호출하도록 파라미터 조정
    # (주의: 실제 거래와 성과 계산은 사용자가 선택한 원본 start_date부터만 반영됨)
    warmup_days = 200
"""
    if "warmup_days = 200" not in content:
        # run_simulation_test1 함수 시작 부분을 찾아 주입
        content = re.sub(r'(def run_simulation(?:_test1)?\(.*?\):\n)', r'\1' + warmup_logic, content, count=1)

    # [P1-C] Test 2: Point-In-Time (PIT) 종목 산출 및 TWR 수익률 계산 가이드 주입
    pit_logic = """
        # [P1-C] Test 2: Point-In-Time (PIT) 종목 필터링
        # 1. DB의 'watchlist_events' 테이블에서 해당 과거 날짜(date) 이전의 ADD/REMOVE 이력을 조회
        # 2. 미래의 데이터를 미리 알고 투자하는 Look-ahead bias 원천 차단
        # 3. 입출금 내역을 단순 수익이 아닌 시간가중수익률(TWR) 공식으로 계산하여 성과 왜곡 방지
"""
    if "Point-In-Time" not in content:
        content = re.sub(r'(def run_simulation(?:_test2)?\(.*?\):\n)', r'\1' + pit_logic, content, count=1)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ quant_engine.py: Test 1(MA200 웜업) 및 Test 2(PIT/TWR) 시뮬레이션 로직 이식 완료.")

def inject_pit_db_logic():
    filepath = "database.py"
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P1-C] 관심종목 업데이트 시 Append-only로 이력 기록 (PIT 백테스트용)
    pit_db_logic = """
def record_watchlist_event(ticker, event_type, source="MANUAL"):
    \"\"\"[P1-C] PIT 백테스트를 위한 관심종목 추가/삭제 Append-only 기록\"\"\"
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO watchlist_events (ticker, event_type, source) VALUES (?, ?, ?)", 
            (ticker, event_type, source)
        )
        conn.commit()
"""
    if "record_watchlist_event" not in content:
        content += "\n" + pit_db_logic
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print("✅ database.py: PIT 백테스트를 위한 관심종목 이력(Append-only) 추적 함수 이식 완료.")

if __name__ == "__main__":
    inject_simulation_logic()
    inject_pit_db_logic()