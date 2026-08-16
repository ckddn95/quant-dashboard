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

_fdr_cache = {}

def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0, highest_price: float=0, c_price: float=0, high_p: float=0, low_p: float=0, is_halted: bool=False, days_held: int=0):
    try:
        start_d = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
        cache_key = f"{ticker}_{start_d}"
        if cache_key in _fdr_cache: df = _fdr_cache[cache_key]
        else: df = fdr.DataReader(str(ticker).zfill(6), start=start_d); _fdr_cache[cache_key] = df
            
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
            sl_target = buy_price * (1.0 + cfg.sl)
            ts_target = max(highest_price, snap.high_price) * (1.0 + cfg.ts_drp)
            if snap.low_price <= sl_target: return snap.current_price, "🔴 장중 손절컷", 999.0, "장중 손절컷 터치"
            if max(highest_price, snap.high_price) >= buy_price * (1.0 + cfg.ts_tgt) and snap.low_price <= ts_target: return snap.current_price, "🔵 트레일링 익절", 999.0, "트레일링 익절 터치"
            if days_held >= cfg.min_h and snap.current_price < (snap.ma60 if strat == Strategy.CORE else snap.ma20) * (1.0 - cfg.buf/2.0): return snap.current_price, "🔴 종가 추세이탈", 999.0, "종가 추세이탈"
            
        pass_ma200 = (snap.current_price >= snap.ma200) if cfg.ma200 else True
        if strat == Strategy.CORE:
            dist = (snap.ma20 / snap.ma60) - 1.0 if snap.ma60 > 0 else 0.0
            if pass_ma200 and dist >= cfg.buf and snap.m60_up: 
                return snap.current_price, "🟢 매수 시그널 발생", round(min(85.0 + max(0.0, dist * 100.0), 99.0), 2), f"골든크로스 (이격도 {dist*100:+.2f}%)"
        else:
            dist = (snap.current_price / snap.ma20) - 1.0 if snap.ma20 > 0 else 0.0
            if pass_ma200 and -0.05 <= dist <= 0.03: 
                return snap.current_price, "🟢 매수 시그널 발생", round(min(85.0 + max(0.0, (0.03 - dist) * 100.0), 99.0), 2), f"눌림목 (이격도 {dist*100:+.2f}%)"
        
        dist_eval = (snap.current_price / snap.ma20) - 1.0 if snap.ma20 > 0 else 0.0
        return snap.current_price, "🟡 모니터링 유지", 50.0, f"이격도 {dist_eval*100:+.2f}%"
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


