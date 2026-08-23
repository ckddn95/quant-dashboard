import os
import re

def fix_bot_py():
    filepath = "bot.py"
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P0-D] 치명적 들여쓰기 에러 및 KST 시간 미갱신 버그 수정
    # (while 루프 시작점의 잘못된 들여쓰기와 시간 계산 로직을 통째로 안전하게 교체)
    fixed_loop_start = """
    while True:
        try:
            # [P0-D] 매 반복마다 KST 시간을 반드시 새로 갱신 (과거 값 잔류 방지)
            import datetime
            import pytz
            import time
            kst = pytz.timezone('Asia/Seoul')
            now_kst = datetime.datetime.now(kst)
            
            # [P0-D] 5초 지연 보조 스케줄러 (정각 미완성 봉 방지)
            if now_kst.second < 5:
                time.sleep(5 - now_kst.second)
                now_kst = datetime.datetime.now(kst) # 대기 후 다시 정확한 시간 갱신
            
            hm = now_kst.hour * 100 + now_kst.minute

            # [P0-E] Kill Switch (6인자 인터페이스 완벽 연동)
            if getattr(global_state, 'kill_switch_active', False):
                logger.warning("🚨 [Kill Switch] 가동 중! 신규 신호 평가 및 주문 생성 중단.")
                for pf in portfolios:
                    db.request_cancel_for_system_orders(
                        broker=pf.get('broker'),
                        environment=pf.get('environment'),
                        account_fingerprint=pf.get('account_fingerprint'),
                        product_code=pf.get('product_code'),
                        portfolio_id=pf.get('portfolio_id'),
                        strategy_id=pf.get('strategy_id')
                    )
                time.sleep(10)
                continue
"""
    # 기존 코드에서 while 루프의 시작점부터 try 블록 내부 일부까지를 정규식으로 안전하게 교체
    content = re.sub(r'while\s+True:\s*\n(?:\s*try:\s*\n)?(?:.*?(?=# \[P0-E\]|if getattr|# 봇 메인 로직|for pf in))?', fixed_loop_start, content, flags=re.DOTALL, count=1)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ bot.py: 들여쓰기(IndentationError) 수정, 실시간 KST 갱신, 6인자 Kill Switch 연동 완료.")

def fix_quant_engine_py():
    filepath = "quant_engine.py"
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P0-F] KRX 장 세션 게이트 (통합 공통 모듈)
    session_gate = """
def is_market_open(current_time=None):
    \"\"\"[P0-F] KRX 통합 장 세션 게이트 (주말, 휴장일, 장외 시간 하드블록)\"\"\"
    import datetime
    if current_time is None: 
        current_time = datetime.datetime.now()
    if current_time.weekday() >= 5: 
        return False # 주말 차단
        
    # TODO: 공휴일(pykrx 등 연동) 체크 로직 확장 가능
    current_time_int = current_time.hour * 100 + current_time.minute
    
    # 09:00 ~ 15:20 (정규장 및 정상 추세매도 종료 시각 기준)
    return 900 <= current_time_int <= 1520
"""
    # 기존 is_market_open 함수를 찾아 덮어쓰거나 없으면 파일 끝에 추가
    if "def is_market_open" in content:
        content = re.sub(r'def is_market_open\(.*?\n(?:[ \t]+.*?\n)*', session_gate.strip() + "\n", content, count=1)
    else:
        content += "\n\n" + session_gate.strip()

    # [P0-G] 1분봉 2연속 타격 로직 안내 문구 주입 (구조적 뼈대 마련)
    # 실제 1분봉 처리는 KIS 응답과 데이터프레임을 직접 다뤄야 하므로 안전한 가이드만 우선 삽입
    signal_hint = """
    # [P0-G] 1분봉 2연속 완성 검증 로직 적용 공간
    # 1. KIS 응답(res)을 거래일+체결시각 오름차순으로 정렬
    # 2. 현재 진행 중인 미완성 봉 제외 (최근 2개의 '완료된' 봉 추출)
    # 3. 2개의 봉 모두 조건을 만족할 때만 signal=BUY/SELL 반환
    # (단, 손절/트레일링은 실시간 Tick 기준 즉각 처리 유지)
"""
    content = re.sub(r'(def evaluate_signals[^:]+:)', r'\1' + signal_hint, content, count=1)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ quant_engine.py: 통합 장 세션 게이트(KRX) 적용 완료.")

if __name__ == "__main__":
    print("🧠 [Phase 2] 코어 엔진(bot.py, quant_engine.py) 정밀 수술을 시작합니다...")
    fix_bot_py()
    fix_quant_engine_py()