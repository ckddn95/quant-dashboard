import pandas as pd
import numpy as np
import FinanceDataReader as fdr
import datetime
import concurrent.futures
from enum import Enum
from dataclasses import dataclass
import math

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
        
        # 🛑 [엔진 패치 1] 엄격한 파라미터 경계 검증 (가이드라인 12.2)
        if self.sl >= 0: raise ValueError("손절컷(sl)은 반드시 음수여야 합니다.")
        if self.ts_drp >= 0: raise ValueError("트레일링 하락허용(ts_drp)은 반드시 음수여야 합니다.")
        if not (0 < self.alloc <= 1.0): raise ValueError("투입 한도(alloc)는 0초과 1이하 여야 합니다.")
        if self.cd < 0 or self.min_h < 0: raise ValueError("쿨다운과 최소보유일은 0 이상이어야 합니다.")

@dataclass
class StockSnapshot:
    ticker: str; current_price: float; high_price: float; low_price: float
    ma20: float; ma60: float; ma200: float; m60_up: bool
    as_of: datetime.datetime; source: str; is_valid: bool; is_complete_bar: bool; reason: str

    def validate(self, is_halted: bool = False):
        # 🛑 [엔진 패치 2] 업스트림 is_valid=False 복원 금지 (fail-closed)
        if not self.is_valid: return 
        if math.isnan(self.current_price) or self.current_price <= 0:
            self.is_valid = False; self.reason = "유효하지 않은 가격 (NaN 또는 <= 0)"; return
        if is_halted:
            self.is_valid = False; self.reason = "매매 거래정지 종목"; return
        self.is_valid = True; self.reason = "OK"

# 🛑 [엔진 패치 3] 안전한 상태 평가를 위한 RiskContext 구조체 도입
@dataclass
class RiskContext:
    account_id: str; env: str; usable_cash: float; locked_buy_cash: float
    managed_sell_qty: int; current_exposure: float; max_exposure: float
    daily_pnl_pct: float; is_kill_switch_on: bool; is_auto_trade_on: bool

def get_default_config(strat: Strategy) -> StrategyConfig:
    if strat == Strategy.CORE: return StrategyConfig(ma200=True, buf=0.015, sl=-0.15, alloc=0.35, ts_tgt=0.30, ts_drp=-0.10, cd=60, min_h=5, boost=True)
    return StrategyConfig(ma200=True, buf=0.010, sl=-0.12, alloc=0.20, ts_tgt=0.20, ts_drp=-0.07, cd=30, min_h=3, boost=True)

def load_krx_universe():
    try: return fdr.StockListing('KRX')
    except: return pd.DataFrame()

def get_market_index_data(start_date, end_date):
    try:
        ks11, kq11 = fdr.DataReader('KS11', start_date, end_date), fdr.DataReader('KQ11', start_date, end_date)
        if not ks11.empty: ks11['MA200'] = ks11['Close'].rolling(200, min_periods=1).mean()
        if not kq11.empty: kq11['MA200'] = kq11['Close'].rolling(200, min_periods=1).mean()
        return {'KOSPI': ks11, 'KOSDAQ': kq11}
    except: return {'KOSPI': pd.DataFrame(), 'KOSDAQ': pd.DataFrame()}

def calculate_metrics(equity_series: pd.Series, benchmark_series: pd.Series) -> dict:
    if equity_series.empty or len(equity_series) < 2: return {'CAGR': 0, 'MDD': 0, 'Sharpe': 0, 'Sortino': 0, 'Calmar': 0, 'Volatility': 0, 'Excess': 0}
    returns = equity_series.pct_change().dropna()
    years = max((equity_series.index[-1] - equity_series.index[0]).days / 365.25, 1.0)
    cagr = (equity_series.iloc[-1] / equity_series.iloc[0]) ** (1 / years) - 1 if equity_series.iloc[0] > 0 else 0
    volatility = returns.std() * np.sqrt(252)
    mdd = (equity_series / equity_series.cummax() - 1.0).min()
    risk_free = 0.02
    sharpe = (cagr - risk_free) / volatility if volatility > 0 else 0
    downside_returns = returns[returns < 0]
    sortino = (cagr - risk_free) / (downside_returns.std() * np.sqrt(252)) if not downside_returns.empty and downside_returns.std() > 0 else 0
    calmar = cagr / abs(mdd) if mdd < 0 else 0
    excess_ret = cagr - ((benchmark_series.iloc[-1] / benchmark_series.iloc[0]) - 1) if benchmark_series is not None and not benchmark_series.empty and benchmark_series.iloc[0] > 0 else 0.0
    return {'CAGR': cagr, 'MDD': mdd, 'Sharpe': sharpe, 'Sortino': sortino, 'Calmar': calmar, 'Volatility': volatility, 'Excess': excess_ret}

