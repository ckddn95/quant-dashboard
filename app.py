import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import altair as alt
import json
import datetime
import time
import re
import requests
import gspread
import hashlib
from google.oauth2.service_account import Credentials
import warnings
warnings.filterwarnings('ignore')

# ==========================================
# 0. 페이지 설정 및 보안 (Authentication)
# ==========================================
st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")

SPREADSHEET_ID = "1hFPs2y8UipaWHfM_VVgAqsq566HnHQLBONSwBX28TQ0"

@st.cache_resource
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(dict(st.secrets["google_sheets_json"]), scopes=scopes)
    return gspread.authorize(creds)

def hash_password(password):
    return hashlib.sha256(str(password).encode('utf-8')).hexdigest()

@st.cache_data(ttl=600)
def get_saved_password_hash():
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SPREADSHEET_ID)
        try: 
            worksheet = sh.worksheet("Settings")
        except:
            worksheet = sh.add_worksheet(title="Settings", rows=10, cols=2)
            default_hash = hash_password("0000")
            worksheet.append_row(["app_password", default_hash])
            return default_hash
        
        cell = worksheet.find("app_password")
        if cell: return worksheet.cell(cell.row, 2).value
        else:
            default_hash = hash_password("0000")
            worksheet.append_row(["app_password", default_hash])
            return default_hash
    except Exception:
        return hash_password(st.secrets.get("app_password", "0000"))
        
def save_password_hash(new_hash):
    try:
        client = get_gspread_client()
        sh = client.open_by_key(SPREADSHEET_ID)
        worksheet = sh.worksheet("Settings")
        cell = worksheet.find("app_password")
        if cell: worksheet.update_cell(cell.row, 2, new_hash)
        else: worksheet.append_row(["app_password", new_hash])
        get_saved_password_hash.clear() 
        return True
    except Exception as e:
        st.error(f"비밀번호 저장 오류: {e}")
        return False

def get_daily_auth_token():
    saved_hash = get_saved_password_hash()
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    return hashlib.sha256((saved_hash + today_str).encode('utf-8')).hexdigest()

daily_token = get_daily_auth_token()

try: url_auth = st.query_params.get("auth", "")
except: 
    try: url_auth = st.experimental_get_query_params().get("auth", [""])[0]
    except: url_auth = ""

if url_auth == daily_token:
    st.session_state["authenticated"] = True
elif "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.markdown("<h2 style='text-align: center;'>🔒 퀀트 대시보드 보안 인증</h2>", unsafe_allow_html=True)
    pwd = st.text_input("비밀번호를 입력하세요", type="password")
    
    if pwd:
        saved_hash = get_saved_password_hash()
        if hash_password(pwd) == saved_hash:
            st.session_state["authenticated"] = True
            try: st.query_params["auth"] = daily_token
            except: st.experimental_set_query_params(auth=daily_token)
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다.")
    st.stop()

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **오토파일럿 무인 감시**, **실계좌 자동매매**, **시뮬레이션**을 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 1. 헬퍼 함수 모음 (통신, DB, 스캐너)
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

@st.cache_data(ttl=120, show_spinner=False)
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
        load_all_portfolios_from_sheets.clear() 
    except Exception as e:
        st.error(f"구글 시트 저장 오류: {e}")

def delete_portfolio_from_sheets(name):
    try:
        worksheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Portfolios")
        cell = worksheet.find(name)
        if cell: worksheet.delete_rows(cell.row)
        load_all_portfolios_from_sheets.clear() 
    except Exception as e:
        st.error(f"구글 시트 삭제 오류: {e}")

def get_kis_access_token(app_key, app_secret, is_mock=True):
    if not app_key or not app_secret: return None
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
    if not token: return None
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).strip().zfill(6)}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return float(res.json()['output']['stck_prpr'])
    except: pass
    return None

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    if not token: return None, None
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

def execute_kis_order(app_key, app_secret, token, cano, acnt_prdt_cd, ticker, qty, price, order_type="BUY", is_market=False, is_mock=True):
    if int(qty) <= 0: return False, "주문 수량 오류"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/order-cash"
    
    if is_mock: tr_id = "VTTC0802U" if order_type == "BUY" else "VTTC0801U"
    else: tr_id = "TTTC0802U" if order_type == "BUY" else "TTTC0801U"
        
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id}
    ord_dvsn = "01" if is_market else "00"
    ord_unpr = "0" if is_market else str(int(price))
    
    body = {"CANO": str(cano).replace("-", "").strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "PDNO": str(ticker).strip().zfill(6), "ORD_DVSN": ord_dvsn, "ORD_QTY": str(int(qty)), "ORD_UNPR": ord_unpr}
    
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200:
            rj = res.json()
            if rj.get('rt_cd') == '0': return True, f"[{'시장가' if is_market else '지정가'}] 주문 완료: {rj.get('msg1')}"
            else: return False, f"주문 거부: {rj.get('msg1')} ({rj.get('msg_cd')})"
        else: return False, f"API 통신 오류: {res.text}"
    except Exception as e:
        return False, f"API 예외 발생: {str(e)}"

def log_daily_trade(p_data, s_name, order_type, price, qty, buy_price=0.0, status="✅ 체결완료", msg=""):
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    now_str = datetime.datetime.now().strftime('%H:%M:%S')
    if p_data.get('daily_trades_date') != today_str:
        p_data['daily_trades'] = []
        p_data['daily_trades_date'] = today_str
    
    pnl = (price - buy_price) * qty if order_type == "SELL" else 0.0
    p_data.setdefault('daily_trades', []).append({
        '체결 시간': now_str, 
        '종목명': s_name, 
        '주문 구분': '매도(청산)' if order_type == "SELL" else '매수(진입)',
        '상태': status,
        '체결 단가': price, 
        '체결 수량': qty, 
        '체결 금액': price * qty, 
        '실현 손익': pnl,
        '비고 (API 메시지)': msg
    })
    return p_data

@st.cache_data(ttl=86400)
def load_krx_universe():
    try: return fdr.StockListing('KRX').dropna(subset=['Code', 'Name'])
    except: return pd.DataFrame()

@st.cache_data(ttl=1800)
def fetch_market_data():
    try:
        k_close = fdr.DataReader('KS11')['Close'].tail(61)
        kospi_ret_60 = ((float(k_close.iloc[-1]) / float(k_close.iloc[-60])) - 1) * 100 if len(k_close) >= 60 else 0.0
        kq_close = fdr.DataReader('KQ11')['Close'].tail(61)
        kosdaq_ret_60 = ((float(kq_close.iloc[-1]) / float(kq_close.iloc[-60])) - 1) * 100 if len(kq_close) >= 60 else 0.0
    except:
        kospi_ret_60, kosdaq_ret_60 = 0.0, 0.0
        
    try:
        vix_df = yf.download("^VIX", period="3mo", progress=False)
        if vix_df.empty: raise ValueError("VIX Data Empty")
        vix_close = vix_df['Close'].dropna()
        vix_val, vix_ma3 = float(vix_close.iloc[-1]), float(vix_close.rolling(3).mean().iloc[-1])
        return vix_val, (vix_val >= 25.0) and (vix_val < vix_ma3), (vix_val < 30.0), kospi_ret_60, kosdaq_ret_60
    except:
        try:
            k_20 = fdr.DataReader('KS11')['Close'].tail(20)
            k_dd = (k_20.iloc[-1] / k_20.max()) - 1
            v_safe = True if k_dd > -0.10 else False 
            v_con = True if k_dd <= -0.15 else False 
            return 20.0, v_con, v_safe, kospi_ret_60, kosdaq_ret_60
        except: return 20.0, False, True, kospi_ret_60, kosdaq_ret_60

@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code):
    try:
        tc = str(ticker_code).strip().zfill(6)
        start_dt = (datetime.date.today() - datetime.timedelta(days=730)).strftime('%Y-%m-%d')
        
        df = fdr.DataReader(tc, start=start_dt)
        if not df.empty and len(df) > 0:
            close_p, vol = df['Close'].dropna(), df['Volume'].dropna()
            low_p = df['Low'].dropna() if 'Low' in df.columns else close_p
            if len(close_p) == 0: return None
            
            y_p, y_l = float(close_p.iloc[-1]), float(low_p.iloc[-1])
            ma200 = float(close_p.rolling(200).mean().iloc[-1]) if len(close_p) >= 200 else y_p
            ma60 = float(close_p.rolling(60).mean().iloc[-1]) if len(close_p) >= 60 else y_p
            ma60_10 = float(close_p.rolling(60).mean().iloc[-11]) if len(close_p) >= 70 else ma60
            ma20 = float(close_p.rolling(20).mean().iloc[-1]) if len(close_p) >= 20 else y_p
            rh = float(close_p.tail(120).max())
            dd = ((y_p / rh) - 1) * 100 if rh > 0 else 0.0
            
            vol_5ma = float(vol.tail(6).iloc[:-1].mean()) if len(vol) >= 6 else float(vol.iloc[-1])
            avg_trade_val = vol_5ma * y_p 
            
            vr = (float(vol.iloc[-1]) / vol_5ma * 100) if vol_5ma > 0 else 100.0
            r60 = ((y_p / float(close_p.iloc[-60])) - 1) * 100 if len(close_p) >= 60 else 0.0
            r20 = ((y_p / float(close_p.iloc[-20])) - 1) * 100 if len(close_p) >= 20 else 0.0
            vr_s = pd.Series(np.where(vol.rolling(5).mean().shift(1) > 0, vol / vol.rolling(5).mean().shift(1) * 100, 100.0), index=vol.index)
            rvm = float(vr_s.tail(20).max())
            
            return (y_p, ma200, ma60, ma20, dd, vr, r60, r20, (ma60 > ma60_10), (y_p >= ma200), rvm >= 200.0, y_l, rvm, avg_trade_val)
    except: pass
    return None

def analyze_quant_strategy(strat_name, c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, buf_pct, ts_target_pct, ts_drop_pct, sat_stop_loss_pct):
    buf = buf_pct / 100.0 if buf_pct else 0.0
    sat_stop_loss = sat_stop_loss_pct / 100.0 if sat_stop_loss_pct else -0.15
    ts_target = ts_target_pct / 100.0 if ts_target_pct else 0.30
    ts_drop = ts_drop_pct / 100.0 if ts_drop_pct else -0.05
    
    user_ret = ((c_price / buy_price) - 1) if buy_price > 0 else 0.0
    diff_ma = ((ma20 / ma60) - 1) if ma60 > 0 else 0.0
    dist_ma20 = ((c_price / ma20) - 1) if ma20 > 0 else 0.0
    
    is_above_ma200 = (c_price >= ma200)
    ma200_cond = is_above_ma200 if use_ma200_filter else True
    current_low = min(yf_low, c_price)
    
    is_ts_active = (user_ret >= ts_target)
    drawdown_from_high = ((c_price / highest_price) - 1) if highest_price > 0 else 0.0
    trailing_stop_triggered = is_ts_active and (drawdown_from_high <= ts_drop)
    
    res = {
        'ai_score': 0.0, 'entry_cond': False, 'exit_cond_trend': False,
        'stop_loss_cond': False, 'trailing_stop_cond': trailing_stop_triggered,
        'diff_ma_pct': diff_ma * 100, 'dist_ma20_pct': dist_ma20 * 100,
        'user_ret_pct': user_ret * 100, 'is_above_ma200': is_above_ma200,
        'vol_surged': vol_surged, 'drawdown': drawdown
    }
    
    if strat_name == '대형주 (Core)':
        res['ai_score'] = round((ret_20 * 0.5) + (ret_60 * 0.3) + (diff_ma * 100 * 0.2), 2)
        res['entry_cond'] = (ma200_cond and (ma20 >= ma60 * (1 + buf)) and ma60_slope_positive and (ret_20 > 0) and vix_safe) or vix_contrarian
        res['exit_cond_trend'] = (ma20 < ma60 * (1 - buf/2)) and not vix_contrarian 
        res['stop_loss_cond'] = False 
    else: 
        res['ai_score'] = round((recent_vol_max / 100.0) * 0.4 + (ret_60 * 0.3) + (ret_20 * 0.3), 2)
        is_dip = (-0.05 <= dist_ma20 <= 0.03) or (current_low <= ma20 * 1.01)
        res['entry_cond'] = (ma200_cond and ((is_dip and vol_surged) or vix_contrarian) and drawdown >= -0.30 and ma60_slope_positive and ret_20 > -0.03)
        res['exit_cond_trend'] = (c_price < ma20 * (1 - buf/2)) and not vix_contrarian 
        res['stop_loss_cond'] = (user_ret <= sat_stop_loss)
        
    return res

