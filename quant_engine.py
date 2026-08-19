import pandas as pd
import numpy as np
import FinanceDataReader as fdr
import datetime
import math
import concurrent.futures
from enum import Enum
from dataclasses import dataclass
import database as db

# 🚨 Hotfix: datetime 네임스페이스 명시적 지정
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
    broker_time: datetime.datetime; received_at: datetime.datetime; source: str; is_halted: bool; freshness_sec: float; executable: bool
    is_valid: bool = True; reason: str = "OK"

    def validate(self, max_ttl_sec: int = 15):
        if not self.is_valid: return 
        if math.isnan(self.current_price) or self.current_price <= 0:
            self.is_valid, self.reason, self.executable = False, "Invalid Price", False; return
        if self.low_price > self.current_price or self.low_price > self.high_price:
            self.is_valid, self.reason, self.executable = False, "Hierarchy Violation", False; return
        if self.source not in ["KIS", "SIMULATION", "UI"]:
            self.is_valid, self.reason, self.executable = False, "Not Executable Source", False; return
        if self.is_halted:
            self.is_valid, self.reason, self.executable = False, "Halted", False; return
        if self.freshness_sec > max_ttl_sec or self.freshness_sec < 0:
            self.is_valid, self.reason, self.executable = False, "Stale Quote", False; return
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
        if side.upper() == "BUY": slippage = notional * cls.BUY_SLIPPAGE
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
    if ctx.is_kill_switch_on and order_spec.signal_source == 'SYSTEM': return False, "KILL_SWITCH ON"
    if not ctx.is_auto_trade_on and ctx.env == "REAL" and order_spec.signal_source == 'SYSTEM': return False, "AUTO_TRADE OFF"
    if not snap.is_valid: return False, f"Invalid Quote: {snap.reason}"
    if not snap.executable: return False, f"Not Executable Source: {snap.source}"

    if order_spec.side == "BUY":
        if ctx.usable_cash <= 0: return False, "Zero Usable Cash"
        buffer = db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05)
        expected_val = order_spec.quantity * order_spec.reference_price * (buffer if order_spec.order_kind == "MARKET" else 1.0)
        if ctx.usable_cash < expected_val: return False, "Insufficient Cash"
        if ctx.daily_pnl_pct < -0.05: return False, "Daily PnL < -5%"
        if ctx.current_exposure + (order_spec.quantity * order_spec.reference_price) > ctx.max_exposure: return False, "Exceeds Max Exposure"
    elif order_spec.side == "SELL":
        if ctx.managed_sell_qty < order_spec.quantity: return False, "Insufficient Managed Qty"
    return True, "PASS"

def calc_buy_signal(strat: Strategy, cfg: StrategyConfig, close_p: float, ma20: float, ma60: float, ma200: float, m60_up: bool) -> tuple[bool, float, str]:
    pass_ma200 = (close_p >= ma200) if cfg.ma200 else True
    if strat == Strategy.CORE:
        dist = (ma20 / ma60) - 1.0 if ma60 > 0 else 0.0
        if pass_ma200 and dist >= cfg.buf and m60_up: return True, round(min(85.0 + max(0.0, dist * 100.0), 99.0), 2), f"골든크로스"
    else:
        dist = (close_p / ma20) - 1.0 if ma20 > 0 else 0.0
        if pass_ma200 and -0.05 <= dist <= 0.03: return True, round(min(85.0 + max(0.0, (0.03 - dist) * 100.0), 99.0), 2), f"눌림목"
    return False, 50.0, "조건미달"

