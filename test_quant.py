import os
import time
import datetime
import pytest
import database as db
import broker.kis_client as kis
import quant_engine as quant

# 🚨 테스트용 독립 DB 격리
TEST_DB_PATH = "test_quant_system.db"
db.DB_PATH = TEST_DB_PATH

@pytest.fixture(autouse=True)
def setup_and_teardown():
    if os.path.exists(TEST_DB_PATH): os.remove(TEST_DB_PATH)
    db.bootstrap_db()
    
    # 기초 설정 세팅
    db.set_setting("kis_cano_core", "12345678")
    db.set_setting("auto_pilot_KIS_MOCK_testfp_01_CORE_CORE", True)
    yield
    if os.path.exists(TEST_DB_PATH): os.remove(TEST_DB_PATH)

def test_01_invalid_state_transition():
    """🚨 허용되지 않는 상태 전이 검증 (리턴값 명시적 확인)"""
    # 임의의 주문 인텐트 생성
    spec = quant.OrderSpec("test_corr", "test_idem", "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", "120000", "Q", "KIS", "2026-08-21 10:00:00", 60, "2.2.0", "2026-08-21 10:00:00")
    db.safe_add_order_intent(spec)
    
    with db.get_connection() as conn:
        order = conn.execute("SELECT id FROM order_intents WHERE ticker='005930'").fetchone()
    
    # INTENT_CREATED 상태에서 곧바로 ACKNOWLEDGED로 건너뛰기 시도 -> 반드시 False를 리턴해야 함
    result = db.transition_order_status(order['id'], 'INTENT_CREATED', 'ACKNOWLEDGED')
    assert result is False, "허용되지 않은 상태 전이가 통과되었습니다!"

def test_02_strict_real_block():
    """🚨 실제 REAL 경로를 태웠을 때 완벽하게 차단되는지 검증"""
    spec = quant.OrderSpec("test_corr2", "test_idem2", "KIS", "REAL", "testfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", "120000", "Q", "KIS", "2026-08-21 10:00:00", 60, "2.2.0", "2026-08-21 10:00:00")
    db.safe_add_order_intent(spec)
    
    with db.get_connection() as conn:
        order = conn.execute("SELECT id FROM order_intents WHERE environment='REAL'").fetchone()
        conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=999 WHERE id=?", (order['id'],))
        conn.execute("INSERT INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES ('KIS', 'REAL', 'testfp', '01', 'CORE', 'CORE', 'test_worker', '2099-01-01', 999)")
        
    # 진짜 authorize_claimed_order 게이트를 통과시키려 시도
    _, passed, reason = db.authorize_claimed_order(order['id'], "KIS", "REAL", "testfp", "01", "CORE", "CORE", "test_worker", 1000000, 50000, False, 0.0, 0.0, 1000000)
    
    assert passed is False, "REAL 환경 주문이 승인되었습니다!"
    assert "Strictly Blocked" in reason or "Blocked by System Contract" in reason, "REAL 차단 사유가 명확하지 않습니다."

def test_03_kis_payload_completeness(monkeypatch):
    """🚨 KIS API Payload에 누락되었던 필수 필드(SLL_TYPE, EXCG 등)가 확실히 들어가는지 검증"""
    captured_data = {}
    
    def mock_strict_post(url, headers, data, **kwargs):
        captured_data.update(data)  # 전송 직전의 payload 캡처
        return kis.KisResult("SUCCESS_DATA", "OK", {"rt_cd": "0", "output": {"ODNO": "12345"}})
        
    # _strict_post를 가로채서 payload를 검사
    monkeypatch.setattr(kis, "_strict_post", mock_strict_post)
    
    kis.execute_kis_order_001x("app", "sec", "12345678", "01", "token", "005930", True, 10, 0, True)
    
    assert captured_data.get("SLL_TYPE") == "00", "SLL_TYPE 필드가 누락되었거나 틀렸습니다."
    assert captured_data.get("EXCG_ID_DVSN_CD") == "01", "EXCG_ID_DVSN_CD 필드가 누락되었습니다."
    assert captured_data.get("CNDT_PRIC") == "0", "CNDT_PRIC 필드가 누락되었습니다."

def test_04_version_mismatch_quarantine():
    """🚨 전략 및 계약 버전 불일치 시 QUARANTINED 상태로 빠지는지 검증"""
    # 과거 버전(0.1.0)으로 인텐트 강제 생성
    spec = quant.OrderSpec("test_corr3", "test_idem3", "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "0.1.0", "0.1.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", "120000", "Q", "KIS", "2026-08-21 10:00:00", 60, "0.1.0", "2026-08-21 10:00:00")
    db.safe_add_order_intent(spec)
    
    with db.get_connection() as conn:
        order = conn.execute("SELECT id FROM order_intents WHERE strategy_version='0.1.0'").fetchone()
        conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=888 WHERE id=?", (order['id'],))
        conn.execute("INSERT INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES ('KIS', 'MOCK', 'testfp', '01', 'CORE', 'CORE', 'test_worker', '2099-01-01', 888)")

    _, passed, reason = db.authorize_claimed_order(order['id'], "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "test_worker", 1000000, 50000, False, 0.0, 0.0, 1000000)
    
    assert passed is False
    assert "Version mismatch" in reason
    
    with db.get_connection() as conn:
        final_state = conn.execute("SELECT status FROM order_intents WHERE id=?", (order['id'],)).fetchone()['status']
    assert final_state == "QUARANTINED", "구버전 스펙이 격리되지 않았습니다!"
