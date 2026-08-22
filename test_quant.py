import os
import time
import datetime
import pytest
import database as db
import broker.kis_client as kis
import quant_engine as quant

# 🚨 패치 10: Bot, Worker 임포트를 강제하여 심각한 문법/Indentation 오류를 테스트 단계에서 사전 검출
import bot
import worker

TEST_DB_PATH = "test_quant_system.db"
db.DB_PATH = TEST_DB_PATH

@pytest.fixture(autouse=True)
def setup_and_teardown():
    if os.path.exists(TEST_DB_PATH): os.remove(TEST_DB_PATH)
    db.bootstrap_db()
    db.set_setting("kis_cano_core", "12345678")
    db.set_setting("auto_pilot_KIS_MOCK_testfp_01_CORE_CORE", True)
    yield
    if os.path.exists(TEST_DB_PATH): os.remove(TEST_DB_PATH)

def test_01_syntax_and_imports():
    """🚨 Worker, Bot 모듈 문법 오류 및 실행 함수 존재 여부 검증"""
    assert hasattr(worker, 'run_worker_loop')
    assert hasattr(bot, 'run_signal_bot')

def test_02_invalid_state_transition():
    """🚨 허용되지 않는 상태 전이 검증 (리턴값 명시적 확인)"""
    now_str = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec("t1", "t1", "KIS", "MOCK", "tfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_str, 60000, "2.2.0", now_str)
    db.safe_add_order_intent(spec)
    with db.get_connection() as conn:
        order_id = conn.execute("SELECT id FROM order_intents WHERE ticker='005930'").fetchone()['id']
    
    result = db.transition_order_status(order_id, 'INTENT_CREATED', 'ACKNOWLEDGED')
    assert result is False, "허용되지 않은 상태 전이가 통과되었습니다!"

def test_03_strict_real_block():
    """🚨 실제 REAL 경로를 태웠을 때 완벽하게 차단되는지 검증"""
    now_str = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec("t2", "t2", "KIS", "REAL", "tfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_str, 60000, "2.2.0", now_str)
    db.safe_add_order_intent(spec)
    
    with db.get_connection() as conn:
        order_id = conn.execute("SELECT id FROM order_intents WHERE environment='REAL'").fetchone()['id']
        conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=999 WHERE id=?", (order_id,))
        conn.execute("INSERT INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES ('KIS', 'REAL', 'tfp', '01', 'CORE', 'CORE', 'w1', '2099-01-01', 999)")
        
    _, passed, reason = db.authorize_claimed_order(order_id, "KIS", "REAL", "tfp", "01", "CORE", "CORE", "w1", 1000000, 50000, False, 0.0, 0.0, 1000000)
    assert passed is False
    assert "Strictly Blocked" in reason or "Blocked by System Contract" in reason

def test_04_version_mismatch_ttl_safe():
    """🚨 전략/계약 버전 불일치 검증 (TTL 만료를 피하기 위해 현재 시간 동적 주입)"""
    now_str = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec("t3", "t3", "KIS", "MOCK", "tfp", "01", "CORE", "CORE", "0.1.0", "0.1.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_str, 999999, "0.1.0", now_str)
    db.safe_add_order_intent(spec)
    
    with db.get_connection() as conn:
        order_id = conn.execute("SELECT id FROM order_intents WHERE strategy_version='0.1.0'").fetchone()['id']
        conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=888 WHERE id=?", (order_id,))
        conn.execute("INSERT INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES ('KIS', 'MOCK', 'tfp', '01', 'CORE', 'CORE', 'w1', '2099-01-01', 888)")

    _, passed, reason = db.authorize_claimed_order(order_id, "KIS", "MOCK", "tfp", "01", "CORE", "CORE", "w1", 1000000, 50000, False, 0.0, 0.0, 1000000)
    assert passed is False
    assert "Version mismatch" in reason

def test_05_payload_completeness(monkeypatch):
    """🚨 KIS API Payload 공식 규격(KRX, 00) 준수 검증"""
    captured = {}
    def mock_post(url, headers, data, **kwargs):
        captured.update(data)
        return kis.KisResult("SUCCESS_DATA", "OK", {"rt_cd": "0", "output": {"ODNO": "123"}})
    monkeypatch.setattr(kis, "_strict_post", mock_post)
    
    kis.execute_kis_order_001x("A", "B", "123", "01", "T", "005930", True, 10, 0, True)
    assert captured.get("EXCG_ID_DVSN_CD") == "KRX"
    assert captured.get("SLL_TYPE") == "00"

def test_06_ui_manual_double_count_atomic_fill():
    """🚨 수동 주문과 자동 주문 수량이 섞이지 않고(Double-count 방지), 델타가 정확히 원자적 처리되는지 검증"""
    now_str = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec("t6", "t6", "KIS", "MOCK", "tfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "UI_MANUAL", "UI_MANUAL", now_str, "Q", "KIS", now_str, 99999, "2.2.0", now_str)
    db.safe_add_order_intent(spec)
    
    with db.get_connection() as conn:
        order_id = conn.execute("SELECT id FROM order_intents WHERE idempotency_key='t6'").fetchone()['id']
        conn.execute("UPDATE order_intents SET status='SUBMITTING' WHERE id=?", (order_id,))
        
    broker_state = {'tot_ccld_qty': 10, 'tot_ccld_amt': 500000.0, 'avg_prvs': 50000.0, 'rmn_qty': 0, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': '123', 'ord_tmd': '100000'}
    db.apply_broker_receipt(order_id, "005930", "BUY", "KIS", "MOCK", "tfp", "01", "CORE", "CORE", broker_state)
    
    with db.get_connection() as conn:
        pos = conn.execute("SELECT managed_qty, manual_qty FROM positions WHERE ticker='005930'").fetchone()
    assert pos['managed_qty'] == 0, "UI_MANUAL 수량이 봇의 managed_qty에 합산되는 치명적 오류 발생!"
    assert pos['manual_qty'] == 10, "manual_qty에 정상 반영되지 않았습니다."

def test_07_cancel_cas_fencing():
    """🚨 다중 워커 중복 취소(Cancel Race Condition) 방어 게이트 검증"""
    now_str = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec("t7", "t7", "KIS", "MOCK", "tfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now_str, "Q", "KIS", now_str, 99999, "2.2.0", now_str)
    db.safe_add_order_intent(spec)
    
    with db.get_connection() as conn:
        order_id = conn.execute("SELECT id FROM order_intents WHERE idempotency_key='t7'").fetchone()['id']
        conn.execute("UPDATE order_intents SET status='CANCEL_CLAIMED', fencing_token=111, broker_order_id='TEST' WHERE id=?", (order_id,))
        
    passed, msg = db.authorize_cancel_order(order_id, "worker_1", 111)
    assert passed is True, "정상적인 Cancel CAS가 거부되었습니다."
    
    # 늦게 도착한 다른 워커가 잘못된 토큰으로 동시 취소를 시도
    passed2, msg2 = db.authorize_cancel_order(order_id, "worker_2", 222)
    assert passed2 is False, "토큰이 불일치하는 다중 워커의 중복 취소가 허용되었습니다!"