import streamlit as st
import streamlit.components.v1 as components
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
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **오토파일럿 무인 감시**, **가상/실계좌 연동**, **시뮬레이션**을 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 텔레그램 메시지 발송 함수
# ==========================================
def send_telegram_message(message):
    try:
        tg_token = st.secrets.get("telegram", {}).get("bot_token")
        tg_chat_id = st.secrets.get("telegram", {}).get("chat_id")
        
        if not tg_token or not tg_chat_id: return False, "Secrets에 텔레그램 정보가 없습니다."
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        payload = {"chat_id": tg_chat_id, "text": message, "parse_mode": "Markdown"}
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200: return True, "발송 성공"
        else: return False, f"API 오류: {res.text}"
    except Exception as e:
        return False, str(e)

# ==========================================
# 0. 구글 스프레드시트 DB 연동 로직
# ==========================================
SPREADSHEET_ID = "1hFPs2y8UipaWHfM_VVgAqsq566HnHQLBONSwBX28TQ0"

@st.cache_resource
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(dict(st.secrets["google_sheets_json"]), scopes=scopes)
    return gspread.authorize(creds)

def load_all_portfolios_from_sheets():
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SPREADSHEET_ID)
        try: worksheet = sh.worksheet("Portfolios")
        except:
            worksheet = sh.add_worksheet(title="Portfolios", rows=100, cols=2)
            worksheet.append_row(["Name", "JSON_Data"])
            
        records = worksheet.get_all_records()
        port_dict = {}
        for r in records:
            name, data_str = str(r.get("Name", "")).strip(), r.get("JSON_Data")
            if name and data_str:
                try: port_dict[name] = json.loads(data_str)
                except: pass
        return port_dict
    except Exception as e:
        st.error(f"구글 시트 로드 오류: {e}")
        return {}

def save_portfolio_to_sheets(name, p_data):
    try:
        client = get_gspread_client()
        worksheet = client.open_by_key(SPREADSHEET_ID).worksheet("Portfolios")
        cell = worksheet.find(name)
        data_str = json.dumps(p_data, ensure_ascii=False)
        if cell: worksheet.update_cell(cell.row, 2, data_str)
        else: worksheet.append_row([name, data_str])
    except Exception as e:
        st.error(f"구글 시트 저장 오류: {e}")

def delete_portfolio_from_sheets(name):
    try:
        worksheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Portfolios")
        cell = worksheet.find(name)
        if cell: worksheet.delete_rows(cell.row)
    except Exception as e:
        st.error(f"구글 시트 삭제 오류: {e}")

# ==========================================
# 한국투자증권 Open API 연동 로직
# ==========================================
@st.cache_data(ttl=43200, show_spinner=False)
def get_kis_access_token(app_key, app_secret, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200: return res.json().get("access_token")
    except: pass
    return None

@st.cache_data(ttl=30, show_spinner=False)
def fetch_kis_current_price(app_key, app_secret, ticker, token, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).strip()}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return float(res.json()['output']['stck_prpr'])
    except: pass
    return None

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-balance"
    tr_id = "VTTC8434R" if is_mock else "TTTC8434R"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    params = {"CANO": str(cano).replace("-", "").strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return res.json().get('output1', []), res.json().get('output2', [])
    except: pass
    return None, None

# ==========================================
# 1. 데이터 수집 & 백테스트 함수 모음
# ==========================================
@st.cache_data(ttl=86400)
def load_krx_universe():
    try: return fdr.StockListing('KRX').dropna(subset=['Code', 'Name'])
    except: return pd.DataFrame(columns=['Code', 'Name', 'Market', 'Marcap', 'Amount'])

@st.cache_data(ttl=1800)
def fetch_market_data():
    try:
        vix_df = yf.download("^VIX", period="3mo", progress=False)
        if isinstance(vix_df.columns, pd.MultiIndex): vix_df.columns = vix_df.columns.get_level_values(0)
        vix_close = vix_df['Close'].dropna()
        vix_val, vix_ma3 = float(vix_close.iloc[-1]), float(vix_close.rolling(3).mean().iloc[-1])
        vix_contrarian, vix_safe = (vix_val >= 25.0) and (vix_val < vix_ma3), (vix_val < 30.0)
        
        k_close = fdr.DataReader('KS11')['Close'].tail(61)
        kospi_ret_60 = ((float(k_close.iloc[-1]) / float(k_close.iloc[-60])) - 1) * 100 if len(k_close) >= 60 else 0.0
        
        kq_close = fdr.DataReader('KQ11')['Close'].tail(61)
        kosdaq_ret_60 = ((float(kq_close.iloc[-1]) / float(kq_close.iloc[-60])) - 1) * 100 if len(kq_close) >= 60 else 0.0
        
        return vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60
    except: return 20.0, False, True, 0.0, 0.0

@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code, live_price=None):
    try:
        for suffix in ['.KS', '.KQ']:
            df = yf.download(f"{ticker_code}{suffix}", period="2y", progress=False)
            if not df.empty and len(df) > 0:
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                close_prices, volumes = df['Close'].dropna(), df['Volume'].dropna()
                low_prices = df['Low'].dropna() if 'Low' in df.columns else close_prices
                if len(close_prices) == 0: continue
                
                current_price = float(live_price) if live_price and float(live_price) > 0 else float(close_prices.iloc[-1])
                current_low = float(live_price) if live_price and float(live_price) < float(low_prices.iloc[-1]) else float(low_prices.iloc[-1])
                    
                ma200 = float(close_prices.rolling(window=200).mean().iloc[-1]) if len(close_prices) >= 200 else current_price
                ma60 = float(close_prices.rolling(window=60).mean().iloc[-1]) if len(close_prices) >= 60 else current_price
                ma60_10d_ago = float(close_prices.rolling(window=60).mean().iloc[-11]) if len(close_prices) >= 70 else ma60
                ma20 = float(close_prices.rolling(window=20).mean().iloc[-1]) if len(close_prices) >= 20 else current_price
                recent_high = float(close_prices.tail(120).max())
                drawdown = ((current_price / recent_high) - 1) * 100 if recent_high > 0 else 0.0
                
                vol_5ma = float(volumes.tail(6).iloc[:-1].mean()) if len(volumes) >= 6 else float(volumes.iloc[-1])
                vol_ratio = (float(volumes.iloc[-1]) / vol_5ma * 100) if vol_5ma > 0 else 100.0
                
                ret_60 = ((current_price / float(close_prices.iloc[-60])) - 1) * 100 if len(close_prices) >= 60 else 0.0
                ret_20 = ((current_price / float(close_prices.iloc[-20])) - 1) * 100 if len(close_prices) >= 20 else 0.0
                
                vol_ratio_series = pd.Series(np.where(volumes.rolling(5).mean().shift(1) > 0, volumes / volumes.rolling(5).mean().shift(1) * 100, 100.0), index=volumes.index)
                recent_20d_vol_max = float(vol_ratio_series.tail(20).max())
                
                return (current_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, (ma60 > ma60_10d_ago), (current_price >= ma200), recent_20d_vol_max >= 200.0, current_low, recent_20d_vol_max)
    except: pass
    return None, None, None, None, None, None, None, None, False, False, False, None, 0.0

