import pandas as pd
import numpy as np
import FinanceDataReader as fdr
import datetime
import math
import concurrent.futures
from enum import Enum
from dataclasses import dataclass
import database as db

CONTRACT = db.CONTRACT
SIM_RULES = CONTRACT['simulation_rules']

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
    ticker: str; current_price: float; high_price: float; low_price: float; ma20: float; ma60: float; ma200: float; m60_up: bool
    as_of: datetime.datetime; source: str; is_valid: bool; is_complete_bar: bool; reason: str; executable: bool
    def validate(self, is_halted: bool = False):
        if not self.is_valid: return 
        if math.isnan(self.current_price) or self.current_price <= 0:
            self.is_valid, self.reason, self.executable = False, "Invalid Price", False; return
        if is_halted: self.is_valid, self.reason, self.executable = False, "Halted", False; return
        if self.source != "KIS": self.executable = False 
        self.is_valid, self.reason = True, "OK"

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

def get_default_config(strat: Strategy) -> StrategyConfig:
    c = CONTRACT['strategy'][strat.value]
    return StrategyConfig(ma200=c['ma200'], buf=c['buf'], sl=c['sl'], alloc=c['alloc'], ts_tgt=c['ts_tgt'], ts_drp=c['ts_drp'], cd=c['cd'], min_h=c['min_h'], boost=c['boost'])

def load_krx_universe():
    try: return fdr.StockListing('KRX')
    except: return pd.DataFrame()

def pre_flight_risk_check(order_spec: OrderSpec, snap: StockSnapshot, ctx: RiskContext) -> tuple[bool, str]:
    if ctx.is_kill_switch_on: return False, "KILL_SWITCH ON"
    if not ctx.is_auto_trade_on: return False, "AUTO_TRADE OFF"
    if not snap.is_valid: return False, f"Invalid Quote: {snap.reason}"
    if not snap.executable: return False, f"Not Executable Source: {snap.source}"

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if order_spec.environment == "REAL":
        if now.weekday() >= 5: return False, "Weekend Closed"
        if now.hour < 9 or (now.hour == 15 and now.minute > 30) or now.hour > 15: return False, "Market Closed"

    if order_spec.side == "BUY":
        if ctx.usable_cash <= 0: return False, "Zero Usable Cash"
        expected_val = order_spec.quantity * (order_spec.limit_price if order_spec.limit_price > 0 else snap.current_price) * (1.0 + SIM_RULES['assumed_cost_pct_per_side'])
        if ctx.usable_cash < expected_val: return False, "Insufficient Cash (Reserved)"
        if ctx.daily_pnl_pct < -0.05: return False, "Daily PnL < -5%"
    elif order_spec.side == "SELL":
        if ctx.managed_sell_qty < order_spec.quantity: return False, "Insufficient Managed Qty"

    if order_spec.limit_price > 0 and snap.current_price > 0:
        dev = abs((snap.current_price / order_spec.limit_price) - 1.0)
        if dev > 0.03: return False, "Price Deviation > 3%"
    return True, "PASS"

# 🛑 [Step 2 패치] 일봉(T-1) 지표와 실시간 가격(T)의 명확한 분리 연산
def calc_buy_signal(strat: Strategy, cfg: StrategyConfig, close_p: float, ma20: float, ma60: float, ma200: float, m60_up: bool) -> tuple[bool, float, str]:
    pass_ma200 = (close_p >= ma200) if cfg.ma200 else True
    if strat == Strategy.CORE:
        dist = (ma20 / ma60) - 1.0 if ma60 > 0 else 0.0
        if pass_ma200 and dist >= cfg.buf and m60_up: return True, round(min(85.0 + max(0.0, dist * 100.0), 99.0), 2), f"골든크로스 (이격도 {dist*100:+.2f}%)"
    else:
        dist = (close_p / ma20) - 1.0 if ma20 > 0 else 0.0
        if pass_ma200 and -0.05 <= dist <= 0.03: return True, round(min(85.0 + max(0.0, (0.03 - dist) * 100.0), 99.0), 2), f"눌림목 (이격도 {dist*100:+.2f}%)"
    dist_eval = (close_p / ma20) - 1.0 if ma20 > 0 else 0.0
    return False, 50.0, f"이격도 {dist_eval*100:+.2f}%"

