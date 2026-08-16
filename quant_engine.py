import pandas as pd
import numpy as np
import FinanceDataReader as fdr
import datetime
import math
from enum import Enum
from dataclasses import dataclass
from typing import Optional

class Strategy(Enum):
    CORE = "CORE"
    SATELLITE = "SATELLITE"

@dataclass
class StrategyConfig:
    ma200: bool; buf: float; sl: float; alloc: float; ts_tgt: float; ts_drp: float; cd: int; min_h: int; boost: bool
    def __post_init__(self):
        for attr in ['buf', 'sl', 'alloc', 'ts_tgt', 'ts_drp']:
            val = getattr(self, attr)
            if math.isnan(val) or math.isinf(val): raise ValueError("NaN/Inf Not Allowed")
        if self.sl >= 0: raise ValueError("sl (Stop-loss) must be negative.")
        if self.ts_drp >= 0: raise ValueError("ts_drp must be negative.")
        if not (0 < self.alloc <= 1.0): raise ValueError("alloc must be (0, 1.0].")
        if self.cd < 0 or self.min_h < 0: raise ValueError("cooldown and min_h must be >= 0.")

@dataclass
class StockSnapshot:
    ticker: str; current_price: float; high_price: float; low_price: float
    ma20: float; ma60: float; ma200: float; m60_up: bool
    as_of: datetime.datetime; source: str; is_valid: bool; is_complete_bar: bool; reason: str; executable: bool

    def validate(self, is_halted: bool = False):
        if not self.is_valid: return 
        if math.isnan(self.current_price) or self.current_price <= 0:
            self.is_valid, self.reason, self.executable = False, "Invalid Price (NaN or <= 0)", False; return
        if is_halted:
            self.is_valid, self.reason, self.executable = False, "Halted Stock", False; return
        if self.source != "KIS":
            self.executable = False # FDR 등 과거 데이터는 실행(실주문) 불가
        self.is_valid, self.reason = True, "OK"

@dataclass
class RiskContext:
    account_id: str; env: str; usable_cash: float; locked_buy_cash: float
    managed_sell_qty: int; current_exposure: float; max_exposure: float
    daily_pnl_pct: float; is_kill_switch_on: bool; is_auto_trade_on: bool

@dataclass(frozen=True)
class OrderSpec:
    idempotency_key: str; broker: str; environment: str; account_id: str; account_product_code: str
    portfolio_id: str; strategy_id: str; strategy_version: str; ticker: str; stock_name: str
    side: str; order_kind: str; quantity: int; limit_price: float; intent_created_at: str

def get_default_config(strat: Strategy) -> StrategyConfig:
    if strat == Strategy.CORE: return StrategyConfig(ma200=True, buf=0.015, sl=-0.15, alloc=0.35, ts_tgt=0.30, ts_drp=-0.10, cd=60, min_h=5, boost=True)
    return StrategyConfig(ma200=True, buf=0.010, sl=-0.12, alloc=0.20, ts_tgt=0.20, ts_drp=-0.07, cd=30, min_h=3, boost=True)

def load_krx_universe():
    try: return fdr.StockListing('KRX')
    except: return pd.DataFrame()

def pre_flight_risk_check(order_spec: OrderSpec, snap: StockSnapshot, ctx: RiskContext) -> tuple[bool, str]:
    if ctx.is_kill_switch_on: return False, "KILL_SWITCH ON"
    if not ctx.is_auto_trade_on: return False, "AUTO_TRADE OFF"
    if not snap.is_valid: return False, f"Quote Invalid: {snap.reason}"
    if not snap.executable: return False, f"Quote Not Executable (Source: {snap.source})"
    
    if order_spec.account_id != ctx.account_id or order_spec.environment != ctx.env:
        return False, "Account/Env Isolation Breach"

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if order_spec.environment == "REAL":
        if now.weekday() >= 5: return False, "Weekend Closed"
        if now.hour < 9 or (now.hour == 15 and now.minute > 30) or now.hour > 15: return False, "Market Closed"

    if order_spec.side == "BUY":
        if ctx.usable_cash <= 0: return False, "Zero Usable Cash"
        expected_val = order_spec.quantity * (order_spec.limit_price if order_spec.limit_price > 0 else snap.current_price)
        if ctx.usable_cash < expected_val: return False, "Insufficient Cash"
        if ctx.daily_pnl_pct < -0.05: return False, "Daily PnL Limit Reached (-5%)"
    elif order_spec.side == "SELL":
        if ctx.managed_sell_qty < order_spec.quantity: return False, "Insufficient Managed Qty"

    if order_spec.limit_price > 0 and snap.current_price > 0:
        dev = abs((snap.current_price / order_spec.limit_price) - 1.0)
        if dev > 0.03: return False, f"Price Deviation > 3% ({dev*100:.1f}%)"
        
    return True, "PASS"