@st.cache_data(ttl=3600)
def run_core_scanner(use_ma200_filter_flag, buf_pct):
    results, krx, buf = [], load_krx_universe(), buf_pct / 100.0
    try: candidates = krx[krx['Market'] == 'KOSPI'].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in krx.columns else krx[krx['Market'] == 'KOSPI'].head(100)
    except: return []
    for _, row in candidates.iterrows():
        res = fetch_stock_status(row['Code'])
        if res[0] is None: continue
        if ((not use_ma200_filter_flag) or res[9]) and (res[3] >= res[2] * (1 + buf)) and res[8] and (res[7] > 0):
            results.append({'종목명': row['Name'], '티커': row['Code'], '현재가': f"{res[0]:,.0f} 원", '20/60선 이격': f"{((res[3] / res[2]) - 1) * 100:+.2f}%", '20일 모멘텀': f"{res[7]:+.2f}%", '진단 근거': "장기 추세선 방어 및 골든크로스 안착"})
    return pd.DataFrame(results)

@st.cache_data(ttl=3600)
def run_satellite_scanner(use_ma200_filter_flag, top_n=5):
    results, krx = [], load_krx_universe()
    try: 
        kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)]
        candidates = kosdaq[kosdaq['Marcap'] >= 100000000000].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in krx.columns else kosdaq.head(100)
    except: return pd.DataFrame()
    for _, row in candidates.iterrows():
        res = fetch_stock_status(row['Code'])
        if res[0] is None: continue
        dist_ma20 = ((res[0] / res[3]) - 1) * 100
        is_dip = ((-5.0 <= dist_ma20 <= 3.0) or ((res[11] <= res[3] * 1.01) and (res[0] >= res[3] * 0.95)))
        if res[10] and is_dip and ((not use_ma200_filter_flag) or res[9]) and (res[4] >= -30.0) and (res[8] and res[7] > -3.0):
            score = (res[12] / 100.0) * 0.4 + (res[6] * 0.3) + (res[7] * 0.3)
            results.append({'종목명': row['Name'], '티커': row['Code'], '현재가': f"{res[0]:,.0f} 원", '20일선 이격도': f"{dist_ma20:+.2f}%", '최근 최대 수급': f"{res[12]:,.0f}%", 'AI 스코어': round(score, 2), '_score_num': score})
    if not results: return pd.DataFrame()
    return pd.DataFrame(results).sort_values('_score_num', ascending=False).head(top_n).drop(columns=['_score_num'])

