import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import altair as alt
import json
import datetime
import re
import requests
import gspread
from google.oauth2.service_account import Credentials
import warnings
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="🚀",
    layout="wide"
)

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **가상 매매 성과 추적기(Forward Test)**, **구글 시트 영구 DB 연동**, **가상/실계좌 탭 분리**를 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 0. 구글 스프레드시트 DB 연동 로직
# ==========================================
SPREADSHEET_ID = "1hFPs2y8UipaWHfM_VVgAqsq566HnHQLBONSwBX28TQ0"

@st.cache_resource
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["google_sheets_json"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client

def load_all_portfolios_from_sheets():
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SPREADSHEET_ID)
        try:
            worksheet = sh.worksheet("Portfolios")
        except:
            worksheet = sh.add_worksheet(title="Portfolios", rows=100, cols=2)
            worksheet.append_row(["Name", "JSON_Data"])
            
        records = worksheet.get_all_records()
        port_dict = {}
        for r in records:
            name = str(r.get("Name", "")).strip()
            data_str = r.get("JSON_Data")
            if name and data_str:
                try:
                    port_dict[name] = json.loads(data_str)
                except:
                    pass
        return port_dict
    except Exception as e:
        st.error(f"구글 시트 데이터 로드 오류: {e}")
        return {}

def save_portfolio_to_sheets(name, p_data):
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SPREADSHEET_ID)
        worksheet = sh.worksheet("Portfolios")
        
        cell = worksheet.find(name)
        data_str = json.dumps(p_data, ensure_ascii=False)
        
        if cell:
            worksheet.update_cell(cell.row, 2, data_str)
        else:
            worksheet.append_row([name, data_str])
    except Exception as e:
        st.error(f"구글 시트 데이터 저장 오류: {e}")

def delete_portfolio_from_sheets(name):
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SPREADSHEET_ID)
        worksheet = sh.worksheet("Portfolios")
        cell = worksheet.find(name)
        if cell:
            worksheet.delete_rows(cell.row)
    except Exception as e:
        st.error(f"구글 시트 데이터 삭제 오류: {e}")

# ==========================================
# 한국투자증권 Open API 연동 로직 (에러 완벽 수정본)
# ==========================================
@st.cache_data(ttl=43200, show_spinner=False)
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
        "tr_id": tr_id,
        "custtype": "P" # 에러 방지용 필수 헤더 추가
    }
    
    # INVALID_CHECK_ACNO 에러를 완벽히 막기 위해 cano는 앞 8자리만 강제 슬라이싱
    safe_cano = str(cano).replace("-", "")[:8]
    
    params = {
        "CANO": safe_cano, 
        "ACNT_PRDT_CD": str(acnt_prdt_cd), 
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "", 
        "INQR_DVSN": "02", 
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", 
        "FNCG_AMT_AUTO_RDPT_YN": "N", 
        "PRCS_DVSN": "01", 
        "CTX_AREA_FK100": "", 
        "CTX_AREA_NK100": ""
    }
    
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('rt_cd') == '0':
                return data.get('output1', []), data.get('output2', [])
            else:
                st.error(f"API 응답 오류: {data.get('msg1')} (에러코드: {data.get('msg_cd')})")
    except Exception as e:
        st.error(f"잔고 조회 통신 오류: {e}")
    return None, None

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
def run_satellite_scanner(use_ma200_filter_flag, top_n=5):
    results = []
    krx = load_krx_universe()
    try:
        kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)]
        if 'Marcap' in kosdaq.columns:
            kosdaq = kosdaq[kosdaq['Marcap'] >= 100000000000]
            candidates = kosdaq.sort_values('Marcap', ascending=False).head(100)
        else:
            candidates = kosdaq.head(100)
    except: return pd.DataFrame()

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
        
        trend_pass = ma60_slope_positive and (ret_20 > -3.0)
        
        if vol_surged and is_dip and ma200_pass and dd_pass and trend_pass:
            score = (recent_vol_max / 100.0) * 0.4 + (ret_60 * 0.3) + (ret_20 * 0.3)
            
            results.append({
                '종목명': name, '티커': code, '현재가': f"{c_price:,.0f} 원",
                '20일선 이격도': f"{dist_ma20:+.2f}%", 
                '최근 최대 수급': f"{recent_vol_max:,.0f}%",
                'AI 스코어': round(score, 2),
                '_score_num': score
            })
            
    if not results:
        return pd.DataFrame()
        
    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values('_score_num', ascending=False).head(top_n)
    df_res = df_res.drop(columns=['_score_num'])
    return df_res

