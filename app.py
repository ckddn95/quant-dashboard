import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import altair as alt
import json
import os
import glob
import datetime
import re
import requests
import warnings
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="🚀",
    layout="wide"
)

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **가상 매매 성과 추적기(Forward Test)**, **엔진 100% 동기화**, **가상/실계좌 탭 분리**를 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 0. 로컬 저장소 디렉토리 세팅
# ==========================================
SAVE_DIR = "./saved_portfolios"
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# ==========================================
# 한국투자증권 Open API 연동 로직
# ==========================================
def get_kis_access_token(app_key, app_secret, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret
    }
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200:
            return res.json().get("access_token")
    except Exception as e:
        st.error(f"토큰 발급 통신 오류: {e}")
    return None

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-balance"
    tr_id = "VTTC8434R" if is_mock else "TTTC8434R"
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id
    }
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N",
        "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", "FCDC_GOOD_YN": "N", "SUM_TOT_DVSN_KEY": "01",
        "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
    }
    
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('rt_cd') == '0':
                return data.get('output1', []), data.get('output2', [])
            else:
                st.error(f"API 응답 오류: {data.get('msg1')}")
    except Exception as e:
        st.error(f"잔고 조회 통신 오류: {e}")
    return None, None

# ==========================================
# 구버전 파일 자동 청소 및 마이그레이션
# ==========================================
raw_files = glob.glob(f"{SAVE_DIR}/*.json")
for f_path in raw_files:
    try:
        with open(f_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not data:
            os.remove(f_path)
            continue
        if 'strategy' not in data:
            for p_name, p_data in data.items():
                safe_name = re.sub(r'[\\/*?:"<>|]', "", p_name)
                new_path = os.path.join(SAVE_DIR, f"{safe_name}.json")
                with open(new_path, 'w', encoding='utf-8') as out_f:
                    json.dump(p_data, out_f, ensure_ascii=False, indent=2)
            os.remove(f_path)
    except Exception:
        try: os.remove(f_path)
        except: pass

# ==========================================
# 1. 데이터 수집 함수 모음
# ==========================================
@st.cache_data(ttl=86400)
def load_krx_universe():
    try:
        df = fdr.StockListing('KRX')
        df = df.dropna(subset=['Code', 'Name'])
        return df
    except Exception:
        return pd.DataFrame(columns=['Code', 'Name', 'Market', 'Marcap', 'Amount'])

@st.cache_data(ttl=1800)
def fetch_market_data():
    try:
        vix_df = yf.download("^VIX", period="3mo", progress=False)
        if isinstance(vix_df.columns, pd.MultiIndex): vix_df.columns = vix_df.columns.get_level_values(0)
        vix_close = vix_df['Close'].dropna()
        vix_val = float(vix_close.iloc[-1])
        vix_ma3 = float(vix_close.rolling(3).mean().iloc[-1])
        vix_contrarian = (vix_val >= 25.0) and (vix_val < vix_ma3)
        vix_safe = (vix_val < 30.0)
        
        kospi_df = fdr.DataReader('KS11')
        k_close = kospi_df['Close'].tail(61)
        kospi_ret_60 = ((float(k_close.iloc[-1]) / float(k_close.iloc[-60])) - 1) * 100 if len(k_close) >= 60 else 0.0
        
        kosdaq_df = fdr.DataReader('KQ11')
        kq_close = kosdaq_df['Close'].tail(61)
        kosdaq_ret_60 = ((float(kq_close.iloc[-1]) / float(kq_close.iloc[-60])) - 1) * 100 if len(kq_close) >= 60 else 0.0
        
        return vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60
    except:
        return 20.0, False, True, 0.0, 0.0

@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code):
    try:
        for suffix in ['.KS', '.KQ']:
            df = yf.download(f"{ticker_code}{suffix}", period="2y", progress=False)
            if not df.empty and len(df) > 0:
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                close_prices = df['Close'].dropna()
                volumes = df['Volume'].dropna()
                low_prices = df['Low'].dropna() if 'Low' in df.columns else close_prices
                if len(close_prices) == 0: continue
                
                current_price = float(close_prices.iloc[-1])
                current_low = float(low_prices.iloc[-1])
                ma200 = float(close_prices.rolling(window=200).mean().iloc[-1]) if len(close_prices) >= 200 else current_price
                ma60 = float(close_prices.rolling(window=60).mean().iloc[-1]) if len(close_prices) >= 60 else current_price
                ma60_10d_ago = float(close_prices.rolling(window=60).mean().iloc[-11]) if len(close_prices) >= 70 else ma60
                ma20 = float(close_prices.rolling(window=20).mean().iloc[-1]) if len(close_prices) >= 20 else current_price
                recent_high = float(close_prices.tail(120).max())
                drawdown = ((current_price / recent_high) - 1) * 100 if recent_high > 0 else 0.0
                
                vol_5ma = float(volumes.tail(6).iloc[:-1].mean()) if len(volumes) >= 6 else float(volumes.iloc[-1])
                curr_vol = float(volumes.iloc[-1])
                vol_ratio = (curr_vol / vol_5ma * 100) if vol_5ma > 0 else 100.0
                
                ret_60 = ((current_price / float(close_prices.iloc[-60])) - 1) * 100 if len(close_prices) >= 60 else 0.0
                ret_20 = ((current_price / float(close_prices.iloc[-20])) - 1) * 100 if len(close_prices) >= 20 else 0.0
                ma60_slope_positive = (ma60 > ma60_10d_ago) 
                is_above_ma200 = (current_price >= ma200) 
                
                vol_5ma_series = volumes.rolling(5).mean().shift(1)
                vol_ratio_arr = np.where(vol_5ma_series > 0, volumes / vol_5ma_series * 100, 100.0)
                vol_ratio_series = pd.Series(vol_ratio_arr, index=volumes.index)
                recent_20d_vol_max = float(vol_ratio_series.tail(20).max())
                vol_surged = recent_20d_vol_max >= 200.0

                return (current_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, 
                        ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_20d_vol_max)
    except Exception: pass
    return None, None, None, None, None, None, None, None, False, False, False, None, 0.0

@st.cache_data(ttl=3600)
def run_core_scanner(use_ma200_filter_flag, buf_pct):
    results = []
    krx = load_krx_universe()
    buf = buf_pct / 100.0
    try:
        kospi = krx[krx['Market'] == 'KOSPI']
        if 'Marcap' in kospi.columns:
            candidates = kospi.sort_values('Marcap', ascending=False).head(100)
        else:
            candidates = kospi.head(100)
    except: return []

    for idx, row in candidates.iterrows():
        code = row['Code']
        name = row['Name']
        res = fetch_stock_status(code)
        if res[0] is None: continue
        c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_vol_max = res
        
        ma200_pass = (not use_ma200_filter_flag) or is_above_ma200
        trend_starting = (ma20 >= ma60 * (1 + buf)) and ma60_slope_positive and (ret_20 > 0)
        
        if ma200_pass and trend_starting:
            diff_ma = ((ma20 / ma60) - 1) * 100
            results.append({
                '종목명': name, '티커': code, '현재가': f"{c_price:,.0f} 원",
                '20/60선 이격': f"{diff_ma:+.2f}%", '20일 모멘텀': f"{ret_20:+.2f}%",
                '진단 근거': "장기 추세선 방어 및 골든크로스 안착"
            })
    return pd.DataFrame(results)

@st.cache_data(ttl=3600)
def run_satellite_scanner(use_ma200_filter_flag):
    results = []
    krx = load_krx_universe()
    try:
        kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)]
        if 'Marcap' in kosdaq.columns:
            kosdaq = kosdaq[kosdaq['Marcap'] >= 100000000000]
            candidates = kosdaq.sort_values('Marcap', ascending=False).head(150)
        else:
            candidates = kosdaq.head(150)
    except: return []

    for idx, row in candidates.iterrows():
        code = row['Code']
        name = row['Name']
        res = fetch_stock_status(code)
        if res[0] is None: continue
        c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_vol_max = res
        
        dist_ma20 = ((c_price / ma20) - 1) * 100
        low_ma20_touch = (current_low <= ma20 * 1.01) and (c_price >= ma20 * 0.95)
        is_dip = ((dist_ma20 >= -5.0) & (dist_ma20 <= 3.0)) | low_ma20_touch 
        
        ma200_pass = (not use_ma200_filter_flag) or is_above_ma200
        dd_pass = drawdown >= -30.0 
        
        if vol_surged and is_dip and ma200_pass and dd_pass:
            results.append({
                '종목명': name, '티커': code, '현재가': f"{c_price:,.0f} 원",
                '20일선 이격도': f"{dist_ma20:+.2f}%", '최근 최대 수급': f"{recent_vol_max:,.0f}%",
                '진단 근거': "수급 폭발 후 20일선 눌림목"
            })
    return pd.DataFrame(results)