@st.cache_data(ttl=1800)
def run_quant_simulation(sim_stocks, strat, init_cash, start_date, end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days):
    index_sym = 'KS11' if strat == '대형주 (Core)' else 'KQ11'
    fetch_start = start_date - datetime.timedelta(days=300)
    market_df = pd.DataFrame()
    benchmark_ret_val, final_benchmark_asset = 0.0, init_cash
    
    try:
        bm_df = fdr.DataReader(index_sym, fetch_start, end_date)
        if not bm_df.empty:
            if bm_df.index.tz is not None: bm_df.index = bm_df.index.tz_localize(None)
            bm_df['Bm_Ret_60'] = bm_df['Close'] / bm_df['Close'].shift(60) - 1
            bm_df['Bm_MA60'] = bm_df['Close'].rolling(60).mean()
            market_df['Bm_Ret_60'] = bm_df['Bm_Ret_60']
            market_df['Bm_Bull'] = bm_df['Close'] > bm_df['Bm_MA60']
            
            sim_bm = bm_df[bm_df.index >= pd.to_datetime(start_date)]['Close'].dropna()
            if len(sim_bm) > 1:
                benchmark_ret_val = ((float(sim_bm.iloc[-1]) / float(sim_bm.iloc[0])) - 1) * 100
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
        ticker, name = row['티커'], row['종목명']
        df = None
        for suf in ['.KS', '.KQ']:
            temp_df = yf.download(f"{ticker}{suf}", start=fetch_start, end=end_date, progress=False)
            if not temp_df.empty:
                if isinstance(temp_df.columns, pd.MultiIndex): temp_df.columns = temp_df.columns.get_level_values(0)
                df = temp_df
                break
        if df is None or df.empty: continue
            
        df['Close'], df['Volume'] = df['Close'].ffill(), df['Volume'].ffill()
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
        
        if df.index.tz is not None: df.index = df.index.tz_localize(None)
        df = df.join(market_df, how='left')
        for col, default in [('VIX_Safe', True), ('VIX_Contrarian', False), ('Bm_Ret_60', 0.0), ('Bm_Bull', False)]: df[col] = df[col].fillna(default)
        
        ma200_cond = df['Is_Above_MA200'] if use_ma200_filter else True
        if strat == '대형주 (Core)':
            entry_cond = (ma200_cond & (df['MA20'] >= df['MA60'] * (1 + buf)) & df['MA60_Slope'] & (df['Ret_20'] > 0) & df['VIX_Safe']) | df['VIX_Contrarian']
            exit_cond = (df['MA20'] < df['MA60'] * (1 - buf/2)) & (~df['VIX_Contrarian'])
        else:
            df['Drawdown'] = (df['Close'] / df['Close'].rolling(window=120, min_periods=1).max()) - 1
            dist_ma20 = ((df['Close'] / df['MA20']) - 1) * 100
            is_dip = ((dist_ma20 >= -5.0) & (dist_ma20 <= 3.0)) | (df['Low'] <= df['MA20'] * 1.01 if 'Low' in df.columns else (dist_ma20 <= 0.0))
            entry_cond = (ma200_cond & ((is_dip & df['Vol_Surged']) | df['VIX_Contrarian'])) & (df['Drawdown'] >= -0.30)
            exit_cond = ((df['MA20'] < df['MA60'] * (1 - buf/2)) & (~df['VIX_Contrarian']))
        
        df['Signal'] = np.where(entry_cond, 1, np.where(exit_cond, 0, np.nan))
        df['Signal'] = df['Signal'].ffill().fillna(0)
        df['Score'] = np.where(entry_cond, 1.0 + np.where(df['Vol_Strong'], 1.0 if strat != '대형주 (Core)' else 0.5, 0.0) + np.where(df['Ret_60'] > df['Bm_Ret_60'], 0.5, 0.0) + np.where(df['VIX_Contrarian'], 1.0, 0.0), 0.0)
        
        stock_dfs[name] = df[df.index >= start_dt].copy()
        
    stock_dfs = {k: v for k, v in stock_dfs.items() if not v.empty}
    if not stock_dfs: return None
        
    all_indices = [df.index for df in stock_dfs.values()]
    common_index = max(all_indices, key=len)
        
    portfolio_history, history_records = [], []
    trade_stats = {name: {'buy': 0, 'sell': 0, 'fee': 0.0, 'realized_pnl': 0.0} for name in stock_dfs}
    shares, hold_days, max_invested, peak_price_since_buy, consecutive_losses, avg_buy_price, realized_pnl = ({name: 0 for name in stock_dfs} for _ in range(7))
    cooldown_until = {name: pd.Timestamp.min for name in stock_dfs}
    cash = init_cash
    
    base_alloc_ratio, ts_target, ts_drop = max_alloc_pct / 100.0, ts_target_pct / 100.0, ts_drop_pct / 100.0
    
    for i, date_val in enumerate(common_index):
        if i == 0:
            portfolio_history.append(init_cash)
            record = {'Date': date_val, '현금(Cash)': init_cash}
            for name in stock_dfs: record[name] = 0.0
            history_records.append(record)
            continue
            
        current_max_alloc_ratio = base_alloc_ratio
        if bull_market_boost and any(date_val in df.index and df.loc[date_val, 'Bm_Bull'] for df in stock_dfs.values()):
            current_max_alloc_ratio = min(base_alloc_ratio * 1.5, 1.0)
        
        for name in stock_dfs: hold_days[name] = hold_days[name] + 1 if shares[name] > 0 else 0
        
        active_stocks, scores = [], {}
        for name, df in stock_dfs.items():
            if date_val not in df.index: continue
            sig, c_price = df.loc[date_val, 'Signal'], df.loc[date_val, 'Close']
            if shares[name] == 0 and date_val < cooldown_until[name]: sig = 0.0
            
            force_exit = trailing_stop_exit = False
            if shares[name] > 0 and avg_buy_price[name] > 0:
                peak_price_since_buy[name] = max(peak_price_since_buy[name], c_price)
                if (c_price / avg_buy_price[name] - 1) >= ts_target and (c_price / peak_price_since_buy[name] - 1) <= ts_drop: trailing_stop_exit = True
                if strat != '대형주 (Core)' and (c_price / avg_buy_price[name] - 1) <= (sat_stop_loss / 100.0): force_exit = True
                
            if trailing_stop_exit or force_exit: sig = 0.0
            elif shares[name] > 0 and hold_days[name] < min_hold_days: sig = 1.0
                
            if sig == 1:
                active_stocks.append(name)
                scores[name] = max(df.loc[date_val, 'Score'], 1.0)
            else:
                peak_price_since_buy[name] = 0.0
                
        for name in stock_dfs:
            curr_sig, prev_sig = (1 if name in active_stocks else 0), (1 if shares[name] > 0 else 0)
            if curr_sig == 1 and prev_sig == 0: trade_stats[name]['buy'] += 1
            elif curr_sig == 0 and prev_sig == 1: trade_stats[name]['sell'] += 1
                
        total_asset = cash + sum(shares[name] * stock_dfs[name].loc[date_val, 'Close'] for name in stock_dfs if date_val in stock_dfs[name].index)
        
        if active_stocks:
            total_score = sum(scores.values()) or len(active_stocks)
            for name in stock_dfs:
                if date_val not in stock_dfs[name].index: continue
                c_price = stock_dfs[name].loc[date_val, 'Close']
                if name in active_stocks:
                    target_alloc = min(total_asset * (scores.get(name, 1.0) / total_score), total_asset * current_max_alloc_ratio)
                    diff_val = target_alloc - (shares[name] * c_price)
                    
                    if diff_val > 0: 
                        cost = min(diff_val, max(cash - (cash * 0.0025), 0))
                        if cost > 0:
                            fee = cost * 0.0025
                            cash -= (cost + fee)
                            avg_buy_price[name] = ((shares[name] * avg_buy_price[name]) + cost) / (shares[name] + cost / c_price) if shares[name] > 0 else c_price
                            shares[name] += cost / c_price
                            trade_stats[name]['fee'] += fee
                            realized_pnl[name] -= fee 
                            max_invested[name] = max(max_invested[name], shares[name] * c_price)
                            peak_price_since_buy[name] = max(peak_price_since_buy[name], c_price)
                    elif diff_val < 0: 
                        proceeds, fee = abs(diff_val), abs(diff_val) * 0.0025
                        realized_pnl[name] += (proceeds / c_price) * (c_price - avg_buy_price[name]) - fee
                        cash += (proceeds - fee)
                        shares[name] -= (proceeds / c_price)
                        trade_stats[name]['fee'] += fee
                else:
                    if shares[name] > 0: 
                        proceeds, fee = shares[name] * c_price, shares[name] * c_price * 0.0025
                        pnl = shares[name] * (c_price - avg_buy_price[name]) - fee
                        if pnl < 0:
                            consecutive_losses[name] += 1
                            if consecutive_losses[name] >= 2 and cooldown_days > 0: cooldown_until[name] = date_val + pd.Timedelta(days=cooldown_days)
                        else: consecutive_losses[name] = 0
                        realized_pnl[name] += pnl
                        cash += (proceeds - fee)
                        trade_stats[name]['fee'] += fee
                        shares[name], avg_buy_price[name], peak_price_since_buy[name] = 0.0, 0.0, 0.0
        else:
            for name in stock_dfs:
                if shares[name] > 0 and date_val in stock_dfs[name].index:
                    c_price, proceeds = stock_dfs[name].loc[date_val, 'Close'], shares[name] * stock_dfs[name].loc[date_val, 'Close']
                    fee, pnl = proceeds * 0.0025, shares[name] * (c_price - avg_buy_price[name]) - proceeds * 0.0025
                    if pnl < 0:
                        consecutive_losses[name] += 1
                        if consecutive_losses[name] >= 2 and cooldown_days > 0: cooldown_until[name] = date_val + pd.Timedelta(days=cooldown_days)
                    else: consecutive_losses[name] = 0
                    realized_pnl[name] += pnl
                    cash += (proceeds - fee)
                    trade_stats[name]['fee'] += fee
                    shares[name], avg_buy_price[name], peak_price_since_buy[name] = 0.0, 0.0, 0.0
                    
        final_eval = sum(shares[name] * stock_dfs[name].loc[date_val, 'Close'] for name in stock_dfs if date_val in stock_dfs[name].index)
        portfolio_history.append(max(cash + final_eval, 0))
        
        record = {'Date': date_val, '현금(Cash)': max(cash, 0)}
        for name in stock_dfs: record[name] = shares[name] * stock_dfs[name].loc[date_val, 'Close'] if date_val in stock_dfs[name].index else 0.0
        history_records.append(record)
        
    ai_portfolio_series = pd.Series(portfolio_history, index=common_index)
    bh_values, dca_values = {}, {}
    for name, df in stock_dfs.items():
        bh_values[name] = (df['Close'] / df['Close'].iloc[0]) * (init_cash / len(stock_dfs))
        n_months = len(df.groupby(df.index.to_period('M')))
        shares_acc = ((init_cash * 0.2) / len(stock_dfs)) / df['Close'].iloc[0]
        dca_list = []
        for d, r in df.iterrows():
            if d != df.index[0] and d.day <= 3 and d.month != df.index[df.index.get_loc(d)-1].month and n_months > 0:
                shares_acc += ((init_cash * 0.8 / n_months) / len(stock_dfs)) / r['Close']
            dca_list.append(shares_acc * r['Close'])
        dca_values[name] = pd.Series(dca_list, index=df.index)
        
    bh_df = pd.DataFrame(bh_values).sum(axis=1)
    dca_df = pd.DataFrame(dca_values).sum(axis=1)
    
    summary_rows, sum_hval, sum_prof, sum_fee, sum_bcnt, sum_scnt = [], 0, 0, 0, 0, 0
    final_asset = ai_portfolio_series.iloc[-1]
    
    for name in stock_dfs:
        c_price = stock_dfs[name].loc[stock_dfs[name].index[-1], 'Close']
        h_val, upnl = shares[name] * c_price, shares[name] * (c_price - avg_buy_price[name]) if shares[name] > 0 else 0.0
        t_prof = realized_pnl[name] + upnl
        inv_base = max_invested[name] if max_invested[name] > 0 else (init_cash / len(stock_dfs))
        b_cnt, s_cnt, fee = trade_stats[name]['buy'], trade_stats[name]['sell'], trade_stats[name]['fee']
        
        sum_hval += h_val; sum_prof += t_prof; sum_fee += fee; sum_bcnt += b_cnt; sum_scnt += s_cnt
        summary_rows.append({
            '종목명': name, '최종 보유 주수': f"{shares[name]:.2f} 주", '기말 평가금': f"{h_val:,.0f} 원",
            '총 순수익 (원)': f"{t_prof:+,.0f} 원", '수익률 (%)': f"{max((t_prof/inv_base)*100, -100.0):+.2f}%",
            '매매 횟수': f"매수 {b_cnt}회 / 매도 {s_cnt}회", '총 발생 수수료': f"{fee:,.0f} 원",
            '기말 포트폴리오 비중': f"{(h_val/final_asset)*100 if final_asset>0 else 0:.2f}%"
        })

    summary_rows.append({
        '종목명': '💡 [전체 합계]', '최종 보유 주수': '-', '기말 평가금': f"{sum_hval:,.0f} 원",
        '총 순수익 (원)': f"{sum_prof:+,.0f} 원 (자산대비 {(sum_prof/init_cash)*100 if init_cash>0 else 0:+.2f}%)",
        '수익률 (%)': '-', '매매 횟수': f"매수 {sum_bcnt}회 / 매도 {s_cnt}회",
        '총 발생 수수료': f"{sum_fee:,.0f} 원", '기말 포트폴리오 비중': f"{(sum_hval/final_asset)*100 if final_asset>0 else 0:.2f}%"
    })
    
    history_df = pd.DataFrame(history_records).set_index('Date')
    eom_val_df = history_df.resample('ME').last() if hasattr(history_df.resample('M'), 'last') else history_df.resample('M').last()
    eom_weights = (eom_val_df.div(eom_val_df.sum(axis=1), axis=0) * 100).fillna(0)
    eom_weights.index = eom_weights.index.strftime('%Y-%m')
    cols_ordered = sorted([c for c in eom_weights.columns if c != '현금(Cash)']) + ['현금(Cash)']
    eom_weights = eom_weights[cols_ordered]
    eom_weights_reset = eom_weights.reset_index().melt('Date', var_name='Asset', value_name='Weight')
    eom_weights_reset['Order'] = eom_weights_reset['Asset'].map({name: i for i, name in enumerate(cols_ordered)})
    
    return {
        'final_asset': final_asset, 'final_port_ret': ((final_asset / init_cash) - 1) * 100,
        'summary_rows': summary_rows, 'eom_weights_reset': eom_weights_reset,
        'benchmark_ret_val': benchmark_ret_val, 'final_benchmark_asset': final_benchmark_asset,
        'final_bh_asset': bh_df.iloc[-1], 'final_bh_ret': ((bh_df.iloc[-1] / init_cash) - 1) * 100,
        'final_dca_asset': dca_df.iloc[-1], 'final_dca_ret': ((dca_df.iloc[-1] / init_cash) - 1) * 100,
        'cols_ordered': cols_ordered, 'color_range': ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'] * 10
    }

