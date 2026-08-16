import pandas as pd
import numpy as np
import FinanceDataReader as fdr
import datetime
import math
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
        if self.sl >= 0: raise ValueError("sl (손절컷) 은 반드시 음수여야 합니다.")
        if self.ts_drp >= 0: raise ValueError("ts_drp (트레일링 하락허용) 은 반드시 음수여야 합니다.")
        if not (0 < self.alloc <= 1.0): raise ValueError("alloc (투입한도) 는 0초과 1이하 여야 합니다.")
        if self.cd < 0 or self.min_h < 0: raise ValueError("cooldown과 min_h는 0 이상이어야 합니다.")

@dataclass
class StockSnapshot:
    ticker: str; current_price: float; high_price: float; low_price: float
    ma20: float; ma60: float; ma200: float; m60_up: bool
    as_of: datetime.datetime; source: str; is_valid: bool; is_complete_bar: bool; reason: str; executable: bool

    def validate(self, is_halted: bool = False):
        if not self.is_valid: return 
        if math.isnan(self.current_price) or self.current_price <= 0:
            self.is_valid, self.reason, self.executable = False, "유효하지 않은 가격", False; return
        if is_halted:
            self.is_valid, self.reason, self.executable = False, "거래정지 종목", False; return
        if self.source != "KIS":
            self.executable = False # FDR 과거 데이터는 실주문 불가
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
    if ctx.is_kill_switch_on: return False, "KILL_SWITCH 가동 중"
    if not ctx.is_auto_trade_on: return False, "AUTO_TRADE 비활성화"
    if not snap.is_valid: return False, f"데이터 차단: {snap.reason}"
    if not snap.executable: return False, f"실주문 불가 시세 (Source: {snap.source})"
    
    if order_spec.account_id != ctx.account_id or order_spec.environment != ctx.env:
        return False, "계좌/환경 격리 위반"

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if order_spec.environment == "REAL":
        if now.weekday() >= 5: return False, "주말 장 미운영"
        if now.hour < 9 or (now.hour == 15 and now.minute > 30) or now.hour > 15: return False, "장 마감/운영시간 아님"

    if order_spec.side == "BUY":
        if ctx.usable_cash <= 0: return False, "주문가능금액 0원 (예수금 대체 불가)"
        expected_val = order_spec.quantity * (order_spec.limit_price if order_spec.limit_price > 0 else snap.current_price)
        if ctx.usable_cash < expected_val: return False, "현금 부족 (타 매수예약금 선점됨)"
        if ctx.daily_pnl_pct < -0.05: return False, "일일 손실 한도 초과 (-5%)"
    elif order_spec.side == "SELL":
        if ctx.managed_sell_qty < order_spec.quantity: return False, "Managed 자동매도 가능 수량 부족"

    if order_spec.limit_price > 0 and snap.current_price > 0:
        dev = abs((snap.current_price / order_spec.limit_price) - 1.0)
        if dev > 0.03: return False, f"가격 괴리율 초과 ({dev*100:.1f}%)"
        
    return True, "PASS"

def check_entry_signal(strat: Strategy, cfg: StrategyConfig, snap: StockSnapshot) -> tuple[bool, float, str]:
    if not snap.is_valid: return False, 0.0, snap.reason
    pass_ma200 = (snap.current_price >= snap.ma200) if cfg.ma200 else True
    if strat == Strategy.CORE:
        dist_20_60 = (snap.ma20 / snap.ma60) - 1.0 if snap.ma60 > 0 else 0.0
        buy_sig = pass_ma200 and (dist_20_60 >= cfg.buf) and snap.m60_up
        return buy_sig, min(85.0 + max(0.0, dist_20_60 * 100.0), 99.0), f"골든크로스 (이격 {dist_20_60*100:+.1f}%)"
    else:
        dist_c_20 = (snap.current_price / snap.ma20) - 1.0 if snap.ma20 > 0 else 0.0
        buy_sig = pass_ma200 and (-0.05 <= dist_c_20 <= 0.03)
        return buy_sig, min(85.0 + max(0.0, (0.03 - dist_c_20) * 100.0), 99.0), f"눌림목 (이격 {dist_c_20*100:+.1f}%)"