# [핵심 통합] 독립된 시뮬레이션 계산 엔진 (가상 운용/백테스트 공통 사용)
@st.cache_data(ttl=1800)
def run_quant_simulation(sim_stocks, strat, init_cash, start_date, end_date,
                         use_ma200_filter, whipsaw_buffer, sat_stop_loss,
                         max_alloc_pct, min_hold_days, ts_target_pct,
                         ts_drop_pct, bull_market_boost, cooldown_days):
    
    index_sym = 'KS11' if strat == '대형주 (Core)' else 'KQ11'
    fetch_start = start_date - datetime.timedelta(days=300)
    
    market_df = pd.DataFrame()
    benchmark_ret_val = 0.0
    final_benchmark_asset = init_cash
    
    try:
        bm_df = fdr.DataReader(index_sym, fetch_start, end_date)
        if not bm_df.empty:
            if bm_df.index.tz is not None:
                bm_df.index = bm_df.index.tz_localize(None)
            bm_df['Bm_Ret_60'] = bm_df['Close'] / bm_df['Close'].shift(60) - 1
            bm_df['Bm_MA60'] = bm_df['Close'].rolling(60).mean()
            market_df['Bm_Ret_60'] = bm_df['Bm_Ret_60']
            market_df['Bm_Bull'] = bm_df['Close'] > bm_df['Bm_MA60']
            
            start_dt = pd.to_datetime(start_date)
            sim_bm = bm_df[bm_df.index >= start_dt]['Close'].dropna()
            if len(sim_bm) > 1:
                k_start = float(sim_bm.iloc[0])
                k_end = float(sim_bm.iloc[-1])
                benchmark_ret_val = ((k_end / k_start) - 1) * 100
                final_benchmark_asset = init_cash * (1 + benchmark_ret_val / 100)
    except: pass

    try:
        vix_df = yf.download("^VIX", start=fetch_start, end=end_date, progress=False)
        if not vix_df.empty:
            if isinstance(vix_df.columns, pd.MultiIndex): vix_df.columns = vix_df.columns.get_level_values(0)
            vix_df['VIX_MA3'] = vix_df['Close'].rolling(3).mean()
            market_df['VIX_Contrarian'] = (vix_df['Close'] >= 25.0) & (vix_df['Close'] < vix_df['VIX_MA3'])
            market_df['VIX_Safe'] = vix_df['Close'] < 30.0
    except: pass
    
    market_df = market_df.ffill().fillna(0)
    
    stock_dfs = {}
    buf = whipsaw_buffer / 100.0
    start_dt = pd.to_datetime(start_date)
    
    for idx, row in sim_stocks.iterrows():
        ticker = row['티커']
        name = row['종목명']
        
        df = None
        for suf in ['.KS', '.KQ']:
            temp_df = yf.download(f"{ticker}{suf}", start=fetch_start, end=end_date, progress=False)
            if not temp_df.empty:
                if isinstance(temp_df.columns, pd.MultiIndex): temp_df.columns = temp_df.columns.get_level_values(0)
                df = temp_df
                break
        
        if df is None or df.empty: continue
            
        df['Close'] = df['Close'].ffill()
        df['Volume'] = df['Volume'].ffill()
        df['Daily_Ret'] = df['Close'].pct_change()
        
        df['MA200'] = df['Close'].rolling(200).mean()
        df['Is_Above_MA200'] = df['Close'] >= df['MA200']
        
        df['MA60'] = df['Close'].rolling(60).mean()
        df['MA60_Slope'] = df['MA60'] > df['MA60'].shift(10)
        df['MA20'] = df['Close'].rolling(20).mean()
        df['Ret_60'] = df['Close'] / df['Close'].shift(60) - 1
        df['Ret_20'] = df['Close'] / df['Close'].shift(20) - 1
        
        df['Vol_5MA'] = df['Volume'].rolling(5).mean().shift(1)
        df['Vol_Ratio'] = np.where(df['Vol_5MA'] > 0, df['Volume'] / df['Vol_5MA'] * 100, 100.0)
        df['Vol_Strong'] = df['Vol_Ratio'] >= 150.0
        
        df['Recent_Vol_Max'] = df['Vol_Ratio'].rolling(window=20, min_periods=1).max()
        df['Vol_Surged'] = df['Recent_Vol_Max'] >= 200.0
        
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
            
        df = df.join(market_df, how='left')
        df['VIX_Safe'] = df['VIX_Safe'].fillna(True)
        df['VIX_Contrarian'] = df['VIX_Contrarian'].fillna(False)
        df['Bm_Ret_60'] = df['Bm_Ret_60'].fillna(0.0)
        df['Bm_Bull'] = df['Bm_Bull'].fillna(False)
        
        ma200_cond = df['Is_Above_MA200'] if use_ma200_filter else True
        
        if strat == '대형주 (Core)':
            entry_cond = (ma200_cond & (df['MA20'] >= df['MA60'] * (1 + buf)) & df['MA60_Slope'] & (df['Ret_20'] > 0) & df['VIX_Safe']) | df['VIX_Contrarian']
            exit_cond = (df['MA20'] < df['MA60'] * (1 - buf/2)) & (~df['VIX_Contrarian'])
        else:
            df['Roll_Max'] = df['Close'].rolling(window=120, min_periods=1).max()
            df['Drawdown'] = (df['Close'] / df['Roll_Max']) - 1
            
            dist_ma20 = ((df['Close'] / df['MA20']) - 1) * 100
            low_ma20_touch = df['Low'] <= df['MA20'] * 1.01 if 'Low' in df.columns else (dist_ma20 <= 0.0)
            is_dip = ((dist_ma20 >= -5.0) & (dist_ma20 <= 3.0)) | low_ma20_touch 
            
            entry_cond = (ma200_cond & ((is_dip & df['Vol_Surged']) | df['VIX_Contrarian'])) & (df['Drawdown'] >= -0.30)
            exit_cond = ((df['MA20'] < df['MA60'] * (1 - buf/2)) & (~df['VIX_Contrarian']))
        
        df['Signal'] = np.where(entry_cond, 1, np.where(exit_cond, 0, np.nan))
        df['Signal'] = df['Signal'].ffill().fillna(0)
        
        rs_condition = df['Ret_60'] > df['Bm_Ret_60']
        df['Score'] = np.where(entry_cond, 
                                1.0 + np.where(df['Vol_Strong'], 1.0 if strat != '대형주 (Core)' else 0.5, 0.0) + 
                                np.where(rs_condition, 0.5, 0.0) + 
                                np.where(df['VIX_Contrarian'], 1.0, 0.0), 
                                0.0)
        
        stock_dfs[name] = df[df.index >= start_dt].copy()
        
    stock_dfs = {k: v for k, v in stock_dfs.items() if not v.empty}
        
    if not stock_dfs:
        return None
        
    all_indices = [df.index for df in stock_dfs.values()]
    common_index = all_indices[0]
    for idx_df in all_indices[1:]:
        if len(idx_df) > len(common_index):
            common_index = idx_df 
        
    portfolio_history = []
    history_records = [] 
    
    trade_stats = {name: {'buy': 0, 'sell': 0, 'fee': 0.0, 'realized_pnl': 0.0} for name in stock_dfs}
    
    dates = common_index
    shares = {name: 0.0 for name in stock_dfs}
    hold_days = {name: 0 for name in stock_dfs}
    max_invested = {name: 0.0 for name in stock_dfs}
    peak_price_since_buy = {name: 0.0 for name in stock_dfs}
    
    consecutive_losses = {name: 0 for name in stock_dfs}
    cooldown_until = {name: pd.Timestamp.min for name in stock_dfs}
    
    cash = init_cash
    avg_buy_price = {name: 0.0 for name in stock_dfs}
    realized_pnl = {name: 0.0 for name in stock_dfs}
    
    base_alloc_ratio = max_alloc_pct / 100.0
    ts_target = ts_target_pct / 100.0
    ts_drop = ts_drop_pct / 100.0
    
    for i, date_val in enumerate(dates):
        if i == 0:
            portfolio_history.append(init_cash)
            record = {'Date': date_val, '현금(Cash)': init_cash}
            for name in stock_dfs: record[name] = 0.0
            history_records.append(record)
            continue
            
        current_max_alloc_ratio = base_alloc_ratio
        market_bull = False
        for df_check in stock_dfs.values():
            if date_val in df_check.index:
                market_bull = df_check.loc[date_val, 'Bm_Bull']
                break
        
        if bull_market_boost and market_bull:
            current_max_alloc_ratio = min(base_alloc_ratio * 1.5, 1.0)
        
        for name in stock_dfs:
            if shares[name] > 0: hold_days[name] += 1
            else: hold_days[name] = 0
        
        active_stocks = []
        scores = {}
        for name, df in stock_dfs.items():
            if date_val not in df.index: continue
            sig = df.loc[date_val, 'Signal']
            c_price = df.loc[date_val, 'Close']
            
            if shares[name] == 0 and date_val < cooldown_until[name]:
                sig = 0.0
            
            trailing_stop_exit = False
            if shares[name] > 0 and avg_buy_price[name] > 0:
                peak_price_since_buy[name] = max(peak_price_since_buy[name], c_price)
                curr_ret = (c_price / avg_buy_price[name]) - 1
                drop_from_peak = (c_price / peak_price_since_buy[name]) - 1
                
                if curr_ret >= ts_target and drop_from_peak <= ts_drop:
                    trailing_stop_exit = True
            
            force_exit = False
            if strat != '대형주 (Core)' and shares[name] > 0 and avg_buy_price[name] > 0:
                user_ret = (c_price / avg_buy_price[name]) - 1
                if user_ret <= (sat_stop_loss / 100.0):
                    force_exit = True
                
            if trailing_stop_exit or force_exit:
                sig = 0.0
            elif shares[name] > 0 and hold_days[name] < min_hold_days:
                sig = 1.0
                
            if sig == 1:
                active_stocks.append(name)
                scores[name] = df.loc[date_val, 'Score'] if df.loc[date_val, 'Score'] > 0 else 1.0
            else:
                peak_price_since_buy[name] = 0.0
                
        for name, df in stock_dfs.items():
            curr_sig = 1 if name in active_stocks else 0
            prev_sig = 1 if shares[name] > 0 else 0
            if curr_sig == 1 and prev_sig == 0:
                trade_stats[name]['buy'] += 1
            elif curr_sig == 0 and prev_sig == 1:
                trade_stats[name]['sell'] += 1
                
        stock_eval_total = sum(shares[name] * stock_dfs[name].loc[date_val, 'Close'] for name in stock_dfs if date_val in stock_dfs[name].index)
        total_asset = cash + stock_eval_total
        
        n_active = len(active_stocks)
        if n_active > 0:
            total_score = sum(scores.values()) if sum(scores.values()) > 0 else n_active
            for name in stock_dfs:
                if date_val not in stock_dfs[name].index: continue
                c_price = stock_dfs[name].loc[date_val, 'Close']
                current_val = shares[name] * c_price
                
                if name in active_stocks:
                    weight = scores.get(name, 1.0) / total_score
                    target_alloc = min(total_asset * weight, total_asset * current_max_alloc_ratio)
                    diff_val = target_alloc - current_val
                    
                    if diff_val > 0: 
                        cost = diff_val
                        fee = cost * 0.0025
                        if cash >= (cost + fee):
                            cash -= (cost + fee)
                            added_shares = cost / c_price
                            if shares[name] > 0:
                                avg_buy_price[name] = ((shares[name] * avg_buy_price[name]) + cost) / (shares[name] + added_shares)
                            else:
                                avg_buy_price[name] = c_price
                                peak_price_since_buy[name] = c_price
                            shares[name] += added_shares
                            trade_stats[name]['fee'] += fee
                            realized_pnl[name] -= fee 
                            max_invested[name] = max(max_invested[name], shares[name] * c_price)
                        else:
                            cost = max(cash - (cash * 0.0025), 0)
                            if cost > 0:
                                fee = cost * 0.0025
                                cash -= (cost + fee)
                                added_shares = cost / c_price
                                if shares[name] > 0:
                                    avg_buy_price[name] = ((shares[name] * avg_buy_price[name]) + cost) / (shares[name] + added_shares)
                                else:
                                    avg_buy_price[name] = c_price
                                    peak_price_since_buy[name] = c_price
                                shares[name] += added_shares
                                trade_stats[name]['fee'] += fee
                                realized_pnl[name] -= fee
                                max_invested[name] = max(max_invested[name], shares[name] * c_price)
                    elif diff_val < 0: 
                        proceeds = abs(diff_val)
                        fee = proceeds * 0.0025
                        sold_shares = proceeds / c_price
                        pnl = sold_shares * (c_price - avg_buy_price[name]) - fee
                        realized_pnl[name] += pnl
                        cash += (proceeds - fee)
                        shares[name] -= sold_shares
                        trade_stats[name]['fee'] += fee
                else:
                    if shares[name] > 0: 
                        proceeds = shares[name] * c_price
                        fee = proceeds * 0.0025
                        pnl = shares[name] * (c_price - avg_buy_price[name]) - fee
                        
                        if pnl < 0:
                            consecutive_losses[name] += 1
                            if consecutive_losses[name] >= 2 and cooldown_days > 0:
                                cooldown_until[name] = date_val + pd.Timedelta(days=cooldown_days)
                        else:
                            consecutive_losses[name] = 0
                            
                        realized_pnl[name] += pnl
                        cash += (proceeds - fee)
                        trade_stats[name]['fee'] += fee
                        shares[name] = 0.0
                        avg_buy_price[name] = 0.0
                        peak_price_since_buy[name] = 0.0
        else:
            for name in stock_dfs:
                if shares[name] > 0 and date_val in stock_dfs[name].index:
                    c_price = stock_dfs[name].loc[date_val, 'Close']
                    proceeds = shares[name] * c_price
                    fee = proceeds * 0.0025
                    pnl = shares[name] * (c_price - avg_buy_price[name]) - fee
                    
                    if pnl < 0:
                        consecutive_losses[name] += 1
                        if consecutive_losses[name] >= 2 and cooldown_days > 0:
                            cooldown_until[name] = date_val + pd.Timedelta(days=cooldown_days)
                    else:
                        consecutive_losses[name] = 0
                        
                    realized_pnl[name] += pnl
                    cash += (proceeds - fee)
                    trade_stats[name]['fee'] += fee
                    shares[name] = 0.0
                    avg_buy_price[name] = 0.0
                    peak_price_since_buy[name] = 0.0
                    
        final_eval = sum(shares[name] * stock_dfs[name].loc[date_val, 'Close'] for name in stock_dfs if date_val in stock_dfs[name].index)
        portfolio_history.append(max(cash + final_eval, 0))
        
        record = {'Date': date_val, '현금(Cash)': max(cash, 0)}
        for name in stock_dfs:
            record[name] = shares[name] * stock_dfs[name].loc[date_val, 'Close'] if date_val in stock_dfs[name].index else 0.0
        history_records.append(record)
        
    ai_portfolio_series = pd.Series(portfolio_history, index=common_index)
    
    bh_values = {}
    dca_values = {}
    cash_per_stock_init = init_cash / len(stock_dfs)
    
    for name, df in stock_dfs.items():
        sim_df = df.copy()
        bh_values[name] = (sim_df['Close'] / sim_df['Close'].iloc[0]) * cash_per_stock_init
        
        n_months = len(sim_df.groupby(sim_df.index.to_period('M')))
        initial_seed = (init_cash * 0.2) / len(stock_dfs)
        shares_acc = initial_seed / sim_df['Close'].iloc[0]
        dca_list = []
        for date_val, row_val in sim_df.iterrows():
            if date_val != sim_df.index[0] and date_val.day <= 3 and date_val.month != sim_df.index[sim_df.index.get_loc(date_val)-1].month:
                if n_months > 0:
                    add_amt = (init_cash * 0.8 / n_months) / len(stock_dfs)
                    shares_acc += add_amt / row_val['Close']
                dca_list.append(shares_acc * row_val['Close'])
            else:
                dca_list.append(shares_acc * row_val['Close'])
        dca_values[name] = pd.Series(dca_list, index=sim_df.index)
        
    bh_df = pd.DataFrame(bh_values).sum(axis=1)
    dca_df = pd.DataFrame(dca_values).sum(axis=1)
    
    final_asset = ai_portfolio_series.iloc[-1]
    final_port_ret = ((final_asset / init_cash) - 1) * 100
    final_bh_asset = bh_df.iloc[-1]
    final_bh_ret = ((final_bh_asset / init_cash) - 1) * 100
    final_dca_asset = dca_df.iloc[-1]
    final_dca_ret = ((final_dca_asset / init_cash) - 1) * 100
    
    summary_rows = []
    sum_holding_val = 0
    sum_total_profit = 0
    sum_fee = 0
    sum_b_cnt = 0
    sum_s_cnt = 0
    
    for name in stock_dfs:
        last_dt = stock_dfs[name].index[-1]
        final_c_price = stock_dfs[name].loc[last_dt, 'Close']
        holding_val = shares[name] * final_c_price
        
        unrealized_pnl = shares[name] * (final_c_price - avg_buy_price[name]) if shares[name] > 0 else 0.0
        total_profit = realized_pnl[name] + unrealized_pnl
        
        invested_base = max_invested[name] if max_invested[name] > 0 else (init_cash / len(stock_dfs))
        ret = (total_profit / invested_base) * 100
        ret = max(ret, -100.0) 
        weight = (holding_val / final_asset) * 100 if final_asset > 0 else 0.0
        
        b_cnt = trade_stats[name]['buy']
        s_cnt = trade_stats[name]['sell']
        fee = trade_stats[name]['fee']
        
        sum_holding_val += holding_val
        sum_total_profit += total_profit
        sum_fee += fee
        sum_b_cnt += b_cnt
        sum_s_cnt += s_cnt
        
        summary_rows.append({
            '종목명': name,
            '최종 보유 주수': f"{shares[name]:.2f} 주",
            '기말 평가금': f"{holding_val:,.0f} 원",
            '총 순수익 (원)': f"{total_profit:+,.0f} 원",
            '수익률 (%)': f"{ret:+.2f}%",
            '매매 횟수': f"매수 {b_cnt}회 / 매도 {s_cnt}회",
            '총 발생 수수료': f"{fee:,.0f} 원",
            '기말 포트폴리오 비중': f"{weight:.2f}%"
        })
        
    profit_pct_of_init = (sum_total_profit / init_cash) * 100 if init_cash > 0 else 0.0
    fee_pct_of_init = (sum_fee / init_cash) * 100 if init_cash > 0 else 0.0
    total_weight = (sum_holding_val / final_asset) * 100 if final_asset > 0 else 0.0

    summary_rows.append({
        '종목명': '💡 [전체 합계]',
        '최종 보유 주수': '-',
        '기말 평가금': f"{sum_holding_val:,.0f} 원",
        '총 순수익 (원)': f"{sum_total_profit:+,.0f} 원 (자산대비 {profit_pct_of_init:+.2f}%)",
        '수익률 (%)': '-',
        '매매 횟수': f"매수 {sum_b_cnt}회 / 매도 {sum_s_cnt}회",
        '총 발생 수수료': f"{sum_fee:,.0f} 원 (자산대비 {fee_pct_of_init:.2f}%)",
        '기말 포트폴리오 비중': f"{total_weight:.2f}%"
    })
    
    history_df = pd.DataFrame(history_records).set_index('Date')
    try:
        eom_val_df = history_df.resample('ME').last()
    except ValueError:
        eom_val_df = history_df.resample('M').last()
        
    eom_weights = eom_val_df.div(eom_val_df.sum(axis=1), axis=0) * 100
    eom_weights = eom_weights.fillna(0)
    eom_weights.index = eom_weights.index.strftime('%Y-%m')
    
    stock_cols = sorted([c for c in eom_weights.columns if c != '현금(Cash)'])
    cols_ordered = stock_cols + ['현금(Cash)'] 
    eom_weights = eom_weights[cols_ordered]
    
    eom_weights_reset = eom_weights.reset_index().melt('Date', var_name='Asset', value_name='Weight')
    order_map = {name: i for i, name in enumerate(cols_ordered)}
    eom_weights_reset['Order'] = eom_weights_reset['Asset'].map(order_map)
    
    return {
        'final_asset': final_asset,
        'final_port_ret': final_port_ret,
        'summary_rows': summary_rows,
        'eom_weights_reset': eom_weights_reset,
        'benchmark_ret_val': benchmark_ret_val,
        'final_benchmark_asset': final_benchmark_asset,
        'final_bh_asset': final_bh_asset,
        'final_bh_ret': final_bh_ret,
        'final_dca_asset': final_dca_asset,
        'final_dca_ret': final_dca_ret,
        'cols_ordered': cols_ordered,
        'color_range': ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'] * (len(stock_cols)//10 + 1)
    }