# ==========================================
# 2. 세션 트래킹 초기화
# ==========================================
if 'show_scanner' not in st.session_state: st.session_state.show_scanner = False

# ==========================================
# 3. 사이드바: 포트폴리오 관리 및 세팅
# ==========================================
st.sidebar.header("🎯 현재 작업할 포트폴리오 선택")

all_ports = load_all_portfolios_from_sheets()
port_names = list(all_ports.keys())
selected_port, p_data, active_strat = None, None, "대형주 (Core)"

if port_names:
    selected_port = st.sidebar.selectbox("구글 시트 DB 목록", port_names)
    p_data = all_ports.get(selected_port)
    active_strat = p_data.get('strategy', '대형주 (Core)') if p_data else "대형주 (Core)"
    if p_data:
        st.sidebar.markdown("---")
        st.sidebar.subheader("💰 Virtual Capital & Settings")
        st.sidebar.markdown(f"**현재 설정 전략:** `{active_strat}`")
        new_cash = st.sidebar.number_input(f"총 투자 운용 자산 (증/감액)", value=int(p_data.get('cash', 10000000)), step=1_000_000, format="%d")
        if new_cash != int(p_data.get('cash', 10000000)):
            p_data['cash'] = new_cash
            save_portfolio_to_sheets(selected_port, p_data)
            st.rerun()
        st.sidebar.caption(f"💵 가상 설정 금액: **{new_cash:,.0f} 원**")
        with st.sidebar.popover(f"🗑️ '{selected_port}' 삭제", use_container_width=True):
            if st.button("🚨 영구 삭제합니다", key=f"del_{selected_port}", type="primary", use_container_width=True):
                delete_portfolio_from_sheets(selected_port)
                st.rerun()
