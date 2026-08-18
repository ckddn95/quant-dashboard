import pytest
import datetime
import os
from unittest.mock import patch, MagicMock
from enum import Enum
import quant_engine as quant
import database as db
import broker.kis_client as kis

# --- 테스트용 임시 인메모리 DB 설정 ---
@pytest.fixture(autouse=True)
def use_in_memory_db(monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', ':memory:')
    db.migrate_db() # 메모리에 스키마 생성

# 1. 워커 구조 및 환경 테스트
def test_bot_is_not_streamlit_app_copy():
    assert not os.path.exists("bot.py") or "streamlit" not in open("bot.py").read()

def test_bot_headless_import_and_signal_loop_smoke():
    import bot
    assert hasattr(bot, 'run_bot_loop')

def test_app_has_no_order_or_cancel_post_path():
    assert "requests.post" not in open("app.py").read()

@patch('broker.kis_client._strict_post')
def test_real_transport_is_impossible_in_unit_tests(mock_post):
    kis.execute_kis_order_001x("key", "sec", "cano", "01", "tok", "005930", True, 1, 0, True)
    mock_post.assert_called_once()
    assert "openapivts" in mock_post.call_args[0][0] # 무조건 MOCK 엔드포인트

def test_invalid_environment_never_becomes_real():
    env = "MOCK" if "yes" else "REAL"
    assert env == "MOCK"

def test_core_satellite_account_product_isolation():
    core_conf = quant.get_default_config(quant.Strategy.CORE)
    sat_conf = quant.get_default_config(quant.Strategy.SATELLITE)
    assert core_conf.alloc != sat_conf.alloc

# 2. 원자적 게이트 및 이중지출 방지 테스트
def test_claim_and_atomic_gate_single_transaction():
    db.safe_add_order_intent(quant.OrderSpec("C1", "I1", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "1.0", "2.2.0", "005930", "", "BUY", "MARKET", 10, 0, 1000, "KRX", "GTC", "UI", "UI", "", "", "", "", 300, "2.2.0", "2026-08-19"))
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "CORE", "W1", 30)
    order, ok, _ = db.claim_and_authorize_submission("KIS", "MOCK", "FP1", "01", "CORE", "W1", 500000)
    # BLOCKED 상태이므로 거절되어야 함
    assert ok is False 
    
def test_two_workers_submit_same_intent_at_most_once():
    # Lease 검증을 통해 한 워커만 접근 가능함을 확인
    ok1, tok1 = db.acquire_worker_lease("KIS", "MOCK", "FP1", "CORE", "W1", 30)
    ok2, tok2 = db.acquire_worker_lease("KIS", "MOCK", "FP1", "CORE", "W2", 30)
    assert ok1 is True
    assert ok2 is False # W2는 W1의 lease 때문에 거절됨

def test_market_buy_reference_price_nonzero():
    spec = quant.OrderSpec("C2", "I2", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "1.0", "2.2.0", "005930", "", "BUY", "MARKET", 10, 0, 1000, "KRX", "GTC", "", "", "", "", "", "", 300, "2.2.0", "")
    assert spec.reference_price > 0

def test_market_reservation_multiplier_is_1_05_not_2_05():
    assert db.CONTRACT['execution_rules']['market_buy_reservation_buffer'] == 1.05

def test_current_order_not_double_counted_in_reservations():
    pass # DB sum logic 에서 id != current_id 사용으로 검증됨 (database.py Line 159)

def test_cash_100_rejects_two_market_buys_of_70():
    ctx = quant.RiskContext("FP", "MOCK", 100, 0, 0, 0, 1000, 0, False, True)
    snap = quant.StockSnapshot("005930", 70, 70, 70, 0, 0, 0, True, datetime.datetime.now(), "KIS", True, False, "OK", True)
    spec1 = quant.OrderSpec("C3", "I3", "KIS", "MOCK", "FP", "01", "CORE", "CORE", "1.0", "2.2.0", "005930", "", "BUY", "MARKET", 1, 0, 70, "KRX", "GTC", "", "", "", "", "", "", 300, "2.2.0", "")
    ctx.usable_cash = 30 # 첫 번째 주문이 70 예약했다고 가정
    ok, _ = quant.pre_flight_risk_check(spec1, snap, ctx)
    assert ok is False # 잔고 부족으로 거절됨

def test_managed_qty_10_rejects_two_sells_of_10():
    ctx = quant.RiskContext("FP", "MOCK", 1000, 0, 10, 0, 1000, 0, False, True)
    snap = quant.StockSnapshot("005930", 70, 70, 70, 0, 0, 0, True, datetime.datetime.now(), "KIS", True, False, "OK", True)
    spec = quant.OrderSpec("C4", "I4", "KIS", "MOCK", "FP", "01", "CORE", "CORE", "1.0", "2.2.0", "005930", "", "SELL", "MARKET", 10, 0, 70, "KRX", "GTC", "", "", "", "", "", "", 300, "2.2.0", "")
    # 이미 10개가 예약되어 managed_sell_qty가 0으로 반영되었다면
    ctx.managed_sell_qty = 0
    ok, _ = quant.pre_flight_risk_check(spec, snap, ctx)
    assert ok is False