# [핵심 통합] 독립된 시뮬레이션 계산 엔진
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

all_ports = load_all_portfolios_from_sheets()
port_names = list(all_ports.keys())

selected_port = None
p_data = None

if port_names:
    selected_port = st.sidebar.selectbox("구글 시트 DB 목록", port_names)
    p_data = all_ports.get(selected_port)
    
    active_strat = p_data.get('strategy', '대형주 (Core)') if p_data else "대형주 (Core)"

    if p_data:
        st.sidebar.markdown("---")
        st.sidebar.subheader("💰 Virtual Capital & Settings")
        
        st.sidebar.markdown(f"**현재 설정 전략:** `{active_strat}`")
        
        new_cash = st.sidebar.number_input(
            f"총 투자 운용 자산 (증/감액)", 
            value=int(p_data.get('cash', 10000000)), 
            step=1_000_000, 
            format="%d"
        )
        
        if new_cash != int(p_data.get('cash', 10000000)):
            p_data['cash'] = new_cash
            save_portfolio_to_sheets(selected_port, p_data)
            st.rerun()
                
        st.sidebar.caption(f"💵 가상 설정 금액: **{new_cash:,.0f} 원**")
        
        with st.sidebar.popover(f"🗑️ '{selected_port}' 삭제", use_container_width=True):
            st.markdown("⚠️ **경고: 정말 삭제하시겠습니까?**<br>구글 시트 DB에서 영구적으로 삭제되며 복구할 수 없습니다.", unsafe_allow_html=True)
            if st.button("🚨 네, 영구 삭제합니다", key=f"del_{selected_port}", type="primary", use_container_width=True):
                delete_portfolio_from_sheets(selected_port)
                st.sidebar.success(f"✅ 완전히 삭제되었습니다.")
                st.rerun()
else:
    st.sidebar.info("👈 구글 시트에 저장된 포트폴리오가 없습니다. 아래에서 새로 추가해 주세요.")
    active_strat = "대형주 (Core)"

st.sidebar.markdown("---")
st.sidebar.subheader("➕ 새 가상 포트폴리오 추가")
new_p_name = st.sidebar.text_input("새 포트폴리오 이름 (특수문자 제외)")
new_p_strat = st.sidebar.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"])
new_p_cash = st.sidebar.number_input("초기 총 투자금", value=10_000_000, step=1_000_000, format="%d", key="new_cash_input")

if st.sidebar.button("새 포트폴리오 생성하기", use_container_width=True):
    if new_p_name:
        safe_name = re.sub(r'[\\/*?:"<>|]', "", new_p_name)
        if safe_name in all_ports:
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
            save_portfolio_to_sheets(safe_name, new_data)
            st.rerun()

# ==========================================
# 전략 동기화 KIS API 로드
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")

SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = None, None, None, None, True

if p_data:
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
        st.sidebar.warning(f"🔑 **KIS API 미연동**\n\nStreamlit Cloud `Secrets`에 `[kis_accounts.{kis_secret_key}]` 정보를 등록해주세요.")
else:
     st.sidebar.info("👈 포트폴리오를 선택하면 실계좌가 자동 매칭됩니다.")

# ==========================================
# 시장 상황판 (신호등 UI)
# ==========================================
vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60 = fetch_market_data()

st.sidebar.markdown("---")
st.sidebar.header("🚥 현재 시장 상태 (Market Status)")
vix_color = "🟢" if vix_safe else ("🔥" if vix_contrarian else "🔴")
kp_color = "🟢" if kospi_ret_60 > 0 else "🔴"
kq_color = "🟢" if kosdaq_ret_60 > 0 else "🔴"

