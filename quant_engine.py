import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
from dataclasses import dataclass
from typing import Optional, Tuple
from datetime import datetime, timedelta

@dataclass
class MarketSnapshot:
    vix_value: float
    is_valid: bool
    reason: str = ""

@dataclass
class StockSnapshot:
    ticker: str
    close: float
    low: float
    volume: float
    ma200: float
    ma60: float
    ma20: float
    ret_60: float
    ret_20: float
    drawdown: float
    avg_trade_val: float
    days_since_peak: int
    vol_contraction: float
    ma60_slope_positive: bool
    is_above_ma200: bool
    vol_surged: bool

@dataclass
class StrategyConfig:
    strategy_name: str
    use_ma200_filter: bool
    ma_buffer_pct: float
    stop_loss_pct: float
    ts_target_pct: float
    ts_drop_pct: float
    max_alloc_pct: float
    min_liquidity: float = 5000000000.0

@dataclass
class PositionState:
    ticker: str
    managed_qty: int
    avg_fill_price: float
    highest_price: float
    trailing_armed: bool

def fetch_market_snapshot() -> MarketSnapshot:
    try:
        # VIX Fail-Closed 정책 적용
        v_df = yf.download("^VIX", period="5d", progress=False)
        if v_df.empty: return MarketSnapshot(0.0, False, "DATA_INVALID")
        if isinstance(v_df.columns, pd.MultiIndex): v_df.columns = v_df.columns.get_level_values(0)
        vix_val = float(v_df['Close'].iloc[-1])
        if vix_val >= 30.0: return MarketSnapshot(vix_val, False, "RISK_OFF")
        return MarketSnapshot(vix_val, True, "NORMAL")
    except Exception as e:
        return MarketSnapshot(0.0, False, f"DATA_ERROR: {str(e)}")

def fetch_stock_snapshot(ticker: str) -> Optional[StockSnapshot]:
    try:
        df = fdr.DataReader(str(ticker).zfill(6), start=(datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d'))
        if len(df) < 200: return None # P0: 데이터 부족 시 거래 금지
        
        close, low, vol = float(df['Close'].iloc[-1]), float(df['Low'].iloc[-1]), float(df['Volume'].iloc[-1])
        ma200, ma60, ma20 = df['Close'].rolling(200).mean().iloc[-1], df['Close'].rolling(60).mean().iloc[-1], df['Close'].rolling(20).mean().iloc[-1]
        
        ret_60 = (close / df['Close'].iloc[-61]) - 1 if len(df) >= 61 else 0
        ret_20 = (close / df['Close'].iloc[-21]) - 1 if len(df) >= 21 else 0
        ma60_slope_positive = float(ma60) > float(df['Close'].rolling(60).mean().iloc[-11])
        
        high_120 = df['Close'].rolling(120).max().iloc[-1]
        drawdown = (close / high_120) - 1 if high_120 > 0 else 0
        days_since_peak = int(np.argmax(df['Close'].tail(120).values[::-1]))
        
        v5 = df['Volume'].rolling(5).mean().shift(1).iloc[-1]
        df['VR'] = np.where(df['Volume'].rolling(5).mean().shift(1) > 0, df['Volume'] / df['Volume'].rolling(5).mean().shift(1) * 100, 100)
        vol_surged = float(df['VR'].rolling(20).max().iloc[-1]) >= 200.0
        
        peak_vol_20 = df['Volume'].rolling(20).max().iloc[-1]
        vol_contraction = (df['Volume'].rolling(5).mean().iloc[-1] / peak_vol_20) if peak_vol_20 > 0 else 1.0
        atv = float((df['Close'] * df['Volume']).rolling(5).mean().iloc[-1])
        
        return StockSnapshot(ticker, close, low, vol, ma200, ma60, ma20, ret_60, ret_20, drawdown, atv, days_since_peak, vol_contraction, ma60_slope_positive, (close >= ma200), vol_surged)
    except: return None

def generate_signal(snap: StockSnapshot, mkt: MarketSnapshot, cfg: StrategyConfig) -> dict:
    if not mkt.is_valid: return {"entry": False, "score": 0.0, "reason": mkt.reason}
    if snap.avg_trade_val < cfg.min_liquidity: return {"entry": False, "score": 0.0, "reason": "LIQUIDITY_LOW"}

    pass_ma200 = snap.is_above_ma200 if cfg.use_ma200_filter else True
    score = snap.ret_60 * 100 # 단순 스코어링

    if cfg.strategy_name == "Core":
        cond = (pass_ma200 and (snap.ma20 >= snap.ma60 * (1 + cfg.ma_buffer_pct)) and snap.ma60_slope_positive and (snap.ret_20 > 0))
        return {"entry": cond, "score": score, "reason": "CORE_COND_MET" if cond else "WAIT"}
        
    elif cfg.strategy_name == "Satellite":
        dist_ma20 = (snap.close / snap.ma20) - 1
        low_dist_ma20 = (snap.low / snap.ma20) - 1
        is_dip = (-0.05 <= dist_ma20 <= 0.03) or (low_dist_ma20 <= 0.01 and dist_ma20 >= -0.05)
        
        cond = (pass_ma200 and is_dip and snap.vol_surged and (snap.days_since_peak <= 45) and 
                (snap.vol_contraction <= 0.50) and (snap.drawdown >= -0.30) and (snap.ret_20 > -0.03))
        return {"entry": cond, "score": score, "reason": "SAT_COND_MET" if cond else "WAIT"}
        
    return {"entry": False, "score": 0.0, "reason": "UNKNOWN_STRATEGY"}

def evaluate_exit(snap: StockSnapshot, pos: PositionState, cfg: StrategyConfig) -> Tuple[bool, str, PositionState]:
    ret = (snap.close / pos.avg_fill_price) - 1
    if snap.close > pos.highest_price: pos.highest_price = snap.close
    if ret >= cfg.ts_target_pct: pos.trailing_armed = True
        
    if ret <= cfg.stop_loss_pct: return True, "STOP_LOSS", pos
        
    if pos.trailing_armed:
        drop_from_high = (snap.close / pos.highest_price) - 1
        if drop_from_high <= cfg.ts_drop_pct: return True, "TRAILING_STOP", pos
            
    if cfg.strategy_name == "Core" and snap.ma20 < snap.ma60 * (1 - cfg.ma_buffer_pct / 2): return True, "TREND_EXIT", pos
    elif cfg.strategy_name == "Satellite" and snap.close < snap.ma20 * (1 - cfg.ma_buffer_pct / 2): return True, "TREND_EXIT", pos
            
    return False, "HOLD", pos