def calc_sell_signal(strat: Strategy, cfg: StrategyConfig, open_p: float, high_p: float, low_p: float, close_p: float, buy_p: float, highest_p: float, days_held: int, ma20: float, ma60: float) -> tuple[bool, float, ExitReason]:
    sl_target = buy_p * (1.0 + cfg.sl)
    ts_target = max(highest_p, high_p) * (1.0 + cfg.ts_drp)
    if open_p <= sl_target: return True, open_p, ExitReason.STOP_LOSS
    if (max(highest_p, high_p) >= buy_p * (1.0 + cfg.ts_tgt)) and (open_p <= ts_target): return True, open_p, ExitReason.TRAILING_STOP
    hit_sl = low_p <= sl_target
    hit_ts = (max(highest_p, high_p) >= buy_p * (1.0 + cfg.ts_tgt)) and (low_p <= ts_target)
    if hit_sl and hit_ts: return True, min(sl_target, ts_target), ExitReason.STOP_LOSS
    elif hit_sl: return True, sl_target, ExitReason.STOP_LOSS
    elif hit_ts: return True, ts_target, ExitReason.TRAILING_STOP
    if days_held >= cfg.min_h:
        if strat == Strategy.CORE and close_p < ma60 * (1.0 - cfg.buf * cfg.buffer_factor): return True, close_p, ExitReason.TREND_EXIT
        elif strat == Strategy.SATELLITE and close_p < ma20 * (1.0 - cfg.buf * cfg.buffer_factor): return True, close_p, ExitReason.TREND_EXIT
    return False, 0.0, ExitReason.UNKNOWN

_fdr_cache = {}