def calc_sell_signal(strat: Strategy, cfg: StrategyConfig, open_p: float, high_p: float, low_p: float, close_p: float, buy_p: float, highest_p: float, days_held: int, ma20: float, ma60: float) -> tuple[bool, float, str]:
    sl_target = buy_p * (1.0 + cfg.sl)
    ts_target = max(highest_p, high_p) * (1.0 + cfg.ts_drp)
    
    # 🛑 [Step 2 패치] 장중 실시간 손절/트레일링은 즉각(Immediate) 판정
    hit_sl = low_p <= sl_target
    hit_ts = (max(highest_p, high_p) >= buy_p * (1.0 + cfg.ts_tgt)) and (low_p <= ts_target)
    if hit_sl and hit_ts: return True, min(open_p, sl_target), "🔴 장중 손절컷"
    elif hit_sl: return True, min(open_p, sl_target), "🔴 장중 손절컷"
    elif hit_ts: return True, min(open_p, ts_target), "🔵 트레일링 익절"
    
    # 🛑 [Step 2 패치] 추세 이탈(정상매도)은 연속성 확인 대상이므로 분리 처리
    if days_held >= cfg.min_h:
        if strat == Strategy.CORE and close_p < ma60 * (1.0 - cfg.buf/2.0): return True, close_p, "🔴 추세이탈 (검증필요)"
        elif strat == Strategy.SATELLITE and close_p < ma20 * (1.0 - cfg.buf/2.0): return True, close_p, "🔴 추세이탈 (검증필요)"
    return False, 0.0, ""

# 🛑 [Step 2 패치] UI 스캐너 전용 (Instantaneous Signal - 즉각 표시용)
_fdr_cache = {}
def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0, highest_price: float=0, c_price: float=0, high_p: float=0, low_p: float=0, is_halted: bool=False, days_held: int=0):
    try:
        # 무조건 어제(T-1)까지의 데이터만 로드하여 콘크리트 지표 생성
        start_d = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
        end_d = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        cache_key = f"{ticker}_{end_d}"
        
        if cache_key in _fdr_cache: df = _fdr_cache[cache_key]
        else: 
            df = fdr.DataReader(str(ticker).zfill(6), start=start_d, end=end_d)
            _fdr_cache[cache_key] = df
            
        if df.empty: return c_price, "분석 불가", 0.0, "전일(T-1) 데이터 없음"
        
        fdr_close, fdr_high, fdr_low = float(df['Close'].iloc[-1]), float(df['High'].iloc[-1]), float(df['Low'].iloc[-1])
        ma20, ma60, ma200 = df['Close'].rolling(20).mean().iloc[-1], df['Close'].rolling(60).mean().iloc[-1], df['Close'].rolling(200).mean().iloc[-1]
        m60_up = True if len(df) < 60 else (ma60 > df['Close'].rolling(60).mean().iloc[-11])
        
        is_kis = c_price > 0
        snap = StockSnapshot(
            ticker=ticker, current_price=c_price if is_kis else fdr_close, high_price=high_p if is_kis else fdr_high, low_price=low_p if is_kis else fdr_low,
            ma20=ma20, ma60=ma60, ma200=ma200, m60_up=m60_up, as_of=datetime.datetime.now(), source="KIS" if is_kis else "FDR", is_valid=True, is_complete_bar=False, reason="OK", executable=is_kis
        )
        snap.validate(is_halted)
        if not snap.is_valid: return snap.current_price, f"차단: {snap.reason}", 0.0, snap.reason
            
        if buy_price > 0:
            is_sell, _, s_reason = calc_sell_signal(strat, cfg, snap.current_price, snap.high_price, snap.low_price, snap.current_price, buy_price, highest_price, days_held, ma20, ma60)
            if is_sell: return snap.current_price, s_reason.replace(" (검증필요)", " (예비)"), 999.0, s_reason
            
        is_buy, score, b_reason = calc_buy_signal(strat, cfg, snap.current_price, ma20, ma60, ma200, m60_up)
        if is_buy: return snap.current_price, "🟢 매수 시그널 발생 (예비)", score, b_reason
        return snap.current_price, "🟡 모니터링 유지", 50.0, b_reason
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

# (이하 run_quant_simulation 등은 기존과 동일하므로 생략 없이 사용)
# ... (답변 길이 한계로 시뮬레이션 본문은 직전 100% 동일 본문 사용)
