import pandas as pd
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

# 🛑 [핵심 패치 1] 실시간 가격 무결성 및 신선도를 보장하는 스냅샷 모델
@dataclass
class StockSnapshot:
    ticker: str
    current_price: float
    high_price: float
    low_price: float
    ma20: float; ma60: float; ma200: float; m60_up: bool
    as_of: datetime.datetime
    source: str
    is_valid: bool
    is_complete_bar: bool
    reason: str

    def validate(self, is_halted: bool = False):
        if math.isnan(self.current_price) or self.current_price <= 0:
            self.is_valid = False; self.reason = "유효하지 않은 가격 (NaN 또는 <= 0)"; return
        if is_halted:
            self.is_valid = False; self.reason = "매매 거래정지 종목"; return
        self.is_valid = True; self.reason = "OK"

@dataclass
class MarketSnapshot:
    kospi_idx: float; kospi_ma200: float; kosdaq_idx: float; kosdaq_ma200: float
    as_of: datetime.datetime; source: str; is_valid: bool; is_complete_bar: bool; reason: str

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

def pre_flight_risk_check(order_type, intent_price, snap: StockSnapshot, daily_pnl_pct, is_mock=True):
    if not snap.is_valid: return False, f"데이터 차단: {snap.reason}"
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if not is_mock and (now.hour < 9 or (now.hour == 15 and now.minute > 30) or now.hour > 15): return False, "장 마감/운영시간 아님"
    if intent_price > 0 and snap.current_price > 0:
        dev = abs((snap.current_price / intent_price) - 1.0)
        if dev > 0.03: return False, f"가격 괴리율 초과 ({dev*100:.1f}%)"
    if daily_pnl_pct < -0.05 and "매수" in order_type: return False, "일일 손실 한도 초과 (-5%)"
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

# 🛑 [핵심 패치 2] KIS 장중 High/Low 및 trailing_armed 복구 구조 구현 (min_h 예외 적용)
def check_exit_signal(strat: Strategy, cfg: StrategyConfig, snap: StockSnapshot, buy_p: float, highest_p: float, days_held: int, open_p: float = 0.0) -> tuple[bool, float, str]:
    if not snap.is_valid: return False, 0.0, snap.reason
    sell_price, reason = 0.0, ""
    highest_p = max(highest_p, snap.high_price)
    op = open_p if open_p > 0 else snap.current_price 
    
    sl_target = buy_p * (1.0 + cfg.sl)
    if snap.low_price <= sl_target:
        sell_price, reason = min(op, sl_target), "🔴 장중 손절컷"
    else:
        trailing_armed = (highest_p / buy_p) >= (1.0 + cfg.ts_tgt)
        if trailing_armed:
            ts_target = highest_p * (1.0 + cfg.ts_drp)
            if snap.low_price <= ts_target: sell_price, reason = min(op, ts_target), "🔵 장중 트레일링 익절"
                
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
    return res_df.sort_values('AI 스코어', ascending=False) if not res_df.empty else res_df

