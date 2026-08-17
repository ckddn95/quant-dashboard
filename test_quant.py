import pytest
import os
import sqlite3
import datetime
from unittest.mock import patch, MagicMock
import quant_engine as quant
import database as db
import broker.kis_client as kis

# 🛑 1. 운영 DB 파괴 방지 및 실계좌 네트워크 차단 (Fail-Safe)
@pytest.fixture(autouse=True)
def safe_test_environment(tmp_path):
    """테스트 시 기존 DB를 보호하고, 외부 HTTP 통신을 전면 차단합니다."""
    # DB 격리
    test_db = tmp_path / "test_quant_system.db"
    original_db_path = db.DB_PATH
    db.DB_PATH = str(test_db)
    db.migrate_db() # 테스트용 스키마 초기화
    
    # HTTP 통신 구조적 원천 차단 (실계좌 Transport 방어)
    patcher_post = patch('requests.post', autospec=True)
    patcher_get = patch('requests.get', autospec=True)
    mock_post = patcher_post.start()
    mock_get = patcher_get.start()
    
    # KIS API Mocking 기본값
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"rt_cd": "0", "msg1": "MOCK_OK", "output": {"ODNO": "MOCK1234", "KRX_FWDG_ORD_ORGNO": "11111"}}
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {"rt_cd": "0", "output": {"stck_prpr": "50000", "stck_hgpr": "51000", "stck_lwpr": "49000"}}
    
    yield
    
    # 환경 복구
    patcher_post.stop()
    patcher_get.stop()
    db.DB_PATH = original_db_path

# 🛑 2. 전략 매수/매도 공식 Golden Test
def test_core_buy_signal_golden():
    cfg = quant.get_default_config(quant.Strategy.CORE)
    # Core 조건: 200일선 상회, MA60 상승, 이격도 버퍼(1.5%) 충족
    is_buy, score, reason = quant.calc_buy_signal(
        strat=quant.Strategy.CORE, cfg=cfg,
        close_p=10500, ma20=10200, ma60=10000, ma200=9000, m60_up=True
    )
    assert is_buy is True
    assert score > 85.0
    assert "골든크로스" in reason

def test_satellite_buy_signal_golden():
    cfg = quant.get_default_config(quant.Strategy.SATELLITE)
    # Satellite 조건: 200일선 상회, 이격도 -5% ~ +3% 사이의 눌림목
    is_buy, score, reason = quant.calc_buy_signal(
        strat=quant.Strategy.SATELLITE, cfg=cfg,
        close_p=9800, ma20=10000, ma60=9000, ma200=8000, m60_up=True
    )
    assert is_buy is True
    assert "눌림목" in reason

def test_immediate_stop_loss_trigger():
    cfg = quant.get_default_config(quant.Strategy.CORE)
    cfg.sl = -0.15 # -15% 손절
    # 진입가 10000원 -> 손절가 8500원. 저가가 8400원이면 즉시 손절 발생 확인
    is_sell, s_price, reason = quant.calc_sell_signal(
        strat=quant.Strategy.CORE, cfg=cfg,
        open_p=8600, high_p=8700, low_p=8400, close_p=8500, 
        buy_p=10000, highest_p=10500, days_held=10, ma20=10000, ma60=10000
    )
    assert is_sell is True
    assert s_price <= 8500 # 보수적 체결가 (Adverse-first) 적용 확인
    assert "장중 손절컷" in reason

# 🛑 3. DB 멱등성 및 상태 머신(State Machine) 전환 검증
def test_order_idempotency_and_state_machine():
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec(
        correlation_id="", idempotency_key="UNIQUE_KEY_001", broker="KIS", environment="MOCK", 
        account_fingerprint="ACC_FP_123", account_product_code="01", portfolio_id="CORE", 
        strategy_id="CORE", strategy_version="1.0", contract_version="1.1.0",
        ticker="005930", stock_name="삼성전자", side="BUY", order_kind="MARKET", quantity=10, limit_price=0, 
        reference_price=0.0, exchange="KRX", time_in_force="GTC", signal_id="SIG_1", signal_source="TEST", 
        signal_cutoff=now_str, quote_id="", quote_source="TEST", quote_timestamp=now_str, 
        intent_ttl=300, cost_model_version="1.0.0", intent_created_at=now_str
    )
    
    # 1차 전송: 성공해야 함
    ok1, msg1 = db.safe_add_order_intent(spec)
    assert ok1 is True
    
    # 2차 전송 (동일 idempotency_key): 멱등성에 의해 차단되어야 함
    ok2, msg2 = db.safe_add_order_intent(spec)
    assert ok2 is False
    assert "Idempotency Blocked" in msg2

    # 상태 전이 검증 (INTENT_CREATED -> CLAIMED -> SUBMITTING)
    orders = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "ACC_FP_123", "CORE")
    order_id = orders[0]['id']
    
    assert db.transition_order_status(order_id, 'INTENT_CREATED', 'CLAIMED') is True
    assert db.transition_order_status(order_id, 'CLAIMED', 'SUBMITTING') is True
    # 잘못된 상태 전이 시도 방어 확인 (SUBMITTING -> INTENT_CREATED 역주행 불가)
    assert db.transition_order_status(order_id, 'SUBMITTING', 'INTENT_CREATED') is False

# 🛑 4. KIS API Mock Fencing 검증
def test_real_domain_fencing():
    """is_mock=False일 때 HTTP POST가 막혀있는지(Mock 동작) 최종 확인합니다."""
    status, msg, odno, branch, code = kis.execute_kis_order(
        app_key="DUMMY", app_secret="DUMMY", cano="12345678", acnt_prdt_cd="01", 
        token="DUMMY_TOKEN", ticker="005930", is_buy=True, qty=10, price=0, is_mock=False
    )
    # 실제 KIS 망을 타지 않고 Mock 데이터가 반환되었는지 검증
    assert status == "ACKNOWLEDGED"
    assert odno == "MOCK1234"
