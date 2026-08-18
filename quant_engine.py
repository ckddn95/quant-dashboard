import pandas as pd
import numpy as np
import FinanceDataReader as fdr
import datetime
import math
import concurrent.futures
from enum import Enum
from dataclasses import dataclass
import database as db

KST = datetime.timezone(datetime.timedelta(hours=9))

class ExitReason(Enum):
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TREND_EXIT = "TREND_EXIT"
    PRO_RATA_SELL = "PRO_RATA_SELL"
    UNKNOWN = "UNKNOWN"

class Strategy(Enum):
    CORE = "CORE"
    SATELLITE = "SATELLITE"

@dataclass
class StrategyConfig:
    ma200: bool; buf: float; sl: float; alloc: float; ts_tgt: float; ts_drp: float; cd: int; min_h: int; boost: bool
    buffer_factor: float = 0.5
    
    def __post_init__(self):
        if self.sl >= 0 or self.ts_drp >= 0: raise ValueError("sl/ts_drp must be negative.")
        if not (0 < self.alloc <= 1.0): raise ValueError("alloc must be (0, 1.0].")

@dataclass
class StockSnapshot:
    ticker: str; current_price: float; high_price: float; low_price: float; ma20: float; ma60: float; ma200: float; m60_up: bool
    as_of: datetime.datetime; source: str; is_valid: bool; is_complete_bar: bool; reason: str; executable: bool

@dataclass
class RiskContext:
    account_id: str; env: str; usable_cash: float; locked_buy_cash: float; managed_sell_qty: int
    current_exposure: float; max_exposure: float; daily_pnl_pct: float; is_kill_switch_on: bool; is_auto_trade_on: bool

@dataclass(frozen=True)
class OrderSpec:
    correlation_id: str; idempotency_key: str; broker: str; environment: str
    account_fingerprint: str; account_product_code: str; portfolio_id: str
    strategy_id: str; strategy_version: str; contract_version: str
    ticker: str; stock_name: str; side: str; order_kind: str; quantity: int
    limit_price: float; reference_price: float; exchange: str; time_in_force: str
    signal_id: str; signal_source: str; signal_cutoff: str
    quote_id: str; quote_source: str; quote_timestamp: str
    intent_ttl: int; cost_model_version: str; intent_created_at: str

class CostModel:
    TAX_RATES = db.CONTRACT.get('cost_model', {}).get('tax_schedule', {
        2022: {"KOSPI": 0.0023, "KOSDAQ": 0.0023}, 2023: {"KOSPI": 0.0020, "KOSDAQ": 0.0020},
        2024: {"KOSPI": 0.0018, "KOSDAQ": 0.0018}, 2025: {"KOSPI": 0.0015, "KOSDAQ": 0.0015},
        2026: {"KOSPI": 0.0020, "KOSDAQ": 0.0020}
    })
    BROKER_FEE = db.CONTRACT.get('cost_model', {}).get('broker_fee_rate', 0.00015)
    OTHER_FEE = db.CONTRACT.get('cost_model', {}).get('org_fee_rate', 0.000036)
    BUY_SLIPPAGE = db.CONTRACT.get('cost_model', {}).get('buy_slippage_rate', 0.001)
    SELL_SLIPPAGE = db.CONTRACT.get('cost_model', {}).get('sell_slippage_rate', 0.001)

    @classmethod
    def calculate_cost(cls, dt: datetime.date, market: str, side: str, price: float, qty: int, is_legacy_025=False):
        notional = price * qty
        if is_legacy_025: return notional * 0.0025, notional * 0.0025, 0.0
        
        fee = notional * (cls.BROKER_FEE + cls.OTHER_FEE)
        tax = 0.0
        
        if side.upper() == "BUY":
            slippage = notional * cls.BUY_SLIPPAGE
        else:
            slippage = notional * cls.SELL_SLIPPAGE
            tax_rate = cls.TAX_RATES.get(dt.year, {"DEFAULT": 0.0020}).get(market.upper(), 0.0020)
            tax = notional * tax_rate
            
        return fee + slippage + tax, slippage, tax

def get_default_config(strat: Strategy) -> StrategyConfig:
    c = db.CONTRACT['strategy'][strat.value]
    bf = db.CONTRACT.get('trend_exit', {}).get('buffer_factor', 0.5)
    return StrategyConfig(ma200=c['ma200'], buf=c['buf'], sl=c['sl'], alloc=c['alloc'], ts_tgt=c['ts_tgt'], ts_drp=c['ts_drp'], cd=c['cd'], min_h=c['min_h'], boost=c['boost'], buffer_factor=bf)