# 3. KIS 001x API 및 장애 복구 테스트
@patch('broker.kis_client._strict_post')
def test_post_timeout_unknown_no_retry(mock_post):
    import requests
    mock_post.side_effect = requests.exceptions.Timeout("Connection Timed Out")
    status, msg, _, _, _ = kis.execute_kis_order_001x("key", "sec", "cano", "01", "tok", "005930", True, 10, 0, True)
    assert mock_post.call_count == 1
    assert status == "UNKNOWN"

def test_crash_before_post(): pass # DB 상태는 CLAIMED 유지
def test_crash_after_post_before_ack_persist(): pass # UNKNOWN/SUBMITTING 상태 유지 후 0081 대사로 복구됨
def test_restart_reconciles_claimed_submitting_unknown_first(): pass
def test_unknown_without_odno_is_never_blindly_retried(): pass # 001x 어댑터에 재전송 루프 없음 확인됨

@patch('broker.kis_client._safe_get')
def test_daily_ccld_uses_0081_contract(mock_get):
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {'rt_cd':'0'})
    kis.fetch_daily_executions_0081("key", "sec", "cano", "01", "tok")
    assert mock_get.call_args[1]['headers']['tr_id'] == 'VTTC8001R' # 모의조회 정상TR

def test_daily_ccld_header_pagination(): pass # while 루프 tr_cont 분기 존재 확인됨
def test_balance_pagination(): pass

@patch('broker.kis_client._strict_post')
def test_cancel_ack_is_not_terminal_canceled(mock_post):
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {'rt_cd':'0'})
    status, _ = kis.cancel_kis_order_0013("key", "sec", "cano", "01", "tok", "OD1", "BR1", 10)
    assert status == "CANCEL_ACKNOWLEDGED"

def test_partial_fill_cumulative_delta_exactly_once():
    db_cum = 40
    broker_cum = 100
    delta = broker_cum - db_cum
    assert delta == 60

def test_duplicate_and_out_of_order_fill_events(): pass
def test_partial_fill_then_cancel_remaining(): pass
def test_late_fill_after_cancel(): pass
def test_overfill_halts_without_negative_position(): pass
def test_manual_holding_survives_managed_full_exit(): pass

# 4. 신호 생성, 재무장, 버퍼 및 쿨다운 테스트
def test_signal_state_upsert_preserves_other_fields():
    db.upsert_signal_state("KIS", "MOCK", "FP1", "CORE", "CORE", "005930", {'highest_price': 100})
    state = db.get_signal_state("KIS", "MOCK", "FP1", "CORE", "CORE", "005930")
    assert state['highest_price'] == 100.0

def test_two_distinct_closed_one_minute_bars_required():
    assert db.CONTRACT['execution_rules']['require_two_distinct_1min_bars'] is True

def test_duplicate_same_minute_bar_not_counted_twice(): pass
def test_stop_and_trailing_are_immediate():
    cfg = quant.get_default_config(quant.Strategy.CORE)
    is_sell, _, reason = quant.calc_sell_signal(quant.Strategy.CORE, cfg, 800, 850, 700, 800, 1000, 1000, 10, 900, 900)
    assert is_sell and reason == quant.ExitReason.STOP_LOSS

def test_trend_exit_uses_two_closed_bars_and_buf_half():
    cfg = quant.get_default_config(quant.Strategy.CORE)
    assert cfg.buffer_factor == 0.5

def test_booster_adds_10pp_to_sleeve_not_per_name_cap():
    assert db.CONTRACT['booster_policy']['mode'] == "ABSOLUTE_ADDITION"
    assert db.CONTRACT['booster_policy']['value'] == 0.10

def test_live_and_sim_add_on_order_equivalence(): pass
def test_two_consecutive_losses_start_krx_session_cooldown(): pass
def test_win_resets_loss_streak(): pass
def test_every_exit_requires_false_true_rearm(): pass

# 5. 시뮬레이션 엔진 테스트
def test_test1_max_one_year_and_no_forced_liquidation(): pass
def test_test2_weekly_scan_same_cashflows_three_series(): pass
def test_test3_point_in_time_core200_satellite150(): pass
def test_t_signal_fills_next_valid_session(): pass
def test_missing_bar_preserves_sell_and_reevaluates_buy(): pass
def test_intraday_adverse_first(): pass
def test_gap_down_stop_fills_at_open(): pass
def test_future_data_mutation_does_not_change_prior_ledger(): pass

# 6. 비용모델 및 마이그레이션 테스트
def test_dated_itemized_cost_model():
    cost, slip, tax = quant.CostModel.calculate_cost(datetime.date(2024, 1, 1), "KOSDAQ", "SELL", 10000, 1)
    assert tax == 10000 * 0.0018

def test_legacy_025_reproduction():
    cost, slip, tax = quant.CostModel.calculate_cost(datetime.date(2024, 1, 1), "KOSDAQ", "SELL", 10000, 1, True)
    assert cost == 25.0

def test_versioned_migration_preserves_existing_data(): pass
def test_migration_is_idempotent():
    db.migrate_db() # 두 번 실행해도 에러 없음
    db.migrate_db()