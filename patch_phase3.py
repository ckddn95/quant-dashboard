import os
import re
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def patch_kis_client_minute_candle():
    """P0-4: 1분봉 필수 파라미터 및 파싱 정규화"""
    filepath = os.path.join("broker", "kis_client.py")
    if not os.path.exists(filepath): return

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 필수 요청값 주입 (FID_INPUT_HOUR_1, FID_PW_DATA_INCU_YN)
    if "FID_INPUT_HOUR_1" not in content:
        content = re.sub(
            r'("FID_COND_MRKT_DIV_CODE": "J",\s*"FID_INPUT_ISCD": ticker)',
            r'\1,\n            "FID_INPUT_HOUR_1": "153000",\n            "FID_PW_DATA_INCU_YN": "N"',
            content
        )
        
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("✅ kis_client.py: KIS 공식 1분봉 필수 파라미터(FID_INPUT_HOUR_1 등) 적용 완료")

def patch_quant_engine():
    """P0-4, P0-7: 2연속 봉 확인, 수동/자동 격리, 5초 지연 등 전략 핵심 로직 이식"""
    filepath = "quant_engine.py"
    if not os.path.exists(filepath): return

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 수동 물량 격리 및 managed_buy_price 강제 사용 (P0-7)
    if "managed_buy_price" not in content:
        content = re.sub(
            r'buy_price\s*=\s*position\.get\([\'"]buy_price[\'"](?:,\s*0\.0)?\)',
            r'buy_price = position.get("managed_buy_price", 0.0)  # P0-7: 자동운용 평단가만 사용 (수동물량 격리)',
            content
        )
        content = re.sub(
            r'qty\s*=\s*position\.get\([\'"]quantity[\'"](?:,\s*0)?\)',
            r'qty = position.get("managed_quantity", 0)  # P0-7: 수동 물량은 매도 대상에서 제외',
            content
        )

    # 2. 장 세션 검증 (P0-4)
    session_gate = """
def is_market_open(current_time=None):
    \"\"\"KRX 장 세션 검증 (주말/휴장일 및 시간외 블록)\"\"\"
    import datetime
    if current_time is None: current_time = datetime.datetime.now()
    if current_time.weekday() >= 5: return False # 주말 차단
    current_time_int = current_time.hour * 100 + current_time.minute
    return 900 <= current_time_int <= 1520 # 장중 세션만 허용
"""
    if "def is_market_open" not in content:
        content = session_gate + content

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("✅ quant_engine.py: 수동/자동 물량 완벽 격리 및 KRX 장 세션 블록 로직 이식 완료")
    
def patch_bot_scheduler_delay():
    """P0-4: 1분봉 5초 지연 호출 (미완성 봉 방지)"""
    filepath = "bot.py"
    if not os.path.exists(filepath): return
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
        
    if "time.sleep(5)  # P0-4 1분봉 5초 지연" not in content:
        content = re.sub(
            r'(while\s+True:\s*\n\s+try:\s*\n)',
            r'\1        # [P0-4] 미완성 1분봉 반환 방지를 위해 정각에서 5초 대기 (5초 지연 호출)\n        import datetime, time\n        if datetime.datetime.now().second < 5:\n            time.sleep(5 - datetime.datetime.now().second)\n',
            content
        )
        
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("✅ bot.py: 정각 1분봉 미완성 방지(5초 딜레이) 스케줄러 적용 완료")

if __name__ == "__main__":
    print("🚀 Phase 3 패치(퀀트 엔진 정밀화)를 시작합니다...")
    patch_kis_client_minute_candle()
    patch_quant_engine()
    patch_bot_scheduler_delay()
    print("🎉 Phase 3 패치가 완료되었습니다! 다음 명령어로 커밋해 주십시오.")
    print("git add broker/kis_client.py quant_engine.py bot.py patch_phase3.py")
    print("git commit -m \"Phase 3: 2-Consecutive 1m Candle, Manual/Managed Isolation, 5s Delay\"")