def load_krx_universe():
    try: return fdr.StockListing('KRX')
    except Exception: return pd.DataFrame()

def pre_flight_risk_check(order_spec: OrderSpec, snap: StockSnapshot, ctx: RiskContext) -> tuple[bool, str]:
    if ctx.is_kill_switch_on: return False, "KILL_SWITCH ON"
    if not ctx.is_auto_trade_on and ctx.env == "REAL": return False, "AUTO_TRADE OFF"
    if not snap.is_valid: return False, f"Invalid Quote: {snap.reason}"
    
    if order_spec.side == "BUY":
        if ctx.usable_cash <= 0: return False, "Zero Usable Cash"
        buffer = db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)
        expected_val = order_spec.quantity * order_spec.reference_price * buffer
        if ctx.usable_cash < expected_val: return False, "Insufficient Cash (Reserved)"
        if ctx.daily_pnl_pct < -0.05: return False, "Daily PnL < -5%"
        
        target_max = ctx.max_exposure
        projected_exposure = ctx.current_exposure + (order_spec.quantity * order_spec.reference_price)
        if projected_exposure > target_max: return False, "Exceeds Portfolio Exposure Limit"
        
    elif order_spec.side == "SELL":
        if ctx.managed_sell_qty < order_spec.quantity: return False, "Insufficient Managed Qty"

    return True, "PASS"

def calc_buy_signal(strat: Strategy, cfg: StrategyConfig, close_p: float, ma20: float, ma60: float, ma200: float, m60_up: bool) -> tuple[bool, float, str]:
    pass_ma200 = (close_p >= ma200) if cfg.ma200 else True
    if strat == Strategy.CORE:
        dist = (ma20 / ma60) - 1.0 if ma60 > 0 else 0.0
        if pass_ma200 and dist >= cfg.buf and m60_up:
            return True, round(min(85.0 + max(0.0, dist * 100.0), 99.0), 2), f"골든크로스 (이격도 {dist*100:+.2f}%)"
    else:
        dist = (close_p / ma20) - 1.0 if ma20 > 0 else 0.0
        if pass_ma200 and -0.05 <= dist <= 0.03:
            return True, round(min(85.0 + max(0.0, (0.03 - dist) * 100.0), 99.0), 2), f"눌림목 (이격도 {dist*100:+.2f}%)"
    return False, 50.0, ""

def calc_sell_signal(strat: Strategy, cfg: StrategyConfig, open_p: float, high_p: float, low_p: float, close_p: float, buy_p: float, highest_p: float, days_held: int, ma20: float, ma60: float) -> tuple[bool, float, ExitReason]:
    sl_target = buy_p * (1.0 + cfg.sl)
    ts_target = max(highest_p, high_p) * (1.0 + cfg.ts_drp)
    
    # 🚨 즉각 판정 (버퍼 없음, 2분봉 대기 없음)
    hit_sl = low_p <= sl_target
    hit_ts = (max(highest_p, high_p) >= buy_p * (1.0 + cfg.ts_tgt)) and (low_p <= ts_target)
    
    if hit_sl and hit_ts: return True, min(open_p, sl_target), ExitReason.STOP_LOSS
    elif hit_sl: return True, min(open_p, sl_target), ExitReason.STOP_LOSS
    elif hit_ts: return True, min(open_p, ts_target), ExitReason.TRAILING_STOP
    
    # 🚨 버퍼가 적용된 정상 추세매도
    if days_held >= cfg.min_h:
        if strat == Strategy.CORE and close_p < ma60 * (1.0 - cfg.buf * cfg.buffer_factor):
            return True, close_p, ExitReason.TREND_EXIT
        elif strat == Strategy.SATELLITE and close_p < ma20 * (1.0 - cfg.buf * cfg.buffer_factor):
            return True, close_p, ExitReason.TREND_EXIT
    return False, 0.0, ExitReason.UNKNOWN

# (시뮬레이션 UI, evaluate_stock_for_ui, run_quant_simulation 함수 등은 지면상 앞선 Phase 1에서 탑재된 버전을 100% 그대로 사용합니다.)