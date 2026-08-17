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
    is_buy, score, reason = quant.calc_buy_signal(
        strat=quant.Strategy.CORE, cfg=cfg,
        close_p=10500, ma20=10200, ma60=10000, ma200=9000, m60_up=True
    )
    assert is_buy is True
    assert score > 85.0
    assert "골든크로스" in reason

def test_immediate_stop_loss_trigger():
    cfg = quant.get_default_config(quant.Strategy.CORE)
    cfg.sl = -0.15 # -15% 손절
    is_sell, s_price, reason = quant.calc_sell_signal(
        strat=quant.Strategy.CORE, cfg=cfg,
        open_p=8600, high_p=8700, low_p=8400, close_p=8500, 
        buy_p=10000, highest_p=10500, days_held=10, ma20=10000, ma60=10000
    )
    assert is_sell is True
    assert s_price <= 8500 # 보수적 체결가 적용 확인
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
    
    ok1, msg1 = db.safe_add_order_intent(spec)
    assert ok1 is True
    
    # 중복 요청 방어 확인
    ok2, msg2 = db.safe_add_order_intent(spec)
    assert ok2 is False
    assert "Idempotency Blocked" in msg2

    orders = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "ACC_FP_123", "CORE")
    order_id = orders[0]['id']
    
    assert db.transition_order_status(order_id, 'INTENT_CREATED', 'CLAIMED') is True
    assert db.transition_order_status(order_id, 'CLAIMED', 'SUBMITTING') is True
    # 역주행 전이 차단 확인
    assert db.transition_order_status(order_id, 'SUBMITTING', 'INTENT_CREATED') is False

# 🛑 4. 회계 정합성: 매수 현금 중복 예약 (Double-Spend) 방어 검증
def test_buy_double_spend_prevention():
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 현금 100만원 가정. 80만원짜리 삼성전자 매수
    spec1 = quant.OrderSpec(
        correlation_id="", idempotency_key="BUY_1", broker="KIS", environment="MOCK", 
        account_fingerprint="ACC_FP_123", account_product_code="01", portfolio_id="CORE", 
        strategy_id="CORE", strategy_version="1.0", contract_version="1.1.0",
        ticker="005930", stock_name="삼성전자", side="BUY", order_kind="MARKET", quantity=10, limit_price=80000, 
        reference_price=80000, exchange="KRX", time_in_force="GTC", signal_id="SIG_1", signal_source="TEST", 
        signal_cutoff=now_str, quote_id="", quote_source="TEST", quote_timestamp=now_str, 
        intent_ttl=300, cost_model_version="1.0.0", intent_created_at=now_str
    )
    
    db.safe_add_order_intent(spec1) # 1번째 주문 큐 삽입
    
    locked_cash, _ = db.get_locked_cash_and_qty("KIS", "MOCK", "ACC_FP_123", "CORE")
    cost_multiplier = 1.0 + db.CONTRACT['simulation_rules']['assumed_cost_pct_per_side']
    expected_lock = 10 * 80000 * cost_multiplier
    
    # 예약금(Locked Cash)이 정확히 설정되었는지 확인
    assert locked_cash == expected_lock

    # 2번째 동일한 80만원 주문 시도
    snap = quant.StockSnapshot("005930", 80000, 80000, 80000, 0, 0, 0, False, datetime.datetime.now(), "KIS", True, False, "OK", True)
    total_cash = 1000000
    usable_cash = total_cash - locked_cash # 가용 현금은 100만 - 80만 = 20만으로 축소됨
    ctx = quant.RiskContext("ACC_FP_123", "MOCK", usable_cash, locked_cash, 0, 0, 1000000, 0, False, True)
    
    spec2 = quant.OrderSpec(
        correlation_id="", idempotency_key="BUY_2", broker="KIS", environment="MOCK", 
        account_fingerprint="ACC_FP_123", account_product_code="01", portfolio_id="CORE", 
        strategy_id="CORE", strategy_version="1.0", contract_version="1.1.0",
        ticker="005930", stock_name="삼성전자", side="BUY", order_kind="MARKET", quantity=10, limit_price=80000, 
        reference_price=80000, exchange="KRX", time_in_force="GTC", signal_id="SIG_2", signal_source="TEST", 
        signal_cutoff=now_str, quote_id="", quote_source="TEST", quote_timestamp=now_str, 
        intent_ttl=300, cost_model_version="1.0.0", intent_created_at=now_str
    )
    
    is_ok, reason = quant.pre_flight_risk_check(spec2, snap, ctx)
    # 두 번째 주문은 가용 현금 부족으로 거절되어야 함
    assert is_ok is False
    assert "Insufficient Cash" in reason

# 🛑 5. 회계 정합성: 잔량 이상의 매도 방어 검증
def test_sell_double_spend_prevention():
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    spec1 = quant.OrderSpec(
        correlation_id="", idempotency_key="SELL_1", broker="KIS", environment="MOCK", 
        account_fingerprint="ACC_FP_123", account_product_code="01", portfolio_id="CORE", 
        strategy_id="CORE", strategy_version="1.0", contract_version="1.1.0",
        ticker="005930", stock_name="삼성전자", side="SELL", order_kind="MARKET", quantity=10, limit_price=80000, 
        reference_price=80000, exchange="KRX", time_in_force="GTC", signal_id="SIG_3", signal_source="TEST", 
        signal_cutoff=now_str, quote_id="", quote_source="TEST", quote_timestamp=now_str, 
        intent_ttl=300, cost_model_version="1.0.0", intent_created_at=now_str
    )
    db.safe_add_order_intent(spec1)
    
    # 10주 매도 예약이 걸려있는지 확인
    _, locked_sell_qty = db.get_locked_cash_and_qty("KIS", "MOCK", "ACC_FP_123", "CORE", "005930")
    assert locked_sell_qty == 10

    # 보유수량이 10개라고 가정할 때, 가용 매도 수량은 0개
    usable_sell_qty = 10 - locked_sell_qty
    
    snap = quant.StockSnapshot("005930", 80000, 80000, 80000, 0, 0, 0, False, datetime.datetime.now(), "KIS", True, False, "OK", True)
    ctx = quant.RiskContext("ACC_FP_123", "MOCK", 1000000, 0, usable_sell_qty, 0, 1000000, 0, False, True)
    
    # 2번째 10주 매도 시도
    spec2 = quant.OrderSpec(
        correlation_id="", idempotency_key="SELL_2", broker="KIS", environment="MOCK", 
        account_fingerprint="ACC_FP_123", account_product_code="01", portfolio_id="CORE", 
        strategy_id="CORE", strategy_version="1.0", contract_version="1.1.0",
        ticker="005930", stock_name="삼성전자", side="SELL", order_kind="MARKET", quantity=10, limit_price=80000, 
        reference_price=80000, exchange="KRX", time_in_force="GTC", signal_id="SIG_4", signal_source="TEST", 
        signal_cutoff=now_str, quote_id="", quote_source="TEST", quote_timestamp=now_str, 
        intent_ttl=300, cost_model_version="1.0.0", intent_created_at=now_str
    )
    
    is_ok, reason = quant.pre_flight_risk_check(spec2, snap, ctx)
    assert is_ok is False
    assert "Insufficient Managed Qty" in reason