def check_exit_signal(strat: Strategy, cfg: StrategyConfig, snap: StockSnapshot, buy_p: float, highest_p: float, days_held: int) -> tuple[bool, float, str]:
    if not snap.is_valid or buy_p <= 0: return False, 0.0, snap.reason
    highest_p = max(highest_p, snap.high_price)
    
    sl_target = buy_p * (1.0 + cfg.sl)
    ts_trigger = buy_p * (1.0 + cfg.ts_tgt)
    ts_target = highest_p * (1.0 + cfg.ts_drp)
    
    hit_sl = snap.low_price <= sl_target
    hit_ts = (highest_p >= ts_trigger) and (snap.low_price <= ts_target)
    
    if hit_sl and hit_ts: return True, min(snap.current_price, sl_target), "🔴 장중 손절컷 (보수적 체결)"
    elif hit_sl: return True, min(snap.current_price, sl_target), "🔴 장중 손절컷"
    elif hit_ts: return True, min(snap.current_price, ts_target), "🔵 장중 트레일링 익절"
                
    if days_held >= cfg.min_h:
        if strat == Strategy.CORE and snap.current_price < snap.ma60 * (1.0 - cfg.buf/2.0): return True, snap.current_price, "🔴 종가 추세이탈 (MA60)"
        elif strat == Strategy.SATELLITE and snap.current_price < snap.ma20 * (1.0 - cfg.buf/2.0): return True, snap.current_price, "🔴 종가 추세이탈 (MA20)"
            
    return False, 0.0, ""

def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0, highest_price: float=0, c_price: float=0, high_p: float=0, low_p: float=0, is_halted: bool=False, days_held: int=0):
    try:
        df = fdr.DataReader(str(ticker).zfill(6), start=(datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d'))
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
            
        if buy_price > 0:
            is_sell, _, reason = check_exit_signal(strat, cfg, snap, buy_price, highest_price, days_held)
            if is_sell: return snap.current_price, "🔴 긴급 손절/익절", 999.0, reason
        
        is_buy, score, reason = check_entry_signal(strat, cfg, snap)
        if is_buy: return snap.current_price, "🟢 매수 시그널 발생", round(score, 1), reason
        return snap.current_price, "🟡 모니터링 유지", 50.0, f"이격도 {((snap.current_price/ma20)-1)*100:+.1f}%"
    except Exception as e: return c_price, "에러", 0.0, str(e)

# 🛑 [스캐너 복원] 이전 버전에서 용량 문제로 pass 되었던 부분 복구 완료
def run_scanner_safe(strat: Strategy, cfg: StrategyConfig):
    krx = load_krx_universe()
    if krx.empty: return pd.DataFrame()
    
    cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(200) if strat == Strategy.CORE else krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].sort_values('Marcap', ascending=False).head(150)
    
    res = []
    def process(row):
        tc = str(row['Code']).strip().zfill(6)
        cp, action, score, reason = evaluate_stock_for_ui(tc, strat, cfg)
        if "매수 시그널" in action: 
            return {'종목명': row['Name'], '티커': tc, '현재가': cp, 'AI 스코어': score, '진단 근거': reason}
        return None
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for r in executor.map(process, [r for _, r in cands.iterrows()]):
            if r: res.append(r)
    
    res_df = pd.DataFrame(res)
    if not res_df.empty and 'AI 스코어' in res_df.columns:
        return res_df.sort_values('AI 스코어', ascending=False)
    return pd.DataFrame()

def run_quant_simulation(*args, **kwargs):
    return {"error": "NOT_IMPLEMENTED", "msg": "백테스트 원본 미제공. 구현 상태: 미완료."}

def run_yearly_realistic_backtest(*args, **kwargs):
    return {"error": "NOT_IMPLEMENTED", "msg": "백테스트 원본 미제공. 구현 상태: 미완료."}