@st.cache_data(ttl=3600)
def run_core_scanner(use_ma200, buf_pct):
    res, krx, buf = [], load_krx_universe(), buf_pct / 100.0
    if krx.empty: return pd.DataFrame()
    cands = krx[krx['Market'] == 'KOSPI'].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in krx.columns else krx[krx['Market'] == 'KOSPI'].head(100)
    for _, row in cands.iterrows():
        tc = str(row['Code']).strip().zfill(6)
        s = fetch_stock_status(tc)
        if s is None: continue
        c_p, ma200, ma60, ma20, dd, vr, r60, r20, m60_up, is_a200, vs, c_l, rvm, atv = s
        if ((not use_ma200) or is_a200) and (ma20 >= ma60 * (1 + buf)) and m60_up and (r20 > 0) and atv >= 5000000000:
            res.append({'종목명': row['Name'], '티커': tc, '현재가': f"{c_p:,.0f} 원", '20/60선 이격': f"{((ma20/ma60)-1)*100:+.2f}%", '20일 모멘텀': f"{r20:+.2f}%", '진단 근거': "장기 추세선 방어 및 골든크로스"})
    return pd.DataFrame(res)

@st.cache_data(ttl=3600)
def run_satellite_scanner(use_ma200, top_n=5):
    res, krx = [], load_krx_universe()
    if krx.empty: return pd.DataFrame()
    kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)]
    cands = kosdaq[kosdaq['Marcap'] >= 100000000000].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in krx.columns else kosdaq.head(100)
    for _, row in cands.iterrows():
        tc = str(row['Code']).strip().zfill(6)
        s = fetch_stock_status(tc)
        if s is None: continue
        c_p, ma200, ma60, ma20, dd, vr, r60, r20, m60_up, is_a200, vs, c_l, rvm, atv = s
        d20 = ((c_p / ma20) - 1) * 100 if ma20 > 0 else 0
        is_dip = (-5.0 <= d20 <= 3.0) or ((c_l <= ma20 * 1.01) and (c_p >= ma20 * 0.95))
        if vs and is_dip and ((not use_ma200) or is_a200) and (dd >= -30.0) and m60_up and (r20 > -3.0) and atv >= 5000000000:
            sc = (rvm / 100.0)*0.4 + (r60*0.3) + (r20*0.3)
            res.append({'종목명': row['Name'], '티커': tc, '현재가': f"{c_p:,.0f} 원", '20일선 이격도': f"{d20:+.2f}%", '최대 수급': f"{rvm:,.0f}%", 'AI 스코어': round(sc, 2), '_sc': sc})
    if not res: return pd.DataFrame()
    return pd.DataFrame(res).sort_values('_sc', ascending=False).head(top_n).drop(columns=['_sc'])