else: st.sidebar.info("👈 포트폴리오를 추가해 주세요.")

st.sidebar.markdown("---")
st.sidebar.subheader("➕ 새 관심종목 포트폴리오 추가")
new_p_name = st.sidebar.text_input("새 포트폴리오 이름 (특수문자 제외)")
new_p_strat = st.sidebar.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"])
new_p_cash = st.sidebar.number_input("초기 총 투자금", value=10_000_000, step=1_000_000, format="%d", key="new_cash_input")
if st.sidebar.button("새 포트폴리오 생성하기", use_container_width=True):
    if new_p_name:
        safe_name = re.sub(r'[\\/*?:"<>|]', "", new_p_name)
        if safe_name not in all_ports:
            save_portfolio_to_sheets(safe_name, {'strategy': new_p_strat, 'cash': new_p_cash, 'stocks': [], 'created_at': datetime.date.today().strftime('%Y-%m-%d')})
            st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")
SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = None, None, None, None, True
if p_data:
    kis_secret_key = "core" if active_strat == "대형주 (Core)" else "satellite"
    kis_account_data = st.secrets.get("kis_accounts", {}).get(kis_secret_key, None)
    if kis_account_data:
        SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = kis_account_data.get("app_key"), kis_account_data.get("app_secret"), str(kis_account_data.get("cano")), str(kis_account_data.get("acnt_prdt", "01")), kis_account_data.get("is_mock", False)
        st.sidebar.success(f"✅ **{kis_account_data.get('name', f'{active_strat} 계좌')}** 자동 매칭됨")
    else: st.sidebar.warning(f"🔑 **KIS API 미연동**")

# ==========================================
# 텔레그램 연동 상태 및 오토파일럿
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header("📱 텔레그램 알림 봇 연동")
tg_token, tg_chat_id = st.secrets.get("telegram", {}).get("bot_token", ""), st.secrets.get("telegram", {}).get("chat_id", "")

try: init_ap = st.query_params.get("autopilot", "false").lower() == "true"; init_min = int(st.query_params.get("ap_min", "10"))
except:
    try: params = st.experimental_get_query_params(); init_ap = params.get("autopilot", ["false"])[0].lower() == "true"; init_min = int(params.get("ap_min", ["10"])[0])
    except: init_ap, init_min = False, 10

auto_pilot = False 

if tg_token and tg_chat_id:
    st.sidebar.success("✅ 텔레그램 봇 연동 완료")
    if st.sidebar.button("🔔 봇 연동 테스트 알림 발송"):
        success, msg = send_telegram_message("🤖 *Core-Satellite Quant System*\n텔레그램 알림 봇이 정상적으로 연결되었습니다!")
        if success: st.toast("텔레그램 알림 발송 성공!")
        else: st.sidebar.error(f"발송 실패: {msg}")
            
    st.sidebar.markdown("---")
    st.sidebar.subheader("🤖 오토파일럿 (무인 감시 모드)")
    auto_pilot = st.sidebar.toggle("오토파일럿 켜기 (Auto-Refresh)", value=init_ap, key='auto_pilot_toggle')
    if auto_pilot != init_ap:
        try: st.query_params["autopilot"] = str(auto_pilot).lower()
        except: st.experimental_set_query_params(autopilot=str(auto_pilot).lower(), ap_min=str(init_min))
        st.rerun()
    
    if auto_pilot:
        check_min = st.sidebar.number_input("감시 주기 (분)", min_value=1, max_value=60, value=init_min)
        if check_min != init_min:
            try: st.query_params["ap_min"] = str(check_min)
            except: st.experimental_set_query_params(autopilot="true", ap_min=str(check_min))
            st.rerun()
        st.sidebar.info(f"🔄 {check_min}분 주기로 백그라운드 자동 감시 중...")
        components.html(f"<script>setTimeout(function(){{window.parent.location.reload();}}, {check_min * 60000});</script>", height=0)
else: st.sidebar.warning("🔑 `Secrets`에 `[telegram]` 정보 미등록. 푸시 알림 비활성화됨.")

# ==========================================
# 파라미터 세팅 (시뮬레이션 상세 설정 포함)
# ==========================================
vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60 = fetch_market_data()
st.sidebar.markdown("---")
st.sidebar.header("⚙️ Advanced Strategy Parameters")