def check_entry_signal(strat: Strategy, cfg: StrategyConfig, snap: StockSnapshot) -> tuple[bool, float, str]:
    if not snap.is_valid: return False, 0.0, snap.reason
    pass_ma200 = (snap.current_price >= snap.ma200) if cfg.ma200 else True
    if strat == Strategy.CORE:
        dist_20_60 = (snap.ma20 / snap.ma60) - 1.0 if snap.ma60 > 0 else 0.0
        buy_sig = pass_ma200 and (dist_20_60 >= cfg.buf) and snap.m60_up
        return buy_sig, min(85.0 + max(0.0, dist_20_60 * 100.0), 99.0), f"GC (Div {dist_20_60*100:+.1f}%)"
    else:
        dist_c_20 = (snap.current_price / snap.ma20) - 1.0 if snap.ma20 > 0 else 0.0
        buy_sig = pass_ma200 and (-0.05 <= dist_c_20 <= 0.03)
        return buy_sig, min(85.0 + max(0.0, (0.03 - dist_c_20) * 100.0), 99.0), f"Pullback (Div {dist_c_20*100:+.1f}%)"

def check_exit_signal(strat: Strategy, cfg: StrategyConfig, snap: StockSnapshot, buy_p: float, highest_p: float, days_held: int) -> tuple[bool, float, str]:
    if not snap.is_valid or buy_p <= 0: return False, 0.0, snap.reason
    highest_p = max(highest_p, snap.high_price)
    
    sl_target = buy_p * (1.0 + cfg.sl)
    ts_trigger = buy_p * (1.0 + cfg.ts_tgt)
    ts_target = highest_p * (1.0 + cfg.ts_drp)
    
    hit_sl = snap.low_price <= sl_target
    hit_ts = (highest_p >= ts_trigger) and (snap.low_price <= ts_target)
    
    if hit_sl and hit_ts: return True, min(snap.current_price, sl_target), "🔴 SL/TS Conflict (SL First)"
    elif hit_sl: return True, min(snap.current_price, sl_target), "🔴 SL Hit"
    elif hit_ts: return True, min(snap.current_price, ts_target), "🔵 TS Hit"
                
    if days_held >= cfg.min_h:
        if strat == Strategy.CORE and snap.current_price < snap.ma60 * (1.0 - cfg.buf/2.0): return True, snap.current_price, "🔴 Trend Breakdown (MA60)"
        elif strat == Strategy.SATELLITE and snap.current_price < snap.ma20 * (1.0 - cfg.buf/2.0): return True, snap.current_price, "🔴 Trend Breakdown (MA20)"
            
    return False, 0.0, ""

def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0, highest_price: float=0, c_price: float=0, high_p: float=0, low_p: float=0, is_halted: bool=False, days_held: int=0):
    try:
        df = fdr.DataReader(str(ticker).zfill(6), start=(datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d'))
        if df.empty: return c_price, "N/A", 0.0, "No Historical Data"
        
        fdr_close, fdr_high, fdr_low = float(df['Close'].iloc[-1]), float(df['High'].iloc[-1]), float(df['Low'].iloc[-1])
        ma20, ma60, ma200 = df['Close'].rolling(20).mean().iloc[-1], df['Close'].rolling(60).mean().iloc[-1], df['Close'].rolling(200).mean().iloc[-1]
        m60_up = True if len(df) < 60 else (ma60 > df['Close'].rolling(60).mean().iloc[-11])
        
        is_kis = c_price > 0
        snap = StockSnapshot(
            ticker=ticker, current_price=c_price if is_kis else fdr_close, high_price=high_p if is_kis else fdr_high, low_price=low_p if is_kis else fdr_low,
            ma20=ma20, ma60=ma60, ma200=ma200, m60_up=m60_up, as_of=datetime.datetime.now(), 
            source="KIS" if is_kis else "FDR", is_valid=True, is_complete_bar=False, reason="OK", executable=is_kis
        )
        snap.validate(is_halted)
        if not snap.is_valid: return snap.current_price, f"Blocked: {snap.reason}", 0.0, snap.reason
            
        if buy_price > 0:
            is_sell, _, reason = check_exit_signal(strat, cfg, snap, buy_price, highest_price, days_held)
            if is_sell: return snap.current_price, "🔴 SELL SIGNAL", 999.0, reason
        
        is_buy, score, reason = check_entry_signal(strat, cfg, snap)
        if is_buy: return snap.current_price, "🟢 BUY SIGNAL", round(score, 1), reason
        return snap.current_price, "🟡 HOLD", 50.0, f"Div {((snap.current_price/ma20)-1)*100:+.1f}%"
    except Exception as e: return c_price, "Error", 0.0, str(e)

def run_quant_simulation(*args, **kwargs):
    return {"error": "NOT_IMPLEMENTED", "msg": "백테스트 원본 미확인"}

def run_yearly_realistic_backtest(*args, **kwargs):
    return {"error": "NOT_IMPLEMENTED", "msg": "백테스트 원본 미확인"}