@st.cache_data(ttl=1800)
def run_quant_simulation(sim_stocks, strat, init_cash, start_date, end_date, use_ma200, w_buf, sl, max_a, min_h, ts_tgt, ts_drp, b_boost, cd_days):
    if sim_stocks.empty: return None
    idx_sym = 'KS11' if strat == '대형주 (Core)' else 'KQ11'
    f_start = pd.to_datetime(start_date) - datetime.timedelta(days=300)
    m_df = pd.DataFrame()
    bm_ret, f_bm = 0.0, init_cash
    try:
        bm = fdr.DataReader(idx_sym, f_start, end_date)
        if not bm.empty:
            bm = bm[~bm.index.duplicated(keep='first')]
            if bm.index.tz is not None: bm.index = bm.index.tz_localize(None)
            bm['Bm_Ret_60'] = bm['Close'] / bm['Close'].shift(60) - 1
            bm['Bm_MA60'] = bm['Close'].rolling(60).mean()
            m_df['Bm_Ret_60'], m_df['Bm_Bull'] = bm['Bm_Ret_60'], bm['Close'] > bm['Bm_MA60']
            s_bm = bm[bm.index >= pd.to_datetime(start_date)]['Close'].dropna()
            if len(s_bm) > 1:
                bm_ret = ((float(s_bm.iloc[-1]) / float(s_bm.iloc[0])) - 1) * 100
                f_bm = init_cash * (1 + bm_ret / 100)
    except: pass
    
    try:
        v_df = yf.download("^VIX", start=f_start, end=end_date, progress=False)
        if not v_df.empty:
            v_df = v_df[~v_df.index.duplicated(keep='first')]
            if isinstance(v_df.columns, pd.MultiIndex): v_df.columns = v_df.columns.get_level_values(0)
            if v_df.index.tz is not None: v_df.index = v_df.index.tz_localize(None)
            v_df['V_M3'] = v_df['Close'].rolling(3).mean()
            m_df['V_Con'] = (v_df['Close'] >= 25.0) & (v_df['Close'] < v_df['V_M3'])
            m_df['V_Safe'] = v_df['Close'] < 30.0
    except: pass
    
    m_df = m_df.ffill().fillna(0)
    s_dfs = {}
    buf = w_buf / 100.0
    s_dt = pd.to_datetime(start_date)
    
    for _, row in sim_stocks.iterrows():
        tk, nm = str(row.get('티커','')).strip().zfill(6), str(row.get('종목명',''))
        if not tk or tk == '000000': continue
        try: df = fdr.DataReader(tk, start=f_start, end=end_date)
        except: df = pd.DataFrame()
        if df is None or df.empty: continue
        
        df['Close'], df['Volume'] = df['Close'].ffill(), df['Volume'].ffill()
        df['M200'], df['M60'], df['M20'] = df['Close'].rolling(200).mean(), df['Close'].rolling(60).mean(), df['Close'].rolling(20).mean()
        df['Abv200'] = df['Close'] >= df['M200']
        df['M60_Up'] = df['M60'] > df['M60'].shift(10)
        df['R60'], df['R20'] = df['Close']/df['Close'].shift(60)-1, df['Close']/df['Close'].shift(20)-1
        df['V5'] = df['Volume'].rolling(5).mean().shift(1)
        df['VR'] = np.where(df['V5']>0, df['Volume']/df['V5']*100, 100.0)
        df['V_Str'] = df['VR'] >= 150.0
        df['RVM'] = df['VR'].rolling(20, min_periods=1).max()
        df['V_Srg'] = df['RVM'] >= 200.0
        
        if df.index.tz is not None: df.index = df.index.tz_localize(None)
        df = df.join(m_df, how='left')
        for c, d in [('V_Safe',True), ('V_Con',False), ('Bm_Ret_60',0.0), ('Bm_Bull',False)]: df[c] = df[c].fillna(d)
        
        m2_c = df['Abv200'] if use_ma200 else True
        if strat == '대형주 (Core)':
            ec = (m2_c & (df['M20'] >= df['M60']*(1+buf)) & df['M60_Up'] & (df['R20']>0) & df['V_Safe']) | df['V_Con']
            xc = (df['M20'] < df['M60']*(1-buf/2)) & (~df['V_Con'])
        else:
            df['DD'] = (df['Close']/df['Close'].rolling(120, min_periods=1).max()) - 1
            d20 = ((df['Close']/df['M20'])-1)*100
            idip = ((d20 >= -5.0) & (d20 <= 3.0)) | (df['Low'] <= df['M20']*1.01 if 'Low' in df.columns else d20 <= 0)
            ec = (m2_c & ((idip & df['V_Srg']) | df['V_Con'])) & (df['DD'] >= -0.30) & df['M60_Up'] & (df['R20'] > -0.03)
            xc = (df['Close'] < df['M20']*(1-buf/2)) & (~df['V_Con'])
            
        df['Sig'] = np.where(ec, 1, np.where(xc, 0, np.nan))
        df['Sig'] = df['Sig'].ffill().fillna(0)
        df['Sc'] = np.where(ec, 1.0 + np.where(df['V_Str'], 1.0 if strat!='대형주 (Core)' else 0.5, 0.0) + np.where(df['R60']>df['Bm_Ret_60'], 0.5, 0.0) + np.where(df['V_Con'], 1.0, 0.0), 0.0)
        s_dfs[nm] = df[df.index >= s_dt].copy()
        
    s_dfs = {k: v for k, v in s_dfs.items() if not v.empty}
    if not s_dfs: return None
    c_idx = max([d.index for d in s_dfs.values()], key=len)
    
    fast_data = {}
    for nm, df in s_dfs.items():
        df_re = df.reindex(c_idx).ffill().fillna(0)
        fast_data[nm] = {
            'Close': df_re['Close'].to_numpy(),
            'Sig': df_re['Sig'].to_numpy(),
            'Sc': df_re['Sc'].to_numpy(),
            'Bm_Bull': df_re['Bm_Bull'].to_numpy()
        }
    
    p_hist, h_recs = [], []
    t_st = {n: {'b':0, 's':0, 'f':0.0, 'rp':0.0} for n in s_dfs}
    sh, hd, mx_i, pk_p, c_loss, ab_p, rpnl, cd_u = ({n: 0 for n in s_dfs} for _ in range(8))
    cash = float(init_cash)
    b_ar, t_tgt, t_drp = max_alloc_pct/100.0, ts_target_pct/100.0, ts_drop_pct/100.0
    
    for i, d in enumerate(c_idx):
        if i == 0:
            p_hist.append(init_cash)
            rec = {'Date': d, '현금(Cash)': init_cash}
            for n in s_dfs: rec[n] = 0.0
            h_recs.append(rec)
            continue
            
        c_ar = b_ar
        if bull_market_boost and any(fast_data[n]['Bm_Bull'][i] for n in s_dfs): c_ar = min(b_ar*1.5, 1.0)
        for n in s_dfs: hd[n] = hd[n] + 1 if sh[n] > 0 else 0
        
        a_s, scs = [], {}
        for n in s_dfs:
            sig, c_p = fast_data[n]['Sig'][i], float(fast_data[n]['Close'][i])
            if sh[n] == 0 and i < cd_u[n]: sig = 0.0
            fe = tse = False
            if sh[n] > 0 and ab_p[n] > 0:
                pk_p[n] = max(pk_p[n], c_p)
                if (c_p/ab_p[n]-1) >= t_tgt and (c_p/pk_p[n]-1) <= t_drp: tse = True
                if strat != '대형주 (Core)' and (c_p/ab_p[n]-1) <= (sl/100.0): fe = True
            if tse or fe: sig = 0.0
            elif sh[n] > 0 and hd[n] < min_h: sig = 1.0
            
            if sig == 1: a_s.append(n); scs[n] = max(fast_data[n]['Sc'][i], 1.0)
            else: pk_p[n] = 0.0
            
        for n in s_dfs:
            cs, ps = (1 if n in a_s else 0), (1 if sh[n] > 0 else 0)
            if cs==1 and ps==0: t_st[n]['b'] += 1
            elif cs==0 and ps==1: t_st[n]['s'] += 1
                
        ta = cash + sum(sh[n]*float(fast_data[n]['Close'][i]) for n in s_dfs)
        
        if a_s:
            t_sc = sum(scs.values()) or len(a_s)
            for n in s_dfs:
                c_p = float(fast_data[n]['Close'][i])
                if c_p <= 0: continue
                
                if n in a_s:
                    tgt = min(ta*(scs.get(n,1.0)/t_sc), ta*c_ar)
                    diff = tgt - (sh[n]*c_p)
                    if diff > 0:
                        bq = int(diff//c_p)
                        if bq > 0:
                            cost = bq*c_p; fee = cost*0.0025
                            if cash >= (cost+fee):
                                cash -= (cost+fee)
                                ab_p[n] = ((sh[n]*ab_p[n])+cost)/(sh[n]+bq) if sh[n]>0 else c_p
                                sh[n] += bq; t_st[n]['f'] += fee; rpnl[n] -= fee
                                mx_i[n] = max(mx_i[n], sh[n]*c_p); pk_p[n] = max(pk_p[n], c_p)
                    elif diff < 0:
                        sq = int(abs(diff)//c_p); sq = min(sq, int(sh[n]))
                        if sq > 0:
                            proc = sq*c_p; fee = proc*0.0025
                            rpnl[n] += sq*(c_p-ab_p[n])-fee; cash += (proc-fee); sh[n] -= sq; t_st[n]['f'] += fee
                else:
                    if sh[n] > 0:
                        sq = int(sh[n]); proc = sq*c_p; fee = proc*0.0025
                        pnl = sq*(c_p-ab_p[n])-fee
                        if pnl < 0:
                            c_loss[n] += 1
                            if c_loss[n] >= 2 and cd_days > 0: cd_u[n] = i + cd_days
                        else: c_loss[n] = 0
                        rpnl[n] += pnl; cash += (proc-fee); t_st[n]['f'] += fee
                        sh[n], ab_p[n], pk_p[n] = 0, 0.0, 0.0
        else:
            for n in s_dfs:
                if sh[n] > 0:
                    c_p = float(fast_data[n]['Close'][i]); sq = int(sh[n]); proc = sq*c_p; fee = proc*0.0025
                    pnl = sq*(c_p-ab_p[n])-fee
                    if pnl < 0:
                        c_loss[n] += 1
                        if c_loss[n] >= 2 and cd_days > 0: cd_u[n] = i + cd_days
                    else: c_loss[n] = 0
                    rpnl[n] += pnl; cash += (proc-fee); t_st[n]['f'] += fee
                    sh[n], ab_p[n], pk_p[n] = 0, 0.0, 0.0
                    
        f_eval = sum(sh[n]*float(fast_data[n]['Close'][i]) for n in s_dfs)
        p_hist.append(max(cash+f_eval, 0))
        rec = {'Date': d, '현금(Cash)': max(cash,0)}
        for n in s_dfs: rec[n] = sh[n]*float(fast_data[n]['Close'][i])
        h_recs.append(rec)
        
    p_s = pd.Series(p_hist, index=c_idx)
    s_r, s_hv, s_pr, s_f, s_b, s_s = [], 0, 0, 0, 0, 0
    fa = p_s.iloc[-1]
    
    for n in s_dfs:
        cp = float(fast_data[n]['Close'][-1])
        hv, upnl = sh[n]*cp, sh[n]*(cp-ab_p[n]) if sh[n]>0 else 0.0
        tp = rpnl[n] + upnl
        ib = mx_i[n] if mx_i[n]>0 else (init_cash/len(s_dfs))
        bc, sc, fe = t_st[n]['b'], t_st[n]['s'], t_st[n]['f']
        s_hv += hv; s_pr += tp; s_f += fe; s_b += bc; s_s += sc
        s_r.append({'종목명':n, '최종 보유 주수':f"{int(sh[n]):,} 주", '기말 평가금':f"{hv:,.0f} 원", '총 순수익 (원)':f"{tp:+,.0f} 원", '수익률 (%)':f"{max((tp/ib)*100,-100.0):+.2f}%", '매매 횟수':f"매수 {bc}회 / 매도 {sc}회", '총 발생 수수료':f"{fe:,.0f} 원", '기말 포트폴리오 비중':f"{(hv/fa)*100 if fa>0 else 0:.2f}%"})

    s_r.append({'종목명':'💡 [전체 합계]', '최종 보유 주수':'-', '기말 평가금':f"{s_hv:,.0f} 원", '총 순수익 (원)':f"{s_pr:+,.0f} 원 (자산대비 {(s_pr/init_cash)*100 if init_cash>0 else 0:+.2f}%)", '수익률 (%)':'-', '매매 횟수':f"매수 {s_b}회 / 매도 {s_s}회", '총 발생 수수료':f"{s_f:,.0f} 원", '기말 포트폴리오 비중':f"{(s_hv/fa)*100 if fa>0 else 0:.2f}%"})
    
    h_df = pd.DataFrame(h_recs)
    if h_df.empty: return None
    h_df['Date'] = pd.to_datetime(h_df['Date']); h_df = h_df.set_index('Date')
    try: ew_df = h_df.resample('ME').last()
    except: ew_df = h_df.resample('M').last()
    if ew_df.empty: ew_df = h_df.tail(1)
    ew = (ew_df.div(ew_df.sum(axis=1), axis=0)*100).fillna(0)
    ew.index = ew.index.strftime('%Y-%m')
    co = sorted([c for c in ew.columns if c != '현금(Cash)']) + ['현금(Cash)']
    ew = ew[co]
    ew_r = ew.reset_index().melt('Date', var_name='Asset', value_name='Weight')
    ew_r['Order'] = ew_r['Asset'].map({n: i for i, n in enumerate(co)})
    
    return {'final_asset':fa, 'final_port_ret':((fa/init_cash)-1)*100, 'summary_rows':s_r, 'eom_weights_reset':ew_r, 'cols_ordered':co, 'color_range':['#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf']*10}

if 'show_scanner' not in st.session_state: st.session_state.show_scanner = False

# ==========================================
# 2. 전역 변수 및 데이터 파싱
# ==========================================
st.sidebar.header("🎯 현재 작업할 포트폴리오 선택")
all_ports = load_all_portfolios_from_sheets()
port_names = list(all_ports.keys())
selected_port = st.sidebar.selectbox("구글 시트 DB 목록", port_names) if port_names else None
p_data = all_ports.get(selected_port) if selected_port else None
active_strat = p_data.get('strategy', '대형주 (Core)') if p_data else "대형주 (Core)"
total_cash = int(p_data.get('cash', 10000000)) if p_data else 10000000

SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = None, None, None, None, True
if p_data:
    kis_secret_key = "core" if active_strat == "대형주 (Core)" else "satellite"
    kis_account_data = st.secrets.get("kis_accounts", {}).get(kis_secret_key, None)
    if kis_account_data:
        SYS_APP_KEY = kis_account_data.get("app_key")
        SYS_APP_SECRET = kis_account_data.get("app_secret")
        SYS_CANO = str(kis_account_data.get("cano"))
        SYS_ACNT_PRDT = str(kis_account_data.get("acnt_prdt", "01"))
        SYS_IS_MOCK = kis_account_data.get("is_mock", False)

tg_noti_signal = p_data.get('tg_noti_signal', True) if p_data else True
tg_noti_order = p_data.get('tg_noti_order', True) if p_data else True

kis_token_global = None
if SYS_APP_KEY and SYS_APP_SECRET and p_data:
    current_time = time.time()
    token_key = f"kis_token_{SYS_APP_KEY[-6:]}"
    time_key = f"kis_time_{SYS_APP_KEY[-6:]}"
    kis_token_global = p_data.get(token_key)
    token_time = p_data.get(time_key, 0)
    
    if not kis_token_global or (current_time - token_time) > 40000:
        new_token = get_kis_access_token(SYS_APP_KEY, SYS_APP_SECRET, is_mock=SYS_IS_MOCK)
        if new_token:
            kis_token_global = new_token
            p_data[token_key] = new_token
            p_data[time_key] = current_time
            save_portfolio_to_sheets(selected_port, p_data)

cache_key = f"kis_global_cache_{SYS_CANO}_{SYS_ACNT_PRDT}" if SYS_CANO else "kis_global_cache_None_None"

if SYS_APP_KEY and kis_token_global:
    if st.sidebar.button("🔄 실계좌 데이터 동기화") or cache_key not in st.session_state:
        holdings, summary = fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, kis_token_global, is_mock=SYS_IS_MOCK)
        if holdings is not None and summary is not None:
            tot_evlu = float(summary[0].get('tot_evlu_amt', 0))
            tot_pnl = float(summary[0].get('evlu_pfls_smtl_amt', 0))
            imported = [{'종목명': i.get('prdt_name'), '티커': str(i.get('pdno')).strip().zfill(6), '실시간 현재가': f"{float(i.get('prpr', 0)):,.0f} 원", '매수평균가': f"{float(i.get('pchs_avg_pric', 0)):,.0f} 원", '보유수량': f"{int(i.get('hldg_qty'))} 주", '평가손익률': f"{float(i.get('evlu_pfls_rt', 0)):+.2f}%", '_raw_price': float(i.get('prpr', 0)), '_raw_buy': float(i.get('pchs_avg_pric', 0))} for i in holdings if int(i.get('hldg_qty', 0)) > 0]
            st.session_state[cache_key] = {'total_eval': tot_evlu, 'total_pnl': tot_pnl, 'stocks': imported}

kis_data = st.session_state.get(cache_key)

real_holdings_tickers = []
real_total_eval, real_eval_pnl = 0.0, 0.0
real_stocks_df = pd.DataFrame()
real_cash_avail = total_cash

if kis_data:
    real_holdings_tickers = [item['티커'] for item in kis_data['stocks']]
    real_total_eval = kis_data.get('total_eval', 0.0)
    real_eval_pnl = kis_data.get('total_pnl', 0.0)
    real_stocks_df = pd.DataFrame(kis_data['stocks'])
    
    if not real_stocks_df.empty:
        viz_df = real_stocks_df.copy()
        viz_df['평가금액'] = viz_df['보유수량'].str.replace(' 주', '').str.replace(',', '').astype(float) * viz_df['_raw_price']
        real_cash_avail = real_total_eval - viz_df['평가금액'].sum()
    else:
        real_cash_avail = real_total_eval

real_base_date_str = p_data.get('real_base_date', p_data.get('created_at', '2024-01-01')) if p_data else '2024-01-01'
try: real_base_date = pd.to_datetime(real_base_date_str).date()
except: real_base_date = datetime.date(2024, 1, 1)

cumulative_realized_pnl = 0.0
if p_data and 'daily_trades' in p_data:
    for trade in p_data['daily_trades']:
        status = trade.get('상태', '✅ 체결')
        if '✅' in status:
            cumulative_realized_pnl += trade.get('실현 손익', 0.0)
        
manual_offset = float(p_data.get('pnl_offset', 0.0)) if p_data else 0.0

total_invested_principal = float(total_cash)
if kis_data:
    total_invested_principal = real_total_eval - real_eval_pnl - cumulative_realized_pnl - manual_offset
    if total_invested_principal <= 0: total_invested_principal = total_cash

# ------------------------------------------
# 3. 사이드바 UI 렌더링
# ------------------------------------------
if p_data:
    st.sidebar.markdown("---")
    st.sidebar.subheader("💰 Virtual Capital & Settings")
    st.sidebar.markdown(f"**현재 설정 전략:** `{active_strat}`")
    new_cash = st.sidebar.number_input(f"총 투자 운용 자산 (가상/증감액)", value=int(total_cash), step=1_000_000, format="%d")
    if new_cash != total_cash:
        p_data['cash'] = new_cash
        save_portfolio_to_sheets(selected_port, p_data)
        try: st.query_params["auth"] = daily_token
        except: st.experimental_set_query_params(auth=daily_token)
        st.rerun()
    st.sidebar.caption(f"💵 가상 설정 금액: **{new_cash:,.0f} 원**")
    with st.sidebar.popover(f"🗑️ '{selected_port}' 삭제", use_container_width=True):
        if st.button("🚨 영구 삭제합니다", key=f"del_{selected_port}", type="primary", use_container_width=True):
            delete_portfolio_from_sheets(selected_port)
            try: st.query_params["auth"] = daily_token
            except: st.experimental_set_query_params(auth=daily_token)
            st.rerun()
            
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
            try: st.query_params["auth"] = daily_token
            except: st.experimental_set_query_params(auth=daily_token)
            st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")
if SYS_APP_KEY and kis_account_data: st.sidebar.success(f"✅ **{kis_account_data.get('name', f'{active_strat} 계좌')}** 연동됨")
else: st.sidebar.warning(f"🔑 **KIS API 미연동**")

st.sidebar.markdown("---")
st.sidebar.header("📱 텔레그램 및 오토파일럿")
tg_token, tg_chat_id = st.secrets.get("telegram", {}).get("bot_token", ""), st.secrets.get("telegram", {}).get("chat_id", "")

if tg_token and tg_chat_id:
    st.sidebar.success("✅ 텔레그램 봇 연동 완료")
    if st.sidebar.button("🔔 연동 테스트 알림 발송"):
        success, msg = send_telegram_message("🤖 *Core-Satellite Quant System*\n텔레그램 정상 연결!")
        if success: st.toast("알림 발송 성공!")
        else: st.sidebar.error(f"발송 실패: {msg}")

    with st.sidebar.expander("⚙️ 텔레그램 알림 수신 항목", expanded=False):
        if p_data:
            init_tg_sig = p_data.get('tg_noti_signal', True)
            init_tg_ord = p_data.get('tg_noti_order', True)
            
            new_tg_sig = st.checkbox("💡 신규 매매 시그널 포착", value=init_tg_sig)
            new_tg_ord = st.checkbox("🛒 매수/매도 체결 결과", value=init_tg_ord)
            
            if new_tg_sig != init_tg_sig or new_tg_ord != init_tg_ord:
                p_data['tg_noti_signal'] = new_tg_sig
                p_data['tg_noti_order'] = new_tg_ord
                save_portfolio_to_sheets(selected_port, p_data)
                try: st.query_params["auth"] = daily_token
                except: st.experimental_set_query_params(auth=daily_token)
                st.rerun()
        else:
            st.info("포트폴리오 선택 후 설정 가능합니다.")

    st.sidebar.markdown("---")
    st.sidebar.subheader("🚨 긴급 제어 및 자동매매")
    
    if p_data:
        ks_key = f"ks_{selected_port}"
        at_key = f"at_{selected_port}"
        ap_key = f"ap_{selected_port}"
        
        init_ks = p_data.get('kill_switch', False)
        init_at = p_data.get('auto_trade_enabled', False)
        init_ap = p_data.get('auto_pilot', False)
        init_ap_min = p_data.get('ap_min', 10)
        
        kill_switch = st.sidebar.toggle("🚨 긴급 정지 (KILL SWITCH)", value=init_ks, key=ks_key)
        auto_trade_enabled = st.sidebar.toggle("🚀 실전 자동주문 활성화", value=init_at, key=at_key)
        auto_pilot = st.sidebar.toggle("🔄 오토파일럿 켜기", value=init_ap, key=ap_key)
        
        needs_settings_save = False
        if kill_switch != init_ks:
            p_data['kill_switch'] = kill_switch
            needs_settings_save = True
        if auto_trade_enabled != init_at:
            p_data['auto_trade_enabled'] = auto_trade_enabled
            needs_settings_save = True
        if auto_pilot != init_ap:
            p_data['auto_pilot'] = auto_pilot
            needs_settings_save = True
            
        if needs_settings_save:
            save_portfolio_to_sheets(selected_port, p_data)
            try: st.query_params["auth"] = daily_token
            except: st.experimental_set_query_params(auth=daily_token)
            st.rerun()

        if kill_switch: st.sidebar.error("⚠️ 킬 스위치 작동 중! 모든 매매 정지.")
            
        check_min = init_ap_min
        if auto_pilot:
            check_min = st.sidebar.number_input("감시 주기 (분)", min_value=1, max_value=60, value=init_ap_min)
            if check_min != init_ap_min:
                p_data['ap_min'] = check_min
                save_portfolio_to_sheets(selected_port, p_data)
                try: st.query_params["auth"] = daily_token
                except: st.experimental_set_query_params(auth=daily_token)
                st.rerun()
            st.sidebar.info(f"🔄 {check_min}분 주기로 무인 감시 중...")
            components.html(f"<script>setTimeout(function(){{window.parent.location.reload();}}, {check_min * 60000});</script>", height=0)
    else:
        kill_switch, auto_trade_enabled, auto_pilot, check_min = False, False, False, 10
else: 
    st.sidebar.warning("🔑 `telegram` 정보 미등록.")
    kill_switch, auto_trade_enabled, auto_pilot, check_min = False, False, False, 10

vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60 = fetch_market_data()
st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터")
use_ma200_filter = st.sidebar.checkbox("🛡️ 200일 추세선 필터 적용", value=True)
whipsaw_buffer = st.sidebar.slider("골든크로스 휩소 방지 버퍼 (%)", 0.0, 5.0, 1.5, 0.5)
sat_stop_loss = st.sidebar.slider("긴급 손절 컷 (%)", -25, -5, -15 if active_strat == '대형주 (Core)' else -12, 1)

with st.sidebar.expander("🧪 시뮬레이션 상세 설정"):
    cooldown_days = st.slider("연속 2회 손실 시 쿨다운(일)", 0, 90, 60, 15)
    max_alloc_pct = st.slider("기본 종목당 투입 한도 (%)", 10, 60, 35 if active_strat == '대형주 (Core)' else 20, 5)
    min_hold_days = st.slider("최소 보유 기간(일)", 0, 20, 5, 1)
    ts_target_pct = st.slider("트레일링 스탑 목표수익 (%)", 10, 100, 30 if active_strat == '대형주 (Core)' else 15, 5)
    ts_drop_pct = st.slider("트레일링 스탑 하락허용 (%)", -20, -5, -10 if active_strat == '대형주 (Core)' else -5, 1)
    bull_market_boost = st.checkbox("🔥 강세장 자금 풀 부스터", value=True)

st.sidebar.markdown("---")
with st.sidebar.expander("🔐 보안 및 시스템 설정", expanded=False):
    st.markdown("**비밀번호 변경**")
    curr_pwd = st.text_input("현재 비밀번호", type="password")
    new_pwd = st.text_input("새 비밀번호", type="password")
    confirm_pwd = st.text_input("새 비밀번호 확인", type="password")
    if st.button("비밀번호 변경 저장", use_container_width=True):
        if not curr_pwd or not new_pwd or not confirm_pwd:
            st.error("모든 항목을 입력해주세요.")
        elif new_pwd != confirm_pwd:
            st.error("새 비밀번호가 일치하지 않습니다.")
        elif hash_password(curr_pwd) != get_saved_password_hash():
            st.error("현재 비밀번호가 틀렸습니다.")
        else:
            if save_password_hash(hash_password(new_pwd)):
                st.success("✅ 비밀번호가 변경되었습니다! (다음 로그인 시 적용)")

# ==========================================
# 4. 메인 화면 구성
# ==========================================
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📝 관심종목 유니버스 & AI 진단", 
    "🔌 실전 계좌 (Real Account)", 
    "🤖 자동매매 실행 & 우선순위 큐", 
    "📊 시뮬레이션", 
    "📄 알고리즘 백서"
])

