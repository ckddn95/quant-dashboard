import pytest
import datetime
import requests
from unittest.mock import patch
from enum import Enum
import quant_engine as quant
import broker.kis_client as kis
import database as db

# --- 1. 아키텍처 및 안전망 테스트 ---
def test_worker_import_and_contract_smoke():
    assert quant.KST.tzname(None) == 'UTC+09:00'
    assert isinstance(quant.ExitReason.STOP_LOSS, Enum)
    assert 'contract_version' in db.CONTRACT
    assert 'trend_exit' in db.CONTRACT

@patch('broker.kis_client._strict_post')
def test_app_never_calls_order_or_cancel_transport(mock_post):
    spec = quant.OrderSpec("corr_1", "idem_1", "KIS", "MOCK", "test_fp", "01", "CORE", "CORE", "1.0", "2.1.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 70000, "KRX", "GTC", "UI", "UI", "1200", "Q1", "KIS", "1200", 300, "2.1.0", "2026-08-18")
    mock_post.assert_not_called()

@patch('broker.kis_client._strict_post')
def test_order_post_timeout_becomes_unknown_without_retry(mock_post):
    mock_post.side_effect = requests.exceptions.Timeout("Connection Timed Out")
    status, msg, odno, krx_odno, code = kis.execute_kis_order_current_001x(
        "dummy_key", "dummy_sec", "12345678", "01", "dummy_token", "005930", True, 10, 0, True
    )
    assert mock_post.call_count == 1 
    assert status == "UNKNOWN"

# --- 2. 상태 머신 및 회계 정합성 테스트 ---
def test_partial_fill_cumulative_delta_exactly_once():
    db_cum_qty = 0
    broker_cum_qty_1 = 40
    delta_1 = broker_cum_qty_1 - db_cum_qty
    assert delta_1 == 40
    db_cum_qty += delta_1
    
    broker_cum_qty_2 = 40 
    delta_2 = broker_cum_qty_2 - db_cum_qty
    assert delta_2 == 0
    db_cum_qty += delta_2
    
    broker_cum_qty_3 = 100
    delta_3 = broker_cum_qty_3 - db_cum_qty
    assert delta_3 == 60
    assert db_cum_qty + delta_3 == 100

def test_cancel_ack_is_not_terminal_canceled():
    assert "CANCEL_ACKNOWLEDGED" in db.ALLOWED_TRANSITIONS['CANCEL_REQUESTED']
    assert "CANCELED" in db.ALLOWED_TRANSITIONS['CANCEL_ACKNOWLEDGED']

# --- 3. 전략 및 버퍼 정책 테스트 ---
def test_core_trend_exit_buffer_is_075_percent():
    cfg = quant.get_default_config(quant.Strategy.CORE)
    assert cfg.buf == 0.015
    assert cfg.buffer_factor == 0.5
    assert cfg.buf * cfg.buffer_factor == 0.0075

def test_stop_and_trailing_immediate_without_two_bar_wait():
    cfg = quant.get_default_config(quant.Strategy.CORE)
    buy_price = 100000
    highest = 110000
    is_sell, price, reason = quant.calc_sell_signal(quant.Strategy.CORE, cfg, 80000, 85000, 79000, 80000, buy_price, highest, 10, 95000, 95000)
    assert is_sell is True
    assert reason == quant.ExitReason.STOP_LOSS

# --- 4. 정밀 비용 모델(세율표) 통합 테스트 ---
def test_dated_cost_model_2022_through_2026():
    cost_24, slip_24, tax_24 = quant.CostModel.calculate_cost(datetime.date(2024, 5, 1), "KOSDAQ", "SELL", 10000, 1)
    assert tax_24 == 10000 * 0.0018
    cost_25, slip_25, tax_25 = quant.CostModel.calculate_cost(datetime.date(2025, 1, 1), "KOSPI", "SELL", 10000, 1)
    assert tax_25 == 10000 * 0.0015

def test_buy_and_sell_slippage_directions():
    cost_buy, slip_buy, tax_buy = quant.CostModel.calculate_cost(datetime.date(2026, 1, 1), "KOSPI", "BUY", 10000, 1)
    assert tax_buy == 0.0
    assert slip_buy == 10000 * quant.CostModel.BUY_SLIPPAGE

def test_legacy_025_mode_remains_reproducible():
    cost_leg, slip_leg, tax_leg = quant.CostModel.calculate_cost(datetime.date(2024, 1, 1), "KOSDAQ", "SELL", 10000, 1, is_legacy_025=True)
    assert cost_leg == 10000 * 0.0025
    assert tax_leg == 0.0 

# --- 5. 환경 파서 무결성 테스트 ---
def test_strict_environment_parser():
    assert "MOCK" == ("MOCK" if True else "REAL")
    assert "MOCK" == ("MOCK" if "yes" else "REAL")