use_ma200_filter = st.sidebar.checkbox("🛡️ 200일 대장기 추세선 필터 적용", value=True)
whipsaw_buffer = st.sidebar.slider("골든크로스 휩소 방지 버퍼 (%)", min_value=0.0, max_value=5.0, value=1.5, step=0.5)
sat_stop_loss = st.sidebar.slider("긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=(-15 if active_strat == '대형주 (Core)' else -12), step=1)

with st.sidebar.expander("🧪 시뮬레이션 전용 상세 설정"):
    cooldown_days = st.slider("🔒 연속 2회 손실 시 쿨다운 (일)", min_value=0, max_value=90, value=60, step=15)
    max_alloc_pct = st.slider("기본 종목당 투입 한도 (%)", min_value=10, max_value=60, value=(35 if active_strat == '대형주 (Core)' else 20), step=5)
    min_hold_days = st.slider("최소 보유 기간 (일)", min_value=0, max_value=20, value=5, step=1)
    ts_target_pct = st.slider("트레일링 스탑 목표 수익률 (%)", min_value=10, max_value=100, value=(30 if active_strat == '대형주 (Core)' else 15), step=5)
    ts_drop_pct = st.slider("트레일링 스탑 하락 허용 폭 (%)", min_value=-20, max_value=-5, value=(-10 if active_strat == '대형주 (Core)' else -5), step=1)
    bull_market_boost = st.checkbox("🔥 강세장 자금 풀 부스터", value=True)

# ==========================================
# [공통] KIS 실계좌 잔고 선제척 조회 (탭 간 연동용)
# ==========================================
real_holdings_tickers = []
kis_token_global = None

if SYS_APP_KEY:
    kis_token_global = get_kis_access_token(SYS_APP_KEY, SYS_APP_SECRET, is_mock=SYS_IS_MOCK)
    cache_key = f"kis_global_cache_{SYS_CANO}_{SYS_ACNT_PRDT}"
    
    if auto_pilot or st.sidebar.button("🔄 전 계좌 데이터 동기화") or cache_key not in st.session_state:
        if kis_token_global:
            holdings, summary = fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, kis_token_global, is_mock=SYS_IS_MOCK)
            if holdings is not None and summary is not None:
                imported = [{'종목명': i.get('prdt_name'), '티커': i.get('pdno'), '실시간 현재가': f"{float(i.get('prpr', 0)):,.0f} 원", '매수평균가': f"{float(i.get('pchs_avg_pric', 0)):,.0f} 원", '보유수량': f"{int(i.get('hldg_qty'))} 주", '평가손익률': f"{float(i.get('evlu_pfls_rt', 0)):+.2f}%", '_raw_price': float(i.get('prpr', 0)), '_raw_buy': float(i.get('pchs_avg_pric', 0))} for i in holdings if int(i.get('hldg_qty', 0)) > 0]
                st.session_state[cache_key] = {'total_eval': float(summary[0].get('tot_evlu_amt', 0)) if summary else 0, 'stocks': imported}

    kis_data = st.session_state.get(cache_key)
    if kis_data:
        real_holdings_tickers = [item['티커'] for item in kis_data['stocks']]

# ==========================================
# 4. 탭 구성
# ==========================================
tab1, tab2, tab3, tab4 = st.tabs(["📝 관심종목 유니버스 & AI 진단", "🔌 실전 계좌 (Real Account)", "📊 시뮬레이션", "📄 알고리즘 백서"])