with tab1:
    st.header("📝 관심종목 유니버스 & 실시간 AI 진단")
    st.markdown("관심 종목을 추가하면 AI가 실시간으로 타점을 진단합니다. **실전 계좌에서 매수한 종목은 자동으로 숨겨지며, 매도 시 복귀합니다.**")
    
    if not p_data or not selected_port:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 생성하거나 선택하세요.")
    else:
        col_s1, col_s2 = st.columns([8, 2])
        with col_s1:
            if st.button("🚀 실시간 AI 타점 스캐너 가동 (신규 발굴)", type="primary", use_container_width=True): 
                st.session_state.show_scanner = True
        with col_s2:
            if st.button("🧹 퇴출 권장 종목 일괄 삭제", type="secondary", use_container_width=True):
                if 'last_eval_actions' in st.session_state:
                    to_remove = [t for t, a in st.session_state.last_eval_actions.items() if "유니버스 제외" in a]
                    if to_remove:
                        p_data['stocks'] = [s for s in p_data['stocks'] if s['티커'] not in to_remove]
                        save_portfolio_to_sheets(selected_port, p_data)
                        st.success(f"{len(to_remove)}개의 종목이 정리되었습니다!")
                        try: st.query_params["auth"] = daily_token
                        except: st.experimental_set_query_params(auth=daily_token)
                        st.rerun()
                    else: st.info("현재 퇴출 대상 종목이 없습니다.")

        if st.session_state.show_scanner:
            with st.spinner("AI 퀀트 필터 검색 중..."):
                scan_result = run_core_scanner(use_ma200_filter, whipsaw_buffer) if active_strat == '대형주 (Core)' else run_satellite_scanner(use_ma200_filter)
                if not scan_result.empty:
                    st.success(f"✅ 새로운 타점 종목 {len(scan_result)}개 발굴!")
                    current_watchlist_tickers = [str(s.get('티커')).strip().zfill(6) for s in p_data.get('stocks', [])]
                    for _, row in scan_result.iterrows():
                        c1, c2, c3 = st.columns([4, 4, 2])
                        c1.write(f"**{row['종목명']}** (`{row['티커']}`)")
                        c2.write(f"현재가: {row['현재가']}")
                        ticker_str = str(row['티커']).strip().zfill(6)
                        if ticker_str in real_holdings_tickers: c3.button("🔌 실계좌 보유", key=f"add_{row['티커']}", disabled=True)
                        elif ticker_str in current_watchlist_tickers: c3.button("📝 관심종목", key=f"add_{row['티커']}", disabled=True)
                        else:
                            if c3.button("➕ 담기", key=f"add_{row['티커']}"):
                                p_data['stocks'].append({'종목명': row['종목명'], '티커': ticker_str, '매수단가': 0, '보유수량': 0})
                                p_data['stocks'] = pd.DataFrame(p_data['stocks']).drop_duplicates(subset=['티커']).to_dict(orient='records')
                                save_portfolio_to_sheets(selected_port, p_data)
                                try: st.query_params["auth"] = daily_token
                                except: st.experimental_set_query_params(auth=daily_token)
                                st.rerun()
                else: st.warning("⚠️ 현재 필터 조건을 만족하는 종목이 없습니다.")

        st.markdown("---")
        sandbox_stocks = p_data.get('stocks', [])
        visible_stocks, hidden_stocks = [], []
        for s in sandbox_stocks:
            s_tick = str(s.get('티커')).strip().zfill(6)
            if s_tick in real_holdings_tickers: hidden_stocks.append(s)
            else: visible_stocks.append(s)
            
        if hidden_stocks: st.info(f"💡 현재 이 포트폴리오의 **{len(hidden_stocks)}개** 종목이 '실전 계좌(탭 2)'에 보유 중이므로 숨김 처리되었습니다.")
        if kis_token_global: st.caption("⚡ **KIS API 연결됨:** 한국투자증권 실시간 호가 및 AI 진단 반영 중입니다.")
            
        display_records, eval_actions_cache = [], {}
        
        # [V5.8 패치] 탭 1에서도 현재 기준 자산을 동적으로 적용
        current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
        if current_asset_base <= 0: current_asset_base = total_cash
            
        is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
        current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
        target_buy_amt = current_asset_base * (current_max_alloc_pct / 100.0)
        
        with st.spinner("AI 실시간 데이터 연동 및 통합 표 생성 중..."):
            for row in visible_stocks:
                ticker = str(row.get('티커', '')).strip().zfill(6)
                c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if kis_token_global else None
                res = fetch_stock_status(ticker)
                action, tech_text, easy_desc, ai_score = "분석 불가", "-", "데이터를 불러오지 못했습니다.", 0.0
                
                if res and res[0] is not None:
                    yf_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, _, vol_surged, yf_low, recent_vol_max, avg_trade_val = res
                    if not c_price or c_price == 0: c_price = yf_price
                    
                    res_q = analyze_quant_strategy(active_strat, c_price, 0.0, 0.0, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss)
                    
                    ai_score = res_q['ai_score']
                    target_shares = int(target_buy_amt // c_price) if c_price > 0 else 0

                    if active_strat == '대형주 (Core)':
                        tech_text = f"20/60선 이격 {res_q['diff_ma_pct']:+.2f}%"
                        if res_q['exit_cond_trend'] or (use_ma200_filter and not res_q['is_above_ma200']):
                            action, easy_desc = "🔴 유니버스 제외 (추세 붕괴)", "[유니버스 제외] 핵심 지지선 하향 이탈 및 모멘텀 소멸이 확인되었습니다."
                        elif res_q['entry_cond'] and avg_trade_val >= 5000000000:
                            action, easy_desc = f"🟢 매수 시그널 발생 (목표: {target_shares:,}주)", "[매수 시그널 발생] 중장기 이동평균선 정배열 및 모멘텀 강세."
                        else:
                            action, easy_desc = "🟡 모니터링 유지", "[모니터링 유지] 거래대금 부족 또는 타점 대기 중입니다."
                    else:
                        tech_text = f"20일선 이격 {res_q['dist_ma20_pct']:+.2f}%"
                        if res_q['exit_cond_trend'] or not res_q['vol_surged'] or res_q['drawdown'] < -30.0 or (use_ma200_filter and not res_q['is_above_ma200']):
                            action, easy_desc = "🔴 유니버스 제외 (수급/추세 상실)", "[유니버스 제외] 핵심 지지선 하향 이탈 및 모멘텀 소멸이 확인되었습니다."
                        elif res_q['entry_cond'] and avg_trade_val >= 5000000000:
                            action, easy_desc = f"🟢 매수 시그널 발생 (목표: {target_shares:,}주)", "[매수 시그널 발생] 유동성 충족 및 눌림목 지지가 확인되었습니다."
                        else:
                            action, easy_desc = "🟡 모니터링 유지", "[모니터링 유지] 거래대금 부족 또는 타점 대기 중입니다."

                eval_actions_cache[ticker] = action
                display_records.append({
                    '선택': False, '종목명': row.get('종목명'), '티커': ticker, '실시간 현재가': c_price, 
                    '🔥 매력도 점수': ai_score, '🤖 AI 액션 플랜': action, '📊 판단 근거': tech_text, '💡 시스템 액션 가이드': easy_desc
                })
                
        st.session_state.last_eval_actions = eval_actions_cache
        display_df = pd.DataFrame(display_records)
        
        if not display_df.empty: 
            display_df = display_df.sort_values(by="🔥 매력도 점수", ascending=False).reset_index(drop=True)
            display_df = display_df[['선택'] + [c for c in display_df.columns if c != '선택']]
        else:
            display_df = pd.DataFrame(columns=['선택', '종목명', '티커', '실시간 현재가', '🔥 매력도 점수', '🤖 AI 액션 플랜', '📊 판단 근거', '💡 시스템 액션 가이드'])

        col_config = {
            "선택": st.column_config.CheckboxColumn("선택", help="삭제할 종목 체크", default=False),
            "종목명": st.column_config.TextColumn("종목명 (수정가능)"), "티커": st.column_config.TextColumn("티커 (수정가능)"),
            "실시간 현재가": st.column_config.NumberColumn("🟢 현재가 (조회용)", format="%d", disabled=True),
            "🔥 매력도 점수": st.column_config.NumberColumn("🔥 매력도 점수", format="%.2f", disabled=True),
            "🤖 AI 액션 플랜": st.column_config.TextColumn("🤖 AI 액션 플랜", disabled=True),
            "📊 판단 근거": st.column_config.TextColumn("📊 판단 근거", disabled=True),
            "💡 시스템 액션 가이드": st.column_config.TextColumn("💡 시스템 액션 가이드", disabled=True)
        }
        
        edited_df = st.data_editor(display_df, num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}", column_config=col_config)
        
        c_btn1, c_btn2 = st.columns(2)
        with c_btn1:
            if st.button("💾 변경된 내용 저장 (추가/수정)", type="primary", use_container_width=True):
                save_df = edited_df[['종목명', '티커']].copy()
                save_df['티커'] = save_df['티커'].astype(str).str.strip().str.zfill(6)
                save_df['매수단가'], save_df['보유수량'] = 0, 0
                p_data['stocks'] = pd.DataFrame(save_df.to_dict('records') + hidden_stocks).drop_duplicates(subset=['티커']).to_dict('records')
                save_portfolio_to_sheets(selected_port, p_data)
                st.success("✅ 저장 및 동기화 완료!")
                try: st.query_params["auth"] = daily_token
                except: st.experimental_set_query_params(auth=daily_token)
                st.rerun()

        with c_btn2:
            if st.button("🗑️ 체크한 종목 삭제", type="secondary", use_container_width=True):
                to_delete = edited_df[edited_df['선택'] == True]['티커'].tolist()
                if to_delete:
                    p_data['stocks'] = [s for s in p_data['stocks'] if s['티커'] not in to_delete]
                    save_portfolio_to_sheets(selected_port, p_data)
                    st.success(f"✅ {len(to_delete)}개 종목 삭제됨!")
                    time.sleep(1)
                    try: st.query_params["auth"] = daily_token
                    except: st.experimental_set_query_params(auth=daily_token)
                    st.rerun()
                else: st.warning("⚠️ 삭제할 종목을 체크박스로 선택해주세요.")

        if auto_pilot or st.button("📲 현재 AI 진단 결과를 텔레그램으로 전송", key="send_tg_virtual"):
            changed_msgs, needs_save = [], False
            for idx, r_dict in enumerate(p_data['stocks']):
                s_name = r_dict['종목명']
                curr_action = next((r['🤖 AI 액션 플랜'] for r in display_records if r['종목명'] == s_name), "기록없음 (실계좌이동)")
                
                if "기록없음" in curr_action: continue 

                if curr_action != r_dict.get('last_action', "기록없음"):
                    p_data['stocks'][idx]['last_action'] = curr_action 
                    needs_save = True
                    if "모니터링 유지" not in curr_action:
                        changed_msgs.append(f"▪️ *{s_name}*: {curr_action}")
            
            if changed_msgs:
                if tg_noti_signal:
                    send_telegram_message(f"🤖 *[{selected_port} 관심종목] 시그널 감지!*\n" + "\n".join(changed_msgs))
                    st.toast("오토파일럿 알림 발송 완료!")
                elif not auto_pilot:
                    st.toast("새로운 신규 시그널이 감지되었습니다. (알림 OFF 설정됨)")
            elif not auto_pilot: st.toast("새로운 신규 시그널이 없습니다.")
            
            if needs_save: save_portfolio_to_sheets(selected_port, p_data)

with tab2:
    st.header("🔌 실전 계좌 (Real Account) 전용 모니터링")
    st.markdown("한국투자증권에 실제로 매수(보유) 중인 종목만 표시되며, AI가 타점을 집중 감시합니다.")
    
    if not SYS_APP_KEY:
        st.warning("사이드바에서 KIS API 키를 등록해주세요.")
    else:
        if kis_data:
            total_pnl_all = real_eval_pnl + cumulative_realized_pnl + manual_offset
            real_ret_pct = (total_pnl_all / total_invested_principal * 100) if total_invested_principal > 0 else 0
            
            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("💰 계좌 총 평가 금액", f"{real_total_eval:,.0f} 원")
            col_m2.metric("📥 자동 역산 투입 원금 (입출금 감지)", f"{total_invested_principal:,.0f} 원")
            col_m3.metric("📈 누적 실현/평가 수익금", f"{total_pnl_all:+,.0f} 원", f"{real_ret_pct:+.2f}%")
            col_m4.metric("💵 가용 현금", f"{real_cash_avail:,.0f} 원")
                
            with st.expander("⚙️ 과거 실적 보정 (선택 사항)", expanded=False):
                st.markdown("봇 가동 전에 이미 발생했던 과거 수익/손실 누적액이 있다면 보정값으로 입력해 주세요.")
                c_offset, c_btn = st.columns([8, 2])
                new_offset = c_offset.number_input("과거 실현 손익 누적액 (원)", value=int(manual_offset), step=100000)
                if c_btn.button("보정 저장", use_container_width=True):
                    p_data['pnl_offset'] = new_offset
                    save_portfolio_to_sheets(selected_port, p_data)
                    st.success("보정값이 저장되었습니다.")
                    try: st.query_params["auth"] = daily_token
                    except: st.experimental_set_query_params(auth=daily_token)
                    st.rerun()

            st.markdown("---")
            if not real_stocks_df.empty:
                tab2_anomaly_flag = False
                tab2_anomaly_reason = ""
                need_live_state_save = False
                
                with st.spinner("실계좌 종목 AI 집중 분석 중..."):
                    # [V5.8 패치] 탭 2에서도 현재 기준 자산을 명확히 정의 (오작동 방지)
                    current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
                    if current_asset_base <= 0: current_asset_base = total_cash
                        
                    is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
                    current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
                    target_buy_amt = current_asset_base * (current_max_alloc_pct / 100.0)
                    
                    total_eval_sum = 0.0
                    total_pnl_sum = 0.0
                    total_buy_sum = 0.0
                    live_results = []
                    
                    for idx, row in real_stocks_df.iterrows():
                        live_c_price, buy_price = float(row.get('_raw_price', 0)), float(row.get('_raw_buy', 0))
                        ticker_str = str(row['티커']).strip().zfill(6)
                        
                        qty_str = str(row.get('보유수량', '0 주')).replace(' 주', '').replace(',', '').strip()
                        try: qty_num = int(float(qty_str))
                        except: qty_num = 0
                        
                        profit_amt = (live_c_price - buy_price) * qty_num
                        current_holding_amt = live_c_price * qty_num
                        buy_tot_amt = buy_price * qty_num
                        
                        total_eval_sum += current_holding_amt
                        total_pnl_sum += profit_amt
                        total_buy_sum += buy_tot_amt
                        
                        if live_c_price <= 0:
                            tab2_anomaly_flag, tab2_anomaly_reason = True, f"[{ticker_str}] 실시간 현재가가 0원 이하({live_c_price}원)로 수신되었습니다. API 오류 또는 상장폐지 위험."
                            break

                        highest_price = p_data.get('ts_tracker', {}).get(ticker_str, buy_price)
                        if buy_price > 0:
                            new_highest = max(highest_price, live_c_price)
                            if new_highest > highest_price:
                                highest_price = new_highest
                                if 'ts_tracker' not in p_data: p_data['ts_tracker'] = {}
                                p_data['ts_tracker'][ticker_str] = highest_price
                                need_live_state_save = True
                        else: highest_price = 0.0

                        res = fetch_stock_status(ticker_str)
                        user_ret = ((live_c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                        
                        if not res or res[0] is None:
                            live_results.append({
                                '보유 종목명': row['종목명'], '🔥 매력도 점수': 0.0, '보유수량': f"{qty_num:,} 주",
                                '매수평균가': f"{buy_price:,.0f} 원", '실시간 현재가': f"{live_c_price:,.0f} 원", 
                                '평가금액': f"{current_holding_amt:,.0f} 원", '평가손익': f"{profit_amt:+,.0f} 원", '수익률': f"{user_ret:+.2f}%", 
                                '🤖 실계좌 전용 액션 플랜': "⚪ 모니터링 불가", '📊 판단 근거': "AI 분석용 과거 데이터 수신 실패"
                            })
                            continue
                            
                        yf_price, ma200, ma60, ma20, drawdown, _, _, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, yf_low, recent_vol_max, avg_trade_val = res

                        res_q = analyze_quant_strategy(active_strat, live_c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss)
                        
                        cd_info = p_data.get('cd_tracker', {}).get(ticker_str, {'losses': 0, 'until': '2000-01-01'})
                        cd_until = pd.to_datetime(cd_info['until']).date()
                        is_cooldown = datetime.date.today() < cd_until
                        
                        additional_amt = max(0, target_buy_amt - current_holding_amt)
                        add_qty = int(additional_amt // live_c_price)

                        action, reason = "-", "-"
                        
                        if res_q['stop_loss_cond']: 
                            action, reason = "🔴 긴급 손절 매도", f"손절 기준선({sat_stop_loss}%) 도달"
                        elif res_q['trailing_stop_cond']: 
                            action, reason = "🔵 트레일링 익절", f"최고가({highest_price:,.0f}원) 대비 목표하락폭 이탈"
                        elif res_q['exit_cond_trend']: 
                            action, reason = "🔴 전량 청산 (추세 이탈)", "핵심 지지선(추세) 하향 이탈"
                        elif is_cooldown:
                            action, reason = "⚪ 진입 쿨다운 (관망)", f"{cd_until.strftime('%m/%d')} 까지 신규 진입 차단"
                        elif res_q['entry_cond']:
                            if avg_trade_val < 5000000000:
                                action, reason = "🟡 유동성 필터 (매수 보류)", "5일 평균 거래대금 50억 미만 (슬리피지 방어)"
                            elif add_qty > 0: action, reason = f"🟢 비중 확대 유효 (+{add_qty:,}주)", "신규 진입 타점 조건 충족"
                            else: action, reason = "🟡 비중 도달 (포지션 홀딩)", f"목표 비중({current_max_alloc_pct}%) 기충족"
                        else: 
                            action, reason = "🟡 포지션 홀딩", "추세 방어 중 및 타점 대기"

                        if pd.isna(res_q['ai_score']) or np.isinf(res_q['ai_score']):
                            tab2_anomaly_flag, tab2_anomaly_reason = True, f"[{ticker_str}] AI 매력도 점수가 비정상(NaN/Inf)으로 도출되었습니다."
                            break
                            
                        # [V5.8 패치] 팻핑거 로직: 실계좌 평가금이 아닌 현재 기준 자산(current_asset_base)과 비교
                        if res_q['entry_cond'] and add_qty > 0:
                            if (add_qty * live_c_price) > (current_asset_base * 1.0):
                                tab2_anomaly_flag, tab2_anomaly_reason = True, f"[{ticker_str}] 산출 매수 금액({add_qty * live_c_price:,.0f}원)이 설정된 계좌 총 자산({current_asset_base:,.0f}원)을 초과하는 팻핑거 로직 감지."
                                break

                        live_results.append({
                            '보유 종목명': row['종목명'], '🔥 매력도 점수': res_q['ai_score'], '보유수량': f"{qty_num:,} 주",
                            '매수평균가': f"{buy_price:,.0f} 원", '실시간 현재가': f"{live_c_price:,.0f} 원", 
                            '평가금액': f"{current_holding_amt:,.0f} 원", '평가손익': f"{profit_amt:+,.0f} 원", 
                            '수익률': f"{user_ret:+.2f}%", '🤖 실계좌 전용 액션 플랜': action, '📊 판단 근거': reason
                        })
                    
                    if need_live_state_save: save_portfolio_to_sheets(selected_port, p_data)
                    
                    if tab2_anomaly_flag:
                        p_data['kill_switch'] = True
                        p_data['auto_trade_enabled'] = False
                        save_portfolio_to_sheets(selected_port, p_data)
                        send_telegram_message(f"🚨 [AI 관제 시스템 긴급 차단]\n사유: {tab2_anomaly_reason}\n계좌 보호를 위해 킬 스위치가 강제 가동되었습니다.")
                        st.error(f"🚨 **[시스템 이상 감지]** {tab2_anomaly_reason}")
                        st.error("안전을 위해 **자동매매가 영구 정지되며 킬 스위치가 강제로 가동**되었습니다.")
                        st.stop()
                    else:
                        live_df = pd.DataFrame(live_results)
                        if not live_df.empty:
                            live_df = live_df.sort_values(by="🔥 매력도 점수", ascending=False).reset_index(drop=True)
                            
                            total_ret_sum_pct = (total_pnl_sum / total_buy_sum * 100) if total_buy_sum > 0 else 0.0
                            summary_row = pd.DataFrame([{
                                '보유 종목명': '💡 [평가총액 합계]', '🔥 매력도 점수': '-', '보유수량': '-', '매수평균가': '-', '실시간 현재가': '-',
                                '평가금액': f"{total_eval_sum:,.0f} 원", '평가손익': f"{total_pnl_sum:+,.0f} 원", '수익률': f"{total_ret_sum_pct:+.2f}%",
                                '🤖 실계좌 전용 액션 플랜': '-', '📊 판단 근거': '-'
                            }])
                            live_df = pd.concat([live_df, summary_row], ignore_index=True)
                            st.table(live_df)
            else:
                st.info("현재 실전 계좌에 매수(보유) 중인 종목이 없습니다. [탭 1]의 관심종목 리스트에서 타점을 대기하세요.")

with tab3:
    st.header("🤖 실전 자동매매 관제센터 & 우선순위 주문 대기열")
    
    col_c1, col_c2, col_c3, col_c4 = st.columns(4)
    col_c1.metric("🚨 킬 스위치 (Kill Switch)", "작동 중 (차단)" if kill_switch else "정상 (대기)")
    col_c2.metric("🚀 자동주문 상태", "활성화 (Auto)" if auto_trade_enabled else "비활성화 (Manual)")
    col_c3.metric("🔄 무인 감시 (Auto-Pilot)", f"가동 중 ({check_min}분)" if auto_pilot else "중지됨")
    col_c4.metric("💵 가용 예수금", f"{real_cash_avail:,.0f} 원")
    st.markdown("---")
    
    temp_queue = []
    eligible_stocks = {}
    if p_data and 'stocks' in p_data:
        for s in p_data['stocks']: eligible_stocks[str(s['티커']).strip().zfill(6)] = s.get('종목명', '')
    if SYS_APP_KEY and kis_data and not real_stocks_df.empty:
        for idx, row in real_stocks_df.iterrows(): eligible_stocks[str(row['티커']).strip().zfill(6)] = row.get('종목명', '')
            
    tab3_anomaly_flag = False
    tab3_anomaly_reason = ""
    need_queue_state_save = False
            
    for ticker, s_name in eligible_stocks.items():
        qty_num, buy_price, live_c_price = 0, 0.0, 0.0
        
        if SYS_APP_KEY and kis_data and not real_stocks_df.empty:
            match_row = real_stocks_df[real_stocks_df['티커'] == ticker]
            if not match_row.empty:
                r = match_row.iloc[0]
                qty_str = str(r.get('보유수량', '0 주')).replace(' 주', '').replace(',', '').strip()
                try: qty_num = int(float(qty_str))
                except: qty_num = 0
                buy_price = float(r.get('_raw_buy', 0))
                live_c_price = float(r.get('_raw_price', 0))

        res = fetch_stock_status(ticker)
        if not res or res[0] is None: continue 
        
        c_p, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, yf_low, recent_vol_max, avg_trade_val = res
        
        if qty_num == 0:
            live_c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if kis_token_global else c_p
            if not live_c_price: live_c_price = c_p
            
        if live_c_price <= 0:
            tab3_anomaly_flag, tab3_anomaly_reason = True, f"[{ticker}] 실시간 현재가가 0원 이하({live_c_price}원)로 수신되었습니다. API 오류 의심."
            break

        highest_price = p_data.get('ts_tracker', {}).get(ticker, buy_price)
        if buy_price > 0:
            new_highest = max(highest_price, live_c_price)
            if new_highest > highest_price:
                highest_price = new_highest
                if 'ts_tracker' not in p_data: p_data['ts_tracker'] = {}
                p_data['ts_tracker'][ticker] = highest_price
                need_queue_state_save = True
        else: highest_price = 0.0

        res_q = analyze_quant_strategy(active_strat, live_c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss)

        if pd.isna(res_q['ai_score']) or np.isinf(res_q['ai_score']):
            tab3_anomaly_flag, tab3_anomaly_reason = True, f"[{ticker}] AI 매력도 점수 이상 수치(NaN/Inf) 감지."
            break

        cd_info = p_data.get('cd_tracker', {}).get(ticker, {'losses': 0, 'until': '2000-01-01'})
        cd_until = pd.to_datetime(cd_info['until']).date()
        is_cooldown = datetime.date.today() < cd_until

        is_sell, sell_type = False, ""
        if qty_num > 0:
            if res_q['stop_loss_cond']: is_sell, sell_type = True, "🔴 긴급 손절 매도"
            elif res_q['trailing_stop_cond']: is_sell, sell_type = True, "🔵 트레일링 익절"
            elif res_q['exit_cond_trend']: is_sell, sell_type = True, "🔴 추세 이탈 매도"
                
        if is_sell:
            status_text = "대기 중"
            if kill_switch: status_text = "🚨 킬 스위치 차단됨"
            elif not auto_trade_enabled: status_text = "⏸️ 자동주문 비활성"
            temp_queue.append({
                '우선순위_분류': 0, '🔥 점수': 999.0, '종목명': s_name, '티커': ticker, '구분': sell_type,
                '목표 주가': f"{live_c_price:,.0f} 원", '목표 주문 수량': f"{qty_num:,} 주", '필요 자금': f"-{live_c_price * qty_num:,.0f} 원 (회수)",
                '_raw_price': live_c_price, '_buy_price': buy_price, '_qty': qty_num, '_req_fund': 0, '주문 실행 상태': status_text
            })
            continue 

        if res_q['entry_cond'] and not is_cooldown and avg_trade_val >= 5000000000:
            # [V5.8 패치] 탭 3에서도 현재 기준 자산을 명확히 정의
            current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
            if current_asset_base <= 0: current_asset_base = total_cash
                
            is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
            current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
            
            target_buy_amt = current_asset_base * (current_max_alloc_pct / 100.0)
            current_holding_amt = qty_num * live_c_price
            additional_amt = max(0, target_buy_amt - current_holding_amt)
            add_qty = int(additional_amt // live_c_price)
            
            # [V5.8 패치] 팻핑거 비교 대상을 current_asset_base 로 완벽 교체
            if add_qty > 0 and (add_qty * live_c_price) > (current_asset_base * 1.0):
                tab3_anomaly_flag, tab3_anomaly_reason = True, f"[{ticker}] 산출 매수 금액({add_qty * live_c_price:,.0f}원)이 설정된 계좌 총 자산({current_asset_base:,.0f}원)을 초과하는 팻핑거 로직 감지."
                break
            
            if add_qty > 0:
                req_fund = add_qty * live_c_price
                status_text = "대기 중"
                buy_type = "🛒 신규 매수" if qty_num == 0 else "🟢 비중 확대"
                
                temp_queue.append({
                    '우선순위_분류': 1, '🔥 점수': res_q['ai_score'], '종목명': s_name, '티커': ticker, '구분': buy_type,
                    '목표 주가': f"{live_c_price:,.0f} 원", '목표 주문 수량': f"{add_qty:,} 주", '필요 자금': f"{req_fund:,.0f} 원",
                    '_raw_price': live_c_price, '_buy_price': 0, '_qty': add_qty, '_req_fund': req_fund, '주문 실행 상태': status_text
                })

    if need_queue_state_save: save_portfolio_to_sheets(selected_port, p_data)

    if tab3_anomaly_flag:
        p_data['kill_switch'] = True
        p_data['auto_trade_enabled'] = False
        save_portfolio_to_sheets(selected_port, p_data)
        send_telegram_message(f"🚨 [AI 관제 시스템 긴급 차단]\n사유: {tab3_anomaly_reason}\n계좌 보호를 위해 킬 스위치가 강제 가동되었습니다.")
        st.error(f"🚨 **[시스템 이상 감지 및 관제 차단 작동]**")
        st.error(f"사유: {tab3_anomaly_reason}")
        st.error("안전을 위해 **대기열 생성을 파기하고 킬 스위치가 강제로 가동**되었습니다.")
    else:
        st.success("🛡️ AI 이상 감지 스캐너 작동 중: 현재 수신된 모든 데이터 및 산출 로직이 정상(무결성 100%)입니다.")
        
        temp_queue = sorted(temp_queue, key=lambda x: (x['우선순위_분류'], -x['🔥 점수']))
        
        sim_cash = real_cash_avail
        order_queue = []
        for q in temp_queue:
            if q['우선순위_분류'] == 0: 
                sim_cash += q['_qty'] * q['_raw_price']
                q['주문 실행 상태'] = "대기 중" if not kill_switch and auto_trade_enabled else ("🚨 킬 스위치 차단됨" if kill_switch else "⏸️ 자동주문 비활성")
                order_queue.append(q)
            else: 
                if sim_cash >= q['_req_fund']:
                    q['주문 실행 상태'] = "대기 중" if not kill_switch and auto_trade_enabled else ("🚨 킬 스위치 차단됨" if kill_switch else "⏸️ 자동주문 비활성")
                    sim_cash -= q['_req_fund']
                    order_queue.append(q)
                else:
                    aff_qty = int(sim_cash // q['_raw_price'])
                    if aff_qty > 0:
                        if not kill_switch and auto_trade_enabled:
                            q['주문 실행 상태'] = f"🔄 부분 매수 대기 (가능: {aff_qty}주)"
                        q['목표 주문 수량'] = f"{q['_qty']} 주 ➡️ {aff_qty} 주"
                        sim_cash -= (aff_qty * q['_raw_price'])
                        q['_qty'] = aff_qty
                        order_queue.append(q)
                    else:
                        if not kill_switch and auto_trade_enabled:
                            q['주문 실행 상태'] = "⚠️ 예수금 부족 (스킵)"
                        order_queue.append(q)
        
        queue_df = pd.DataFrame(order_queue)
        
        if not queue_df.empty:
            queue_df['우선순위'] = [f"{i+1}위" for i in range(len(queue_df))]
            
            display_queue = queue_df.copy()
            display_queue['🔥 점수'] = display_queue['🔥 점수'].apply(lambda x: "🚨 최우선 (매도)" if float(x) >= 900.0 else f"{float(x):.2f}")
            
            st.subheader("📋 AI 매매 우선순위 대기열 (Order Queue)")
            st.table(display_queue[['우선순위', '종목명', '구분', '🔥 점수', '목표 주가', '목표 주문 수량', '필요 자금', '주문 실행 상태']])
            
            st.markdown("---")
            if auto_trade_enabled: btn_label, btn_type = "⚡ 자동 감시 주기 무시하고 즉시 강제 집행 (입금 직후용)", "secondary"
            else: btn_label, btn_type = "⚡ 대기열 일괄 주문 수동 전송", "primary"
                
            if st.button(btn_label, type=btn_type, use_container_width=True):
                if kill_switch: st.error("🚨 킬 스위치가 활성화되어 있어 주문을 전송할 수 없습니다.")
                elif not auto_trade_enabled and btn_type == "secondary": st.warning("🚀 사이드바에서 '실전 자동주문 활성화' 스위치를 켜주세요.")
                else:
                    with st.spinner("우선순위 대기열 KIS 리얼 서버 순차 주문 전송 중..."):
                        exec_msgs, needs_save = [], False
                        for idx, q_row in queue_df.iterrows():
                            s_name, t_code, q_type = q_row['종목명'], q_row['티커'], q_row['구분']
                            raw_qty, raw_price, buy_p = q_row['_qty'], q_row['_raw_price'], q_row['_buy_price']
                            
                            if "매도" in q_type or "익절" in q_type:
                                succ, msg = execute_kis_order(SYS_APP_KEY, SYS_APP_SECRET, kis_token_global, SYS_CANO, SYS_ACNT_PRDT, t_code, raw_qty, raw_price, order_type="SELL", is_market=True, is_mock=SYS_IS_MOCK)
                                if succ:
                                    p_data = log_daily_trade(p_data, s_name, "SELL", raw_price, raw_qty, buy_p, status="✅ 체결완료", msg="시장가 매도 접수")
                                    exec_msgs.append(f"▪️ [{q_type}] *{s_name}*: {msg}")
                                    
                                    pnl = (raw_price - buy_p) * raw_qty
                                    if 'cd_tracker' not in p_data: p_data['cd_tracker'] = {}
                                    cd_i = p_data['cd_tracker'].get(t_code, {'losses': 0, 'until': '2000-01-01'})
                                    if pnl < 0:
                                        cd_i['losses'] += 1
                                        if cd_i['losses'] >= 2:
                                            cd_i['until'] = (datetime.date.today() + datetime.timedelta(days=cooldown_days)).strftime('%Y-%m-%d')
                                    else: cd_i['losses'] = 0
                                    p_data['cd_tracker'][t_code] = cd_i
                                    if 'ts_tracker' in p_data and t_code in p_data['ts_tracker']: del p_data['ts_tracker'][t_code]
                                    needs_save = True
                                else: 
                                    p_data = log_daily_trade(p_data, s_name, "SELL", raw_price, raw_qty, buy_p, status="❌ 주문실패", msg=msg)
                                    exec_msgs.append(f"❌ [{q_type} 실패] *{s_name}*: {msg}")
                                    needs_save = True
                                    
                            elif "매수" in q_type or "확대" in q_type:
                                if "스킵" in q_row['주문 실행 상태']:
                                    p_data = log_daily_trade(p_data, s_name, "BUY", raw_price, raw_qty, status="⚠️ 매수스킵", msg="예수금 부족으로 차순위 큐 진행")
                                    needs_save = True
                                    continue

                                succ, msg = execute_kis_order(SYS_APP_KEY, SYS_APP_SECRET, kis_token_global, SYS_CANO, SYS_ACNT_PRDT, t_code, raw_qty, raw_price, order_type="BUY", is_market=False, is_mock=SYS_IS_MOCK)
                                if succ:
                                    p_data = log_daily_trade(p_data, s_name, "BUY", raw_price, raw_qty, status="✅ 체결완료", msg="지정가 매수 접수")
                                    real_cash_avail -= (raw_qty * raw_price)
                                    if "부분 매수" in q_row['주문 실행 상태']:
                                        exec_msgs.append(f"🔄 [{q_type} 부분 체결] *{s_name}*: 예수금 한도 내 {raw_qty}주 매수 완료")
                                    else:
                                        exec_msgs.append(f"▪️ [{q_type}] *{s_name}*: {raw_qty}주 매수 완료")
                                    needs_save = True
                                else: 
                                    p_data = log_daily_trade(p_data, s_name, "BUY", raw_price, raw_qty, status="❌ 주문실패", msg=msg)
                                    exec_msgs.append(f"❌ [{q_type} 실패] *{s_name}*: {msg}")
                                    needs_save = True
                        
                        if needs_save: save_portfolio_to_sheets(selected_port, p_data)
                        if exec_msgs:
                            if tg_noti_order:
                                send_telegram_message("🤖 *[주문 전송 집행 결과]*\n" + "\n".join(exec_msgs))
                            st.success("주문 집행이 완료되었습니다!")
                            time.sleep(1)
                            try: st.query_params["auth"] = daily_token
                            except: st.experimental_set_query_params(auth=daily_token)
                            st.rerun()
        else: st.info("💡 현재 AI 퀀트 엔진이 포착한 신규 매수 또는 매도 시그널이 없습니다.")

    st.markdown("---")
    st.subheader("📜 당일 매매(API 전송) 내역 및 실적 요약")
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    if p_data and p_data.get('daily_trades_date') == today_str and p_data.get('daily_trades'):
        trades_df = pd.DataFrame(p_data['daily_trades'])
        
        succ_df = trades_df[trades_df['상태'].str.contains('✅')] if '상태' in trades_df.columns else trades_df
        total_buy = succ_df[succ_df['주문 구분'] == '매수(진입)']['체결 금액'].sum() if not succ_df.empty else 0
        total_sell = succ_df[succ_df['주문 구분'] == '매도(청산)']['체결 금액'].sum() if not succ_df.empty else 0
        total_pnl = succ_df['실현 손익'].sum() if not succ_df.empty else 0
        
        c1, c2, c3 = st.columns(3)
        c1.metric("🛒 당일 체결된 총 매수대금", f"{total_buy:,.0f} 원")
        c2.metric("💸 당일 체결된 총 매도대금", f"{total_sell:,.0f} 원")
        c3.metric("🎯 당일 확정 실현손익", f"{total_pnl:+,.0f} 원")
        
        display_trades = trades_df.copy()
        if '상태' not in display_trades.columns: display_trades['상태'] = "✅ 체결완료"
        if '비고 (API 메시지)' not in display_trades.columns: display_trades['비고 (API 메시지)'] = "-"
            
        display_trades['체결 단가'] = display_trades['체결 단가'].apply(lambda x: f"{x:,.0f} 원")
        display_trades['체결 수량'] = display_trades['체결 수량'].apply(lambda x: f"{x:,} 주")
        display_trades['체결 금액'] = display_trades['체결 금액'].apply(lambda x: f"{x:,.0f} 원")
        display_trades['실현 손익'] = display_trades.apply(lambda row: f"{row['실현 손익']:+,.0f} 원" if row['주문 구분'] == '매도(청산)' and '✅' in row['상태'] else "-", axis=1)
        
        st.dataframe(display_trades, use_container_width=True)
    else: st.info("오늘 KIS API 엔진을 통해 체결 시도된 거래 내역이 없습니다.")

with tab4:
    st.header("🧪 시뮬레이션 및 백테스트 (Simulation & Backtest)")
    if not p_data or not selected_port: 
        st.warning("포트폴리오가 없습니다.")
    else:
        stocks_df = pd.DataFrame(p_data.get('stocks', []))
        today_date = datetime.date.today()
        
        st.subheader("🎯 포워드 테스트 (Forward Test) vs 실전 계좌 성적")
        
        with st.expander("⚙️ 실전 계좌 누적 수익률 보정 및 기준일 설정", expanded=False):
            st.markdown("포트폴리오 시작일(포워드 테스트 기준일)과 자동매매 봇 가동 전에 발생한 과거 실현 손익 보정값을 입력하세요.")
            c_d1, c_d2, c_d3 = st.columns([3, 4, 3])
            new_date = c_d1.date_input("📅 포트폴리오 시작일", real_base_date, key="t4_date")
            new_offset = c_d2.number_input("과거 실현 손익 누적액 (원)", value=int(manual_offset), step=100000, key="t4_offset")
            if c_d3.button("💾 수익률 기준 저장", use_container_width=True, key="t4_save"):
                p_data['real_base_date'] = new_date.strftime('%Y-%m-%d')
                p_data['pnl_offset'] = new_offset
                save_portfolio_to_sheets(selected_port, p_data)
                st.success("✅ 성과 측정 기준이 업데이트되었습니다.")
                try: st.query_params["auth"] = daily_token
                except: st.experimental_set_query_params(auth=daily_token)
                st.rerun()
                
        st.markdown(f"설정된 기준일(`{real_base_date}`)부터 오늘까지 관심종목 유니버스를 바탕으로 AI 전략을 가동했을 때의 **이론적 누적 수익률**과, 고객님의 **실계좌 수익률**을 나란히 비교합니다.")

        if st.button("▶️ 포워드 테스트 1:1 비교 실행", use_container_width=True):
            if stocks_df.empty: 
                st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner(f"{real_base_date} 부터 현재까지 초고속 벡터 연산 AI 시뮬레이션 중..."):
                    fw_result = run_quant_simulation(stocks_df, active_strat, total_invested_principal, real_base_date, today_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if fw_result:
                        st.markdown("### 🏆 누적 수익률 비교 (Yield Comparison)")
                        col_fw1, col_fw2 = st.columns(2)
                        col_fw1.metric("📈 AI 포워드 테스트 (이론)", f"{fw_result['final_port_ret']:+.2f}%", f"기말 자산: {fw_result['final_asset']:,.0f} 원")
                        if SYS_APP_KEY and kis_data:
                            total_pnl_all = real_eval_pnl + cumulative_realized_pnl + manual_offset
                            real_ret_pct_custom = (total_pnl_all / total_invested_principal * 100) if total_invested_principal > 0 else 0
                            col_fw2.metric("🔌 나의 실전 계좌 (실제)", f"{real_ret_pct_custom:+.2f}%", f"현재 자산: {real_total_eval:,.0f} 원")
                        else: col_fw2.info("한국투자증권 API 연동이 필요합니다.")
                            
                        st.markdown("---")
                        st.markdown("### 🔍 종목별 상세 매매 & 보유 현황 비교")
                        real_summary = {item['종목명']: item for item in kis_data['stocks']} if kis_data else {}
                        ai_summary = {item['종목명']: item for item in fw_result['summary_rows'] if item['종목명'] != '💡 [전체 합계]'}
                        all_stocks = sorted(list(set(list(ai_summary.keys()) + list(real_summary.keys()))))

                        comp_data = []
                        for name in all_stocks:
                            ai_data = ai_summary.get(name)
                            rl_data = real_summary.get(name)
                            if ai_data:
                                ai_qty, ai_trades = ai_data['최종 보유 주수'], ai_data['매매 횟수']
                                ai_display = f"{ai_data['총 순수익 (원)'].replace(' 원', '').strip()}원 ({ai_data['수익률 (%)']})"
                            else: ai_qty, ai_trades, ai_display = "0 주", "매수 0회 / 매도 0회", "0원 (0.00%)"

                            if rl_data:
                                rl_qty_num = int(str(rl_data.get('보유수량', '0')).replace(' 주', '').replace(',', ''))
                                rl_qty, rl_pct = f"{rl_qty_num:,} 주", rl_data.get('평가손익률', '0.00%')
                                rl_prof_amt = (float(rl_data.get('_raw_price', 0)) - float(rl_data.get('_raw_buy', 0))) * rl_qty_num
                                rl_display = f"{rl_prof_amt:+,.0f}원 ({rl_pct})"
                            else: rl_qty, rl_display = "0 주", "0원 (0.00%)"

                            comp_data.append({"종목명": name, "🤖 AI 잔고": ai_qty, "🤖 누적손익": ai_display, "🔌 실계좌 잔고": rl_qty, "🔌 평가손익": rl_display})
                        st.dataframe(pd.DataFrame(comp_data), use_container_width=True)
                    else: st.warning("데이터가 부족하여 시뮬레이션을 완료할 수 없습니다.")

        st.markdown("---")
        st.subheader("📊 장기 초과수익 검증 (Long-Term Backtest)")
        col_sim1, col_sim2 = st.columns(2)
        with col_sim1: start_date = st.date_input("시작일", datetime.date(2023, 1, 1))
        with col_sim2: end_date = st.date_input("종료일", today_date)

        if st.button(f"🚀 장기 Backtest 실행", type="secondary", use_container_width=True):
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner(f"벤치마크 퀀트 백테스트 구동 중... (약 15초 소요)"):
                    bt_result = run_quant_simulation(stocks_df, active_strat, total_cash, start_date, end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if bt_result:
                        st.success(f"✅ 장기 백테스트 실행 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric("총 초기 투입 자산", f"{total_cash:,.0f} 원")
                        col_r2.metric("AI 초과수익 기말 자산", f"{bt_result['final_asset']:,.0f} 원", f"{bt_result['final_port_ret']:+.2f}%")
                        st.table(pd.DataFrame(bt_result['summary_rows']))
                        chart = alt.Chart(bt_result['eom_weights_reset']).mark_bar().encode(
                            x=alt.X('Date:O', title=''), y=alt.Y('Weight:Q', title='비중 (%)', stack='zero'),
                            color=alt.Color('Asset:N', scale=alt.Scale(domain=bt_result['cols_ordered'], range=bt_result['color_range'])), order=alt.Order('Order:Q')
                        ).properties(height=450)
                        st.altair_chart(chart, use_container_width=True)

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 (v5.8 Final Master)</h1>
    <p style='text-align: center; font-size: 1.1em; color: #4B5563;'>본 보고서는 <strong>Core-Satellite Quant System</strong>에 탑재된 AI 매매 엔진의 전략 기획서 및 핵심 로직 명세서입니다.</p>
    <hr>
    
    ## 1. 🏛️ 핵심 투자 철학: Core-Satellite 듀얼 엔진
    본 시스템은 시장의 방어적 추세 추종(Core)과 공격적인 알파 창출(Satellite)을 분리하여 운용하는 **듀얼 엔진 아키텍처**를 채택하고 있습니다.
    
    | 구분 | 대형주 (Core / 핵심 자산) | 중소형주 (Satellite / 위성 자산) |
    | :--- | :--- | :--- |
    | **운용 목표** | 안정적인 시장 우상향 추종 및 방어적 복리 누적 | 시장 주도주 발굴 및 단기 초과 알파 수익 창출 |
    | **핵심 타점** | 200일선 기반 **골든크로스 추세 추종** | 수급 폭발 후 **20일선 눌림목 스윙** |
    | **투입 한도** | 종목당 기본 35% (강세장 최대 52.5%) | 종목당 기본 20% (강세장 최대 30%) |
    
    ---

    ## 2. 🧠 엔진별 매수/매도 알고리즘 상세 명세
    
    ### 🏛️ [전략 A] 대형주 (Core) - 중장기 추세 추종
    시가총액 상위 대형주를 대상으로, 기관과 외국인의 거대한 수급 추세가 형성되는 초입(Golden Cross)을 포착하여 안정적으로 수익을 누적합니다.
    
    *   **📌 타겟 유니버스:** KOSPI 시가총액 상위 100종목
    *   **🟢 AI 진입 조건 (모두 만족 시):**
        1.  **장기 추세 방어:** 현재 주가가 200일 이동평균선(MA200) 이상에 위치 (안전구간 확인).
        2.  **중기 추세 상승:** 60일 이동평균선(MA60)의 기울기가 양수 (현재 MA60 > 10일 전 MA60).
        3.  **단기 모멘텀:** 20일 전 주가 대비 현재 주가가 상승 (단기 수익률 > 0%).
        4.  **골든크로스 안착:** 20일 이동평균선이 60일선을 상향 돌파하고, 휩소 방지 버퍼(기본 `1.5%`) 이상 격차를 벌린 확실한 추세 형성 구간. `[MA20 >= MA60 * (1 + 0.015)]`
        5.  **시장 안정:** VIX(공포지수)가 30 미만으로 시장이 패닉 상태가 아닐 것.
    *   **🔴 AI 이탈(매도/퇴출) 조건:**
        1.  **추세 붕괴 (Dead Cross):** 20일 이동평균선이 60일 이동평균선을 하향 이탈하려는 징후 발생 시 전량 기계적 매도 및 관심종목 퇴출. `[MA20 < MA60 * (1 - 0.0075)]`
    
    ### 🚀 [전략 B] 중소형주 (Satellite) - 스마트 수급 눌림목
    시장에 강력한 테마가 형성되어 돈이 몰린 주도주를 필터링하고, 해당 종목이 단기 조정을 받을 때(눌림목) 진입하여 기술적 반등을 노립니다.
    
    *   **📌 타겟 유니버스:** KOSDAQ 시가총액 1,000억 원 이상 상위 종목
    *   **🟢 AI 진입 조건 (모두 만족 시):**
        1.  **장기 추세 방어:** 현재 주가가 200일 이동평균선(MA200) 이상에 위치.
        2.  **수급(거래량) 폭발 🚀:** 최근 20일 이내에, 거래량이 5일 평균 거래량 대비 **200% 이상 급증**한 이력이 존재하는 명백한 주도주.
        3.  **스마트 눌림목 포착:** 단기 급등 후 조정을 받아 현재 주가가 20일선 부근(`-5% ~ +3%`)으로 조정을 받았거나, 당일 저가가 20일선을 터치(`1.01배 이내`)하고 지지를 받으며 꼬리를 만듦.
        4.  **하방 리스크 제한:** 최근 120일 최고가 대비 하락폭(MDD)이 `-30%` 이내일 것 (심각한 악재로 인한 폭락 방지).
    *   **🔴 AI 이탈(매도/손절/퇴출) 조건:**
        1.  **추세 붕괴 (Support Break):** 주가가 20일 이동평균선을 하향 이탈하면 상승 동력이 꺾인 것으로 간주하여 전량 매도 및 관심종목 퇴출.
        2.  **강제 손절 컷 (Stop Loss):** 매수 단가 대비 수익률이 **`-12%`** (사용자 설정 가능) 도달 시, 지표와 무관하게 즉각적인 기계적 손절 집행.
    
    ---
    
    ## 3. 🌍 거시경제 및 마켓 타이밍 엔진 (Macro Regime)
    개별 종목의 타점이 완벽하더라도 시장 전체가 무너지면 승률이 급감합니다. 이를 방어하기 위한 3중 거시 필터가 작동합니다.
    
    *   **🚨 VIX 공포지수 브레이크 (VIX Safe):**
        *   미국 S&P 500 VIX 지수가 **`30`**을 초과하는 시스템 리스크 구간에서는 모든 신규 매수를 전면 동결합니다.
    *   **🔥 극한의 역발상 매수 (VIX Contrarian):**
        *   VIX 지수가 **`25 이상`**으로 치솟아 투매가 발생했으나, 단기 이동평균선(3일선)을 깨고 내려오는 **'공포 극점 확인 후 회복 초기'** 단계에서는, 모든 보조지표 조건을 무시하고 시장의 낙폭 과대 반등을 노려 즉시 공격적 매수를 집행합니다.
    *   **🐃 강세장 자금 풀 부스터 (Bull Market Boost):**
        *   KOSPI/KOSDAQ 지수가 60일 이동평균선 위에 있는 완연한 강세장(Bull Market)으로 판별될 경우, 엔진이 자동으로 리스크를 낮게 평가하여 종목당 투입 자금 한도를 **기본값의 1.5배**로 확장합니다. (예: 대형주 35% -> 52.5% 상향)
    *   **VIX 통신 장애 Fallback 시스템:** 해외 Yahoo Finance 통신 오류 시, KOSPI 지수의 최근 20일 변동성(MDD)을 내부적으로 역산하여 시장 공포도를 대리 판독하는 2중 안전망이 가동됩니다.

    ---
    
    ## 4. 🛡️ AI 리스크 매니지먼트 (Risk Control)
    수익을 내는 것만큼 중요한 것은 번 돈을 지키고 뇌동매매를 방지하는 것입니다.
    
    *   **🎣 라이브 트레일링 스탑 (Live Trailing Stop):**
        *   수익률이 목표치(대형주 `30%`, 중소형주 `15%`)를 돌파하면 시스템은 즉각 익절 대기 모드로 전환됩니다.
        *   이후 DB에 실시간으로 **'매수 이후 최고 도달가(Highest Price)'를 추적 및 기록**하며, 이 고점 대비 특정 비율(`-10%` 또는 `-5%` 수준)만큼 꺾이는 순간 기계적인 시장가 익절 매도를 집행하여 최대 수익을 보존합니다.
    *   **🥶 실전 쿨다운 시스템 (Live Cooldown):**
        *   특정 종목에서 연속으로 2회 이상 손실(매도/손절)이 발생하면, 해당 종목은 구글 DB(블랙리스트)에 등재되어 설정된 기간(기본 `60일`) 동안 어떠한 매수 시그널이 떠도 진입을 강제 거부당합니다. (휩소 구간에서 계좌가 녹는 현상 원천 차단)
    *   **💧 유동성 필터 (Liquidity Filter):**
        *   중소형주의 호가 공백으로 인한 막대한 슬리피지(Slippage)를 피하기 위해, 최근 5일 평균 거래대금이 **50억 원 미만**인 종목은 시그널이 발생해도 매수를 보류(관망)합니다.
    
    ---
    
    ## 5. ⚙️ 자동매매 우선순위 & 다이내믹 자금 배분 로직
    동시에 여러 종목의 매수 시그널이 발생할 경우, 한정된 예수금 내에서 효율적으로 자본을 배분하기 위해 **AI 매력도 스코어링**을 산출하여 매수 우선순위를 정합니다.
    *   **1순위 매도 (Emergency Exit & Override):** 손절 및 추세 이탈 종목을 최우선 청산하여 현금을 확보합니다. 매수보다 무조건 선행하도록 내부 시스템 점수 **`🚨 최우선 (매도)`**를 강제 할당합니다.
    *   **2순위 매수 (Score-based Allocation):** AI 매력도 점수가 높은 종목 순서대로 한정된 가용 예수금을 순차적 배분합니다.
    *   **다이내믹 자금 배분 (Dynamic Position Sizing & Skip):** 매수 큐 진행 중 예수금이 부족해지면 무조건 매수를 포기하지 않습니다. 가용 현금 내에서 살 수 있는 만큼 수량을 깎아서 **부분 매수(Partial Fill)**를 진행하며, 1주도 살 수 없는 경우 즉시 **스킵(Skip)**하고 다음 차순위 유망 종목의 매수를 시도하는 지능형 자금 융통 알고리즘이 적용되어 있습니다.
    *   **매수/매도 지정 로직:** 시장가 주문 시 증권사에서 요구하는 과도한 상한가 증거금 차단 현상을 우려해, 모든 신규 매수는 타겟팅된 가격의 **지정가(Limit Order)**로 쏘아 예수금 누수를 방어하며, 손절/익절 등 청산 주문은 1초라도 빨리 탈출하기 위해 **시장가(Market Order)**로 강제 집행됩니다.

    ---

    ## 6. 🚨 자동매매 페일세이프 (Fail-Safe) 및 통합 관제
    자동매매가 예상치 못한 시장 상황이나 프로그램 오류로 인해 계좌를 망가뜨리지 않도록 최우선 보안 장치가 결합되어 있습니다.
    *   **무작위 재시도 금지 (No Blind Retry):** 주문 실패 시 이전 조건을 맹목적으로 반복하지 않으며, 실시간 호가 및 조건을 재검증합니다.
    *   **하드코딩 킬 스위치 (Kill Switch):** 활성화 즉시 어떠한 상황에서도 모든 KIS API 매매 호출이 차단되어 계좌를 안전하게 보호합니다.
    *   **수학적 팩트 기반 입출금 추적:** 기존의 부정확한 수동 입출금 입력 방식을 폐기하고, `현재 계좌 총 평가금 - 보유 주식 평가손익 - 봇 누적 실현손익` 공식을 통해 외부에서 입출금된 순수 투입 원금을 1원 단위까지 100% 정확하게 자동 역산합니다.
    *   **통합 평가총액 집계 및 무결성 표출:** 보유 종목별 개별 평가금액(`수량 × 현재가`)과 실계좌 보유 주식의 전체 평가총액/평가손익/수익률을 자동 집계하여 최하단 요약행에 직관적으로 표시합니다.
    *   **보안 로그인 및 세션 영구 보존:** SHA-256 해시 기반의 보안 로그인 기능과 Daily URL 인증 토큰을 통해, 모바일 화면 꺼짐이나 오토파일럿의 브라우저 새로고침 발생 시에도 로그인 세션이 절대 해제되지 않도록 방어합니다.
    *   **데이터 무결성 스위칭:** 클라우드 서버의 잦은 IP 차단에 대비하여 불안정한 Yahoo Finance를 배제하고, 한국거래소(KRX)와 Naver 데이터를 직접 파싱하는 FinanceDataReader 전용 엔진으로 데이터 소스를 전면 교체하여 끊김 없는 시그널 분석을 제공합니다.
    *   **AI 무결성 관제견 (Anomaly Supervisor):** 단가가 `0`원이거나, 산출된 매수 금액이 전체 계좌 자산의 100%를 초과하는 등(팻 핑거)의 논리적 오류가 단 1개라도 감지되면 즉각적으로 대기열 파기, 자동주문 정지, 킬 스위치 가동 및 텔레그램 SOS를 발송하여 내 계좌를 안전하게 수호합니다.
    *   **[V5.8] 팻핑거 오탐지 방지 동적 스위칭:** 실전 API 연동 전 가상 시뮬레이션 상태일 경우, 팻핑거 관제 로직이 실계좌 잔고(0원)가 아닌 현재 설정된 가상 투자금을 기준으로 동적 비교하도록 완벽하게 교정되었습니다.
    *   **실패 로그 완전 기록(Full Trace Logging):** 증권사 API 통신 실패, 잔고 부족 등 모든 주문 거절 사유가 "당일 매매 일지"에 실시간으로 상세히(Status, Reason) 기록되어, 사용자가 HTS를 켜지 않고도 대시보드 내에서 즉각적인 원인 분석 및 대처가 가능하도록 설계되었습니다.
    """, unsafe_allow_html=True)