# ==========================================
# 2. 세션 트래킹 초기화
# ==========================================
if 'auto_diagnose' not in st.session_state:
    st.session_state.auto_diagnose = False
if 'show_scanner' not in st.session_state:
    st.session_state.show_scanner = False


# ==========================================
# 3. 사이드바: 단일 작업공간(포트폴리오 스위칭)
# ==========================================
st.sidebar.header("🎯 현재 작업할 포트폴리오 선택")

valid_files = glob.glob(f"{SAVE_DIR}/*.json")
port_names = [os.path.basename(f).replace('.json', '') for f in valid_files]

selected_port = None
p_data = None
file_path = None

if port_names:
    selected_port = st.sidebar.selectbox("가상 포트폴리오(파일) 목록", port_names)
    file_path = os.path.join(SAVE_DIR, f"{selected_port}.json")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            p_data = json.load(f)
            if 'strategy' not in p_data: p_data['strategy'] = '대형주 (Core)'
            if 'cash' not in p_data: p_data['cash'] = 10000000
            if 'stocks' not in p_data: p_data['stocks'] = []
            if 'created_at' not in p_data: p_data['created_at'] = (datetime.date.today() - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    except Exception:
        st.sidebar.error("파일을 읽는 데 실패했습니다.")
        p_data = None

    active_strat = p_data['strategy'] if p_data else "대형주 (Core)"

    if p_data:
        st.sidebar.markdown("---")
        st.sidebar.subheader("💰 Virtual Capital & Settings")
        
        st.sidebar.markdown(f"**현재 설정 전략:** `{active_strat}`")
        
        new_cash = st.sidebar.number_input(
            f"총 투자 운용 자산 (증/감액)", 
            value=int(p_data['cash']), 
            step=1_000_000, 
            format="%d"
        )
        
        if new_cash != int(p_data['cash']):
            p_data['cash'] = new_cash
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(p_data, f, ensure_ascii=False, indent=2)
                
        st.sidebar.caption(f"💵 가상 설정 금액: **{new_cash:,.0f} 원**")
        
        with st.sidebar.popover(f"🗑️ '{selected_port}' 삭제", use_container_width=True):
            st.markdown("⚠️ **경고: 정말 삭제하시겠습니까?**<br>하드디스크에서 영구적으로 파일이 삭제되며 복구할 수 없습니다.", unsafe_allow_html=True)
            if st.button("🚨 네, 영구 삭제합니다", key=f"del_{selected_port}", type="primary", use_container_width=True):
                try:
                    os.remove(file_path)
                    st.sidebar.success(f"✅ 완전히 삭제되었습니다.")
                    st.rerun()
                except Exception as e:
                    st.sidebar.error(f"삭제 실패: {e}")
else:
    st.sidebar.info("👈 생성된 포트폴리오가 없습니다. 아래에서 새로 추가해 주세요.")
    active_strat = "대형주 (Core)"

st.sidebar.markdown("---")
st.sidebar.subheader("➕ 새 가상 포트폴리오 추가")
new_p_name = st.sidebar.text_input("새 포트폴리오 이름 (특수문자 제외)")
new_p_strat = st.sidebar.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"])
new_p_cash = st.sidebar.number_input("초기 총 투자금", value=10_000_000, step=1_000_000, format="%d")

if st.sidebar.button("새 포트폴리오 생성하기", use_container_width=True):
    if new_p_name:
        safe_name = re.sub(r'[\\/*?:"<>|]', "", new_p_name)
        new_file_path = os.path.join(SAVE_DIR, f"{safe_name}.json")
        
        if os.path.exists(new_file_path):
            st.sidebar.warning("이미 존재하는 이름입니다.")
        else:
            if new_p_strat == "대형주 (Core)":
                default_stocks = [
                    {'종목명': '삼성전자', '티커': '005930', '매수단가': 0, '보유수량': 0},
                    {'종목명': 'SK하이닉스', '티커': '000660', '매수단가': 0, '보유수량': 0},
                    {'종목명': '현대차', '티커': '005380', '매수단가': 0, '보유수량': 0},
                    {'종목명': '삼성바이오로직스', '티커': '207940', '매수단가': 0, '보유수량': 0},
                    {'종목명': 'LG에너지솔루션', '티커': '373220', '매수단가': 0, '보유수량': 0},
                    {'종목명': 'KB금융', '티커': '105560', '매수단가': 0, '보유수량': 0},
                    {'종목명': 'POSCO홀딩스', '티커': '005490', '매수단가': 0, '보유수량': 0},
                    {'종목명': 'NAVER', '티커': '035420', '매수단가': 0, '보유수량': 0},
                    {'종목명': 'HD한국조선해양', '티커': '009540', '매수단가': 0, '보유수량': 0},
                    {'종목명': '한화에어로스페이스', '티커': '012450', '매수단가': 0, '보유수량': 0},
                    {'종목명': 'HD현대일렉트릭', '티커': '267260', '매수단가': 0, '보유수량': 0},
                    {'종목명': '하이브', '티커': '352820', '매수단가': 0, '보유수량': 0},
                    {'종목명': '삼양식품', '티커': '003230', '매수단가': 0, '보유수량': 0}
                ]
            else:
                default_stocks = []

            new_data = {
                'strategy': new_p_strat, 
                'cash': new_p_cash,
                'stocks': default_stocks,
                'created_at': datetime.date.today().strftime('%Y-%m-%d')
            }
            with open(new_file_path, 'w', encoding='utf-8') as f:
                json.dump(new_data, f, ensure_ascii=False, indent=2)
            st.rerun()

# ==========================================
# [신규] 전략 동기화 KIS API 로드
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")

SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = None, None, None, None, True

if p_data:
    # 전략에 따른 키 결정 (core vs satellite)
    kis_secret_key = "core" if active_strat == "대형주 (Core)" else "satellite"
    
    try:
        kis_account_data = st.secrets.get("kis_accounts", {}).get(kis_secret_key, None)
    except Exception:
        kis_account_data = None
        
    if kis_account_data:
        SYS_APP_KEY = kis_account_data.get("app_key")
        SYS_APP_SECRET = kis_account_data.get("app_secret")
        SYS_CANO = str(kis_account_data.get("cano"))
        SYS_ACNT_PRDT = str(kis_account_data.get("acnt_prdt", "01"))
        SYS_IS_MOCK = kis_account_data.get("is_mock", False)
        
        acc_name = kis_account_data.get("name", f"{active_strat} 계좌")
        acc_type_str = "모의투자" if SYS_IS_MOCK else "실전투자"
        st.sidebar.success(f"✅ **{acc_name}** 자동 매칭됨\n\n(`{SYS_CANO[:4]}****-{SYS_ACNT_PRDT}` / {acc_type_str})")
    else:
        st.sidebar.warning(f"🔑 **KIS API 미연동**\n\nStreamlit Cloud 우측 하단 `Manage app` -> `Settings` -> `Secrets`에 `[kis_accounts.{kis_secret_key}]` 정보를 등록해주세요.")
else:
     st.sidebar.info("👈 포트폴리오를 선택하면 실계좌가 자동 매칭됩니다.")

# ==========================================
# 파라미터 세팅 및 테마 인디케이터 
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header("⚙️ Advanced Strategy Parameters")

if active_strat == '대형주 (Core)':
    st.sidebar.markdown("<div style='padding: 12px; border-radius: 8px; background-color: rgba(31, 119, 180, 0.1); border-left: 5px solid #1f77b4; color: #1f77b4;'><b>🟦 대형주 (Core) 모드</b><br><span style='font-size: 0.85em; color: #a0a0a0;'>안정적 우량주 장기 추세 추종 필터</span></div>", unsafe_allow_html=True)
else:
    st.sidebar.markdown("<div style='padding: 12px; border-radius: 8px; background-color: rgba(255, 127, 14, 0.1); border-left: 5px solid #ff7f0e; color: #ff7f0e;'><b>🟧 중소형주 (Satellite) 모드</b><br><span style='font-size: 0.85em; color: #a0a0a0;'>주도주 눌림목 단기 스윙 타점 필터</span></div>", unsafe_allow_html=True)

st.sidebar.write("") 

def_sl = -15 if active_strat == '대형주 (Core)' else -12
def_alloc = 35 if active_strat == '대형주 (Core)' else 20
def_ts_target = 30 if active_strat == '대형주 (Core)' else 15
def_ts_drop = -10 if active_strat == '대형주 (Core)' else -5

if st.sidebar.button("🔄 초기 AI 권장 세팅으로 복구", use_container_width=True):
    st.session_state[f"ma200_{active_strat}"] = True
    st.session_state[f"cd_{active_strat}"] = 60
    st.session_state[f"wb_{active_strat}"] = 1.5
    st.session_state[f"sl_{active_strat}"] = def_sl
    st.session_state[f"alloc_{active_strat}"] = def_alloc
    st.session_state[f"hold_{active_strat}"] = 5
    st.session_state[f"ts_t_{active_strat}"] = def_ts_target
    st.session_state[f"ts_d_{active_strat}"] = def_ts_drop
    st.session_state[f"boost_{active_strat}"] = True
    st.rerun()

st.sidebar.markdown("**횡보/하락장 방어 필터**")
use_ma200_filter = st.sidebar.checkbox("🛡️ 200일 대장기 추세선 필터 적용", value=st.session_state.get(f"ma200_{active_strat}", True), key=f"ma200_{active_strat}")
cooldown_days = st.sidebar.slider("🔒 연속 2회 손실 시 쿨다운 (일)", min_value=0, max_value=90, value=st.session_state.get(f"cd_{active_strat}", 60), step=15, key=f"cd_{active_strat}")

st.sidebar.markdown("**기본 리스크 관리**")
whipsaw_buffer = st.sidebar.slider("골든크로스 휩소 방지 버퍼 (%)", min_value=0.0, max_value=5.0, value=st.session_state.get(f"wb_{active_strat}", 1.5), step=0.5, key=f"wb_{active_strat}")
sat_stop_loss = st.sidebar.slider("긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=st.session_state.get(f"sl_{active_strat}", def_sl), step=1, key=f"sl_{active_strat}")
max_alloc_pct = st.sidebar.slider("기본 종목당 투입 한도 (%)", min_value=10, max_value=60, value=st.session_state.get(f"alloc_{active_strat}", def_alloc), step=5, key=f"alloc_{active_strat}")
min_hold_days = st.sidebar.slider("최소 보유 기간 (일)", min_value=0, max_value=20, value=st.session_state.get(f"hold_{active_strat}", 5), step=1, key=f"hold_{active_strat}")

st.sidebar.markdown("**🔥 대세 추세장 셋업**")
ts_target_pct = st.sidebar.slider("트레일링 스탑 목표 수익률 (%)", min_value=10, max_value=100, value=st.session_state.get(f"ts_t_{active_strat}", def_ts_target), step=5, key=f"ts_t_{active_strat}")
ts_drop_pct = st.sidebar.slider("트레일링 스탑 하락 허용 폭 (%)", min_value=-20, max_value=-5, value=st.session_state.get(f"ts_d_{active_strat}", def_ts_drop), step=1, key=f"ts_d_{active_strat}")
bull_market_boost = st.sidebar.checkbox("🔥 강세장 자금 풀 부스터", value=st.session_state.get(f"boost_{active_strat}", True), key=f"boost_{active_strat}")

# ==========================================
# 4. 탭 구성
# ==========================================
tab1, tab2, tab3, tab4 = st.tabs(["📝 가상 샌드박스", "🔌 KIS 실전 계좌", "📊 시뮬레이션", "📄 알고리즘 백서"])

with tab1:
    st.header("📝 가상 포트폴리오 샌드박스")
    st.caption("수동으로 종목을 관리하고 진단하는 공간입니다. 여기서의 수정은 실제 계좌에 영향을 주지 않습니다.")
    
    if not p_data or not selected_port:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 생성하거나 선택하세요.")
    else:
        current_strategy = p_data['strategy']
        total_cash = p_data['cash']
        stocks_df = pd.DataFrame(p_data['stocks'])
        if stocks_df.empty:
            stocks_df = pd.DataFrame(columns=['종목명', '티커', '매수단가', '보유수량'])
            
        if current_strategy == '대형주 (Core)':
            st.markdown(f"<div style='padding: 15px; border-radius: 10px; background-color: rgba(31, 119, 180, 0.05); border: 1px solid rgba(31, 119, 180, 0.2);'><h3 style='margin-top:0; color: #1f77b4;'>🟦 📂 <code>{selected_port}</code></h3><span style='color:#555;'><b>적용 엔진:</b> {current_strategy} &nbsp;|&nbsp; <b>가상 자산 풀:</b> {total_cash:,.0f}원</span></div>", unsafe_allow_html=True)
        else:
            st.markdown(f"<div style='padding: 15px; border-radius: 10px; background-color: rgba(255, 127, 14, 0.05); border: 1px solid rgba(255, 127, 14, 0.2);'><h3 style='margin-top:0; color: #ff7f0e;'>🟧 📂 <code>{selected_port}</code></h3><span style='color:#555;'><b>적용 엔진:</b> {current_strategy} &nbsp;|&nbsp; <b>가상 자산 풀:</b> {total_cash:,.0f}원</span></div>", unsafe_allow_html=True)
        
        st.write("")
        
        st.markdown("### 💡 종목 Sourcing 센터")
        st.caption("클릭 한 번으로 전략에 맞는 대표 종목을 채우거나, AI 스캐너로 오늘 진입 가능한 종목을 찾아냅니다.")
        
        col_src1, col_src2 = st.columns(2)
        
        with col_src1:
            st.markdown("**[📦 원클릭 우량주 팩 추가]**")
            if current_strategy == '대형주 (Core)':
                if st.button("➕ 대형주 섹터별 13선 채우기", use_container_width=True):
                    sector_core_stocks = [
                        {'종목명': '삼성전자', '티커': '005930', '매수단가': 0, '보유수량': 0},
                        {'종목명': 'SK하이닉스', '티커': '000660', '매수단가': 0, '보유수량': 0},
                        {'종목명': '현대차', '티커': '005380', '매수단가': 0, '보유수량': 0},
                        {'종목명': '삼성바이오로직스', '티커': '207940', '매수단가': 0, '보유수량': 0},
                        {'종목명': 'LG에너지솔루션', '티커': '373220', '매수단가': 0, '보유수량': 0},
                        {'종목명': 'KB금융', '티커': '105560', '매수단가': 0, '보유수량': 0},
                        {'종목명': 'POSCO홀딩스', '티커': '005490', '매수단가': 0, '보유수량': 0},
                        {'종목명': 'NAVER', '티커': '035420', '매수단가': 0, '보유수량': 0},
                        {'종목명': 'HD한국조선해양', '티커': '009540', '매수단가': 0, '보유수량': 0},
                        {'종목명': '한화에어로스페이스', '티커': '012450', '매수단가': 0, '보유수량': 0},
                        {'종목명': 'HD현대일렉트릭', '티커': '267260', '매수단가': 0, '보유수량': 0},
                        {'종목명': '하이브', '티커': '352820', '매수단가': 0, '보유수량': 0},
                        {'종목명': '삼양식품', '티커': '003230', '매수단가': 0, '보유수량': 0}
                    ]
                    p_data['stocks'] = sector_core_stocks
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(p_data, f, ensure_ascii=False, indent=2)
                    st.rerun()
            else:
                if st.button("➕ KOSDAQ 우량 45선 채우기", use_container_width=True):
                    kosdaq_top45_tuples = [
                        ("에코프로비엠", "247540"), ("알테오젠", "196170"), ("HLB", "028300"), ("엔켐", "348370"),
                        ("리가켐바이오", "141080"), ("휴젤", "145020"), ("클래시스", "214150"), ("삼천당제약", "000250"),
                        ("셀트리온제약", "068760"), ("실리콘투", "257720"), ("HPSP", "403870"), ("레인보우로보틱스", "277810"),
                        ("이오테크닉스", "039030"), ("솔브레인", "357780"), ("JYP Ent.", "035900"), ("에스엠", "041510"),
                        ("동진쎄미켐", "052710"), ("파마리서치", "214450"), ("피에스케이", "319660"), ("원익IPS", "240810"),
                        ("테크윙", "089030"), ("주성엔지니어링", "036930"), ("심텍", "222800"), ("에스티팜", "237690"),
                        ("브이티", "018290"), ("티씨케이", "064760"), ("윤성에프앤씨", "372170"), ("워트", "396470"),
                        ("하나마이크론", "067310"), ("루닛", "328130"), ("리노공업", "058470"), ("펩트론", "087010"),
                        ("에이비엘바이오", "298380"), ("에이피알", "278470"), ("제룡전기", "033100"), ("비에이치아이", "083650"),
                        ("태광", "023160"), ("원익QnC", "074600"), ("서진시스템", "178320"), ("파두", "440110"),
                        ("메디톡스", "086900"), ("디어유", "376300"), ("펌텍코리아", "251970"), ("하이록코리아", "013030"),
                        ("우양", "105630")
                    ]
                    sat_stocks = [{'종목명': name, '티커': code, '매수단가': 0, '보유수량': 0} for name, code in kosdaq_top45_tuples]
                    p_data['stocks'] = sat_stocks
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(p_data, f, ensure_ascii=False, indent=2)
                    st.rerun()

        with col_src2:
            st.markdown("**[🔍 실시간 AI 타점 스캐너]**")
            if current_strategy == '대형주 (Core)':
                if st.button("🚀 KOSPI 우량주 골든크로스 탐색", type="primary", use_container_width=True):
                    st.session_state.show_scanner = True
            else:
                if st.button("🚀 KOSDAQ 주도주 눌림목 탐색", type="primary", use_container_width=True):
                    st.session_state.show_scanner = True

        if st.session_state.show_scanner:
            if current_strategy == '대형주 (Core)':
                with st.spinner("KOSPI 시가총액 상위 100종목 대세 상승 전환 분석 중... (약 10초 소요)"):
                    scan_result = run_core_scanner(use_ma200_filter, whipsaw_buffer)
                    if not scan_result.empty:
                        st.success(f"✅ AI가 새로운 추세를 시작한 대장주 {len(scan_result)}개를 발굴했습니다!")
                        hc1, hc2, hc3, hc4, hc5 = st.columns([2.5, 1.5, 1.5, 2, 2])
                        hc1.write("**종목명 (티커)**")
                        hc2.write("**현재가**")
                        hc3.write("**20/60선 이격**")
                        hc4.write("**20일 모멘텀**")
                        hc5.write("**가상 포트 추가**")
                        st.markdown("---")
                        
                        for idx, row in scan_result.iterrows():
                            c1, c2, c3, c4, c5 = st.columns([2.5, 1.5, 1.5, 2, 2])
                            ticker = row['티커']
                            name = row['종목명']
                            
                            c1.write(f"**{name}** (`{ticker}`)")
                            c2.write(row['현재가'])
                            c3.write(row['20/60선 이격'])
                            c4.write(row['20일 모멘텀'])
                            
                            is_exist = False
                            if not stocks_df.empty and '티커' in stocks_df.columns:
                                is_exist = (stocks_df['티커'] == ticker).any()
                            
                            if is_exist:
                                c5.button("✔️ 추가됨", key=f"scan_{ticker}_disabled", disabled=True)
                            else:
                                if c5.button("➕ 샌드박스 담기", key=f"scan_{ticker}_add"):
                                    new_row = {'종목명': name, '티커': ticker, '매수단가': 0, '보유수량': 0}
                                    p_data['stocks'].append(new_row)
                                    temp_df = pd.DataFrame(p_data['stocks']).drop_duplicates(subset=['티커'])
                                    p_data['stocks'] = temp_df.to_dict(orient='records')
                                    with open(file_path, 'w', encoding='utf-8') as f:
                                        json.dump(p_data, f, ensure_ascii=False, indent=2)
                                    st.rerun() 
                    else:
                        st.warning("⚠️ 현재 조건(200일선 방어 및 골든크로스 전환)을 완벽히 만족하는 대형 우량주가 없습니다.")
            else:
                with st.spinner("코스닥 시가총액 상위 150종목 눌림목 분석 중... (약 15초 소요)"):
                    scan_result = run_satellite_scanner(use_ma200_filter)
                    if not scan_result.empty:
                        st.success(f"✅ AI가 오늘 진입 가능한 눌림목 종목 {len(scan_result)}개를 발굴했습니다! (200일선 방어 완벽 일치)")
                        hc1, hc2, hc3, hc4, hc5 = st.columns([2.5, 1.5, 1.5, 2, 2])
                        hc1.write("**종목명 (티커)**")
                        hc2.write("**현재가**")
                        hc3.write("**20일선 이격도**")
                        hc4.write("**최대 수급(거래량)**")
                        hc5.write("**가상 포트 추가**")
                        st.markdown("---")
                        
                        for idx, row in scan_result.iterrows():
                            c1, c2, c3, c4, c5 = st.columns([2.5, 1.5, 1.5, 2, 2])
                            ticker = row['티커']
                            name = row['종목명']
                            
                            c1.write(f"**{name}** (`{ticker}`)")
                            c2.write(row['현재가'])
                            c3.write(row['20일선 이격도'])
                            c4.write(row['최근 최대 수급'])
                            
                            is_exist = False
                            if not stocks_df.empty and '티커' in stocks_df.columns:
                                is_exist = (stocks_df['티커'] == ticker).any()
                            
                            if is_exist:
                                c5.button("✔️ 추가됨", key=f"scan_{ticker}_disabled", disabled=True)
                            else:
                                if c5.button("➕ 샌드박스 담기", key=f"scan_{ticker}_add"):
                                    new_row = {'종목명': name, '티커': ticker, '매수단가': 0, '보유수량': 0}
                                    p_data['stocks'].append(new_row)
                                    temp_df = pd.DataFrame(p_data['stocks']).drop_duplicates(subset=['티커'])
                                    p_data['stocks'] = temp_df.to_dict(orient='records')
                                    with open(file_path, 'w', encoding='utf-8') as f:
                                        json.dump(p_data, f, ensure_ascii=False, indent=2)
                                    st.rerun() 
                    else:
                        st.warning("⚠️ 현재 조건(수급 폭발 후 눌림목 & 200일선 방어)을 완벽히 만족하는 주도주가 없습니다.")
        
        st.markdown("---")
        st.markdown("### 📝 수동 종목 관리 (검색 / 삭제)")
        col_manage1, col_manage2 = st.columns(2)
        
        with col_manage1:
            st.markdown("**[➕ 수동 종목 검색 추가]**")
            search_kw = st.text_input("추가할 종목명 검색", key=f"search_{selected_port}")
            if search_kw:
                krx_df = load_krx_universe()
                filtered_stocks = krx_df[krx_df['Name'].str.contains(search_kw, case=False, na=False)]
                if not filtered_stocks.empty:
                    display_options = [f"{row['Name']} ({row['Code']})" for _, row in filtered_stocks.iterrows()]
                    selected_option = st.selectbox("검색 결과", display_options, key=f"sel_{selected_port}")
                    if st.button("수동 종목 추가하기", key=f"add_btn_{selected_port}", use_container_width=True):
                        sel_name = selected_option.split(" (")[0]
                        sel_code = selected_option.split(" (")[1].replace(")", "")
                        
                        new_row = {'종목명': sel_name, '티커': sel_code, '매수단가': 0, '보유수량': 0}
                        p_data['stocks'].append(new_row)
                        temp_df = pd.DataFrame(p_data['stocks']).drop_duplicates(subset=['티커'])
                        p_data['stocks'] = temp_df.to_dict(orient='records')
                        
                        with open(file_path, 'w', encoding='utf-8') as f:
                            json.dump(p_data, f, ensure_ascii=False, indent=2)
                        st.rerun()

        with col_manage2:
            st.markdown("**[🗑️ 종목 삭제]**")
            if not stocks_df.empty:
                del_options = stocks_df['종목명'].tolist()
                del_selected = st.selectbox("삭제할 종목 선택", del_options, key=f"del_sel_{selected_port}")
                if st.button("선택 종목 삭제하기", key=f"del_btn_{selected_port}", use_container_width=True):
                    stocks_df = stocks_df[stocks_df['종목명'] != del_selected]
                    p_data['stocks'] = stocks_df.to_dict(orient='records')
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(p_data, f, ensure_ascii=False, indent=2)
                    st.rerun()
            else:
                st.caption("현재 등록된 종목이 없습니다.")

        st.markdown("---")
        st.markdown("**가상 포트폴리오 내역 (매수단가 및 보유수량 테스트 입력)**")
        
        edited_df = st.data_editor(
            stocks_df, num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}"
        )
        
        col_qsave1, col_qsave2 = st.columns([1, 1])
        with col_qsave1:
            with st.popover("💾 표 데이터 수정 후 덮어쓰기 (Quick Save)", use_container_width=True):
                st.markdown("⚠️ **현재 입력하신 '매수 단가'와 '보유 수량'을 기존 파일에 덮어씁니다.**<br>정말 저장하시겠습니까?", unsafe_allow_html=True)
                if st.button("✔️ 네, 덮어쓰기 저장합니다.", key=f"save_{selected_port}", type="primary", use_container_width=True):
                    p_data['stocks'] = edited_df.to_dict(orient='records')
                    with open(file_path, "w", encoding="utf-8") as f:
                        json.dump(p_data, f, ensure_ascii=False, indent=2)
                    st.success("✅ 포트폴리오 변경사항이 안전하게 저장되었습니다!")
                    
        with col_qsave2:
            st.markdown("""<style>[data-testid="stPopover"] { width: 100%; }</style>""", unsafe_allow_html=True)
            with st.popover("📄 새 이름으로 복사하기 (Save As)", use_container_width=True):
                save_filename = st.text_input("새 파일명", value=f"{selected_port}_복사본")
                if st.button("복사본 생성하기", type="primary"):
                    safe_new_name = re.sub(r'[\\/*?:"<>|]', "", save_filename)
                    new_file_path = os.path.join(SAVE_DIR, f"{safe_new_name}.json")
                    with open(new_file_path, "w", encoding="utf-8") as f:
                         json.dump(p_data, f, ensure_ascii=False, indent=2)
                    st.rerun()

        st.markdown("---")
        st.subheader("🩺 가상 포트폴리오 AI 진단 결과")
        
        run_btn = st.button("가상 종목 진단 실행", type="secondary")
        
        if run_btn or st.session_state.auto_diagnose:
            st.session_state.auto_diagnose = False
            
            if edited_df.empty:
                st.warning("진단할 종목이 없습니다.")
            else:
                with st.spinner("거시 지표 및 개별 종목 필터, 자본 증감액 룰 분석 중..."):
                    vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60 = fetch_market_data()
                    
                    vix_text = f"VIX {vix_val:.1f}"
                    if vix_contrarian: vix_status = f"{vix_text}(🔥극단적 공포 V자 반등 포착)"
                    elif vix_safe: vix_status = f"{vix_text}(시장 안정)"
                    else: vix_status = f"{vix_text}(시장 경계/공포)"

                    market_ret_60 = kospi_ret_60 if current_strategy == '대형주 (Core)' else kosdaq_ret_60

                    stock_data_cache = {}
                    buy_scores = {}
                    buf = whipsaw_buffer / 100.0
                    
                    for idx, row in edited_df.iterrows():
                        s_ticker = row['티커']
                        s_name = row['종목명']
                        buy_price = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                        quantity = pd.to_numeric(row.get('보유수량', 0), errors='coerce')
                        if pd.isna(buy_price): buy_price = 0
                        if pd.isna(quantity): quantity = 0
                        is_holding = (quantity > 0) and (buy_price > 0)
                        
                        c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_vol_max = fetch_stock_status(s_ticker)
                        if c_price is None: continue
                        
                        stock_data_cache[s_name] = {
                            'price': c_price, 'ma200': ma200, 'ma60': ma60, 'ma20': ma20, 
                            'drawdown': drawdown, 'vol_ratio': vol_ratio, 'ret_60': ret_60, 'ret_20': ret_20,
                            'ma60_slope': ma60_slope_positive, 'is_above_ma200': is_above_ma200, 'is_holding': is_holding,
                            'qty': quantity, 'vol_surged': vol_surged, 'low': current_low
                        }
                        
                        vol_strong = vol_ratio >= 150.0
                        rs_strong = ret_60 > market_ret_60
                        ma200_pass = (not use_ma200_filter) or is_above_ma200
                        
                        if current_strategy == '대형주 (Core)':
                            if ma200_pass and (((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) and vix_safe) or vix_contrarian):
                                score = 1.0
                                if vol_strong: score += 0.5
                                if rs_strong: score += 0.5
                                if vix_contrarian: score += 1.0
                                buy_scores[s_name] = score
                        else:
                            dist_ma20 = ((c_price / ma20) - 1) * 100
                            low_ma20_touch = (current_low <= ma20 * 1.01) and (c_price >= ma20 * 0.95)
                            is_dip = (-5.0 <= dist_ma20 <= 3.0) or low_ma20_touch
                            
                            if ma200_pass and (is_dip or vix_contrarian) and drawdown >= -30.0:
                                score = 1.0
                                if vol_strong: score += 1.0
                                if rs_strong: score += 0.5
                                if vix_contrarian: score += 1.0
                                buy_scores[s_name] = score

                    total_score = sum(buy_scores.values()) if buy_scores else 1.0
                    current_max_alloc_ratio = max_alloc_pct / 100.0
                    if bull_market_boost and market_ret_60 > 0:
                        current_max_alloc_ratio = min(current_max_alloc_ratio * 1.5, 1.0)

                    current_stock_eval = sum((data['qty'] * data['price']) for data in stock_data_cache.values() if data['is_holding'])
                    available_cash = total_cash - current_stock_eval
                    force_sell_plans = {}
                    
                    if available_cash < 0:
                        deficit = abs(available_cash)
                        held_stocks_info = [
                            {'name': k, 'eval_amt': v['qty'] * v['price'], 'price': v['price'], 'score': buy_scores.get(k, 0.0), 'ret_20': v['ret_20']}
                            for k, v in stock_data_cache.items() if v['is_holding']
                        ]
                        held_stocks_info.sort(key=lambda x: (x['score'], x['ret_20']))
                        for stock in held_stocks_info:
                            if deficit <= 0: break
                            s_name = stock['name']
                            eval_amt = stock['eval_amt']
                            price = stock['price']
                            if eval_amt <= deficit:
                                force_sell_plans[s_name] = "🔴 전량 매도 (설정 투자금 감액에 따른 우선 청산)"
                                deficit -= eval_amt
                            else:
                                sell_qty = int(np.ceil(deficit / price))
                                force_sell_plans[s_name] = f"🟡 부분 매도 (설정 투자금 감액 맞춤: {sell_qty}주 매도)"
                                deficit = 0

                    current_cash = max(available_cash, 0)

                    results = []
                    for idx, row in edited_df.iterrows():
                        s_name = row['종목명']
                        if s_name not in stock_data_cache:
                            results.append({'종목명': s_name, '상태': '알수없음', '현재가': '-', '액션 플랜': '⚠️ 확인 불가', '상세 AI 판단 근거': '-'})
                            continue
                            
                        data = stock_data_cache[s_name]
                        c_price = data['price']
                        is_holding = data['is_holding']
                        holding_status = "보유중" if is_holding else "신규/관심"
                        
                        vol_strong = data['vol_ratio'] >= 150.0
                        rs_strong = data['ret_60'] > market_ret_60
                        vol_status = f"거래량 {data['vol_ratio']:.0f}%(수급 급증)" if vol_strong else (f"거래량 {data['vol_ratio']:.0f}%(수급 보통)" if data['vol_ratio'] >= 80 else f"거래량 {data['vol_ratio']:.0f}%(수급 침체)")
                        rs_status = f"상대강도 우위" if rs_strong else "상대강도 열위"
                        ma200_status = "200일선 상회" if data['is_above_ma200'] else "200일선 하회"

                        ma20, ma60, ret_20, ma60_slope_positive = data['ma20'], data['ma60'], data['ret_20'], data['ma60_slope']
                        diff_ma = ((ma20 / ma60) - 1) * 100
                        dist_ma20 = ((c_price / ma20) - 1) * 100
                        
                        if current_strategy == '대형주 (Core)':
                            tech_text = f"20/60선 이격 {diff_ma:+.2f}%, 20일 모멘텀 {ret_20:+.2f}%"
                        else:
                            tech_text = f"20일선 이격 {dist_ma20:+.2f}% (눌림목 타점)"
                            
                        slope_status = "60일선 우상향" if ma60_slope_positive else "60일선 횡보/우하향"

                        stock_weight = (buy_scores.get(s_name, 1.0) / total_score) if total_score > 0 else (1 / max(len(buy_scores), 1))
                        target_amt = min(total_cash * stock_weight, total_cash * current_max_alloc_ratio)
                        current_val = data['qty'] * c_price
                        diff_amt = target_amt - current_val

                        if is_holding and s_name in force_sell_plans:
                            action = force_sell_plans[s_name]
                            detail = f"[{tech_text}] | [{rs_status}]\n➔ ⚠️ 총 투자 운용 자산이 감액 설정됨에 따라, AI 모멘텀이 가장 낮은 본 종목을 우선 매도하여 현금 비중을 맞춥니다."
                        elif current_strategy == '대형주 (Core)':
                            if is_holding: 
                                if ma20 >= ma60 * (1 - buf/2): 
                                    add_cond = ((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) and vix_safe) or vix_contrarian
                                    if diff_amt > 0 and current_cash >= c_price and add_cond:
                                        add_shares = int(min(diff_amt, current_cash) // c_price)
                                        if add_shares > 0:
                                            action = f"🟢 추가 매수 (비중 확대: {add_shares}주 추가)"
                                            detail = f"[{tech_text}] | [{rs_status}]\n➔ 자본 증액 또는 모멘텀 우위로 비중을 리밸런싱(확대)합니다."
                                        else:
                                            action = "🟢 보유 유지"
                                            detail = f"[{tech_text}] | [{vix_status}]\n➔ 정배열 추세 지속 중."
                                    elif diff_amt < -c_price: 
                                        reduce_shares = int(abs(diff_amt) // c_price)
                                        action = f"🟡 부분 매도 (비중 조절: {reduce_shares}주 매도)"
                                        detail = f"[{tech_text}]\n➔ 목표 비중 초과로 부분 익절 리밸런싱을 진행합니다."
                                    else:
                                        action = "🟢 보유 유지"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{rs_status}]\n➔ 정배열 추세 지속 중."
                                else: 
                                    action = "🔴 전량 매도 (현금화)"
                                    detail = f"[{tech_text}] | [{vix_status}] | [{rs_status}]\n➔ 데드크로스 발생으로 즉각 현금화."
                            else: 
                                if (use_ma200_filter and not data['is_above_ma200']):
                                    action = "🔴 진입 보류 (200일선 하회)"
                                    detail = f"[{tech_text}] | [{ma200_status}]\n➔ 장기 추세선 아래 역배열 구간 진입 금지."
                                else:
                                    if ((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) and vix_safe) or vix_contrarian: 
                                        rec_shares = int(min(target_amt, current_cash) // c_price) if c_price > 0 else 0
                                        if rec_shares > 0:
                                            action = f"🟢 적극 신규 진입 (추천: {rec_shares}주)"
                                            reason = "V자 반등 바닥잡기" if vix_contrarian else f"{whipsaw_buffer}% 버퍼 및 200일선 통과"
                                            detail = f"[{tech_text}] | [{ma200_status}] | [{vix_status}] | [{vol_status}]\n➔ 스캐너 조건 완벽 일치! {reason} 대세 상승 진입."
                                        else:
                                            action = "🟡 진입 보류 (현금 부족)"
                                            detail = f"[{tech_text}]\n➔ 타점은 좋으나 가용 현금이 부족합니다."
                                    else: 
                                        action = "🟡 관망 (타점 대기)"
                                        detail = f"[{tech_text}] | [{ma200_status}]\n➔ 아직 확실한 추세 전환(골든크로스)이 발생하지 않아 관망합니다."
                        else: # 중소형주
                            if is_holding: 
                                buy_price = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                                user_ret = ((c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                                tech_text_sat = f"수익률 {user_ret:+.2f}%"
                                if user_ret <= (sat_stop_loss): 
                                    action = "🔴 강제 손절 집행"
                                    detail = f"[{tech_text_sat}]\n➔ 긴급 손절선({sat_stop_loss}%) 이탈로 하드 컷."
                                elif ma20 >= ma60 * (1 - buf/2):
                                    low_ma20_touch = (data['low'] <= ma20 * 1.01) and (c_price >= ma20 * 0.95)
                                    is_dip = (-5.0 <= dist_ma20 <= 3.0) or low_ma20_touch
                                    add_cond = (is_dip or vix_contrarian) and data['drawdown'] >= -30.0
                                    if diff_amt > 0 and current_cash >= c_price and add_cond:
                                        add_shares = int(min(diff_amt, current_cash) // c_price)
                                        if add_shares > 0:
                                            action = f"🟢 추가 매수 (비중 확대: {add_shares}주 추가)"
                                            detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 자본 증액 반영 추가 매수."
                                        else:
                                            action = "🟢 보유 유지"
                                            detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 정배열 추세 홀딩."
                                    elif diff_amt < -c_price:
                                        reduce_shares = int(abs(diff_amt) // c_price)
                                        action = f"🟡 부분 매도 (비중 조절: {reduce_shares}주 매도)"
                                        detail = f"[{tech_text_sat}]\n➔ 목표 비중 조절."
                                    else:
                                        action = "🟢 보유 유지"
                                        detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 정배열 추세 홀딩 구간."
                                else:
                                    action = "🔴 전량 매도"
                                    detail = f"[{tech_text_sat}]\n➔ 20일선 데드크로스로 완전 이탈."
                            else: # 미보유 (관심종목 샌드박스)
                                if (use_ma200_filter and not data['is_above_ma200']):
                                    action = "🔴 진입 보류 (200일선 하회)"
                                    detail = f"[{tech_text}] | [{ma200_status}]\n➔ 장기 추세선 아래 역배열 구간이므로 진입을 금지합니다."
                                else:
                                    low_ma20_touch = (data['low'] <= ma20 * 1.01) and (c_price >= ma20 * 0.95)
                                    is_dip = (-5.0 <= dist_ma20 <= 3.0) or low_ma20_touch
                                    
                                    if (is_dip or vix_contrarian) and data['drawdown'] >= -30.0: 
                                        rec_shares = int(min(target_amt, current_cash) // c_price) if c_price > 0 else 0
                                        if rec_shares > 0:
                                            if data['vol_surged'] or vix_contrarian:
                                                action = f"🟢 적극 신규 진입 (추천: {rec_shares}주)"
                                                detail = f"[{tech_text}] | [{vix_status}]\n➔ 스캐너 조건 완벽 일치! 수급 폭발(200%+) 후 정확한 20일선 눌림목 타점입니다."
                                            else:
                                                action = f"🟡 분할 매수 (관심종목 타점 도달)"
                                                detail = f"[{tech_text}] | [{vix_status}]\n➔ 20일선 타점은 도달했으나 최근 폭발적인 수급 이력이 없습니다. 샌드박스 편입 종목이므로 소액 진입을 고려해볼 수 있습니다."
                                        else:
                                            action = "🟡 진입 보류 (현금 부족)"
                                            detail = f"[{tech_text}]\n➔ 타점은 좋으나 가용 현금이 부족합니다."
                                    else: 
                                        action = "🟡 관망 (타점 대기)"
                                        detail = f"[{tech_text}] | [{ma200_status}]\n➔ 현재 가격이 20일선에서 멀리 떨어져 있습니다. 눌림목(-5%~+3%)을 기다리세요."
                                
                        results.append({
                            '종목명': s_name, '상태': holding_status, '현재가': f"{c_price:,.0f} 원",
                            '액션 플랜': action, '상세 AI 판단 근거': detail
                        })
                    
                    warning_msg = "⚠️ **[투자금 감액 감지]** 감액된 투자금에 맞춰 최약체 종목 우선 청산 액션 플랜이 발동되었습니다." if available_cash < 0 else ""
                    index_name = "KOSPI" if current_strategy == '대형주 (Core)' else "KOSDAQ"
                    boost_msg = f" (🔥{index_name} 강세장 부스터 작동 중: 한도 최대 {current_max_alloc_ratio*100:.0f}%)" if (bull_market_boost and market_ret_60 > 0) else ""
                    st.info(f"📊 **[가상 자금 리밸런싱 및 현금 현황]**\n\n"
                            f"• **총 운용 설정 자산:** `{total_cash:,.0f} 원`\n"
                            f"• **가용 현금 잔고:** `{current_cash:,.0f} 원` (보유 주식 평가액: `{current_stock_eval:,.0f} 원`)\n"
                            f"• **운용 특징:** 가상 투자금을 증액하면 추가 매수를 지시하고, 감액 시 최약체부터 우선 청산하는 산식이 반영되었습니다.\n"
                            f"{warning_msg}{boost_msg}")
                    st.table(pd.DataFrame(results))

with tab2:
    st.header("🔌 실전 계좌(API) 연동 현황")
    st.caption("아래 설정된 한국투자증권 실계좌 정보 및 잔고를 확인하고 진단합니다.")

    if not SYS_APP_KEY:
        st.markdown("<div style='padding: 15px; border-radius: 8px; background-color: rgba(255, 193, 7, 0.1); border-left: 5px solid #ffc107; color: #856404;'>💡 현재 선택된 포트폴리오 전략에 매칭되는 KIS 계좌가 없습니다.<br>Streamlit Cloud <b>Secrets</b>에 API 키를 등록해주세요.</div>", unsafe_allow_html=True)
    else:
        acc_type_str = "모의투자 계좌" if SYS_IS_MOCK else "실전투자 계좌"
        st.success(f"✅ 연동 계좌: **`{SYS_CANO[:4]}****-{SYS_ACNT_PRDT}`** ({acc_type_str})")
        
        col_ref1, col_ref2 = st.columns([2, 8])
        with col_ref1:
            refresh_btn = st.button("🔄 이 계좌 잔고 실시간 새로고침", type="primary", use_container_width=True)

        cache_key = f"kis_global_cache_{SYS_CANO}_{SYS_ACNT_PRDT}"
        
        if refresh_btn or cache_key not in st.session_state:
            with st.spinner("한투증권 API 서버와 통신 중..."):
                token = get_kis_access_token(SYS_APP_KEY, SYS_APP_SECRET, is_mock=SYS_IS_MOCK)
                if token:
                    holdings, summary = fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, token, is_mock=SYS_IS_MOCK)
                    if holdings is not None and summary is not None:
                        tot_evlu = float(summary[0].get('tot_evlu_amt', 0)) if summary else 0
                        imported = [{'종목명': item.get('prdt_name', ''), '티커': item.get('pdno', ''), '매수단가': float(item.get('pchs_avg_pric', 0)), '보유수량': int(item.get('hldg_qty', 0))} for item in holdings if int(item.get('hldg_qty', 0)) > 0]
                        st.session_state[cache_key] = {'total_eval': tot_evlu, 'stocks': imported}
                        st.toast("✅ 계좌 잔고 새로고침 완료!")

        kis_data = st.session_state.get(cache_key)
        if kis_data:
            real_total_eval = kis_data['total_eval']
            real_stocks_df = pd.DataFrame(kis_data['stocks'])
            
            st.metric("💰 계좌 총 평가 금액 (현금+주식)", f"{real_total_eval:,.0f} 원")
            
            st.markdown("### 📊 실계좌 보유 종목 리스트")
            if real_stocks_df.empty:
                st.info("현재 이 계좌에 보유 중인 주식이 없습니다.")
            else:
                st.dataframe(real_stocks_df, use_container_width=True)

            st.markdown("---")
            st.subheader("🩺 실전 계좌 AI 매매 진단기")
            
            if st.button("🚀 실전 계좌 종목 진단 실행", type="secondary"):
                if real_stocks_df.empty:
                    st.warning("진단할 보유 종목이 없습니다.")
                elif not p_data:
                    st.error("좌측 사이드바에서 비교 기준이 될 '가상 포트폴리오(전략)'를 먼저 선택해 주세요.")
                else:
                    with st.spinner("실계좌 종목 퀀트 필터링 분석 중..."):
                        vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60 = fetch_market_data()
                        current_strategy = p_data['strategy']
                        market_ret_60 = kospi_ret_60 if current_strategy == '대형주 (Core)' else kosdaq_ret_60
                        buf = whipsaw_buffer / 100.0

                        live_results = []
                        for idx, row in real_stocks_df.iterrows():
                            s_ticker = row['티커']
                            s_name = row['종목명']
                            buy_price = float(row.get('매수단가', 0))
                            
                            c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_vol_max = fetch_stock_status(s_ticker)
                            if c_price is None: continue

                            rs_strong = ret_60 > market_ret_60
                            rs_status = f"상대강도 우위" if rs_strong else "상대강도 열위"
                            
                            diff_ma = ((ma20 / ma60) - 1) * 100
                            dist_ma20 = ((c_price / ma20) - 1) * 100
                            
                            tech_text = f"20/60선 이격 {diff_ma:+.2f}%" if current_strategy == '대형주 (Core)' else f"20일선 이격 {dist_ma20:+.2f}%"

                            if current_strategy == '대형주 (Core)':
                                if ma20 >= ma60 * (1 - buf/2): 
                                    action = "🟢 보유 유지"
                                    detail = f"[{tech_text}] | [{rs_status}]\n➔ 정배열 추세 유지 중."
                                else: 
                                    action = "🔴 즉각 매도 (추세 이탈)"
                                    detail = f"[{tech_text}] | [{rs_status}]\n➔ 데드크로스 발생으로 추세 이탈. 현금화 권장."
                            else:
                                user_ret = ((c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                                tech_text_sat = f"실수익률 {user_ret:+.2f}%"
                                
                                if user_ret <= (sat_stop_loss): 
                                    action = "🔴 강제 손절 집행"
                                    detail = f"[{tech_text_sat}]\n➔ 긴급 손절선({sat_stop_loss}%) 이탈로 하드 컷 권장."
                                elif ma20 >= ma60 * (1 - buf/2):
                                    action = "🟢 보유 유지"
                                    detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 정배열 추세 홀딩 구간."
                                else:
                                    action = "🔴 전량 매도"
                                    detail = f"[{tech_text_sat}]\n➔ 20일선 데드크로스로 주도주 대열 이탈."
                                    
                            live_results.append({
                                '보유 종목명': s_name, '현재가': f"{c_price:,.0f} 원",
                                '실제 매수단가': f"{buy_price:,.0f} 원" if buy_price > 0 else "-",
                                'AI 액션 플랜 (권장)': action, '상세 판단 근거': detail
                            })
                        
                        st.table(pd.DataFrame(live_results))

with tab3:
    st.header("🧪 시뮬레이션 및 백테스트 (Simulation & Backtest)")
    st.markdown("이곳은 **가상 샌드박스의 관심 종목** 및 **전략 유니버스**를 대상으로 AI 알고리즘의 성과를 검증하는 공간입니다.\n*(※ KIS 실전 연동 계좌의 주식이 아닌, 좌측에서 설정한 가상 포트폴리오를 기준으로 연산됩니다.)*")

    if not p_data or not selected_port:
        st.warning("포트폴리오가 없습니다.")
    else:
        stocks_df = pd.DataFrame(p_data['stocks'])
        current_strategy = p_data['strategy']
        total_cash = p_data['cash']
        
        # ---------------------------------------------------------
        # 1. Forward Test (가상 자동매매 운용 성과) 구역
        # ---------------------------------------------------------
        st.subheader("📈 [Forward Test] 가상 샌드박스 관심 종목 단기 성과 추적")
        st.markdown("현재 '가상 샌드박스' 탭에 구성해 둔 **관심 종목 풀**을 대상으로, 지정한 시작일부터 오늘까지 AI가 100% 자동 매매(리밸런싱)를 수행했다고 가정했을 때의 단기 누적 수익률을 확인합니다.")
        
        created_str = p_data.get('created_at', (datetime.date.today() - datetime.timedelta(days=90)).strftime('%Y-%m-%d'))
        created_dt = datetime.datetime.strptime(created_str, "%Y-%m-%d").date()
        
        col_ft1, col_ft2 = st.columns([1, 2])
        with col_ft1:
            ft_start = st.date_input("가상 운용 시작일 (포트폴리오 생성/변경일)", value=created_dt)
        with col_ft2:
            st.write("")
            st.write("")
            ft_run = st.button("🚀 관심 종목(샌드박스) Forward Test 실행", type="primary", use_container_width=True)

        if ft_run:
            if stocks_df.empty:
                st.error("샌드박스에 등록된 종목이 없습니다.")
            else:
                with st.spinner("AI 퀀트 엔진 가상 매매 추적 중... (수 초 소요)"):
                    ft_result = run_quant_simulation(
                        sim_stocks=stocks_df.copy(),
                        strat=current_strategy,
                        init_cash=total_cash,
                        start_date=ft_start,
                        end_date=datetime.date.today(),
                        use_ma200_filter=use_ma200_filter,
                        whipsaw_buffer=whipsaw_buffer,
                        sat_stop_loss=sat_stop_loss,
                        max_alloc_pct=max_alloc_pct,
                        min_hold_days=min_hold_days,
                        ts_target_pct=ts_target_pct,
                        ts_drop_pct=ts_drop_pct,
                        bull_market_boost=bull_market_boost,
                        cooldown_days=cooldown_days
                    )
                    
                    if ft_result:
                        st.success(f"✅ 가상 운용 성과 도출 완료! ({ft_start.strftime('%Y-%m-%d')} ~ 현재)")
                        col_r1, col_r2, col_r3 = st.columns(3)
                        col_r1.metric("총 초기 투입 자산", f"{total_cash:,.0f} 원")
                        col_r2.metric("AI 자동매매 현재 총 자산", f"{ft_result['final_asset']:,.0f} 원")
                        
                        ret_color = "normal" if ft_result['final_port_ret'] >= 0 else "inverse"
                        col_r3.metric("가상 누적 수익률", f"{ft_result['final_port_ret']:+.2f}%", delta=f"{ft_result['final_port_ret']:+.2f}%", delta_color=ret_color)
                        
                        with st.expander("종목별 가상 매매 성과 상세 보기"):
                            st.table(pd.DataFrame(ft_result['summary_rows']))
                    else:
                        st.error("계산할 수 있는 유효한 주가 데이터가 없습니다.")

        st.markdown("---")
        
        # ---------------------------------------------------------
        # 2. Backtest (장기 과거 성과 시뮬레이션) 구역
        # ---------------------------------------------------------
        st.subheader("📊 [Backtest] AI 전략 유니버스 장기 초과수익 검증")
        st.markdown("설정된 **퀀트 전략(대형주/중소형주)**을 과거 장기 주가 데이터에 적용하여 엔진의 신뢰성을 검증합니다. 수수료 및 방어 산식이 완벽히 보정된 환경에서 시장 지수 및 단순 보유 전략 대비 초과 수익을 달성하는지 확인합니다.")

        col_sim1, col_sim2 = st.columns(2)
        with col_sim1:
            start_date = st.date_input("시작일", datetime.date(2023, 1, 1))
        with col_sim2:
            end_date = st.date_input("종료일", datetime.date.today())

        if st.button(f"🚀 '{selected_port}' 전략 기반 Backtest 실행", type="primary", use_container_width=True):
            index_sym = 'KS11' if current_strategy == '대형주 (Core)' else 'KQ11'
            index_name = 'KOSPI' if current_strategy == '대형주 (Core)' else 'KOSDAQ'

            if current_strategy == '중소형주 (Satellite)':
                kosdaq_top45 = [
                    ("에코프로비엠", "247540"), ("알테오젠", "196170"), ("HLB", "028300"), ("엔켐", "348370"),
                    ("리가켐바이오", "141080"), ("휴젤", "145020"), ("클래시스", "214150"), ("삼천당제약", "000250"),
                    ("셀트리온제약", "068760"), ("실리콘투", "257720"), ("HPSP", "403870"), ("레인보우로보틱스", "277810"),
                    ("이오테크닉스", "039030"), ("솔브레인", "357780"), ("JYP Ent.", "035900"), ("에스엠", "041510"),
                    ("동진쎄미켐", "052710"), ("파마리서치", "214450"), ("피에스케이", "319660"), ("원익IPS", "240810"),
                    ("테크윙", "089030"), ("주성엔지니어링", "036930"), ("심텍", "222800"), ("에스티팜", "237690"),
                    ("브이티", "018290"), ("티씨케이", "064760"), ("윤성에프앤씨", "372170"), ("워트", "396470"),
                    ("하나마이크론", "067310"), ("루닛", "328130"), ("리노공업", "058470"), ("펩트론", "087010"),
                    ("에이비엘바이오", "298380"), ("에이피알", "278470"), ("제룡전기", "033100"), ("비에이치아이", "083650"),
                    ("태광", "023160"), ("원익QnC", "074600"), ("서진시스템", "178320"), ("파두", "440110"),
                    ("메디톡스", "086900"), ("디어유", "376300"), ("펌텍코리아", "251970"), ("하이록코리아", "013030"),
                    ("우양", "105630")
                ]
                sim_stocks = pd.DataFrame([{'종목명': name, '티커': code} for name, code in kosdaq_top45])
                st.info("💡 **[AI 스캐너 시뮬레이션 모드]** 중소형주 전략 백테스트 시 **섹터별 코스닥 우량주 45선**이 검색 풀로 자동 적용됩니다.")
            else:
                sim_stocks = stocks_df.copy()

            if sim_stocks.empty:
                st.error("종목이 없습니다.")
            else:
                with st.spinner(f"산식 보정된 {index_name} 벤치마크 및 스캐너 동기화 백테스트 구동 중... (종목이 많을 시 수 분 소요)"):
                    
                    bt_result = run_quant_simulation(
                        sim_stocks=sim_stocks, strat=current_strategy, init_cash=total_cash,
                        start_date=start_date, end_date=end_date, use_ma200_filter=use_ma200_filter,
                        whipsaw_buffer=whipsaw_buffer, sat_stop_loss=sat_stop_loss, max_alloc_pct=max_alloc_pct,
                        min_hold_days=min_hold_days, ts_target_pct=ts_target_pct, ts_drop_pct=ts_drop_pct,
                        bull_market_boost=bull_market_boost, cooldown_days=cooldown_days
                    )
                    
                    if bt_result:
                        st.success(f"✅ 벤치마크 자동 동기화 및 백테스트 실행 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 초기 자산", f"{total_cash:,.0f} 원")
                        col_r2.metric(f"AI 초과수익 전략 최종 기말 자산 (수익률)", f"{bt_result['final_asset']:,.0f} 원", f"{bt_result['final_port_ret']:+.2f}%")
                        
                        st.markdown("---")
                        
                        st.subheader(f"📊 [전략 비교] {index_name} 지수 vs 단순보유 vs 적립식 매수 vs AI 초과수익 추구 전략")
                        
                        comparison_data = [
                            {
                                '전략 구분': '🚀 AI 초과수익 전략 (200D Filter + Cooldown)',
                                '최종 기말 자산': f"{bt_result['final_asset']:,.0f} 원",
                                '총 수익률': f"{bt_result['final_port_ret']:+.2f}%",
                                '운용 방식 및 특징': f'200일선 아래 장기 하락 종목 매수 금지 및 연속 2회 손실 종목 {cooldown_days}일 매수 동결. 버퍼({whipsaw_buffer}%) 통과 상승 종목에 집중 배분하여 박스권 수수료 낭비 원천 차단.'
                            },
                            {
                                '전략 구분': f'📈 시장 벤치마크 ({index_name} 지수 ^{index_sym})',
                                '최종 기말 자산': f"{bt_result['final_benchmark_asset']:,.0f} 원",
                                '총 수익률': f"{bt_result['benchmark_ret_val']:+.2f}%",
                                '운용 방식 및 특징': f'한국 종합주가지수({index_name}) 시장 수익률 추종'
                            },
                            {
                                '전략 구분': '📉 단순보유 (Buy & Hold)',
                                '최종 기말 자산': f"{bt_result['final_bh_asset']:,.0f} 원",
                                '총 수익률': f"{bt_result['final_bh_ret']:+.2f}%",
                                '운용 방식 및 특징': '동일 종목 풀 초기 전액 동일 비중 매수 후 매도 없이 홀딩 (변동성 그대로 노출)'
                            },
                            {
                                '전략 구분': '💰 적립식 매수 (DCA)',
                                '최종 기말 자산': f"{bt_result['final_dca_asset']:,.0f} 원",
                                '총 수익률': f"{bt_result['final_dca_ret']:+.2f}%",
                                '운용 방식 및 특징': '동일 종목 풀 시드 분할 후 매월 정기 추가 투입으로 매입단가 분산'
                            }
                        ]
                        st.table(pd.DataFrame(comparison_data))

                        st.markdown("---")
                        
                        st.subheader("📋 종목별 상세 매매 통계 및 성과 분석")
                        st.table(pd.DataFrame(bt_result['summary_rows']))
                        
                        st.markdown("---")
                        
                        chart = alt.Chart(bt_result['eom_weights_reset']).mark_bar().encode(
                            x=alt.X('Date:O', title='', axis=alt.Axis(labelAngle=-45)),
                            y=alt.Y('Weight:Q', title='비중 (%)', stack='zero'),
                            color=alt.Color('Asset:N', scale=alt.Scale(domain=bt_result['cols_ordered'], range=bt_result['color_range']), title='자산 구분'),
                            order=alt.Order('Order:Q', sort='ascending'),
                            tooltip=['Date', 'Asset', alt.Tooltip('Weight:Q', format='.2f', title='비중(%)')]
                        ).properties(height=450)
                        
                        st.subheader("📊 월말 기준 포트폴리오 비중 추이 (현금 포함, 누적 막대)")
                        st.altair_chart(chart, use_container_width=True)
                    else:
                        st.error("유효한 데이터가 없습니다.")

with tab4:
    st.header("📄 AI 퀀트 투자 전략 및 운용 알고리즘 백서")
    
    st.markdown("""
    본 대시보드에 탑재된 AI 퀀트 엔진은 단순한 기술적 지표의 조합을 넘어, **시장 거시 지표(Macro), 개별 종목 모멘텀(Micro), 그리고 강력한 리스크 관리(Risk Management)**가 통합된 기관급(Institutional-grade) 다이내믹 자산 배분 알고리즘을 사용합니다.
    """)
    
    st.markdown("---")
    
    st.subheader("1. 핵심 운용 철학 (Core Philosophy)")
    st.markdown("""
    * **추세 추종과 손익 비대칭성 (Let Winners Run, Cut Losses Short):** 확실한 상승 추세에 올라타 이익을 길게 가져가고, 횡보 및 하락장에서는 휩소(잦은 매매)를 차단하여 수수료와 원금을 철저히 방어합니다.
    * **동적 자본 관리 (Dynamic Capital Allocation):** 투자금의 증액/감액을 실시간으로 반영하며, 증액 시 즉시 비중 추가 확대 매수를 지시하고, 감액 시 최약체(모멘텀 하위) 종목부터 기계적으로 청산합니다.
    * **공포 탐욕 지수 역발상 (Contrarian):** 대다수 투자자가 시장을 떠나는 극단적 공포(VIX 폭등 후 꺾임) 시점을 수리적으로 포착하여 V자 반등을 낚아챕니다.
    """)

    st.markdown("---")
    
    st.subheader("2. 실시간 데이터 수집 및 판단 지표 (Data Pipeline)")
    
    col1, col2 = st.columns(2)
    with col1:
        st.info("**📈 거시 지표 (Macro Indicators)**")
        st.markdown(f"""
        * **시장 벤치마크 지수 (Market Strength):** 대형주는 KOSPI, 중소형주는 KOSDAQ 지수의 최근 60일 수익률을 계산하여 강세장 여부를 판별하고, 개별 종목의 상대강도(RS)를 비교하는 벤치마크로 사용합니다.
        * **시장 공포지수 (VIX):** 
            * `안정/경계 구간`: VIX < 30 (정상적인 매매 작동)
            * `극단적 공포 (VIX Contrarian)`: VIX $\ge$ 25 이상 고점 도달 후 3일 이동평균선을 하향 이탈할 때 (공포 절정 후 반등 시그널)
        """)
    with col2:
        st.success("**🔎 개별 종목 지표 (Micro Indicators)**")
        st.markdown(f"""
        * **20일/60일/200일 이동평균선:** 단기, 중기, 장기 추세를 정의합니다. 특히 200일선은 대장기 추세를 필터링하는 핵심 방어선입니다.
        * **60일선 기울기 (Slope):** 10일 전의 60일선 값과 비교하여 추세가 위를 향하고 있는지 수치화합니다.
        * **거래량 폭발 (Volume Surge):** 당일 거래량이 최근 5일 평균 거래량 대비 150% 이상 급증했는지 파악하여 세력(수급) 개입을 추적합니다.
        """)

    st.markdown("---")
    
    st.subheader("3. 진입 및 매수 알고리즘 (Entry Rules)")
    st.markdown("대형주(Core)와 중소형주(Satellite)는 시장 특성에 맞춰 완전히 분리된 진입 로직을 사용합니다.")
    
    st.markdown(f"""
    **[대형주 (Core) 전략 4대 필터]**
    1. **대장기 하락장 차단:** 주가가 200일선 위에 위치해야 함.
    2. **가짜 반등 차단:** 단기 20일선이 중기 60일선을 단순 교차를 넘어 **버퍼 이상 확실하게 돌파**해야 함.
    3. **진짜 추세 검증:** 60일선 우상향(Slope > 0) 및 최근 20일 모멘텀 양수(> 0).
    4. **거시적 승인:** VIX가 30 미만이거나 VIX 역발상 바닥 시그널 발생.
    
    **[중소형주 (Satellite) 전략 전용 필터: 1+2+3 동시 만족 시 진입]**
    1. **유동성 및 수급 폭발 이력:** KOSDAQ 시가총액 1000억 이상 유동성 종목 중, 최근 20일 내 거래량이 평소 대비 200% 이상 폭발한 이력이 있어야 함.
    2. **눌림목(Dip-Buying) 타점:** 수급이 터진 주도주가 조정을 받아 주가가 **20일선 부근(-5% ~ +3% 또는 당일 저가 20일선 터치)**에 안착했을 때 진입.
    3. **하락장 및 투매 방어:** 200일선(옵션)을 상회해야 하며, 최근 단기 고점 대비 -30% 이상 무너진 폭락 종목은 제외.
    """)
    
    st.markdown("---")
    
    st.subheader("4. 자산 배분 및 자본금 증감액 룰 (Capital & Weight Allocation)")
    
    st.markdown(f"""
    * **기본 배분 및 부스터:** 한 종목 최대 투입 비중은 사이드바 파라미터에 따르며, 벤치마크 지수가 60일선 위에 있는 대세 상승장에서는 최대 투입 한도를 **1.5배** 강제로 상향합니다.
    * **알파 점수 부여 (Score-Tilt):** 수급 폭발(+0.5), 시장 주도주(+0.5), VIX 바닥잡기(+1.0) 조건 만족 시 가점를 부여하여 주도주에 자금을 싹쓸이(Overweight) 합니다.
    * **자본 증액 리밸런싱 (Capital Inflow):** 사이드바의 설정 운용 자금이 늘어나면, **이미 보유 중인 주도주라도 새로 늘어난 한도(Target Weight)만큼 정확히 계산하여 추가 매수를 지시**합니다.
    * **자본 감액 최약체 청산 (Deficit Liquidation):** 운용 자본이 현재 주식 평가액보다 낮게 감액 설정될 경우, 부족한 현금을 마련하기 위해 **'AI 스코어 하위 ➔ 20일 모멘텀 하위'** 순서로 가장 부진한 종목부터 기계적으로 부분/전량 매도 지시를 내립니다.
    """)

    st.markdown("---")
    
    st.subheader("5. 리스크 관리 및 청산 알고리즘 (Exit Rules)")
    
    st.warning("**🔻 매도 및 방어 규정**")
    st.markdown(f"""
    * **추세 이탈 (데드크로스):** 20일선이 60일선을 하향 이탈하되, 잦은 손절을 막기 위해 버퍼의 절반 이상 뚫고 내려갈 때 전량 매도합니다.
    * **트레일링 스탑 익절 (Trailing Stop):** 지정된 목표 수익률 도달 이후부터 룰이 켜집니다. 이후 최고점 대비 허용 하락폭 이상 떨어지면 즉시 수익을 확정 짓습니다.
    * **연속 손실 쿨다운 (Cooldown):** 횡보 박스권에 갇혀 연속으로 2회 손실을 발생시킨 종목은 지정된 기간 동안 강제로 매수를 금지(격리)시켜 수수료 누수를 막습니다.
    * **최소 보유 기간:** 한 번 매수하면 잔파도에 털리지 않도록 일정 기간 강제로 홀딩합니다. (단, 매수가 대비 손절컷 도달 시 즉각 전량 매도 탈출)
    """)