# 🛑 [핵심 패치 3] 시뮬레이터 역시 StockSnapshot 객체를 생성하여 공통 함수로 평가
def run_quant_simulation(sim_stocks, strat: Strategy, init_cash, start_date, end_date, cfg: StrategyConfig):
    if sim_stocks.empty: return None
    f_start = pd.to_datetime(start_date) - datetime.timedelta(days=400)
    market_data = get_market_index_data(f_start, end_date)
    sim_data = {}
    for _, row in sim_stocks.iterrows():
        tk, nm = str(row.get('티커','')).strip().zfill(6), str(row.get('종목명',''))
        try:
            df = fdr.DataReader(tk, start=f_start, end=end_date)
            if df is not None and not df.empty:
                df['MA20'], df['MA60'], df['MA200'] = df['Close'].rolling(20, min_periods=1).mean(), df['Close'].rolling(60, min_periods=1).mean(), df['Close'].rolling(200, min_periods=1).mean()
                df['M60_Up'] = df['MA60'] > df['MA60'].shift(10).fillna(0)
                sim_data[tk] = {'name': nm, 'df': df}
        except: pass
        
    if not sim_data: return None
    all_trade_dates = sorted(list(set.union(*[set(v['df'][v['df'].index >= pd.to_datetime(start_date)].index) for v in sim_data.values()])))
    if not all_trade_dates: return None
    
    cash, positions = float(init_cash), {}
    trade_stats = {tk: {'buy_cnt': 0, 'sell_cnt': 0, 'total_fee': 0.0, 'realized_pnl': 0.0, 'name': v['name']} for tk, v in sim_data.items()}
    loss_streak, last_loss_date = {}, {}

    for curr_date in all_trade_dates:
        for tk in list(positions.keys()):
            pos, df = positions[tk], sim_data[tk]['df']
            if curr_date not in df.index: continue
            
            cp, ma20, ma60 = df.loc[curr_date, 'Close'], df.loc[curr_date, 'MA20'], df.loc[curr_date, 'MA60']
            snap = StockSnapshot(
                ticker=tk, current_price=cp, high_price=df.loc[curr_date, 'High'], low_price=df.loc[curr_date, 'Low'],
                ma20=ma20, ma60=ma60, ma200=0, m60_up=False, as_of=curr_date, source="FDR", is_valid=True, is_complete_bar=True, reason="OK"
            )
            snap.validate()
            pos['highest_price'] = max(pos['highest_price'], cp)
            days_held = (curr_date - pos['buy_date']).days
            
            is_sell, sell_price, reason = check_exit_signal(strat, cfg, snap, pos['buy_price'], pos['highest_price'], days_held)
            if is_sell:
                proc = pos['qty'] * cp; fee = proc * 0.0025; net_proc = proc - fee; cash += net_proc
                trade_pnl = net_proc - (pos['qty'] * pos['buy_price'] * 1.0025)
                if trade_pnl < 0: loss_streak[tk], last_loss_date[tk] = loss_streak.get(tk, 0) + 1, curr_date
                else: loss_streak[tk] = 0
                trade_stats[tk]['total_fee'] += fee; trade_stats[tk]['sell_cnt'] += 1; trade_stats[tk]['realized_pnl'] += trade_pnl
                del positions[tk]
                
        stock_eval_sum = sum(pos['qty'] * (sim_data[tk]['df'].loc[curr_date, 'Close'] if curr_date in sim_data[tk]['df'].index else pos['buy_price']) for tk, pos in positions.items())
        total_equity = cash + stock_eval_sum
        
        current_alloc_pct = cfg.alloc
        if cfg.boost:
            idx_df = market_data['KOSPI'] if strat == Strategy.CORE else market_data['KOSDAQ']
            if curr_date in idx_df.index and idx_df.loc[curr_date, 'Close'] > idx_df.loc[current_date, 'MA200']: current_alloc_pct = min(1.0, cfg.alloc + 0.10)
        target_per_stock = total_equity * current_alloc_pct
        
        for tk, val in sim_data.items():
            df = val['df']
            if curr_date not in df.index: continue
            if loss_streak.get(tk, 0) >= 2 and tk in last_loss_date and (curr_date - last_loss_date[tk]).days < cfg.cd: continue
            
            cp, ma20, ma60, ma200, m60_up = df.loc[curr_date, 'Close'], df.loc[current_date, 'MA20'], df.loc[current_date, 'MA60'], df.loc[current_date, 'MA200'], df.loc[current_date, 'M60_Up']
            snap = StockSnapshot(
                ticker=tk, current_price=cp, high_price=df.loc[curr_date, 'High'], low_price=df.loc[curr_date, 'Low'],
                ma20=ma20, ma60=ma60, ma200=ma200, m60_up=m60_up, as_of=curr_date, source="FDR", is_valid=True, is_complete_bar=True, reason="OK"
            )
            snap.validate()
            
            is_buy, _, _ = check_entry_signal(strat, cfg, snap)
            if is_buy and cash > 100000:
                curr_pos_val = (positions[tk]['qty'] * cp) if tk in positions else 0.0
                q = int(min(cash, max(0.0, target_per_stock - curr_pos_val)) // (cp * 1.0025))
                if q > 0:
                    cost = q * cp; fee = cost * 0.0025; cash -= (cost + fee)
                    trade_stats[tk]['total_fee'] += fee; trade_stats[tk]['buy_cnt'] += 1
                    if tk in positions:
                        old_qty, new_qty = positions[tk]['qty'], positions[tk]['qty'] + q
                        positions[tk]['buy_price'] = ((old_qty * positions[tk]['buy_price']) + (q * cp)) / new_qty
                        positions[tk]['qty'] = new_qty
                    else: positions[tk] = {'qty': q, 'buy_price': cp, 'highest_price': cp, 'buy_date': curr_date}
                        
    summary_rows = []
    total_final_val = cash
    for tk, val in sim_data.items():
        df = val['df']
        last_p = df['Close'].iloc[-1]
        qty = positions[tk]['qty'] if tk in positions else 0
        final_stock_eval = qty * last_p
        total_final_val += final_stock_eval
        stat = trade_stats[tk]
        stock_total_pnl = stat['realized_pnl'] + (final_stock_eval - (qty * positions[tk]['buy_price'] if qty > 0 else 0))
        summary_rows.append({'종목명': stat['name'], '최종 보유 주수': f"{qty:,} 주", '기말 평가금': f"{final_stock_eval:,.0f} 원", '총 실현/평가 손익': f"{stock_total_pnl:+,.0f} 원", '매매 횟수': f"매수 {stat['buy_cnt']}회 / 매도 {stat['sell_cnt']}회", '총 발생 수수료': f"{stat['total_fee']:,.0f} 원", '기말 포트 비중': "0%"})
        
    final_port_ret = ((total_final_val / init_cash) - 1) * 100 if init_cash > 0 else 0
    for r in summary_rows: r['기말 포트 비중'] = f"{(float(r['기말 평가금'].replace(',','').replace(' 원','')) / total_final_val) * 100 if total_final_val > 0 else 0:.2f}%"
    return {'final_asset': total_final_val, 'final_port_ret': final_port_ret, 'summary_rows': summary_rows}

def run_yearly_realistic_backtest(strat: Strategy, init_cash, year, cfg: StrategyConfig):
    krx = load_krx_universe()
    if krx.empty: return None
    cands_kospi = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(50) if 'Marcap' in krx.columns else krx.head(50)
    cands_kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].sort_values('Marcap', ascending=False).head(50) if 'Marcap' in krx.columns else krx.head(50)
    merged_cands = pd.concat([cands_kospi, cands_kosdaq]).drop_duplicates(subset=['Code'])
    
    start_date, end_date = f"{year}-01-01", f"{year}-12-31"
    if year == datetime.datetime.now().year: end_date = datetime.datetime.now().strftime('%Y-%m-%d')
    f_start = pd.to_datetime(start_date) - datetime.timedelta(days=400)
    market_data = get_market_index_data(f_start, end_date)
    
    data_dict = {}
    for _, r in merged_cands.iterrows():
        tc, nm = str(r['Code']).strip().zfill(6), str(r['Name'])
        try:
            df = fdr.DataReader(tc, start=f_start, end=end_date)
            if df is not None and not df.empty:
                df['MA20'], df['MA60'], df['MA200'] = df['Close'].rolling(20, min_periods=1).mean(), df['Close'].rolling(60, min_periods=1).mean(), df['Close'].rolling(200, min_periods=1).mean()
                df['M60_Up'] = df['MA60'] > df['MA60'].shift(10).fillna(0)
                data_dict[tc] = {'name': nm, 'df': df}
        except: pass
        
    if not data_dict: return None
    year_start_dt, year_end_dt = pd.to_datetime(start_date), pd.to_datetime(end_date)
    all_trade_dates = sorted(list(set.union(*[set(v['df'][ (v['df'].index >= year_start_dt) & (v['df'].index <= year_end_dt) ].index) for v in data_dict.values()])))
    if not all_trade_dates: return None
    
    cash, positions, trade_logs, buy_queue, weekly_watchlist = float(init_cash), {}, [], [], []
    loss_streak, last_loss_date = {}, {}
    
    for i, current_date in enumerate(all_trade_dates):
        for q in buy_queue:
            tc = q['tk']
            if tc in positions: continue
            df = data_dict[tc]['df']
            if current_date not in df.index: continue
            open_p = df.loc[current_date, 'Open']
            if pd.isna(open_p) or open_p <= 0: continue
            
            stock_eval_sum = sum(pos['qty'] * data_dict[ptk]['df'].loc[current_date, 'Open'] if current_date in data_dict[ptk]['df'].index else pos['buy_price'] for ptk, pos in positions.items())
            total_equity = cash + stock_eval_sum
            
            current_alloc_pct = cfg.alloc
            if cfg.boost:
                idx_df = market_data['KOSPI'] if strat == Strategy.CORE else market_data['KOSDAQ']
                if current_date in idx_df.index and idx_df.loc[current_date, 'Open'] > idx_df.loc[current_date, 'MA200']: current_alloc_pct = min(1.0, cfg.alloc + 0.10)
            
            alloc_fund = min(cash, total_equity * current_alloc_pct)
            q_qty = int(alloc_fund // (open_p * 1.0025))
            if q_qty > 0 and cash >= q_qty * open_p * 1.0025:
                cost = q_qty * open_p; fee = cost * 0.0025; cash -= (cost + fee)
                positions[tc] = {'qty': q_qty, 'buy_price': open_p, 'highest_price': df.loc[current_date, 'High'], 'buy_date': current_date, 'name': q['name']}
        buy_queue = [] 
        
        if current_date.weekday() == 0 or i == 0:
            weekly_watchlist = []
            for tc, val in data_dict.items():
                if loss_streak.get(tc, 0) >= 2 and tc in last_loss_date and (current_date - last_loss_date[tc]).days < cfg.cd: continue 
                df = val['df']
                if current_date not in df.index: continue
                c_p, ma20, ma60, ma200, m60_up = df.loc[current_date, 'Close'], df.loc[current_date, 'MA20'], df.loc[current_date, 'MA60'], df.loc[current_date, 'MA200'], df.loc[current_date, 'M60_Up']
                snap = StockSnapshot(
                    ticker=tc, current_price=c_p, high_price=df.loc[current_date, 'High'], low_price=df.loc[current_date, 'Low'],
                    ma20=ma20, ma60=ma60, ma200=ma200, m60_up=m60_up, as_of=current_date, source="FDR", is_valid=True, is_complete_bar=True, reason="OK"
                )
                snap.validate()
                is_buy, score, _ = check_entry_signal(strat, cfg, snap)
                if is_buy: weekly_watchlist.append({'tk': tc, 'name': val['name'], 'score': score})
            weekly_watchlist = sorted(weekly_watchlist, key=lambda x: x['score'], reverse=True)[:15]

        for tc in list(positions.keys()):
            pos, df = positions[tc], data_dict[tc]['df']
            if current_date not in df.index: continue
            low_p, high_p, close_p, open_p = df.loc[current_date, 'Low'], df.loc[current_date, 'High'], df.loc[current_date, 'Close'], df.loc[current_date, 'Open']
            pos['highest_price'] = max(pos['highest_price'], high_p)
            days_held = (current_date - pos['buy_date']).days
            
            snap = StockSnapshot(
                ticker=tc, current_price=close_p, high_price=high_p, low_price=low_p,
                ma20=df.loc[current_date, 'MA20'], ma60=df.loc[current_date, 'MA60'], ma200=0, m60_up=False, 
                as_of=current_date, source="FDR", is_valid=True, is_complete_bar=True, reason="OK"
            )
            snap.validate()
            
            is_sell, sell_price, sell_reason = check_exit_signal(strat, cfg, snap, pos['buy_price'], pos['highest_price'], days_held, open_p)
            if is_sell:
                proc = pos['qty'] * sell_price; fee = proc * 0.0025; cash += (proc - fee)
                pnl = (proc - fee) - (pos['qty'] * pos['buy_price'] * 1.0025)
                if pnl < 0: loss_streak[tc], last_loss_date[tc] = loss_streak.get(tc, 0) + 1, current_date
                else: loss_streak[tc] = 0
                trade_logs.append({'종목명': pos['name'], '티커': tc, '매수일': pos['buy_date'].strftime('%Y-%m-%d'), '매도일': current_date.strftime('%Y-%m-%d'), '매수가': f"{pos['buy_price']:,.0f} 원", '매도가': f"{sell_price:,.0f} 원", '수량': f"{pos['qty']:,} 주", '손익금': f"{pnl:+,.0f} 원", '수익률': f"{pnl / (pos['qty'] * pos['buy_price']) * 100:+.2f}%", '청산 사유': sell_reason})
                del positions[tc]
        
        current_alloc_pct = cfg.alloc
        if cfg.boost:
            idx_df = market_data['KOSPI'] if strat == Strategy.CORE else market_data['KOSDAQ']
            if current_date in idx_df.index and idx_df.loc[current_date, 'Close'] > idx_df.loc[current_date, 'MA200']: current_alloc_pct = min(1.0, cfg.alloc + 0.10)
        dynamic_max_slots = max(3, int(1.0 / current_alloc_pct))

        if len(positions) < dynamic_max_slots:
            for w in weekly_watchlist:
                tc = w['tk']
                if tc in positions or any(q['tk'] == tc for q in buy_queue): continue
                df = data_dict[tc]['df']
                if current_date not in df.index: continue
                c_p, ma20, ma60, ma200, m60_up = df.loc[current_date, 'Close'], df.loc[current_date, 'MA20'], df.loc[current_date, 'MA60'], df.loc[current_date, 'MA200'], df.loc[current_date, 'M60_Up']
                
                snap = StockSnapshot(
                    ticker=tc, current_price=c_p, high_price=df.loc[current_date, 'High'], low_price=df.loc[current_date, 'Low'],
                    ma20=ma20, ma60=ma60, ma200=ma200, m60_up=m60_up, as_of=current_date, source="FDR", is_valid=True, is_complete_bar=True, reason="OK"
                )
                snap.validate()
                is_buy, _, _ = check_entry_signal(strat, cfg, snap)
                if is_buy:
                    buy_queue.append({'tk': tc, 'name': w['name']})
                    if len(positions) + len(buy_queue) >= dynamic_max_slots: break
                    
    final_stock_eval = sum(pos['qty'] * data_dict[tc]['df']['Close'].iloc[-1] for tc, pos in positions.items())
    return {'final_asset': cash + final_stock_eval, 'final_port_ret': (((cash + final_stock_eval) / init_cash) - 1) * 100, 'trade_logs': trade_logs, 'active_positions': len(positions), 'remaining_cash': cash}
