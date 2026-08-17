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
    except Exception: return pd.DataFrame()

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
    
    hit_sl = low_p <= sl_target
    hit_ts = (max(highest_p, high_p) >= buy_p * (1.0 + cfg.ts_tgt)) and (low_p <= ts_target)
    if hit_sl and hit_ts: return True, min(open_p, sl_target), "🔴 장중 손절컷"
    elif hit_sl: return True, min(open_p, sl_target), "🔴 장중 손절컷"
    elif hit_ts: return True, min(open_p, ts_target), "🔵 트레일링 익절"
    
    if days_held >= cfg.min_h:
        if strat == Strategy.CORE and close_p < ma60 * (1.0 - cfg.buf/2.0): return True, close_p, "🔴 추세이탈 (검증필요)"
        elif strat == Strategy.SATELLITE and close_p < ma20 * (1.0 - cfg.buf/2.0): return True, close_p, "🔴 추세이탈 (검증필요)"
    return False, 0.0, ""

_fdr_cache = {}

def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0, highest_price: float=0, c_price: float=0, high_p: float=0, low_p: float=0, is_halted: bool=False, days_held: int=0):
    try:
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

# 🛑 [Step 3 패치] 외부 현금 입출금(external_cash_flows) 반영 및 고급 지표 산출 엔진
def run_quant_simulation(target_stocks_df: pd.DataFrame, strat: Strategy, init_cash: float, start_date: datetime.date, end_date: datetime.date, cfg: StrategyConfig, is_weekly_scan: bool = False, external_cash_flows: dict = None):
    try:
        if target_stocks_df.empty: return {"status": "error", "msg": "분석 대상 종목이 없습니다."}
        if not external_cash_flows: external_cash_flows = {}
        
        ticker_to_name = dict(zip(target_stocks_df['티커'].astype(str).str.zfill(6), target_stocks_df['종목명']))
        tickers = list(ticker_to_name.keys())
        
        dfs = {}
        fetch_start = start_date - datetime.timedelta(days=365)
        all_dates = set()
        for tk in tickers:
            df = fdr.DataReader(tk, start=fetch_start, end=end_date)
            if not df.empty:
                df['MA20'], df['MA60'], df['MA200'] = df['Close'].rolling(20).mean(), df['Close'].rolling(60).mean(), df['Close'].rolling(200).mean()
                df['M60_UP'] = df['MA60'] > df['Close'].rolling(60).mean().shift(10)
                dfs[tk] = df
                all_dates.update(df.index)
        
        if not dfs: return {"status": "error", "msg": "POINT_IN_TIME_DATA_UNAVAILABLE: 생존자 편향 등 데이터 조회 불가."}
        
        market_df = pd.DataFrame()
        if cfg.boost:
            try:
                market_df = fdr.DataReader('KS11', start=fetch_start, end=end_date)
                market_df['MA200'] = market_df['Close'].rolling(200).mean()
            except Exception: pass

        calendar = sorted([d for d in list(all_dates) if d.date() >= start_date and d.date() <= end_date])
        
        cash = float(init_cash)
        positions = {}
        nav_history = []
        pending_orders = []
        cooldown_tracker = {}
        trade_log = []
        closed_trades_log = []
        assumed_cost_pct = SIM_RULES['assumed_cost_pct_per_side']
        
        # [Step 3] 고급 지표 추적 변수
        total_traded_value = 0.0
        total_cost_drag = 0.0
        twr_index = 1.0 # Time-Weighted Return (시간가중수익률)
        
        for i, current_date in enumerate(calendar):
            current_date_str = current_date.strftime('%Y-%m-%d')
            
            # 🛑 1. 외부 입출금 처리 및 비례 매도 (Pro-rata Sell)
            if current_date_str in external_cash_flows:
                cash_flow = external_cash_flows[current_date_str]
                cash += cash_flow
                
                # 출금으로 현금이 마이너스인 경우, 보유 주식을 비율대로 강제 매도
                if cash < 0 and positions:
                    shortage = abs(cash)
                    total_stock_value = sum([pos['qty'] * (dfs[tk].loc[current_date, 'Open'] if current_date in dfs[tk].index else pos['buy_price']) for tk, pos in positions.items()])
                    
                    if total_stock_value > 0:
                        sell_ratio = min(1.0, shortage / total_stock_value)
                        for tk, pos in list(positions.items()):
                            if current_date not in dfs[tk].index: continue
                            sell_qty = int(pos['qty'] * sell_ratio)
                            if sell_qty > 0:
                                sell_price = dfs[tk].loc[current_date, 'Open'] * (1.0 - assumed_cost_pct)
                                profit_pct = (sell_price / pos['buy_price']) - 1.0
                                profit_amt = (sell_price - pos['buy_price']) * sell_qty
                                
                                cost_incurred = dfs[tk].loc[current_date, 'Open'] * sell_qty * assumed_cost_pct
                                total_cost_drag += cost_incurred
                                total_traded_value += sell_price * sell_qty
                                
                                closed_trades_log.append({
                                    "종목명": ticker_to_name.get(tk, tk), "진입일": pos["entry_date"].strftime('%Y-%m-%d'), "청산일": current_date_str,
                                    "보유일수": f"{pos['days']}일", "진입단가": pos['buy_price'], "청산단가": sell_price, "수량": sell_qty,
                                    "손익금": profit_amt, "수익률": f"{profit_pct*100:+.2f}%", "사유": "현금 인출 (비례매도)"
                                })
                                
                                cash += sell_price * sell_qty
                                pos['qty'] -= sell_qty
                                if pos['qty'] == 0: del positions[tk]
                    
            # 2. 대기 주문(신규/매도) 처리
            for order in pending_orders:
                tk = order['ticker']
                if tk not in dfs or current_date not in dfs[tk].index: continue 
                open_p = dfs[tk].loc[current_date, 'Open']
                if pd.isna(open_p) or open_p <= 0: continue 
                
                if order['side'] == 'BUY':
                    cost_price = open_p * (1.0 + assumed_cost_pct)
                    executable_qty = int(order['qty'])
                    if cash < cost_price * executable_qty: executable_qty = int(cash // cost_price)
                    
                    if executable_qty > 0:
                        cost_incurred = open_p * executable_qty * assumed_cost_pct
                        total_cost_drag += cost_incurred
                        total_traded_value += open_p * executable_qty
                        
                        cash -= cost_price * executable_qty 
                        if tk in positions:
                            old_qty, old_bp = positions[tk]['qty'], positions[tk]['buy_price']
                            new_qty, new_bp = old_qty + executable_qty, ((old_qty * old_bp) + (executable_qty * cost_price)) / (old_qty + executable_qty)
                            positions[tk].update({"qty": new_qty, "buy_price": new_bp, "highest": cost_price})
                        else: positions[tk] = {"qty": executable_qty, "buy_price": cost_price, "highest": cost_price, "days": 0, "entry_date": current_date}
                        
                elif order['side'] == 'SELL' and tk in positions:
                    sell_price = open_p * (1.0 - assumed_cost_pct)
                    profit_pct, profit_amt = (sell_price / positions[tk]['buy_price']) - 1.0, (sell_price - positions[tk]['buy_price']) * positions[tk]['qty']
                    trade_log.append(profit_pct)
                    if profit_pct < 0: cooldown_tracker[tk] = current_date 
                    
                    cost_incurred = open_p * positions[tk]['qty'] * assumed_cost_pct
                    total_cost_drag += cost_incurred
                    total_traded_value += sell_price * positions[tk]['qty']
                    
                    closed_trades_log.append({
                        "종목명": ticker_to_name.get(tk, tk), "진입일": positions[tk]["entry_date"].strftime('%Y-%m-%d'), "청산일": current_date_str,
                        "보유일수": f"{positions[tk]['days']}일", "진입단가": positions[tk]['buy_price'], "청산단가": sell_price, "수량": positions[tk]['qty'],
                        "손익금": profit_amt, "수익률": f"{profit_pct*100:+.2f}%", "사유": order.get('reason', '종가 추세이탈')
                    })
                    cash += sell_price * positions[tk]['qty']
                    del positions[tk]
            
            pending_orders, sell_signals = [], []
            
            # 3. 장중 손절/트레일링 검사
            for tk, pos in list(positions.items()):
                if current_date not in dfs[tk].index: continue
                row = dfs[tk].loc[current_date]
                if row['Low'] <= 0 or row['High'] <= 0: continue 
                
                pos['days'] += 1; pos['highest'] = max(pos['highest'], row['High'])
                is_sell, sell_price, reason = calc_sell_signal(strat, cfg, row['Open'], row['High'], row['Low'], row['Close'], pos['buy_price'], pos['highest'], pos['days'], row['MA20'], row['MA60'])
                
                if is_sell and sell_price > 0 and "종가 추세이탈" not in reason:
                    real_sell_price = sell_price * (1.0 - assumed_cost_pct)
                    profit_pct, profit_amt = (real_sell_price / pos['buy_price']) - 1.0, (real_sell_price - pos['buy_price']) * pos['qty']
                    trade_log.append(profit_pct)
                    if profit_pct < 0: cooldown_tracker[tk] = current_date
                    
                    cost_incurred = sell_price * pos['qty'] * assumed_cost_pct
                    total_cost_drag += cost_incurred
                    total_traded_value += real_sell_price * pos['qty']
                    
                    closed_trades_log.append({
                        "종목명": ticker_to_name.get(tk, tk), "진입일": pos["entry_date"].strftime('%Y-%m-%d'), "청산일": current_date_str,
                        "보유일수": f"{pos['days']}일", "진입단가": pos['buy_price'], "청산단가": real_sell_price, "수량": pos['qty'], "손익금": profit_amt, "수익률": f"{profit_pct*100:+.2f}%", "사유": reason
                    })
                    cash += real_sell_price * pos['qty']
                    del positions[tk]
                    continue
                elif is_sell and "종가 추세이탈" in reason: sell_signals.append({"ticker": tk, "side": "SELL", "qty": pos['qty'], "reason": reason})
            
            pending_orders.extend(sell_signals)
            
            # 4. 일일 자산 평가 및 TWR 계산
            daily_eval = cash
            for tk, pos in positions.items():
                try: daily_eval += pos['qty'] * dfs[tk]['Close'].loc[:current_date].dropna().iloc[-1]
                except Exception: daily_eval += pos['qty'] * pos['buy_price']
            
            nav_history.append({"Date": current_date, "NAV": daily_eval})
            
            if len(nav_history) >= 2:
                prev_nav = nav_history[-2]["NAV"]
                if prev_nav > 0:
                    # 입출금을 보정한 순수 일일 수익률 (Modified Dietz 근사)
                    daily_pure_ret = (daily_eval - external_cash_flows.get(current_date_str, 0)) / prev_nav
                    twr_index *= daily_pure_ret
                    daily_pnl_pct = daily_pure_ret - 1.0
                else: daily_pnl_pct = 0.0
            else: daily_pnl_pct = 0.0
            
            # 5. 스캔(매수 검토)
            is_weekly_scan_day = False
            if is_weekly_scan:
                current_iso_week = current_date.isocalendar()[1]
                if i == len(calendar) - 1: is_weekly_scan_day = True
                else:
                    next_iso_week = calendar[i+1].isocalendar()[1]
                    is_weekly_scan_day = current_iso_week != next_iso_week
            else: is_weekly_scan_day = True 
                
            if is_weekly_scan_day and daily_pnl_pct >= -0.05:
                alloc_mult = 1.5 if (cfg.boost and not market_df.empty and current_date in market_df.index and pd.notna(market_df.loc[current_date, 'MA200']) and market_df.loc[current_date, 'Close'] > market_df.loc[current_date, 'MA200']) else 1.0
                buy_candidates = []
                for tk in tickers:
                    if tk in positions or tk not in dfs or current_date not in dfs[tk].index: continue
                    if tk in cooldown_tracker and (current_date - cooldown_tracker[tk]).days < cfg.cd: continue 
                    row = dfs[tk].loc[current_date]
                    if pd.isna(row['MA200']) or row['Close'] <= 0: continue
                    
                    is_buy, score, reason = calc_buy_signal(strat, cfg, row['Close'], row['MA20'], row['MA60'], row['MA200'], row['M60_UP'])
                    if is_buy: buy_candidates.append({"ticker": tk, "score": score, "close": row['Close'], "reason": reason})
                
                buy_candidates = sorted(buy_candidates, key=lambda x: x['score'], reverse=True)
                target_alloc_amt = daily_eval * min(1.0, cfg.alloc * alloc_mult) 
                
                available_cash = cash
                for cand in buy_candidates:
                    if available_cash <= 0: break
                    alloc_amt = min(available_cash, target_alloc_amt)
                    qty = int(alloc_amt // (cand['close'] * (1.0 + assumed_cost_pct)))
                    if qty > 0:
                        pending_orders.append({"ticker": cand['ticker'], "side": "BUY", "qty": qty, "reason": cand['reason']})
                        available_cash -= qty * cand['close'] * (1.0 + assumed_cost_pct)

        for tk, pos in positions.items():
            last_close = dfs[tk]['Close'].loc[:end_date].dropna().iloc[-1] if tk in dfs else pos['buy_price']
            profit_pct = (last_close / pos['buy_price']) - 1.0 if pos['buy_price'] > 0 else 0
            profit_amt = (last_close - pos['buy_price']) * pos['qty']
            closed_trades_log.append({
                "종목명": ticker_to_name.get(tk, tk), "진입일": pos["entry_date"].strftime('%Y-%m-%d'), "청산일": "-", "보유일수": f"{pos['days']}일",
                "진입단가": pos['buy_price'], "청산단가": last_close, "수량": pos['qty'], "손익금": profit_amt, "수익률": f"{profit_pct*100:+.2f}%", "사유": "보유 중 (미청산 MTM)"
            })

        if len(nav_history) < 2: return {"status": "error", "msg": "수익률 및 지표를 계산하기 위한 거래일 데이터가 부족합니다. (최소 2영업일 이상 필요)"}
        
        nav_df = pd.DataFrame(nav_history)
        final_asset = nav_df['NAV'].iloc[-1]
        
        # 고급 지표 연산
        cagr = (twr_index) ** (252 / len(nav_df)) - 1 if len(nav_df) > 0 else 0
        mdd = (nav_df['NAV'] / nav_df['NAV'].cummax() - 1).min()
        win_rate = (sum(1 for p in trade_log if p > 0) / len(trade_log) * 100) if trade_log else 0.0
        avg_nav = nav_df['NAV'].mean()
        turnover_rate = (total_traded_value / 2.0) / avg_nav if avg_nav > 0 else 0.0
        
        return {
            "status": "success", "final_asset": final_asset, 
            "final_port_ret": (final_asset / init_cash - 1) * 100, 
            "metrics": {"CAGR": cagr, "MDD": mdd, "TWR": (twr_index - 1) * 100, "Turnover": turnover_rate * 100, "Cost_Drag": total_cost_drag},
            "trade_logs": closed_trades_log, 
            "summary_rows": [
                {"항목": "시간가중수익률 (TWR)", "값": f"{(twr_index - 1) * 100:+.2f} %"},
                {"항목": "포트폴리오 회전율 (Turnover)", "값": f"{turnover_rate * 100:.1f} %"},
                {"항목": "총 누수 비용 (Cost Drag)", "값": f"{total_cost_drag:,.0f} 원"},
                {"항목": "승률 (Win Rate)", "값": f"{win_rate:.1f} % (총 {len(trade_log)}회)"},
                {"항목": "데이터 처리 모드", "값": "DAILY_APPROX (생존자 편향 근사)"}
            ]
        }
    except Exception as e: return {"status": "error", "msg": f"엔진 오류: {str(e)}"}

def run_yearly_realistic_backtest(strat: Strategy, init_cash: float, year: int, cfg: StrategyConfig):
    krx = load_krx_universe()
    if krx.empty: return {"status": "error", "msg": "유니버스 로드 실패"}
    cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].head(100) if strat == Strategy.CORE else krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].head(100)
    target_df = pd.DataFrame([{'티커': str(r['Code']).zfill(6), '종목명': r['Name']} for _, r in cands.iterrows()])
    return run_quant_simulation(target_df, strat, init_cash, datetime.date(year, 1, 1), datetime.date(year, 12, 31), cfg, is_weekly_scan=False)