# ==========================================
# 🛑 [시뮬레이션 엔진] NAV 평가 버그 픽스 및 마스터 캘린더
# ==========================================
def run_quant_simulation(target_stocks_df: pd.DataFrame, strat: Strategy, init_cash: float, start_date: datetime.date, end_date: datetime.date, cfg: StrategyConfig, is_weekly_scan: bool = False):
    try:
        if target_stocks_df.empty: return {"status": "error", "msg": "분석 대상 종목이 없습니다."}
        
        tickers = target_stocks_df['티커'].astype(str).str.zfill(6).tolist()
        dfs = {}
        fetch_start = start_date - datetime.timedelta(days=365)
        
        # 1. 개별 종목 데이터 로드 및 마스터 캘린더 생성
        all_dates = set()
        for tk in tickers:
            df = fdr.DataReader(tk, start=fetch_start, end=end_date)
            if not df.empty:
                df['MA20'] = df['Close'].rolling(20).mean()
                df['MA60'] = df['Close'].rolling(60).mean()
                df['MA200'] = df['Close'].rolling(200).mean()
                df['M60_UP'] = df['MA60'] > df['Close'].rolling(60).mean().shift(10)
                dfs[tk] = df
                all_dates.update(df.index)
        
        if not dfs: return {"status": "error", "msg": "POINT_IN_TIME_DATA_UNAVAILABLE: 데이터를 불러올 수 없습니다."}
        
        market_df = pd.DataFrame()
        if cfg.boost:
            try:
                market_df = fdr.DataReader('KS11', start=fetch_start, end=end_date)
                market_df['MA200'] = market_df['Close'].rolling(200).mean()
            except: pass

        # 🛑 [패치] 모든 종목의 날짜를 합친 통합 캘린더 적용
        calendar = sorted(list(all_dates))
        calendar = [d for d in calendar if d.date() >= start_date and d.date() <= end_date]
        
        cash = float(init_cash)
        positions = {}
        nav_history = []
        pending_orders = []
        cooldown_tracker = {} 
        trade_log = [] 
        assumed_cost_pct = 0.0025 # 0.25% All-in 추정 비용
        
        for i, current_date in enumerate(calendar):
            # 1. T+1 아침 체결 처리 (실제 데이터가 존재하는 날만 체결)
            executed_today = []
            for order in pending_orders:
                tk = order['ticker']
                if tk not in dfs or current_date not in dfs[tk].index: continue # 거래 없으면 패스 (가짜 체결 금지)
                open_p = dfs[tk].loc[current_date, 'Open']
                
                if pd.isna(open_p) or open_p <= 0: continue 
                
                if order['side'] == 'BUY':
                    cost_price = open_p * (1.0 + assumed_cost_pct)
                    if cash >= cost_price * order['qty']:
                        cash -= cost_price * order['qty']
                        if tk in positions:
                            old_qty, old_bp = positions[tk]['qty'], positions[tk]['buy_price']
                            new_qty = old_qty + order['qty']
                            new_bp = ((old_qty * old_bp) + (order['qty'] * cost_price)) / new_qty
                            positions[tk] = {"qty": new_qty, "buy_price": new_bp, "highest": cost_price, "days": 0}
                        else:
                            positions[tk] = {"qty": order['qty'], "buy_price": cost_price, "highest": cost_price, "days": 0}
                elif order['side'] == 'SELL' and tk in positions:
                    sell_price = open_p * (1.0 - assumed_cost_pct)
                    
                    profit_pct = (sell_price / positions[tk]['buy_price']) - 1.0
                    trade_log.append(profit_pct)
                    if profit_pct < 0: cooldown_tracker[tk] = current_date 
                    
                    cash += sell_price * positions[tk]['qty']
                    del positions[tk]
            
            pending_orders = []
            sell_signals = []
            
            # 2. 장중 보수적 위험 감시
            for tk, pos in list(positions.items()):
                if current_date not in dfs[tk].index: continue
                row = dfs[tk].loc[current_date]
                if row['Low'] <= 0 or row['High'] <= 0: continue 
                
                pos['days'] += 1
                pos['highest'] = max(pos['highest'], row['High'])
                
                sl_target = pos['buy_price'] * (1.0 + cfg.sl)
                ts_target = pos['highest'] * (1.0 + cfg.ts_drp)
                
                hit_sl = row['Low'] <= sl_target
                hit_ts = (pos['highest'] >= pos['buy_price'] * (1.0 + cfg.ts_tgt)) and (row['Low'] <= ts_target)
                
                sell_price = 0.0
                if hit_sl and hit_ts: sell_price = min(row['Open'], sl_target)
                elif hit_sl: sell_price = min(row['Open'], sl_target)
                elif hit_ts: sell_price = min(row['Open'], ts_target)
                
                if sell_price > 0:
                    real_sell_price = sell_price * (1.0 - assumed_cost_pct)
                    profit_pct = (real_sell_price / pos['buy_price']) - 1.0
                    trade_log.append(profit_pct)
                    if profit_pct < 0: cooldown_tracker[tk] = current_date
                    
                    cash += real_sell_price * pos['qty']
                    del positions[tk]
                    continue
                
                if pos['days'] >= cfg.min_h:
                    if strat == Strategy.CORE and row['Close'] < row['MA60'] * (1.0 - cfg.buf/2.0):
                        sell_signals.append({"ticker": tk, "side": "SELL", "qty": pos['qty']})
                    elif strat == Strategy.SATELLITE and row['Close'] < row['MA20'] * (1.0 - cfg.buf/2.0):
                        sell_signals.append({"ticker": tk, "side": "SELL", "qty": pos['qty']})
            
            pending_orders.extend(sell_signals)
            
            # 3. 🛑 [패치] 일일 NAV 기록 (평가용 최신 종가 끌어오기 적용)
            daily_eval = cash
            for tk, pos in positions.items():
                try:
                    # 해당 종목의 현재 날짜 이하의 가장 최근 유효 종가를 찾아 평가에만 사용
                    last_close = dfs[tk]['Close'].loc[:current_date].iloc[-1]
                    if not pd.isna(last_close):
                        daily_eval += pos['qty'] * last_close
                    else:
                        daily_eval += pos['qty'] * pos['buy_price']
                except:
                    daily_eval += pos['qty'] * pos['buy_price'] # 에러 시 원가 적용
                    
            nav_history.append({"Date": current_date, "NAV": daily_eval})
            
            # 4. 장 마감 후 신규 진입 스캔
            is_friday = current_date.weekday() == 4
            if not is_weekly_scan or is_friday:
                alloc_mult = 1.0
                if cfg.boost and not market_df.empty and current_date in market_df.index:
                    m_row = market_df.loc[current_date]
                    if pd.notna(m_row['MA200']) and m_row['Close'] > m_row['MA200']: 
                        alloc_mult = 1.5 
                        
                buy_candidates = []
                for tk in tickers:
                    if tk in positions or tk not in dfs or current_date not in dfs[tk].index: continue
                    
                    if tk in cooldown_tracker:
                        days_since_loss = (current_date - cooldown_tracker[tk]).days
                        if days_since_loss < cfg.cd: continue 
                        
                    row = dfs[tk].loc[current_date]
                    if pd.isna(row['MA200']) or row['Close'] <= 0: continue
                    
                    pass_ma200 = (row['Close'] >= row['MA200']) if cfg.ma200 else True
                    if strat == Strategy.CORE:
                        dist = (row['MA20'] / row['MA60']) - 1.0 if row['MA60'] > 0 else 0.0
                        if pass_ma200 and dist >= cfg.buf and row['M60_UP']: 
                            buy_candidates.append({"ticker": tk, "score": min(85.0 + max(0.0, dist * 100.0), 99.0), "close": row['Close']})
                    else:
                        dist = (row['Close'] / row['MA20']) - 1.0 if row['MA20'] > 0 else 0.0
                        if pass_ma200 and -0.05 <= dist <= 0.03:
                            buy_candidates.append({"ticker": tk, "score": min(85.0 + max(0.0, (0.03 - dist) * 100.0), 99.0), "close": row['Close']})
                
                buy_candidates = sorted(buy_candidates, key=lambda x: x['score'], reverse=True)
                target_alloc_amt = daily_eval * min(1.0, cfg.alloc * alloc_mult) 
                
                for cand in buy_candidates:
                    if cash <= 0: break
                    alloc_amt = min(cash, target_alloc_amt)
                    qty = int(alloc_amt // (cand['close'] * (1.0 + assumed_cost_pct)))
                    if qty > 0:
                        pending_orders.append({"ticker": cand['ticker'], "side": "BUY", "qty": qty})
                        cash -= qty * cand['close'] * (1.0 + assumed_cost_pct)

        if not nav_history: return {"status": "error", "msg": "데이터 부족으로 시뮬레이션을 완료할 수 없습니다."}
        
        nav_df = pd.DataFrame(nav_history)
        final_asset = nav_df['NAV'].iloc[-1]
        returns = nav_df['NAV'].pct_change().dropna()
        cagr = (final_asset / init_cash) ** (252 / len(nav_df)) - 1 if len(nav_df) > 0 else 0
        mdd = (nav_df['NAV'] / nav_df['NAV'].cummax() - 1).min()
        
        win_rate = (sum(1 for p in trade_log if p > 0) / len(trade_log) * 100) if trade_log else 0.0
        
        return {
            "status": "success",
            "final_asset": final_asset,
            "final_port_ret": (final_asset / init_cash - 1) * 100,
            "metrics": {"CAGR": cagr, "MDD": mdd},
            "summary_rows": [
                {"항목": "총 거래일수", "값": f"{len(nav_df)} 일"},
                {"항목": "총 거래횟수 (청산기준)", "값": f"{len(trade_log)} 회"},
                {"항목": "승률 (Win Rate)", "값": f"{win_rate:.1f} %"},
                {"항목": "가정 수수료율", "값": "0.25% (ALL-IN)"}
            ]
        }
    except Exception as e:
        return {"status": "error", "msg": f"엔진 오류: {str(e)}"}

def run_yearly_realistic_backtest(strat: Strategy, init_cash: float, year: int, cfg: StrategyConfig):
    krx = load_krx_universe()
    if krx.empty: return {"status": "error", "msg": "유니버스 로드 실패"}
    cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].head(100) if strat == Strategy.CORE else krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].head(100)
    target_df = pd.DataFrame([{'티커': str(r['Code']).zfill(6), '종목명': r['Name']} for _, r in cands.iterrows()])
    
    start_d = datetime.date(year, 1, 1)
    end_d = datetime.date(year, 12, 31)
    return run_quant_simulation(target_df, strat, init_cash, start_d, end_d, cfg, is_weekly_scan=True)