with tab1:
    st.header("📝 관심종목 유니버스 & 실시간 AI 진단")
    st.markdown("관심 종목을 추가하면 AI가 실시간으로 타점을 진단합니다. **실전 계좌에서 매수한 종목은 이 목록에서 자동으로 숨겨지며, 매도 시 다시 복귀합니다.**")
    
    if not p_data or not selected_port:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 생성하거나 선택하세요.")
    else:
        current_strategy, total_cash = p_data.get('strategy', '대형주 (Core)'), p_data.get('cash', 10000000)
        
        if st.button("🚀 실시간 AI 타점 스캐너 가동", type="primary", use_container_width=True): 
            st.session_state.show_scanner = True

        if st.session_state.show_scanner:
            with st.spinner("AI 퀀트 필터 검색 중..."):
                scan_result = run_core_scanner(use_ma200_filter, whipsaw_buffer) if current_strategy == '대형주 (Core)' else run_satellite_scanner(use_ma200_filter)
                if not scan_result.empty:
                    st.success(f"✅ 새로운 타점 종목 {len(scan_result)}개 발굴!")
                    
                    # [V2.10 추가] 스캐너 발굴 기준 상세 안내 UI
                    with st.expander("🔍 어떤 기준으로 이 종목들을 발굴했나요?", expanded=True):
                        if current_strategy == '대형주 (Core)':
                            st.markdown(f"""
                            **[대형주 Core 전략 스캐너 발굴 조건]**
                            *   **📌 유니버스:** KOSPI 시가총액 상위 100종목
                            *   **🛡️ 장기 추세 필터:** 현재가가 200일 이동평균선 위에 위치 (안전구간)
                            *   **📈 중기 추세 우상향:** 60일 이동평균선이 10일 전보다 상승 중
                            *   **🔥 단기 모멘텀:** 20일 전 대비 주가 상승 (단기 수익률 > 0%)
                            *   **🎯 골든크로스 타점:** 20일선이 60일선을 뚫고 올라가 안착 (휩소 방지 버퍼 `{whipsaw_buffer}%` 이상 이격된 확실한 자리)
                            """)
                        else:
                            st.markdown(f"""
                            **[중소형주 Satellite 전략 스캐너 발굴 조건]**
                            *   **📌 유니버스:** KOSDAQ 시가총액 1,000억 이상 상위 종목
                            *   **💥 수급(거래량) 폭발:** 최근 20일 내 거래량이 5일 평균 대비 **200% 이상 급증**한 이력이 있는 주도주
                            *   **📉 20일선 눌림목:** 주가가 20일선 부근(`-5% ~ +3%`)으로 조정을 받았거나 20일선을 터치하며 지지받는 1차 반등 자리
                            *   **🛡️ 리스크 및 추세 방어:** 고점 대비 하락폭(MDD)이 `-30%` 이내이며, 60일선이 우상향 유지 중
                            *   **🏆 AI 스코어링:** 최근 거래량 급증 크기(40%) + 60일 모멘텀(30%) + 20일 모멘텀(30%) 점수를 합산하여 가장 타점이 좋은 상위 5개 종목 추천
                            """)
                    
                    for _, row in scan_result.iterrows():
                        c1, c2, c3 = st.columns([4, 4, 2])
                        c1.write(f"**{row['종목명']}** (`{row['티커']}`)")
                        c2.write(f"현재가: {row['현재가']}")
                        if c3.button("➕ 담기", key=f"add_{row['티커']}"):
                            p_data['stocks'].append({'종목명': row['종목명'], '티커': row['티커'], '매수단가': 0, '보유수량': 0})
                            p_data['stocks'] = pd.DataFrame(p_data['stocks']).drop_duplicates(subset=['티커']).to_dict(orient='records')
                            save_portfolio_to_sheets(selected_port, p_data)
                            st.rerun()
                else: st.warning("⚠️ 현재 필터 조건을 만족하는 종목이 없습니다.")

        st.markdown("---")
        
        sandbox_stocks = p_data.get('stocks', [])
        visible_stocks, hidden_stocks = [], []
        for s in sandbox_stocks:
            if s.get('티커') in real_holdings_tickers: hidden_stocks.append(s)
            else: visible_stocks.append(s)
            
        if hidden_stocks:
            st.info(f"💡 현재 이 포트폴리오의 **{len(hidden_stocks)}개** 종목이 '실전 계좌(탭 2)'에 보유 중이므로 화면에서 숨김 처리되었습니다. (전량 매도 시 다시 이곳으로 자동 복귀)")

        if kis_token_global: st.caption("⚡ **KIS API 연결됨:** 한국투자증권 실시간 호가 및 AI 진단 반영 중입니다.")
        else: st.caption("📡 **KIS API 미연결:** Yahoo Finance 지연 데이터(약 15분)를 기반으로 진단합니다.")
            
        display_records = []
        buf = whipsaw_buffer / 100.0
        
        with st.spinner("AI 퀀트 엔진 실시간 데이터 연동 및 통합 표 생성 중..."):
            for row in visible_stocks:
                ticker = row.get('티커', '')
                
                c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if kis_token_global else None
                if not c_price:
                    try: 
                        yf_df = yf.download(f"{ticker}.KS", period="1d", progress=False)
                        if yf_df.empty: yf_df = yf.download(f"{ticker}.KQ", period="1d", progress=False)
                        if not yf_df.empty: c_price = float(yf_df['Close'].iloc[-1])
                    except: c_price = 0
                
                res = fetch_stock_status(ticker, live_price=c_price)
                action, tech_text = "분석 불가", "-"
                
                if res and res[0] is not None:
                    _, ma200, ma60, ma20, drawdown, _, _, ret_20, ma60_slope_positive, is_above_ma200, _, current_low, _ = res
                    dist_ma20 = ((c_price / ma20) - 1) * 100
                    tech_text = f"20/60선 이격 {((ma20 / ma60) - 1) * 100:+.2f}%" if current_strategy == '대형주 (Core)' else f"20일선 이격 {dist_ma20:+.2f}%"

                    if current_strategy == '대형주 (Core)':
                        action = "🔴 진입 보류" if (use_ma200_filter and not is_above_ma200) else ("🟢 적극 신규 진입 권장" if ((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) or vix_contrarian) else "🟡 관망 (타점 대기)")
                    else:
                        action = "🔴 진입 보류" if (use_ma200_filter and not is_above_ma200) else ("🟢 적극 신규 진입 권장" if (((-5.0 <= dist_ma20 <= 3.0) or (current_low <= ma20 * 1.01) or vix_contrarian) and drawdown >= -30.0) else "🟡 관망 (타점 대기)")

                display_records.append({
                    '종목명': row.get('종목명'), 
                    '티커': ticker, 
                    '실시간 현재가': c_price, 
                    '🤖 AI 액션 플랜': action, 
                    '📊 판단 근거': tech_text
                })
                
        display_df = pd.DataFrame(display_records)
        if display_df.empty: display_df = pd.DataFrame(columns=['종목명', '티커', '실시간 현재가', '🤖 AI 액션 플랜', '📊 판단 근거'])

        col_config = {
            "종목명": st.column_config.TextColumn("종목명 (수정가능)"),
            "티커": st.column_config.TextColumn("티커 (수정가능)"),
            "실시간 현재가": st.column_config.NumberColumn("🟢 현재가 (조회용)", format="%d", disabled=True),
            "🤖 AI 액션 플랜": st.column_config.TextColumn("🤖 AI 액션 플랜", disabled=True),
            "📊 판단 근거": st.column_config.TextColumn("📊 판단 근거", disabled=True)
        }
        
        edited_df = st.data_editor(display_df, num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}", column_config=col_config)
        
        if st.button("💾 관심종목 리스트 저장", type="primary"):
            save_df = edited_df[['종목명', '티커']].copy()
            save_df['매수단가'] = 0 
            save_df['보유수량'] = 0
            
            p_data['stocks'] = pd.DataFrame(save_df.to_dict('records') + hidden_stocks).drop_duplicates(subset=['티커']).to_dict('records')
            save_portfolio_to_sheets(selected_port, p_data)
            st.success("✅ 안전하게 저장 및 동기화되었습니다!")
            st.rerun()

        if auto_pilot or st.button("📲 현재 AI 진단 결과를 텔레그램으로 전송", key="send_tg_virtual"):
            changed_msgs, needs_save = [], False
            for idx, r_dict in enumerate(p_data['stocks']):
                s_name = r_dict['종목명']
                curr_action = next((r['🤖 AI 액션 플랜'] for r in display_records if r['종목명'] == s_name), "기록없음 (실계좌이동)")
                
                if "기록없음" in curr_action: continue 

                if curr_action != r_dict.get('last_action', "기록없음"):
                    p_data['stocks'][idx]['last_action'] = curr_action 
                    needs_save = True
                    if "관망" not in curr_action and "보류" not in curr_action:
                        changed_msgs.append(f"▪️ *{s_name}*: {curr_action}")
            
            if changed_msgs:
                send_telegram_message(f"🤖 *[{selected_port} 관심종목] 시그널 감지!*\n" + "\n".join(changed_msgs))
                st.toast("오토파일럿 알림 발송 완료!")
            elif not auto_pilot: st.toast("새로운 시그널이 없어 전송을 생략했습니다.")
            
            if needs_save: save_portfolio_to_sheets(selected_port, p_data)