st.sidebar.markdown(f"{vix_color} **VIX 공포지수:** {vix_val:.2f} ({'바닥 반등' if vix_contrarian else ('안정' if vix_safe else '경계')})")
st.sidebar.markdown(f"{kp_color} **KOSPI (대형주):** {kospi_ret_60:+.2f}%")
st.sidebar.markdown(f"{kq_color} **KOSDAQ (중소형):** {kosdaq_ret_60:+.2f}%")

# ==========================================
# 파라미터 세팅
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
    st.header("📝 가상 포트폴리오 샌드박스 (Google Sheets DB 연동)")
    st.caption("수동으로 종목을 관리하고 진단하는 공간입니다. 변경사항은 구글 스프레드시트에 영구 저장됩니다.")
    
    if not p_data or not selected_port:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 생성하거나 선택하세요.")
    else:
        current_strategy = p_data.get('strategy', '대형주 (Core)')
        total_cash = p_data.get('cash', 10000000)
        stocks_df = pd.DataFrame(p_data.get('stocks', []))
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
                    save_portfolio_to_sheets(selected_port, p_data)
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
                    save_portfolio_to_sheets(selected_port, p_data)
                    st.rerun()

        with col_src2:
            st.markdown("**[🔍 실시간 AI 타점 스캐너]**")
            if current_strategy == '대형주 (Core)':
                if st.button("🚀 KOSPI 우량주 골든크로스 탐색", type="primary", use_container_width=True):
                    st.session_state.show_scanner = True
            else:
                if st.button("🚀 KOSDAQ 주도주 Top 5 눌림목 탐색", type="primary", use_container_width=True):
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
                                    save_portfolio_to_sheets(selected_port, p_data)
                                    st.rerun() 
                    else:
                        st.warning("⚠️ 현재 조건(200일선 방어 및 골든크로스 전환)을 완벽히 만족하는 대형 우량주가 없습니다.")
            else:
                with st.spinner("코스닥 시가총액 상위 100종목 중 최상위 눌림목 5선 분석 중... (약 15초 소요)"):
                    scan_result = run_satellite_scanner(use_ma200_filter)
                    if not scan_result.empty:
                        st.success(f"✅ AI가 스코어 기준 최상위 주도주 눌림목 {len(scan_result)}개를 엄선했습니다!")
                        hc1, hc2, hc3, hc4, hc5 = st.columns([2.5, 1.5, 1.5, 2, 2])
                        hc1.write("**종목명 (티커)**")
                        hc2.write("**현재가**")
                        hc3.write("**20일선 이격도**")
                        hc4.write("**AI 스코어 (수급/모멘텀)**")
                        hc5.write("**가상 포트 추가**")
                        st.markdown("---")
                        
                        for idx, row in scan_result.iterrows():
                            c1, c2, c3, c4, c5 = st.columns([2.5, 1.5, 1.5, 2, 2])
                            ticker = row['티커']
                            name = row['종목명']
                            
                            c1.write(f"**{name}** (`{ticker}`)")
                            c2.write(row['현재가'])
                            c3.write(row['20일선 이격도'])
                            c4.write(f"⭐ **{row['AI 스코어']}점** (수급 {row['최근 최대 수급']})")
                            
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
                                    save_portfolio_to_sheets(selected_port, p_data)
                                    st.rerun() 
                    else:
                        st.warning("⚠️ 현재 조건(수급 폭발 + 60일선 우상향 + 20일선 눌림목)을 엄격히 만족하는 최상위 주도주가 없습니다.")
        
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
                        
                        save_portfolio_to_sheets(selected_port, p_data)
                        st.rerun()

        with col_manage2:
            st.markdown("**[🗑️ 종목 삭제]**")
            if not stocks_df.empty:
                del_options = stocks_df['종목명'].tolist()
                del_selected = st.selectbox("삭제할 종목 선택", del_options, key=f"del_sel_{selected_port}")
                if st.button("선택 종목 삭제하기", key=f"del_btn_{selected_port}", use_container_width=True):
                    stocks_df = stocks_df[stocks_df['종목명'] != del_selected]
                    p_data['stocks'] = stocks_df.to_dict(orient='records')
                    
                    save_portfolio_to_sheets(selected_port, p_data)
                    st.rerun()
            else:
                st.caption("현재 등록된 종목이 없습니다.")

        st.markdown("---")
        st.markdown("**가상 포트폴리오 내역 (매수단가 및 보유수량 테스트 입력)**")
        
        edited_df = st.data_editor(
            stocks_df, num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}"
        )
        
        st.markdown("""<style>[data-testid="stPopover"] { width: 100%; }</style>""", unsafe_allow_html=True)
        
        col_qsave1, col_qsave2 = st.columns([1, 1])
        with col_qsave1:
            with st.popover("💾 표 데이터 수정 후 덮어쓰기 (Quick Save)", use_container_width=True):
                st.markdown("⚠️ **현재 입력하신 내용을 구글 시트 DB에 덮어씁니다.**<br>정말 저장하시겠습니까?", unsafe_allow_html=True)
                if st.button("✔️ 네, 덮어쓰기 저장합니다.", key=f"save_{selected_port}", type="primary", use_container_width=True):
                    p_data['stocks'] = edited_df.to_dict(orient='records')
                    save_portfolio_to_sheets(selected_port, p_data)
                    st.success("✅ 구글 시트 DB에 안전하게 저장되었습니다!")
                    
        with col_qsave2:
            with st.popover("📄 새 이름으로 복사하기 (Save As)", use_container_width=True):
                save_filename = st.text_input("새 파일명", value=f"{selected_port}_복사본", key=f"save_as_input_{selected_port}")
                if st.button("복사본 생성하기", type="primary", key=f"save_as_btn_{selected_port}"):
                    safe_new_name = re.sub(r'[\\/*?:"<>|]', "", save_filename)
                    if safe_new_name in all_ports:
                        st.warning("이미 존재하는 이름입니다.")
                    else:
                        new_copied_data = p_data.copy()
                        save_portfolio_to_sheets(safe_new_name, new_copied_data)
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

                        buy_price_val = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                        if is_holding and buy_price_val > 0:
                            target_p = buy_price_val * (1 + ts_target_pct / 100.0)
                            stop_p = buy_price_val * (1 + sat_stop_loss / 100.0)
                            target_str = f"{target_p:,.0f} 원"
                            stop_str = f"{stop_p:,.0f} 원"
                        else:
                            target_str = "-"
                            stop_str = "-"

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
                            else: # 미보유
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
                                                detail = f"[{tech_text}] | [{vix_status}]\n➔ 20일선 타점은 도달했으나 최근 폭발적인 수급 이력이 없습니다."
                                        else:
                                            action = "🟡 진입 보류 (현금 부족)"
                                            detail = f"[{tech_text}]\n➔ 타점은 좋으나 가용 현금이 부족합니다."
                                    else: 
                                        action = "🟡 관망 (타점 대기)"
                                        detail = f"[{tech_text}] | [{ma200_status}]\n➔ 현재 가격이 20일선에서 멀리 떨어져 있습니다."
                                
                        results.append({
                            '종목명': s_name, '상태': holding_status, '현재가': f"{c_price:,.0f} 원",
                            '권장 익절가': target_str, '강제 손절가': stop_str,
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
            with st.spinner("한투증권 API 서버와 실시간 잔고 통신 중..."):
                token = get_kis_access_token(SYS_APP_KEY, SYS_APP_SECRET, is_mock=SYS_IS_MOCK)
                if token:
                    holdings, summary = fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, token, is_mock=SYS_IS_MOCK)
                    if holdings is not None and summary is not None:
                        tot_evlu = float(summary[0].get('tot_evlu_amt', 0)) if summary else 0
                        
                        imported = []
                        for item in holdings:
                            qty = int(item.get('hldg_qty', 0))
                            if qty > 0:
                                kis_prpr = float(item.get('prpr', 0)) 
                                kis_pchs = float(item.get('pchs_avg_pric', 0)) 
                                kis_profit_rt = float(item.get('evlu_pfls_rt', 0)) 
                                
                                imported.append({
                                    '종목명': item.get('prdt_name', ''),
                                    '티커': item.get('pdno', ''),
                                    '실시간 현재가': f"{kis_prpr:,.0f} 원",
                                    '매수평균가': f"{kis_pchs:,.0f} 원",
                                    '보유수량': f"{qty:,} 주",
                                    '평가손익률': f"{kis_profit_rt:+.2f}%",
                                    '_raw_price': kis_prpr, 
                                    '_raw_buy': kis_pchs,
                                    '_raw_qty': qty
                                })
                        st.session_state[cache_key] = {'total_eval': tot_evlu, 'stocks': imported}
                        st.toast("✅ 한투증권 실시간 잔고/현재가 동기화 완료!")

        kis_data = st.session_state.get(cache_key)
        if kis_data:
            real_total_eval = kis_data['total_eval']
            real_stocks_df = pd.DataFrame(kis_data['stocks'])
            
            st.metric("💰 계좌 총 평가 금액 (현금+주식)", f"{real_total_eval:,.0f} 원")
            
            st.markdown("### 📊 실계좌 보유 종목 리스트 (한투증권 실시간 시세 기준)")
            if real_stocks_df.empty:
                st.info("현재 이 계좌에 보유 중인 주식이 없습니다.")
            else:
                display_df = real_stocks_df[['종목명', '티커', '실시간 현재가', '매수평균가', '보유수량', '평가손익률']]
                st.dataframe(display_df, use_container_width=True)

            st.markdown("---")
            st.subheader("🩺 실전 계좌 AI 매매 진단기")
            
            if st.button("🚀 실전 계좌 종목 진단 실행", type="secondary"):
                if real_stocks_df.empty:
                    st.warning("진단할 보유 종목이 없습니다.")
                elif not p_data:
                    st.error("좌측 사이드바에서 비교 기준이 될 '가상 포트폴리오(전략)'를 먼저 선택해 주세요.")
                else:
                    with st.spinner("실계좌 종목 퀀트 필터링 분석 중..."):
                        current_strategy = p_data.get('strategy', '대형주 (Core)')
                        market_ret_60 = kospi_ret_60 if current_strategy == '대형주 (Core)' else kosdaq_ret_60
                        buf = whipsaw_buffer / 100.0

                        live_results = []
                        for idx, row in real_stocks_df.iterrows():
                            s_ticker = row['티커']
                            s_name = row['종목명']
                            buy_price = float(row.get('_raw_buy', 0))
                            
                            c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_vol_max = fetch_stock_status(s_ticker)
                            
                            live_c_price = float(row.get('_raw_price', c_price if c_price else 0))
                            if live_c_price == 0: continue

                            rs_strong = ret_60 > market_ret_60
                            rs_status = f"상대강도 우위" if rs_strong else "상대강도 열위"
                            
                            diff_ma = ((ma20 / ma60) - 1) * 100 if (ma20 and ma60) else 0.0
                            dist_ma20 = ((live_c_price / ma20) - 1) * 100 if ma20 else 0.0
                            
                            tech_text = f"20/60선 이격 {diff_ma:+.2f}%" if current_strategy == '대형주 (Core)' else f"20일선 이격 {dist_ma20:+.2f}%"

                            if current_strategy == '대형주 (Core)':
                                if ma20 and ma60 and (ma20 >= ma60 * (1 - buf/2)): 
                                    action = "🟢 보유 유지"
                                    detail = f"[{tech_text}] | [{rs_status}]\n➔ 정배열 추세 유지 중."
                                else: 
                                    action = "🔴 즉각 매도 (추세 이탈)"
                                    detail = f"[{tech_text}] | [{rs_status}]\n➔ 데드크로스 발생으로 추세 이탈. 현금화 권장."
                            else:
                                user_ret = ((live_c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                                tech_text_sat = f"실수익률 {user_ret:+.2f}%"
                                
                                if user_ret <= (sat_stop_loss): 
                                    action = "🔴 강제 손절 집행"
                                    detail = f"[{tech_text_sat}]\n➔ 긴급 손절선({sat_stop_loss}%) 이탈로 하드 컷 권장."
                                elif ma20 and ma60 and (ma20 >= ma60 * (1 - buf/2)):
                                    action = "🟢 보유 유지"
                                    detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 정배열 추세 홀딩 구간."
                                else:
                                    action = "🔴 전량 매도"
                                    detail = f"[{tech_text_sat}]\n➔ 20일선 데드크로스로 주도주 대열 이탈."
                                    
                            if buy_price > 0:
                                target_p = buy_price * (1 + ts_target_pct / 100.0)
                                stop_p = buy_price * (1 + sat_stop_loss / 100.0)
                                target_str = f"{target_p:,.0f} 원"
                                stop_str = f"{stop_p:,.0f} 원"
                            else:
                                target_str = "-"
                                stop_str = "-"

                            live_results.append({
                                '보유 종목명': s_name, 
                                '한투 실시간 현재가': f"{live_c_price:,.0f} 원",
                                '권장 익절가': target_str,
                                '강제 손절가': stop_str,
                                'AI 액션 플랜 (권장)': action, 
                                '상세 판단 근거': detail
                            })
                        
                        st.table(pd.DataFrame(live_results))

with tab3:
    st.header("🧪 시뮬레이션 및 백테스트 (Simulation & Backtest)")
    st.markdown("이곳은 **가상 샌드박스의 관심 종목** 및 **전략 유니버스**를 대상으로 AI 알고리즘의 성과를 검증하는 공간입니다.")

    if not p_data or not selected_port:
        st.warning("포트폴리오가 없습니다.")
    else:
        stocks_df = pd.DataFrame(p_data.get('stocks', []))
        current_strategy = p_data.get('strategy', '대형주 (Core)')
        total_cash = p_data.get('cash', 10000000)
        
        st.subheader("📈 [Forward Test] 가상 샌드박스 관심 종목 단기 성과 추적")
        created_str = p_data.get('created_at', (datetime.date.today() - datetime.timedelta(days=90)).strftime('%Y-%m-%d'))
        created_dt = datetime.datetime.strptime(created_str, "%Y-%m-%d").date()
        
        col_ft1, col_ft2 = st.columns([1, 2])
        with col_ft1:
            ft_start = st.date_input("가상 운용 시작일", value=created_dt)
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
        
        st.subheader("📊 [Backtest] AI 전략 유니버스 장기 초과수익 검증")
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
                st.info("💡 **[AI 스캐너 시뮬레이션 모드]** 중소형주 전략 백테스트 시 **섹터별 코스닥 우량주 45선**이 자동 적용됩니다.")
            else:
                sim_stocks = stocks_df.copy()

            if sim_stocks.empty:
                st.error("종목이 없습니다.")
            else:
                with st.spinner(f"산식 보정된 {index_name} 벤치마크 및 스캐너 동기화 백테스트 구동 중..."):
                    bt_result = run_quant_simulation(
                        sim_stocks=sim_stocks, strat=current_strategy, init_cash=total_cash,
                        start_date=start_date, end_date=end_date, use_ma200_filter=use_ma200_filter,
                        whipsaw_buffer=whipsaw_buffer, sat_stop_loss=sat_stop_loss, max_alloc_pct=max_alloc_pct,
                        min_hold_days=min_hold_days, ts_target_pct=ts_target_pct, ts_drop_pct=ts_drop_pct,
                        bull_market_boost=bull_market_boost, cooldown_days=cooldown_days
                    )
                    
                    if bt_result:
                        st.success(f"✅ 백테스트 실행 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 초기 자산", f"{total_cash:,.0f} 원")
                        col_r2.metric(f"AI 초과수익 전략 최종 기말 자산 (수익률)", f"{bt_result['final_asset']:,.0f} 원", f"{bt_result['final_port_ret']:+.2f}%")
                        
                        st.markdown("---")
                        st.subheader(f"📊 [전략 비교] {index_name} 지수 vs 단순보유 vs 적립식 매수 vs AI 초과수익 전략")
                        
                        comparison_data = [
                            {
                                '전략 구분': '🚀 AI 초과수익 전략 (200D Filter + Cooldown)',
                                '최종 기말 자산': f"{bt_result['final_asset']:,.0f} 원",
                                '총 수익률': f"{bt_result['final_port_ret']:+.2f}%",
                                '운용 방식 및 특징': f'200일선 아래 하락 종목 매수 금지 및 연속 손실 종목 {cooldown_days}일 매수 동결.'
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
                                '운용 방식 및 특징': '동일 종목 풀 초기 전액 동일 비중 매수 후 홀딩'
                            },
                            {
                                '전략 구분': '💰 적립식 매수 (DCA)',
                                '최종 기말 자산': f"{bt_result['final_dca_asset']:,.0f} 원",
                                '총 수익률': f"{bt_result['final_dca_ret']:+.2f}%",
                                '운용 방식 및 특징': '매월 정기 추가 투입으로 매입단가 분산'
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
    st.markdown("본 대시보드에 탑재된 AI 퀀트 엔진은 시장 거시 지표(Macro), 개별 종목 모멘텀(Micro), 그리고 리스크 관리(Risk Management)가 통합된 기관급 다이내믹 자산 배분 알고리즘을 사용합니다.")
    st.divider()
    
    st.markdown("""
    ### 1. 📊 핵심 투자 철학: Core-Satellite 전략
    본 알고리즘의 가장 큰 특징은 자산을 성격이 다른 두 개의 독립된 엔진(계좌)으로 완전히 분리하여 운용한다는 점입니다. 이를 통해 '안정성'과 '수익성'이라는 두 마리 토끼를 동시에 잡습니다.

    * **Core (핵심 자산 - 대형주):** 전체 자산의 70~80%를 배분하며, 시장의 장기적인 우상향(Beta)을 안정적으로 추종합니다. 주요 13개 섹터 1위 기업 위주로 구성하여 변동성을 최소화합니다.
    * **Satellite (위성 자산 - 중소형주):** 전체 자산의 20~30%를 배분하며, 강력한 모멘텀을 가진 45개 테마/중소형주에 집중 투자하여 초과 수익(Alpha)을 극대화합니다.

    ### 2. 🔍 주식 발굴 방식 (다중 팩터 스코어링)
    종목을 선정할 때는 단순한 감이나 뉴스가 아닌, 철저히 검증된 **퀀트 팩터(Quant Factor)** 데이터를 혼합하여 점수를 매깁니다.

    * **가치 팩터 (Value):** PER, PBR이 동종 업계 대비 낮아 본질 가치보다 저평가된 주식을 찾습니다.
    * **퀄리티 팩터 (Quality):** ROE, 영업이익률, 부채비율을 분석하여 튼튼하고 돈을 잘 버는 우량 기업을 선별합니다.
    * **모멘텀 팩터 (Momentum):** 최근 3~6개월간 주가 상승 추세가 뚜렷하고 거래량이 동반된 주식을 찾습니다. "오르는 주식이 더 오른다"는 관성 효과를 이용합니다.

    ### 3. ⚙️ 자산 운용 규칙 및 시스템 설계
    * **독립 계좌 운영 (API 분리):** Core와 Satellite 전략이 서로 간섭하지 않도록 계좌와 API 키를 물리적으로 완벽히 분리하여 운영합니다. 
    * **구글 시트 영구 DB 연동:** 포트폴리오의 모든 변경 내역, 종목 구성, 투자금은 구글 스프레드시트에 실시간으로 영구 저장됩니다.
    * **정기 리밸런싱 (Rebalancing):** 월말 또는 분기 말 등 정해진 주기에 따라 자산 비중을 원래 목표대로 되돌려, 자연스러운 'Buy Low, Sell High'를 자동 실행합니다.

    ### 4. 🛡️ 리스크 관리 근거 (Drawdown 방어)
    * **동적 현금 비중 조절:** 시장 전체의 추세가 하락장으로 꺾일 경우, 주식 비중을 기계적으로 줄이고 현금 비중을 늘립니다.
    * **하드 스탑로스 (고정 손절매):** 개별 종목이 매수가 대비 특정 비율(예: -10%) 이상 하락하면 즉각 청산하여 계좌 전체의 치명적인 손실을 차단합니다.
    """)