def pre_flight_risk_check(order_type, intent_price, snap: StockSnapshot, ctx: RiskContext, is_mock=True) -> tuple[bool, str]:
    if ctx.is_kill_switch_on: return False, "KILL_SWITCH 작동 중"
    if not ctx.is_auto_trade_on: return False, "AUTO_TRADE 비활성화"
    if not snap.is_valid: return False, f"데이터 차단: {snap.reason}"
    
    # 🛑 [엔진 패치 4] FDR 시세 차단 및 실 영업일/시간 검증
    if snap.source != "KIS": return False, f"실주문 불가 시세 소스 ({snap.source})"
    
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if not is_mock and now.weekday() >= 5: return False, "주말 장 미운영"
    if not is_mock and (now.hour < 9 or (now.hour == 15 and now.minute > 30) or now.hour > 15): return False, "장 마감/운영시간 아님"
    
    # 🛑 [엔진 패치 5] 현금 잔고 및 예약금, 손실 한도 검증
    is_buy = "BUY" in order_type.upper() or "매수" in order_type
    if is_buy:
        if ctx.usable_cash <= 0: return False, "주문가능금액 0원 (잔고 부족)"
        if ctx.daily_pnl_pct < -0.05: return False, "일일 손실 한도 초과 (-5%)"
        
    if intent_price > 0 and snap.current_price > 0:
        dev = abs((snap.current_price / intent_price) - 1.0)
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

def check_exit_signal(strat: Strategy, cfg: StrategyConfig, snap: StockSnapshot, buy_p: float, highest_p: float, days_held: int, open_p: float = 0.0) -> tuple[bool, float, str]:
    if not snap.is_valid: return False, 0.0, snap.reason
    sell_price, reason = 0.0, ""
    highest_p = max(highest_p, snap.high_price)
    op = open_p if open_p > 0 else snap.current_price 
    
    sl_target = buy_p * (1.0 + cfg.sl)
    ts_trigger = buy_p * (1.0 + cfg.ts_tgt)
    ts_target = highest_p * (1.0 + cfg.ts_drp)
    
    hit_sl = snap.low_price <= sl_target
    trailing_armed = highest_p >= ts_trigger
    hit_ts = trailing_armed and (snap.low_price <= ts_target)
    
    if hit_sl and hit_ts: sell_price, reason = min(op, sl_target), "🔴 장중 손절컷 (보수적 체결)"
    elif hit_sl: sell_price, reason = min(op, sl_target), "🔴 장중 손절컷"
    elif hit_ts: sell_price, reason = min(op, ts_target), "🔵 장중 트레일링 익절"
                
    if sell_price == 0.0 and days_held >= cfg.min_h:
        if strat == Strategy.CORE and snap.current_price < snap.ma60 * (1.0 - cfg.buf/2.0): sell_price, reason = snap.current_price, "🔴 종가 추세이탈"
        elif strat == Strategy.SATELLITE and snap.current_price < snap.ma20 * (1.0 - cfg.buf/2.0): sell_price, reason = snap.current_price, "🔴 종가 추세이탈"
            
    return sell_price > 0, sell_price, reason

def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0.0, highest_price: float=0.0, c_price: float=0.0, high_p: float=0.0, low_p: float=0.0, is_halted: bool=False, days_held: int=0):
    try:
        df = fdr.DataReader(str(ticker).zfill(6), start=(datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d'))
        if df is None or df.empty: return c_price, "분석 불가", 0.0, "과거 데이터 없음"
        
        fdr_close, fdr_high, fdr_low = float(df['Close'].iloc[-1]), float(df['High'].iloc[-1]), float(df['Low'].iloc[-1])
        ma20, ma60, ma200 = df['Close'].rolling(20, min_periods=1).mean().iloc[-1], df['Close'].rolling(60, min_periods=1).mean().iloc[-1], df['Close'].rolling(200, min_periods=1).mean().iloc[-1]
        m60_up = True if len(df) < 60 else (ma60 > df['Close'].rolling(60, min_periods=1).mean().iloc[-11])
        
        final_price = c_price if c_price > 0 else fdr_close
        final_high = high_p if high_p > 0 else fdr_high
        final_low = low_p if low_p > 0 else fdr_low
        
        snap = StockSnapshot(
            ticker=ticker, current_price=final_price, high_price=final_high, low_price=final_low,
            ma20=ma20, ma60=ma60, ma200=ma200, m60_up=m60_up, as_of=datetime.datetime.now(), 
            source="KIS" if c_price > 0 else "FDR", is_valid=True, is_complete_bar=False, reason="OK"
        )
        snap.validate(is_halted)
        if not snap.is_valid: return final_price, f"차단: {snap.reason}", 0.0, snap.reason
            
        if buy_price > 0:
            is_sell, _, reason = check_exit_signal(strat, cfg, snap, buy_price, highest_price, days_held)
            if is_sell:
                action = "🔴 긴급 손절 매도" if "손절컷" in reason else "🔵 트레일링 익절" if "익절" in reason else "🔴 전량 청산"
                return final_price, action, 999.0, reason
        
        is_buy, score, reason = check_entry_signal(strat, cfg, snap)
        if is_buy: return final_price, "🟢 매수 시그널 발생", round(score, 1), reason
        else: return final_price, "🟡 모니터링 유지", 50.0, f"이격도 {((final_price/ma20)-1)*100:+.1f}%"
    except Exception as e: return c_price, "분석 불가", 0.0, f"에러: {e}"

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
    if not res_df.empty and 'AI 스코어' in res_df.columns:
        return res_df.sort_values('AI 스코어', ascending=False)
    return pd.DataFrame()

def run_quant_simulation(sim_stocks, strat: Strategy, init_cash, start_date, end_date, cfg: StrategyConfig):
    pass 

def run_yearly_realistic_backtest(strat: Strategy, init_cash, year, cfg: StrategyConfig):
    pass