with tab2:
    st.header("🔌 실전 계좌 (Real Account) 전용 모니터링")
    st.markdown("한국투자증권에 실제로 매수(보유) 중인 종목만 이곳에 표시되며, AI가 매도/손절 타점을 집중 감시합니다.")
    
    if not SYS_APP_KEY:
        st.warning("사이드바에서 KIS API 키를 등록해주세요.")
    else:
        kis_data = st.session_state.get(cache_key)
        if kis_data:
            real_total_eval, real_stocks_df = kis_data['total_eval'], pd.DataFrame(kis_data['stocks'])
            
            col_m1, col_m2, col_m3 = st.columns(3)
            col_m1.metric("💰 계좌 총 평가 금액", f"{real_total_eval:,.0f} 원")
            if not real_stocks_df.empty:
                viz_df = real_stocks_df.copy()
                viz_df['평가금액'] = viz_df['보유수량'].str.replace(' 주', '').str.replace(',', '').astype(float) * viz_df['_raw_price']
                total_stock_eval = viz_df['평가금액'].sum()
                col_m2.metric("📈 주식 평가액", f"{total_stock_eval:,.0f} 원")
                col_m3.metric("💵 가용 현금", f"{real_total_eval - total_stock_eval:,.0f} 원")
                
                st.markdown("---")
                with st.spinner("실계좌 종목 AI 집중 분석 중..."):
                    buf, live_results = whipsaw_buffer / 100.0, []
                    for idx, row in real_stocks_df.iterrows():
                        live_c_price, buy_price = float(row.get('_raw_price', 0)), float(row.get('_raw_buy', 0))
                        if live_c_price == 0: continue
                            
                        _, _, ma60, ma20, _, _, _, _, _, _, _, _, _ = fetch_stock_status(row['티커'], live_price=live_c_price)

                        if active_strat == '대형주 (Core)': action = "🟢 보유 유지" if ma20 and ma60 and (ma20 >= ma60 * (1 - buf/2)) else "🔴 즉각 매도 (추세 이탈)"
                        else:
                            user_ret = ((live_c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                            action = "🔴 강제 손절 집행" if user_ret <= sat_stop_loss else ("🟢 보유 유지" if ma20 and ma60 and (ma20 >= ma60 * (1 - buf/2)) else "🔴 전량 매도")

                        live_results.append({'보유 종목명': row['종목명'], '한투 실시간 현재가': f"{live_c_price:,.0f} 원", '수익률': f"{((live_c_price / buy_price) - 1) * 100:+.2f}%", '🤖 실계좌 전용 액션 플랜': action})
                    
                    st.table(pd.DataFrame(live_results))
                    
                    if auto_pilot:
                        changed_msgs, needs_save = [], False
                        if 'real_last_actions' not in p_data: p_data['real_last_actions'] = {}
                        for r in live_results:
                            s_name, curr_action = r['보유 종목명'], r['🤖 실계좌 전용 액션 플랜']
                            if curr_action != p_data['real_last_actions'].get(s_name, "기록없음"):
                                p_data['real_last_actions'][s_name], needs_save = curr_action, True
                                if "유지" not in curr_action: changed_msgs.append(f"▪️ *{s_name}*: {curr_action}")
                        if changed_msgs:
                            send_telegram_message(f"🚨 *[실전계좌] 긴급 시그널!*\n" + "\n".join(changed_msgs))
                            st.toast("실계좌 오토파일럿 알림 발송 완료!")
                        if needs_save: save_portfolio_to_sheets(selected_port, p_data)
            else:
                st.info("현재 실전 계좌에 매수(보유) 중인 종목이 없습니다. [탭 1]의 관심종목 리스트에서 타점을 대기하세요.")

with tab3:
    st.header("🧪 시뮬레이션 및 백테스트 (Simulation & Backtest)")
    if not p_data or not selected_port: st.warning("포트폴리오가 없습니다.")
    else:
        stocks_df, current_strategy, total_cash = pd.DataFrame(p_data.get('stocks', [])), p_data.get('strategy', '대형주 (Core)'), p_data.get('cash', 10000000)
        
        st.subheader("📊 AI 전략 유니버스 장기 초과수익 검증 (Backtest)")
        st.caption("※ 좌측 사이드바의 '시뮬레이션 전용 상세 설정' 메뉴에서 리스크 및 자금 배분 옵션을 조절할 수 있습니다.")
        col_sim1, col_sim2 = st.columns(2)
        with col_sim1: start_date = st.date_input("시작일", datetime.date(2023, 1, 1))
        with col_sim2: end_date = st.date_input("종료일", datetime.date.today())

        if st.button(f"🚀 '{selected_port}' 전략 기반 Backtest 실행", type="primary", use_container_width=True):
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner(f"벤치마크 퀀트 백테스트 구동 중... (약 15초 소요)"):
                    bt_result = run_quant_simulation(stocks_df, current_strategy, total_cash, start_date, end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if bt_result:
                        st.success(f"✅ 백테스트 실행 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 초기 자산", f"{total_cash:,.0f} 원")
                        col_r2.metric(f"AI 초과수익 전략 기말 자산", f"{bt_result['final_asset']:,.0f} 원", f"{bt_result['final_port_ret']:+.2f}%")
                        
                        st.markdown("---")
                        st.subheader("📋 종목별 상세 매매 통계")
                        st.table(pd.DataFrame(bt_result['summary_rows']))
                        
                        chart = alt.Chart(bt_result['eom_weights_reset']).mark_bar().encode(
                            x=alt.X('Date:O', title=''), y=alt.Y('Weight:Q', title='비중 (%)', stack='zero'),
                            color=alt.Color('Asset:N', scale=alt.Scale(domain=bt_result['cols_ordered'], range=bt_result['color_range'])), order=alt.Order('Order:Q')
                        ).properties(height=450)
                        st.altair_chart(chart, use_container_width=True)

with tab4:
    st.markdown("<h1 style='text-align: center;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서</h1>", unsafe_allow_html=True)
    st.markdown("본 보고서는 **'Core-Satellite Quant System'**에 탑재된 AI 매매 엔진의 핵심 로직 명세서입니다.")
    st.divider()
    st.markdown("""
    ### 1. 🏛️ 핵심 투자 철학: Core-Satellite 듀얼 엔진
    | 구분 | 대형주 (Core / 핵심 자산) | 중소형주 (Satellite / 위성 자산) |
    | :--- | :--- | :--- |
    | **운용 목표** | 안정적인 시장 우상향 추종 및 방어 | 시장 주도주 발굴 및 초과 수익 창출 |
    | **핵심 타점** | 200일선 기반 **골든크로스 추세 추종** | 수급 폭발 후 **20일선 눌림목 스윙** |
    
    ### 2. 🌍 시장 환경 필터 (Macro Regime)
    * **VIX 필터:** VIX 25 이상 시 공포 극점(Bottom) 인식 적극 매수, 30 이상 시 매매 전면 금지.
    * **강세장 부스터:** 지수 60일 수익률 양수 및 정배열 시 종목당 투입 한도 1.5배 확장.
    
    ### 3. 🛡️ 극단적 리스크 관리 (Drawdown Defense)
    * **트레일링 스탑:** 목표 수익률 도달 후 특정 비율 하락 시 즉시 익절.
    * **손실 쿨다운:** 연속 2회 손절 시 지정 기간(기본 60일) 해당 종목 매수 동결.
    """)
