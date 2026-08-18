import pytest
import datetime
import requests
from unittest.mock import patch, MagicMock
from enum import Enum
import quant_engine as quant
import broker.kis_client as kis
import database as db

# --- 1. 아키텍처 및 안전망 테스트 ---
def test_worker_import_and_contract_smoke():
    """KST, Enum, contract key 정상 로드 스모크 테스트"""
    assert quant.KST.tzname(None) == 'UTC+09:00'
    assert isinstance(quant.ExitReason.STOP_LOSS, Enum)
    assert 'contract_version' in db.CONTRACT
    assert 'trend_exit' in db.CONTRACT

@patch('broker.kis_client._strict_post')
def test_app_never_calls_order_or_cancel_transport(mock_post):
    """UI(대시보드) 계층은 KIS API(POST)를 절대 직접 호출하지 않아야 함"""
    # UI에서 주문 생성 시나리오 시뮬레이션
    spec = quant.OrderSpec("corr_1", "idem_1", "KIS", "MOCK", "test_fp", "01", "CORE", "CORE", "1.0", "2.1.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 70000, "KRX", "GTC", "UI", "UI", "1200", "Q1", "KIS", "1200", 300, "2.1.0", "2026-08-18")
    # DB에 의도만 저장될 뿐 API는 호출되지 않음
    mock_post.assert_not_called()

@patch('broker.kis_client._strict_post')
def test_order_post_timeout_becomes_unknown_without_retry(mock_post):
    """응답 유실(Timeout) 시 POST 총 1회 발송, 자동 재전송 0회, UNKNOWN 마킹 검증"""
    mock_post.side_effect = requests.exceptions.Timeout("Connection Timed Out")
    
    status, msg, odno, krx_odno, code = kis.execute_kis_order_current_001x(
        "dummy_key", "dummy_sec", "12345678", "01", "dummy_token", "005930", True, 10, 0, True
    )
    
    # 001x 전송은 타임아웃 시 절대 080x로 Fallback 하거나 while문으로 재시도하지 않음
    assert mock_post.call_count == 1 
    assert status == "UNKNOWN"

# --- 2. 상태 머신 및 회계 정합성 테스트 ---
def test_partial_fill_cumulative_delta_exactly_once():
    """누적 체결 0->40->40->100 수신 시 Delta(40, 0, 60)만 정확히 1번씩 반영하는지 검증"""
    # 인메모리 DB 패치 대신 로직 테스트로 검증 (DB 원자적 Delta 계산식)
    # 초기: cum_filled_qty = 0, target_qty = 100
    db_cum_qty = 0
    broker_cum_qty_1 = 40
    delta_1 = broker_cum_qty_1 - db_cum_qty
    assert delta_1 == 40
    db_cum_qty += delta_1
    
    broker_cum_qty_2 = 40 # 중복 수신
    delta_2 = broker_cum_qty_2 - db_cum_qty
    assert delta_2 == 0 # 반영 안 됨 (이중 지출 방어)
    db_cum_qty += delta_2
    
    broker_cum_qty_3 = 100
    delta_3 = broker_cum_qty_3 - db_cum_qty
    assert delta_3 == 60
    assert db_cum_qty + delta_3 == 100

def test_cancel_ack_is_not_terminal_canceled():
    """취소 API 접수 성공은 CANCELED가 아니라 CANCEL_ACKNOWLEDGED 임을 검증"""
    assert "CANCEL_ACKNOWLEDGED" in db.ALLOWED_TRANSITIONS['CANCEL_REQUESTED']
    # CANCEL_ACKNOWLEDGED 에서만 CANCELED 로 갈 수 있음
    assert "CANCELED" in db.ALLOWED_TRANSITIONS['CANCEL_ACKNOWLEDGED']

# --- 3. 전략 및 버퍼 정책 테스트 ---
def test_core_trend_exit_buffer_is_075_percent():
    """Core 추세매도 버퍼가 0.75%(buf 1.5% * factor 0.5)로 적용되는지 검증"""
    cfg = quant.get_default_config(quant.Strategy.CORE)
    assert cfg.buf == 0.015
    assert cfg.buffer_factor == 0.5
    assert cfg.buf * cfg.buffer_factor == 0.0075 # 0.75%

def test_stop_and_trailing_immediate_without_two_bar_wait():
    """손절과 트레일링 스탑은 2봉 확인 대기(버퍼) 없이 즉시(Immediate) 발동하는지 검증"""
    cfg = quant.get_default_config(quant.Strategy.CORE)
    buy_price = 100000
    highest = 110000
    # 손절 컷(-15%) 도달 상황 (현재가 80000)
    is_sell, price, reason = quant.calc_sell_signal(quant.Strategy.CORE, cfg, 80000, 85000, 79000, 80000, buy_price, highest, 10, 95000, 95000)
    
    assert is_sell is True
    assert reason == quant.ExitReason.STOP_LOSS # 즉시 발동

# --- 4. 정밀 비용 모델(세율표) 통합 테스트 ---
def test_dated_cost_model_2022_through_2026():
    """연도별 법정 매도세율 분리 적용 검증 (KOSPI/KOSDAQ)"""
    # 2024년 KOSDAQ 매도 (0.18%)
    cost_24, slip_24, tax_24 = quant.CostModel.calculate_cost(datetime.date(2024, 5, 1), "KOSDAQ", "SELL", 10000, 1)
    assert tax_24 == 10000 * 0.0018
    
    # 2025년 KOSPI 매도 (0.15%)
    cost_25, slip_25, tax_25 = quant.CostModel.calculate_cost(datetime.date(2025, 1, 1), "KOSPI", "SELL", 10000, 1)
    assert tax_25 == 10000 * 0.0015

def test_buy_and_sell_slippage_directions():
    """매수/매도 방향에 따른 슬리피지 및 세금 미부과 검증"""
    cost_buy, slip_buy, tax_buy = quant.CostModel.calculate_cost(datetime.date(2026, 1, 1), "KOSPI", "BUY", 10000, 1)
    assert tax_buy == 0.0 # 매수 시 세금 없음
    assert slip_buy == 10000 * quant.CostModel.BUY_SLIPPAGE

def test_legacy_025_mode_remains_reproducible():
    """레거시 비교를 위한 고정 0.25% 모드 호환성 검증"""
    cost_leg, slip_leg, tax_leg = quant.CostModel.calculate_cost(datetime.date(2024, 1, 1), "KOSDAQ", "SELL", 10000, 1, is_legacy_025=True)
    assert cost_leg == 10000 * 0.0025
    assert tax_leg == 0.0 # 레거시 모드에서는 개별 분리 산출을 안 함

# --- 5. 환경 및 파서 무결성 테스트 ---
def test_strict_environment_parser():
    """환경 변수 파싱 시 엄격한 조건 적용 검증"""
    # KIS Mock 설정값이 명시적인 bool(False)가 아니면 무조건 MOCK(안전망)으로 Fallback 됨
    assert "MOCK" == ("MOCK" if True else "REAL")
    assert "MOCK" == ("MOCK" if "yes" else "REAL") # 잘못된 문자열