def evaluate_stock_for_ui(ticker: str, strat: Strategy, cfg: StrategyConfig, buy_price: float=0, highest_price: float=0, c_price: float=0, high_p: float=0, low_p: float=0, is_halted: bool=False, days_held: int=0):
    try:
        start_d = (datetime.datetime.now(KST) - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
        end_d = (datetime.datetime.now(KST) - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        cache_key = f"{ticker}_{end_d}"
        if cache_key in _fdr_cache: df = _fdr_cache[cache_key]
        else: 
            df = fdr.DataReader(str(ticker).zfill(6), start=start_d, end=end_d)
            _fdr_cache[cache_key] = df
        if df.empty: return c_price, "분석 불가", 0.0, "T-1 일봉 없음"
        fdr_close, fdr_high, fdr_low = float(df['Close'].iloc[-1]), float(df['High'].iloc[-1]), float(df['Low'].iloc[-1])
        ma20, ma60, ma200 = df['Close'].rolling(20).mean().iloc[-1], df['Close'].rolling(60).mean().iloc[-1], df['Close'].rolling(200).mean().iloc[-1]
        m60_up = True if len(df) < 60 else (ma60 > df['Close'].rolling(60).mean().iloc[-11])
        is_kis = c_price > 0
        now_dt = datetime.datetime.now(KST)
        snap = StockSnapshot(ticker=ticker, current_price=c_price if is_kis else fdr_close, high_price=high_p if is_kis else fdr_high, low_price=low_p if is_kis else fdr_low, ma20=ma20, ma60=ma60, ma200=ma200, m60_up=m60_up, broker_time=now_dt, received_at=now_dt, source="UI" if is_kis else "SIMULATION", is_halted=is_halted, freshness_sec=0.0, executable=is_kis)
        snap.validate(is_halted)
        if not snap.is_valid: return snap.current_price, f"차단: {snap.reason}", 0.0, snap.reason
        if buy_price > 0:
            is_sell, _, s_reason = calc_sell_signal(strat, cfg, snap.current_price, snap.high_price, snap.low_price, snap.current_price, buy_price, highest_price, days_held, ma20, ma60)
            if is_sell: return snap.current_price, f"🔴 {s_reason.value} (예비)", 999.0, s_reason.value
        is_buy, score, b_reason = calc_buy_signal(strat, cfg, snap.current_price, ma20, ma60, ma200, m60_up)
        if is_buy: return snap.current_price, "🟢 매수 시그널 (예비)", score, b_reason
        return snap.current_price, "🟡 유지", 50.0, b_reason
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
    return res_df.sort_values('AI 스코어', ascending=False) if not res_df.empty else pd.DataFrame()

def run_quant_simulation(target_stocks_df: pd.DataFrame, strat: Strategy, init_cash: float, start_date: datetime.date, end_date: datetime.date, cfg: StrategyConfig, is_weekly_scan: bool = False, external_cash_flows: dict = None, use_legacy_cost: bool = False, user_restricted_universe_by_date: dict = None):
    try:
        if target_stocks_df.empty and not is_weekly_scan: return {"status": "error", "msg": "분석 대상 종목이 없습니다."}
        external_cash_flows = external_cash_flows or {}
        
        krx_df = load_krx_universe()
        if krx_df.empty: return {"status": "error", "msg": "DATA_UNAVAILABLE: 주가 데이터를 불러올 수 없습니다."}
        krx_df['Code'] = krx_df['Code'].astype(str).str.zfill(6)
        ticker_to_market = {r['Code']: ("KOSPI" if "KOSPI" in str(r['Market']).upper() else "KOSDAQ") for _, r in krx_df.iterrows()}
        ticker_to_name = {r['Code']: r['Name'] for _, r in krx_df.iterrows()}
        
        if target_stocks_df is not None and not target_stocks_df.empty: 
            tickers = list(target_stocks_df['티커'].astype(str).str.zfill(6))
        else: 
            if strat == Strategy.CORE:
                tickers = list(krx_df[krx_df['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(100)['Code'])
            else:
                tickers = list(krx_df[krx_df['Market'].str.contains('KOSDAQ', case=False, na=False)].sort_values('Marcap', ascending=False).head(100)['Code'])
            if not tickers: tickers = list(krx_df.sort_values('Marcap', ascending=False).head(100)['Code'])
            
        dfs = {}
        fetch_start = start_date - datetime.timedelta(days=365)
        all_dates = set()
        
        def fetch_data(tk):
            try:
                df = fdr.DataReader(tk, start=fetch_start, end=end_date)
                if not df.empty:
                    df['MA20'] = df['Close'].rolling(20).mean()
                    df['MA60'] = df['Close'].rolling(60).mean()
                    df['MA200'] = df['Close'].rolling(200).mean()
                    df['M60_UP'] = df['MA60'] > df['Close'].rolling(60).mean().shift(10)
                    return tk, df
            except Exception: pass
            return tk, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(fetch_data, tk) for tk in tickers]
            for future in concurrent.futures.as_completed(futures):
                tk, df = future.result()
                if df is not None:
                    dfs[tk] = df
                    all_dates.update(df.index)
        
        if not dfs: return {"status": "error", "msg": "유효한 주가 데이터가 없습니다."}
        
        market_df = pd.DataFrame()
        if cfg.boost:
            try:
                idx_tk = 'KS11' if strat == Strategy.CORE else 'KQ11'
                market_df = fdr.DataReader(idx_tk, start=fetch_start, end=end_date)
                market_df['MA200'] = market_df['Close'].rolling(200).mean()
            except Exception: pass

        calendar = sorted([d for d in list(all_dates) if d.date() >= start_date and d.date() <= end_date])
        cash = float(init_cash)
        positions = {}
        nav_history = []
        pending_orders = [] 
        cooldown_tracker = {} 
        rearm_state = {tk: True for tk in tickers}
        trade_log = []
        closed_trades_log = []
        total_cost_drag, total_traded_value, twr_index = 0.0, 0.0, 1.0 
        loss_streak_sim = {}
        
        for i, current_date in enumerate(calendar):
            current_date_str = current_date.strftime('%Y-%m-%d')
            if current_date_str in external_cash_flows:
                cash += external_cash_flows[current_date_str]
                    
            still_pending = []
            for order in pending_orders:
                tk = order['ticker']
                if tk not in dfs or current_date not in dfs[tk].index:
                    still_pending.append(order)
                    continue 
                open_p = dfs[tk].loc[current_date, 'Open']
                if pd.isna(open_p) or open_p <= 0:
                    still_pending.append(order)
                    continue 
                
                mkt = ticker_to_market.get(tk, "KOSDAQ")
                if order['side'] == 'BUY':
                    cost_inc, slip, tax = CostModel.calculate_cost(current_date.date(), mkt, "BUY", open_p, 1, use_legacy_cost)
                    cost_price_per_share = open_p + cost_inc
                    executable_qty = int(order['qty'])
                    if cash < cost_price_per_share * executable_qty: executable_qty = int(cash // cost_price_per_share)
                    if executable_qty > 0:
                        cash -= cost_price_per_share * executable_qty 
                        total_cost_drag += (cost_inc * executable_qty)
                        total_traded_value += open_p * executable_qty
                        if tk in positions:
                            old_qty, old_bp = positions[tk]['qty'], positions[tk]['buy_price']
                            new_qty = old_qty + executable_qty
                            positions[tk].update({"qty": new_qty, "buy_price": ((old_qty * old_bp) + (executable_qty * cost_price_per_share)) / new_qty, "highest": max(positions[tk]['highest'], cost_price_per_share)})
                        else: positions[tk] = {"qty": executable_qty, "buy_price": cost_price_per_share, "highest": cost_price_per_share, "days": 0, "entry_date": current_date}
                        rearm_state[tk] = False
                elif order['side'] == 'SELL' and tk in positions:
                    cost_inc, slip, tax = CostModel.calculate_cost(current_date.date(), mkt, "SELL", open_p, positions[tk]['qty'], use_legacy_cost)
                    sell_price = open_p - (cost_inc / positions[tk]['qty'])
                    profit_pct = (sell_price / positions[tk]['buy_price']) - 1.0
                    trade_log.append(profit_pct)
                    
                    if profit_pct < 0:
                        loss_streak_sim[tk] = loss_streak_sim.get(tk, 0) + 1
                        if loss_streak_sim[tk] >= 2: cooldown_tracker[tk] = i + cfg.cd
                    else: loss_streak_sim[tk] = 0
                        
                    total_cost_drag += cost_inc
                    total_traded_value += open_p * positions[tk]['qty']
                    closed_trades_log.append({"종목명": ticker_to_name.get(tk, tk), "진입일": positions[tk]["entry_date"].strftime('%Y-%m-%d'), "청산일": current_date_str, "보유일수": positions[tk]['days'], "진입단가": positions[tk]['buy_price'], "청산단가": sell_price, "수량": positions[tk]['qty'], "손익금": (sell_price - positions[tk]['buy_price']) * positions[tk]['qty'], "수익률": f"{profit_pct*100:+.2f}%", "사유": order.get('reason', ExitReason.TREND_EXIT.value)})
                    cash += sell_price * positions[tk]['qty']
                    del positions[tk]
            pending_orders = still_pending
            
            for tk, pos in list(positions.items()):
                if current_date not in dfs[tk].index: continue
                row = dfs[tk].loc[current_date]
                if row['Low'] <= 0 or row['High'] <= 0: continue 
                
                pos['days'] += 1; pos['highest'] = max(pos['highest'], row['High'])
                is_sell, trigger_price, exit_reason = calc_sell_signal(strat, cfg, row['Open'], row['High'], row['Low'], row['Close'], pos['buy_price'], pos['highest'], pos['days'], row['MA20'], row['MA60'])
                
                if is_sell and trigger_price > 0 and exit_reason in [ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP]:
                    mkt = ticker_to_market.get(tk, "KOSDAQ")
                    cost_inc, slip, tax = CostModel.calculate_cost(current_date.date(), mkt, "SELL", trigger_price, pos['qty'], use_legacy_cost)
                    real_sell_price = trigger_price - (cost_inc / pos['qty'])
                    profit_pct = (real_sell_price / pos['buy_price']) - 1.0
                    trade_log.append(profit_pct)
                    
                    if profit_pct < 0:
                        loss_streak_sim[tk] = loss_streak_sim.get(tk, 0) + 1
                        if loss_streak_sim[tk] >= 2: cooldown_tracker[tk] = i + cfg.cd
                    else: loss_streak_sim[tk] = 0
                    
                    total_cost_drag += cost_inc
                    total_traded_value += trigger_price * pos['qty']
                    closed_trades_log.append({"종목명": ticker_to_name.get(tk, tk), "진입일": pos["entry_date"].strftime('%Y-%m-%d'), "청산일": current_date_str, "보유일수": pos['days'], "진입단가": pos['buy_price'], "청산단가": real_sell_price, "수량": pos['qty'], "손익금": (real_sell_price - pos['buy_price']) * pos['qty'], "수익률": f"{profit_pct*100:+.2f}%", "사유": exit_reason.value})
                    cash += real_sell_price * pos['qty']
                    del positions[tk]
                    continue
                elif is_sell and exit_reason == ExitReason.TREND_EXIT:
                    pending_orders.append({"ticker": tk, "side": "SELL", "qty": pos['qty'], "reason": exit_reason.value})
            
            daily_eval = cash
            for tk, pos in positions.items():
                try: daily_eval += pos['qty'] * dfs[tk]['Close'].loc[:current_date].dropna().iloc[-1]
                except Exception: daily_eval += pos['qty'] * pos['buy_price']
            nav_history.append({"Date": current_date, "NAV": daily_eval, "Cash": cash})
            
            if len(nav_history) >= 2 and nav_history[-2]["NAV"] > 0:
                daily_pure_ret = (daily_eval - external_cash_flows.get(current_date_str, 0)) / nav_history[-2]["NAV"]
                twr_index *= daily_pure_ret
                daily_pnl_pct = daily_pure_ret - 1.0
            else: daily_pnl_pct = 0.0
            
            is_weekly_scan_day = (i == len(calendar) - 1) or (not is_weekly_scan) or (current_date.isocalendar()[1] != calendar[i+1].isocalendar()[1])
                
            if is_weekly_scan_day and daily_pnl_pct >= -0.05:
                base_portfolio_alloc = 0.90
                booster_val = db.CONTRACT.get('booster_policy', {}).get('value', 0.10) if (cfg.boost and not market_df.empty and current_date in market_df.index and pd.notna(market_df.loc[current_date, 'MA200']) and market_df.loc[current_date, 'Close'] > market_df.loc[current_date, 'MA200']) else 0.0
                max_portfolio_exposure = daily_eval * min(1.0, base_portfolio_alloc + booster_val)
                current_exposure = sum([pos['qty'] * pos['highest'] for pos in positions.values()])
                available_budget = max(0.0, max_portfolio_exposure - current_exposure)

                buy_candidates = []
                allowed_universe = user_restricted_universe_by_date.get(current_date_str, tickers) if user_restricted_universe_by_date is not None else tickers
                
                for tk in allowed_universe:
                    if tk not in dfs or current_date not in dfs[tk].index: continue
                    row = dfs[tk].loc[current_date]
                    if pd.isna(row['MA200']) or row['Close'] <= 0: continue
                    
                    is_buy, score, reason = calc_buy_signal(strat, cfg, row['Close'], row['MA20'], row['MA60'], row['MA200'], row['M60_UP'])
                    if not is_buy:
                        rearm_state[tk] = True
                    elif is_buy and rearm_state.get(tk, True):
                        if tk in cooldown_tracker and i < cooldown_tracker[tk]: continue 
                        buy_candidates.append({"ticker": tk, "score": score, "close": row['Close'], "reason": reason})
                
                buy_candidates = sorted(buy_candidates, key=lambda x: (x['score'], x['ticker']), reverse=True)
                
                available_cash = cash
                for cand in buy_candidates:
                    if available_budget <= 0 or available_cash <= 0: break
                    stock_alloc_limit = daily_eval * cfg.alloc
                    held_val = positions[cand['ticker']]['qty'] * cand['close'] if cand['ticker'] in positions else 0.0
                    room = max(0.0, stock_alloc_limit - held_val)
                    
                    alloc_amt = min(available_cash, available_budget, room)
                    if alloc_amt > 0:
                        mkt = ticker_to_market.get(cand['ticker'], "KOSDAQ")
                        cost_inc, slip, tax = CostModel.calculate_cost(current_date.date(), mkt, "BUY", cand['close'], 1, use_legacy_cost)
                        cost_price_per_share = cand['close'] + cost_inc
                        qty = int(alloc_amt // cost_price_per_share)
                        if qty > 0:
                            pending_orders.append({"ticker": cand['ticker'], "side": "BUY", "qty": qty, "reason": cand['reason']})
                            deduct_amt = qty * cost_price_per_share
                            available_cash -= deduct_amt
                            available_budget -= deduct_amt

        for tk, pos in positions.items():
            last_close = dfs[tk]['Close'].loc[:end_date].dropna().iloc[-1] if tk in dfs else pos['buy_price']
            profit_pct = (last_close / pos['buy_price']) - 1.0 if pos['buy_price'] > 0 else 0
            closed_trades_log.append({"종목명": ticker_to_name.get(tk, tk), "진입일": pos["entry_date"].strftime('%Y-%m-%d'), "청산일": "-", "보유일수": pos['days'], "진입단가": pos['buy_price'], "청산단가": last_close, "수량": pos['qty'], "손익금": (last_close - pos['buy_price']) * pos['qty'], "수익률": f"{profit_pct*100:+.2f}%", "사유": "보유 중 (현재가 평가)"})

        if len(nav_history) < 2: return {"status": "error", "msg": "데이터 부족"}
        
        nav_df = pd.DataFrame(nav_history)
        final_asset = nav_df['NAV'].iloc[-1]
        nav_df['Return'] = nav_df['NAV'].pct_change().fillna(0)
        rf_daily = 0.03 / 252 
        excess_returns = nav_df['Return'] - rf_daily
        sharpe_ratio = (excess_returns.mean() / excess_returns.std() * np.sqrt(252)) if excess_returns.std() > 0 else 0.0
        downside_returns = excess_returns[excess_returns < 0]
        downside_std = np.sqrt((downside_returns**2).mean()) if len(downside_returns) > 0 else 0.0
        sortino_ratio = (excess_returns.mean() / downside_std * np.sqrt(252)) if downside_std > 0 else 0.0
        cagr = (twr_index) ** (252 / len(nav_df)) - 1 if len(nav_df) > 0 else 0
        mdd = (nav_df['NAV'] / nav_df['NAV'].cummax() - 1).min()
        turnover_rate = (total_traded_value / 2.0) / nav_df['NAV'].mean() if nav_df['NAV'].mean() > 0 else 0.0
        win_rate = (sum(1 for p in trade_log if p > 0) / len(trade_log) * 100) if trade_log else 0.0
        
        # 🚨 교정 1: 무한대/오류값 방어 및 알아듣기 쉬운 우리말 전문용어로 교체
        disp_sharpe = f"{sharpe_ratio:.2f}" if -100 < sharpe_ratio < 100 else "측정 불가(변동성 낮음)"
        disp_sortino = f"{sortino_ratio:.2f}" if -100 < sortino_ratio < 100 else "측정 불가(변동성 낮음)"

        return {
            "status": "success", "final_asset": final_asset, "final_port_ret": (final_asset / init_cash - 1) * 100, 
            "metrics": {"TWR": (twr_index - 1) * 100},
            "trade_logs": closed_trades_log, "nav_history": nav_df,
            "summary_rows": [
                {"분석 지표": "누적 수익률 (복리)", "결과값": f"{(twr_index - 1) * 100:+.2f} %"},
                {"분석 지표": "자금 회전율 (매매 빈도)", "결과값": f"{turnover_rate * 100:.1f} %"},
                {"분석 지표": "세금/수수료/슬리피지 비용", "결과값": f"{total_cost_drag:,.0f} 원"},
                {"분석 지표": "매매 승률 (성공 횟수)", "결과값": f"{win_rate:.1f} % (총 {len(trade_log)}회 거래)"},
                {"분석 지표": "위험 대비 수익성 (샤프/소르티노)", "결과값": f"{disp_sharpe} / {disp_sortino}"}
            ]
        }
    except Exception as e: return {"status": "error", "msg": f"엔진 오류: {str(e)}"}
        
def run_yearly_realistic_backtest(strat: Strategy, init_cash: float, year: int, cfg: StrategyConfig, use_legacy_cost: bool=False):
    return {"status": "error", "msg": "DATA_UNAVAILABLE: 해당 과거 연도(Point-in-time)의 KOSPI/KOSDAQ 정확한 유니버스 및 상장폐지 데이터가 시스템에 존재하지 않아 생존자 편향 위험으로 시뮬레이션을 중단합니다."}
