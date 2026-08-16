import pandas as pd
import numpy as np
import FinanceDataReader as fdr
import datetime
import math
import uuid
import concurrent.futures
from enum import Enum
from dataclasses import dataclass

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
        if self.sl >= 0 or self.ts_drp >= 0: raise ValueError("sl/ts_drp must be negative.")
        if not (0 < self.alloc <= 1.0): raise ValueError("alloc must be (0, 1.0].")

@dataclass
class StockSnapshot:
    ticker: str; current_price: float; high_price: float; low_price: float
    ma20: float; ma60: float; ma200: float; m60_up: bool
    as_of: datetime.datetime; source: str; is_valid: bool; is_complete_bar: bool; reason: str; executable: bool

    def validate(self, is_halted: bool = False):
        if not self.is_valid: return 
        if math.isnan(self.current_price) or self.current_price <= 0:
            self.is_valid, self.reason, self.executable = False, "Invalid Price", False; return
        if is_halted:
            self.is_valid, self.reason, self.executable = False, "Halted", False; return
        if self.source != "KIS":
            self.executable = False 
        self.is_valid, self.reason = True, "OK"

@dataclass
class RiskContext:
    account_id: str; env: str; usable_cash: float; locked_buy_cash: float
    managed_sell_qty: int; current_exposure: float; max_exposure: float
    daily_pnl_pct: float; is_kill_switch_on: bool; is_auto_trade_on: bool

@dataclass(frozen=True)
class OrderSpec:
    correlation_id: str; idempotency_key: str; broker: str; environment: str; account_id: str; account_product_code: str
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
    if not snap.is_valid: return False, f"Invalid Quote: {snap.reason}"
    if not snap.executable: return False, f"Not Executable Source: {snap.source}"
    if order_spec.account_id != ctx.account_id or order_spec.environment != ctx.env: return False, "Account/Env Isolation Breach"

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if order_spec.environment == "REAL":
        if now.weekday() >= 5: return False, "Weekend Closed"
        if now.hour < 9 or (now.hour == 15 and now.minute > 30) or now.hour > 15: return False, "Market Closed"

    if order_spec.side == "BUY":
        if ctx.usable_cash <= 0: return False, "Zero Usable Cash"
        expected_val = order_spec.quantity * (order_spec.limit_price if order_spec.limit_price > 0 else snap.current_price)
        if ctx.usable_cash < expected_val: return False, "Insufficient Cash (Reserved)"
        if ctx.daily_pnl_pct < -0.05: return False, "Daily PnL < -5%"
    elif order_spec.side == "SELL":
        if ctx.managed_sell_qty < order_spec.quantity: return False, "Insufficient Managed Qty"

    if order_spec.limit_price > 0 and snap.current_price > 0:
        dev = abs((snap.current_price / order_spec.limit_price) - 1.0)
        if dev > 0.03: return False, f"Price Deviation > 3%"
        
    return True, "PASS"

# 🛑 [스캐너 고속화 패치] FDR 무한 대기 방지 메모리 캐싱 
_fdr_cache = {}

def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0, highest_price: float=0, c_price: float=0, high_p: float=0, low_p: float=0, is_halted: bool=False, days_held: int=0):
    try:
        start_d = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
        cache_key = f"{ticker}_{start_d}"
        
        # 캐싱 로직으로 스캐너 속도 대폭 상향
        if cache_key in _fdr_cache:
            df = _fdr_cache[cache_key]
        else:
            df = fdr.DataReader(str(ticker).zfill(6), start=start_d)
            _fdr_cache[cache_key] = df
            
        if df.empty: return c_price, "분석 불가", 0.0, "과거 데이터 없음"
        
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
        if not snap.is_valid: return snap.current_price, f"차단: {snap.reason}", 0.0, snap.reason
            
        # 🛑 [용어 복구] 백서에 명시된 엄격한 한글 단어로 100% 롤백
        if buy_price > 0:
            sl_target = buy_price * (1.0 + cfg.sl)
            ts_target = max(highest_price, snap.high_price) * (1.0 + cfg.ts_drp)
            if snap.low_price <= sl_target: return snap.current_price, "🔴 장중 손절컷", 999.0, "장중 손절컷 터치"
            if max(highest_price, snap.high_price) >= buy_price * (1.0 + cfg.ts_tgt) and snap.low_price <= ts_target: return snap.current_price, "🔵 트레일링 익절", 999.0, "트레일링 익절 터치"
            if days_held >= cfg.min_h and snap.current_price < (snap.ma60 if strat == Strategy.CORE else snap.ma20) * (1.0 - cfg.buf/2.0): return snap.current_price, "🔴 종가 추세이탈", 999.0, "종가 추세이탈"
            
        pass_ma200 = (snap.current_price >= snap.ma200) if cfg.ma200 else True
        if strat == Strategy.CORE:
            dist = (snap.ma20 / snap.ma60) - 1.0 if snap.ma60 > 0 else 0.0
            if pass_ma200 and dist >= cfg.buf and snap.m60_up: return snap.current_price, "🟢 매수 시그널 발생", min(85.0 + max(0.0, dist * 100.0), 99.0), f"골든크로스 (이격도 {dist*100:+.1f}%)"
        else:
            dist = (snap.current_price / snap.ma20) - 1.0 if snap.ma20 > 0 else 0.0
            if pass_ma200 and -0.05 <= dist <= 0.03: return snap.current_price, "🟢 매수 시그널 발생", min(85.0 + max(0.0, (0.03 - dist) * 100.0), 99.0), f"눌림목 (이격도 {dist*100:+.1f}%)"
        
        return snap.current_price, "🟡 모니터링 유지", 50.0, "관망 대기"
    except Exception as e: return c_price, "에러", 0.0, str(e)

def run_scanner_safe(strat: Strategy, cfg: StrategyConfig):
    krx = load_krx_universe()
    if krx.empty: return pd.DataFrame()
    cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(200) if strat == Strategy.CORE else krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].sort_values('Marcap', ascending=False).head(150)
    res = []
    def process(row):
        tc = str(row['Code']).strip().zfill(6)
        cp, action, score, reason = evaluate_stock_for_ui(tc, strat, cfg)
        if "매수 시그널" in action: return {'종목명': row['Name'], '티커': tc, '현재가': cp, 'AI 스코어': score, '진단 근거': reason}
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for r in executor.map(process, [r for _, r in cands.iterrows()]):
            if r: res.append(r)
    res_df = pd.DataFrame(res)
    if not res_df.empty and 'AI 스코어' in res_df.columns: return res_df.sort_values('AI 스코어', ascending=False)
    return pd.DataFrame()

def run_quant_simulation(*args, **kwargs):
    return {"error": "NOT_IMPLEMENTED", "msg": "백테스트 원본 미제공. 구현 상태: 미완료."}

def run_yearly_realistic_backtest(*args, **kwargs):
    return {"error": "NOT_IMPLEMENTED", "msg": "백테스트 원본 미제공. 구현 상태: 미완료."}
