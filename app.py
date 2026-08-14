import streamlit as st
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
import concurrent.futures
from google.oauth2.service_account import Credentials
import warnings

# 공통 두뇌 로드
import quant_engine as qe 

warnings.filterwarnings('ignore')

# ==========================================
# 0. 페이지 설정, 보안 및 타임존(KST) 셋팅
# ==========================================
st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")

KST = datetime.timezone(datetime.timedelta(hours=9))
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
    today_str = datetime.datetime.now(KST).strftime('%Y-%m-%d')
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
# 1. 헬퍼 함수 모음 (통신, DB)
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
    today_str = datetime.datetime.now(KST).strftime('%Y-%m-%d')
    now_str = datetime.datetime.now(KST).strftime('%H:%M:%S')
    
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
    
    # [핵심] 구글 시트 5만자 에러 방지: 최근 30개만 보관
    if len(p_data['daily_trades']) > 30:
        p_data['daily_trades'] = p_data['daily_trades'][-30:]
        
    return p_data

# ==========================================
# 2. 공통 두뇌(quant_engine) 연결 브릿지
# ==========================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_krx_universe():
    return qe.load_krx_universe()

@st.cache_data(ttl=1800)
def fetch_market_data():
    return qe.fetch_market_data()

@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code):
    return qe.fetch_stock_status(ticker_code)

def analyze_quant_strategy(*args, **kwargs):
    return qe.analyze_quant_strategy(*args, **kwargs)

@st.cache_data(ttl=3600)
def run_core_scanner(use_ma200, buf_pct):
    krx = load_krx_universe()
    buf = buf_pct / 100.0
    if krx.empty: return pd.DataFrame()
    
    if 'Marcap' in krx.columns and 'Market' in krx.columns:
        cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(100)
    elif 'Market' in krx.columns:
        cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].head(100)
    else: cands = krx.head(100)
    
    def process_stock(row):
        tc = str(row['Code']).strip().zfill(6)
        s = fetch_stock_status(tc)
        if s is None: return None
        c_p, ma200, ma60, ma20, dd, vr, r60, r20, m60_up, is_a200, vs, c_l, rvm, atv, dsp, vc = s
        if ((not use_ma200) or is_a200) and (ma20 >= ma60 * (1 + buf)) and m60_up and (r20 > 0) and atv >= 5000000000:
            return {'종목명': row['Name'], '티커': tc, '현재가': f"{c_p:,.0f} 원", '20/60선 이격': f"{((ma20/ma60)-1)*100:+.2f}%", '20일 모멘텀': f"{r20:+.2f}%", '진단 근거': "장기 추세선 방어 및 골든크로스"}
        return None

    res = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        results = executor.map(process_stock, cands.to_dict('records'))
        for r in results:
            if r is not None: res.append(r)
    return pd.DataFrame(res)

@st.cache_data(ttl=3600)
def run_satellite_scanner(use_ma200, top_n=5):
    krx = load_krx_universe()
    if krx.empty: return pd.DataFrame()
    
    if 'Market' in krx.columns: kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)]
    else: kosdaq = krx
        
    if 'Marcap' in kosdaq.columns: cands = kosdaq[kosdaq['Marcap'] >= 100000000000].sort_values('Marcap', ascending=False).head(100)
    else: cands = kosdaq.head(100)
    
    def process_stock(row):
        tc = str(row['Code']).strip().zfill(6)
        s = fetch_stock_status(tc)
        if s is None: return None
        c_p, ma200, ma60, ma20, dd, vr, r60, r20, m60_up, is_a200, vs, c_l, rvm, atv, dsp, vc = s
        d20 = ((c_p / ma20) - 1) * 100 if ma20 > 0 else 0
        is_dip = (-5.0 <= d20 <= 3.0) or ((c_l <= ma20 * 1.01) and (c_p >= ma20 * 0.95))
        sat_normal_buy = is_dip and vs and (dsp <= 45) and (vc <= 0.50)
        
        if sat_normal_buy and ((not use_ma200) or is_a200) and (dd >= -30.0) and m60_up and (r20 > -3.0) and atv >= 5000000000:
            sc = (rvm / 100.0)*0.4 + (r60*0.3) + (r20*0.3)
            return {'종목명': row['Name'], '티커': tc, '현재가': f"{c_p:,.0f} 원", '20일선 이격도': f"{d20:+.2f}%", '최대 수급': f"{rvm:,.0f}%", 'AI 스코어': round(sc, 2), '_sc': sc}
        return None

    res = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        results = executor.map(process_stock, cands.to_dict('records'))
        for r in results:
            if r is not None: res.append(r)
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
    
    def fetch_sim_data(row):
        tk, nm = str(row.get('티커','')).strip().zfill(6), str(row.get('종목명',''))
        if not tk or tk == '000000': return None
        try: df = fdr.DataReader(tk, start=f_start, end=end_date)
        except: return None
        if df is None or df.empty: return None
        
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
        
        df['Days_Since_Peak'] = df['Close'].rolling(120, min_periods=1).apply(lambda x: len(x) - 1 - np.argmax(x), raw=True)
        df['Peak_Vol_20'] = df['Volume'].rolling(20, min_periods=1).max()
        df['V5_mean'] = df['Volume'].rolling(5, min_periods=1).mean()
        df['Vol_Contraction'] = np.where(df['Peak_Vol_20']>0, df['V5_mean'] / df['Peak_Vol_20'], 1.0)
        
        if df.index.tz is not None: df.index = df.index.tz_localize(None)
        df = df.join(m_df, how='left')
        for c, d in [('V_Safe',True), ('V_Con',False), ('Bm_Ret_60',0.0), ('Bm_Bull',False)]: df[c] = df[c].fillna(d)
        
        m2_c = df['Abv200'] if use_ma200 else True
        if strat == '대형주 (Core)':
            ec = m2_c & (((df['M20'] >= df['M60']*(1+buf)) & df['M60_Up'] & (df['R20']>0) & df['V_Safe']) | df['V_Con'])
            xc = (df['M20'] < df['M60']*(1-buf/2)) & (~df['V_Con'])
        else:
            df['DD'] = (df['Close']/df['Close'].rolling(120, min_periods=1).max()) - 1
            d20 = ((df['Close']/df['M20'])-1)*100
            idip = ((d20 >= -5.0) & (d20 <= 3.0)) | (df['Low'] <= df['M20']*1.01 if 'Low' in df.columns else d20 <= 0)
            sat_normal_buy = idip & df['V_Srg'] & (df['Days_Since_Peak'] <= 45) & (df['Vol_Contraction'] <= 0.50)
            ec = m2_c & (sat_normal_buy | df['V_Con']) & (df['DD'] >= -0.30) & df['M60_Up'] & (df['R20'] > -0.03)
            xc = (df['Close'] < df['M20']*(1-buf/2)) & (~df['V_Con'])
            
        df['Sig'] = np.where(ec, 1, np.where(xc, 0, np.nan))
        df['Sig'] = df['Sig'].ffill().fillna(0)
        df['Sc'] = np.where(ec, 1.0 + np.where(df['V_Str'], 1.0 if strat!='대형주 (Core)' else 0.5, 0.0) + np.where(df['R60']>df['Bm_Ret_60'], 0.5, 0.0) + np.where(df['V_Con'], 1.0, 0.0), 0.0)
        return nm, df[df.index >= s_dt].copy()

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        results = executor.map(fetch_sim_data, [row for _, row in sim_stocks.iterrows()])
        for r in results:
            if r is not None and not r[1].empty:
                s_dfs[r[0]] = r[1]
                
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
    
    active_sl = -0.15 if strat == '대형주 (Core)' else (sl/100.0)
    
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
                if (c_p/ab_p[n]-1) <= active_sl: fe = True
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

def color_profit_loss(val):
    color = ''
    val_str = str(val)
    if val_str.startswith('+'):
        color = 'color: #FF5050; font-weight: bold;'
    elif val_str.startswith('-') and len(val_str) > 1 and val_str != '-':
        color = 'color: #3b82f6; font-weight: bold;'
    return color

def apply_mts_style(df, subset_cols):
    valid_cols = [c for c in subset_cols if c in df.columns]
    if not valid_cols: return df
    if hasattr(df.style, 'map'): return df.style.map(color_profit_loss, subset=valid_cols)
    else: return df.style.applymap(color_profit_loss, subset=valid_cols)

def mts_metric_html(label, value, delta=None):
    val_color = "white"
    val_str = str(value)
    if not delta: 
        if val_str.startswith('+'): val_color = "#FF5050"
        elif val_str.startswith('-') and val_str != '-': val_color = "#3b82f6"
        
    if delta:
        delta_str = str(delta)
        if delta_str.startswith('+'): d_color = "#FF5050"
        elif delta_str.startswith('-') and delta_str != '-': d_color = "#3b82f6"
        else: d_color = "#a3a8b8"
        delta_html = f'<div style="color: {d_color}; font-size: 1rem; font-weight: bold; margin-top: 4px;">{delta_str}</div>'
    else:
        delta_html = ""
        
    return f"""
    <div style="background-color: rgba(255, 255, 255, 0.05); padding: 1.2rem; border-radius: 0.5rem; margin-bottom: 1rem; border: 1px solid rgba(250, 250, 250, 0.1);">
        <div style="color: #a3a8b8; font-size: 0.9rem; font-weight: 600; margin-bottom: 0.5rem;">{label}</div>
        <div style="font-size: 1.8rem; font-weight: 700; color: {val_color};">{val_str}</div>
        {delta_html}
    </div>
    """

# ==========================================
# 3. 사이드바 UI 렌더링
# ==========================================
if 'search_q' not in st.session_state: st.session_state.search_q = None
if 'search_sec' not in st.session_state: st.session_state.search_sec = None

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
            dnca_tot = float(summary[0].get('dnca_tot_amt', 0)) # KIS 공식 예수금
            imported = [{'종목명': i.get('prdt_name'), '티커': str(i.get('pdno')).strip().zfill(6), '실시간 현재가': f"{float(i.get('prpr', 0)):,.0f} 원", '매수평균가': f"{float(i.get('pchs_avg_pric', 0)):,.0f} 원", '보유수량': f"{int(i.get('hldg_qty'))} 주", '평가손익률': f"{float(i.get('evlu_pfls_rt', 0)):+.2f}%", '_raw_price': float(i.get('prpr', 0)), '_raw_buy': float(i.get('pchs_avg_pric', 0))} for i in holdings if int(i.get('hldg_qty', 0)) > 0]
            st.session_state[cache_key] = {'total_eval': tot_evlu, 'total_pnl': tot_pnl, 'cash_avail': dnca_tot, 'stocks': imported}

kis_data = st.session_state.get(cache_key)

real_holdings_tickers = []
real_total_eval, real_eval_pnl = 0.0, 0.0
real_stocks_df = pd.DataFrame()
real_cash_avail = total_cash

if kis_data:
    real_holdings_tickers = [item['티커'] for item in kis_data['stocks']]
    real_total_eval = kis_data.get('total_eval', 0.0)
    real_eval_pnl = kis_data.get('total_pnl', 0.0)
    real_cash_avail = kis_data.get('cash_avail', total_cash)
    real_stocks_df = pd.DataFrame(kis_data['stocks'])

real_base_date_str = p_data.get('real_base_date', p_data.get('created_at', '2024-01-01')) if p_data else '2024-01-01'
try: real_base_date = pd.to_datetime(real_base_date_str).date()
except: real_base_date = datetime.datetime.now(KST).date()

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
            save_portfolio_to_sheets(safe_name, {'strategy': new_p_strat, 'cash': new_p_cash, 'stocks': [], 'created_at': datetime.datetime.now(KST).strftime('%Y-%m-%d')})
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
            check_min = st.sidebar.number_input("백그라운드 스케줄러 기준 주기 (분)", min_value=1, value=init_ap_min, disabled=True)
            st.sidebar.success(f"✅ 오토파일럿 스위치 ON. (창을 닫아도 안전하게 감시됩니다.)")
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

        st.markdown("### 🔎 직접 종목 추가 및 섹터별 대장주 검색")
        c_search, c_theme = st.columns([1, 1])
        
        with c_search:
            st.markdown("#### ⌨️ 직접 종목 검색")
            with st.form("manual_search_form"):
                search_query = st.text_input("종목명 또는 종목코드(6자리) 입력", placeholder="예: 삼성전자, 005930")
                if st.form_submit_button("🔍 검색하기"):
                    st.session_state.search_q = search_query
                    st.session_state.search_sec = None
        
        with c_theme:
            st.markdown("#### 🏭 주요 섹터별 대장주 보기")
            b1, b2, b3, b4 = st.columns(4)
            if b1.button("💻 반도체", use_container_width=True): 
                st.session_state.search_sec = "반도체"; st.session_state.search_q = None
            if b2.button("🔋 2차전지", use_container_width=True): 
                st.session_state.search_sec = "2차전지"; st.session_state.search_q = None
            if b3.button("🧬 바이오", use_container_width=True): 
                st.session_state.search_sec = "바이오"; st.session_state.search_q = None
            if b4.button("🌐 플랫폼", use_container_width=True): 
                st.session_state.search_sec = "플랫폼"; st.session_state.search_q = None
                
            b5, b6, b7, b8 = st.columns(4)
            if b5.button("🚗 자동차", use_container_width=True): 
                st.session_state.search_sec = "자동차"; st.session_state.search_q = None
            if b6.button("🏦 금융", use_container_width=True): 
                st.session_state.search_sec = "금융"; st.session_state.search_q = None
            if b7.button("🚀 방산", use_container_width=True): 
                st.session_state.search_sec = "방산"; st.session_state.search_q = None
            if b8.button("💄 화장품", use_container_width=True): 
                st.session_state.search_sec = "화장품"; st.session_state.search_q = None

        current_watchlist_tickers = [str(s.get('티커')).strip().zfill(6) for s in p_data.get('stocks', [])]

        if st.session_state.search_q:
            st.markdown(f"##### 🎯 '{st.session_state.search_q}' 검색 결과")
            krx_df = load_krx_universe()
            if not krx_df.empty:
                matched = krx_df[krx_df['Name'].str.contains(st.session_state.search_q, case=False, na=False) | krx_df['Code'].str.contains(st.session_state.search_q, na=False)].head(5)
                if not matched.empty:
                    for _, r in matched.iterrows():
                        m_name, m_code = r['Name'], str(r['Code']).zfill(6)
                        cc1, cc2 = st.columns([8, 2])
                        cc1.markdown(f"`{m_code}` **{m_name}**")
                        if m_code in real_holdings_tickers: cc2.button("✅ 보유중", key=f"add_m_{m_code}", disabled=True, use_container_width=True)
                        elif m_code in current_watchlist_tickers: cc2.button("✅ 관심종목", key=f"add_m_{m_code}", disabled=True, use_container_width=True)
                        else:
                            if cc2.button("➕ 등록", key=f"add_m_{m_code}", use_container_width=True):
                                p_data['stocks'].append({'종목명': m_name, '티커': m_code, '매수단가': 0, '보유수량': 0})
                                p_data['stocks'] = pd.DataFrame(p_data['stocks']).drop_duplicates(subset=['티커']).to_dict(orient='records')
                                save_portfolio_to_sheets(selected_port, p_data)
                                try: st.query_params["auth"] = daily_token
                                except: st.experimental_set_query_params(auth=daily_token)
                                st.rerun()
                else: st.caption("검색 결과가 없습니다.")

        elif st.session_state.search_sec:
            st.markdown(f"##### 🎯 '{st.session_state.search_sec}' 섹터 대표주")
            sector_stocks = {
                "반도체": [("삼성전자", "005930"), ("SK하이닉스", "000660")],
                "2차전지": [("LG에너지솔루션", "373220"), ("삼성SDI", "006400")],
                "바이오": [("삼성바이오로직스", "207940"), ("셀트리온", "068270")],
                "플랫폼": [("NAVER", "035420"), ("카카오", "035720")],
                "자동차": [("현대차", "005380"), ("기아", "000270")],
                "금융": [("KB금융", "105560"), ("신한지주", "055550")],
                "방산": [("한화에어로스페이스", "012450"), ("LIG넥스원", "079550")],
                "화장품": [("아모레퍼시픽", "090430"), ("LG생활건강", "090530")]
            }
            for t_name, t_code in sector_stocks[st.session_state.search_sec]:
                tc1, tc2 = st.columns([8, 2])
                tc1.markdown(f"`{t_code}` **{t_name}**")
                if t_code in real_holdings_tickers: tc2.button("✅ 보유중", key=f"add_t_{t_code}", disabled=True, use_container_width=True)
                elif t_code in current_watchlist_tickers: tc2.button("✅ 관심종목", key=f"add_t_{t_code}", disabled=True, use_container_width=True)
                else:
                    if tc2.button("➕ 등록", key=f"add_t_{t_code}", use_container_width=True):
                        p_data['stocks'].append({'종목명': t_name, '티커': t_code, '매수단가': 0, '보유수량': 0})
                        p_data['stocks'] = pd.DataFrame(p_data['stocks']).drop_duplicates(subset=['티커']).to_dict(orient='records')
                        save_portfolio_to_sheets(selected_port, p_data)
                        try: st.query_params["auth"] = daily_token
                        except: st.experimental_set_query_params(auth=daily_token)
                        st.rerun()

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
                        if ticker_str in real_holdings_tickers: c3.button("🔌 실계좌 보유", key=f"add_scan_{row['티커']}", disabled=True)
                        elif ticker_str in current_watchlist_tickers: c3.button("📝 관심종목", key=f"add_scan_{row['티커']}", disabled=True)
                        else:
                            if c3.button("➕ 담기", key=f"add_scan_{row['티커']}"):
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
                    yf_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, _, vol_surged, yf_low, recent_vol_max, avg_trade_val, dsp, vc = res
                    if not c_price or c_price == 0: c_price = yf_price
                    
                    res_q = analyze_quant_strategy(active_strat, c_price, 0.0, 0.0, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, dsp, vc)
                    
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
                        tech_text = f"20일선 이격 {res_q['dist_ma20_pct']:+.2f}% / 거래량 축소 {vc*100:.0f}%"
                        if res_q['exit_cond_trend'] or not res_q['vol_surged'] or res_q['drawdown'] < -30.0 or (use_ma200_filter and not res_q['is_above_ma200']):
                            action, easy_desc = "🔴 유니버스 제외 (수급/추세 상실)", "[유니버스 제외] 핵심 지지선 하향 이탈 및 모멘텀 소멸이 확인되었습니다."
                        elif res_q['entry_cond'] and avg_trade_val >= 5000000000:
                            action, easy_desc = f"🟢 매수 시그널 발생 (목표: {target_shares:,}주)", f"[매수 시그널] 고점 대비 {dsp}일 경과, 거래량 {vc*100:.0f}%로 축소된 완벽한 눌림목입니다."
                        else:
                            action, easy_desc = "🟡 모니터링 유지", "[모니터링 유지] 거래대금/경과일수 필터 미달 또는 타점 대기 중입니다."

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

        if auto_pilot or st.button("📲 현재 AI 진단 결과를 텔레그램 전송", key="send_tg_virtual"):
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
            with col_m1: st.markdown(mts_metric_html("💰 계좌 총 평가 금액", f"{real_total_eval:,.0f} 원"), unsafe_allow_html=True)
            with col_m2: st.markdown(mts_metric_html("📥 투자 원금 (입출금 감지)", f"{total_invested_principal:,.0f} 원"), unsafe_allow_html=True)
            with col_m3: st.markdown(mts_metric_html("📈 누적 수익금 (실현+평가)", f"{total_pnl_all:+,.0f} 원", f"{real_ret_pct:+.2f}%"), unsafe_allow_html=True)
            with col_m4: st.markdown(mts_metric_html("💵 가용 현금", f"{real_cash_avail:,.0f} 원"), unsafe_allow_html=True)
                
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
                    current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
                    if current_asset_base <= 0: current_asset_base = total_cash
                        
                    is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
                    current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
                    target_buy_amt = current_asset_base * (current_max_alloc_pct / 100.0)
                    
                    total_eval_sum = 0.0
                    total_pnl_sum = 0.0
                    total_buy_sum = 0.0
                    live_results = []
                    
                    bot_managed_tickers = [str(s.get('티커')).strip().zfill(6) for s in p_data.get('stocks', [])] if p_data else []
                    manual_holdings = []
                    
                    for idx, row in real_stocks_df.iterrows():
                        ticker_str = str(row['티커']).strip().zfill(6)
                        
                        if ticker_str not in bot_managed_tickers:
                            manual_holdings.append(row.get('종목명', ticker_str))
                            continue
                            
                        live_c_price, buy_price = float(row.get('_raw_price', 0)), float(row.get('_raw_buy', 0))
                        
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
                            
                        yf_price, ma200, ma60, ma20, drawdown, _, _, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, yf_low, recent_vol_max, avg_trade_val, dsp, vc = res

                        res_q = analyze_quant_strategy(active_strat, live_c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, dsp, vc)
                        
                        cd_info = p_data.get('cd_tracker', {}).get(ticker_str, {'losses': 0, 'until': '2000-01-01'})
                        cd_until = pd.to_datetime(cd_info['until']).date()
                        is_cooldown = datetime.datetime.now(KST).date() < cd_until
                        
                        additional_amt = max(0, target_buy_amt - current_holding_amt)
                        add_qty = int(additional_amt // live_c_price)

                        action, reason = "-", "-"
                        
                        if res_q['stop_loss_cond']: 
                            action, reason = "🔴 긴급 손절 매도", f"손절 기준선 도달"
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
                                '보유 종목명': '💡 [평가총액 합계 (봇 관제 대상)]', '🔥 매력도 점수': '-', '보유수량': '-', '매수평균가': '-', '실시간 현재가': '-',
                                '평가금액': f"{total_eval_sum:,.0f} 원", '평가손익': f"{total_pnl_sum:+,.0f} 원", '수익률': f"{total_ret_sum_pct:+.2f}%",
                                '🤖 실계좌 전용 액션 플랜': '-', '📊 판단 근거': '-'
                            }])
                            live_df = pd.concat([live_df, summary_row], ignore_index=True)
                            st.dataframe(apply_mts_style(live_df, ['평가손익', '수익률']), use_container_width=True, hide_index=True)
                            
                    if manual_holdings:
                        st.info(f"🛡️ **[논리적 격리 작동 중]** 수동으로 매수하여 보유 중인 종목({len(manual_holdings)}개: {', '.join(manual_holdings)})은 AI 관제 대상에서 안전하게 제외되어 강제 매도되지 않습니다.")
            else:
                st.info("현재 실전 계좌에 매수(보유) 중인 봇 관리 종목이 없습니다. [탭 1]의 관심종목 리스트에서 타점을 대기하세요.")

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
        for s in p_data['stocks']: 
            eligible_stocks[str(s['티커']).strip().zfill(6)] = s.get('종목명', '')
            
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
        
        c_p, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, yf_low, recent_vol_max, avg_trade_val, dsp, vc = res
        
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

        res_q = analyze_quant_strategy(active_strat, live_c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, dsp, vc)

        if pd.isna(res_q['ai_score']) or np.isinf(res_q['ai_score']):
            tab3_anomaly_flag, tab3_anomaly_reason = True, f"[{ticker}] AI 매력도 점수 이상 수치(NaN/Inf) 감지."
            break

        cd_info = p_data.get('cd_tracker', {}).get(ticker, {'losses': 0, 'until': '2000-01-01'})
        cd_until = pd.to_datetime(cd_info['until']).date()
        is_cooldown = datetime.datetime.now(KST).date() < cd_until

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
                '주문 단가': f"{live_c_price:,.0f} 원", '주문 수량': f"{qty_num:,} 주", '필요 자금': f"-{live_c_price * qty_num:,.0f} 원 (회수)",
                '_raw_price': live_c_price, '_buy_price': buy_price, '_qty': qty_num, '_req_fund': 0, '주문 실행 상태': status_text
            })
            continue 

        if res_q['entry_cond'] and not is_cooldown and avg_trade_val >= 5000000000:
            current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
            if current_asset_base <= 0: current_asset_base = total_cash
                
            is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
            current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
            
            target_buy_amt = current_asset_base * (current_max_alloc_pct / 100.0)
            current_holding_amt = qty_num * live_c_price
            additional_amt = max(0, target_buy_amt - current_holding_amt)
            add_qty = int(additional_amt // live_c_price)
            
            if add_qty > 0 and (add_qty * live_c_price) > (current_asset_base * 1.0):
                tab3_anomaly_flag, tab3_anomaly_reason = True, f"[{ticker}] 산출 매수 금액({add_qty * live_c_price:,.0f}원)이 설정된 계좌 총 자산({current_asset_base:,.0f}원)을 초과하는 팻핑거 로직 감지."
                break
            
            if add_qty > 0:
                req_fund = add_qty * live_c_price
                status_text = "대기 중"
                buy_type = "🛒 신규 매수" if qty_num == 0 else "🟢 비중 확대"
                
                temp_queue.append({
                    '우선순위_분류': 1, '🔥 점수': res_q['ai_score'], '종목명': s_name, '티커': ticker, '구분': buy_type,
                    '주문 단가': f"{live_c_price:,.0f} 원", '주문 수량': f"{add_qty:,} 주", '필요 자금': f"{req_fund:,.0f} 원",
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
                        q['주문 수량'] = f"{q['_qty']} 주 ➡️ {aff_qty} 주"
                        sim_cash -= (aff_qty * q['_raw_price'])
                        q['_qty'] = aff_qty
                        q['_req_fund'] = aff_qty * q['_raw_price']
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
            st.table(display_queue[['우선순위', '종목명', '구분', '🔥 점수', '주문 단가', '주문 수량', '필요 자금', '주문 실행 상태']])
            
            st.markdown("---")
            
            trigger_auto = False
            if auto_trade_enabled and not kill_switch:
                for idx, q_row in queue_df.iterrows():
                    if "대기" in str(q_row['주문 실행 상태']):
                        trigger_auto = True
                        break
            
            if auto_trade_enabled: btn_label, btn_type = "⚡ 자동 감시 주기 무시하고 즉시 강제 집행 (입금 직후용)", "secondary"
            else: btn_label, btn_type = "⚡ 대기열 일괄 주문 수동 전송", "primary"
            
            manual_btn = st.button(btn_label, type=btn_type, use_container_width=True)
                
            if trigger_auto or manual_btn:
                if kill_switch: st.error("🚨 킬 스위치가 활성화되어 있어 주문 전송할 수 없습니다.")
                elif not auto_trade_enabled and not trigger_auto: st.warning("🚀 사이드바에서 '실전 자동주문 활성화' 스위치를 켜주세요.")
                else:
                    with st.spinner("🚀 [오토파일럿] KIS 리얼 서버로 순차 주문 전송 중..."):
                        exec_msgs, needs_save = [], False
                        for idx, q_row in queue_df.iterrows():
                            if "대기" not in str(q_row['주문 실행 상태']): continue
                            
                            s_name, t_code, q_type = q_row['종목명'], q_row['티커'], q_row['구분']
                            raw_qty, raw_price, buy_p = q_row['_qty'], q_row['_raw_price'], q_row['_buy_price']
                            
                            if "매도" in q_type or "익절" in q_type:
                                succ, msg = execute_kis_order(SYS_APP_KEY, SYS_APP_SECRET, kis_token_global, SYS_CANO, SYS_ACNT_PRDT, t_code, raw_qty, raw_price, order_type="SELL", is_market=True, is_mock=SYS_IS_MOCK)
                                if succ:
                                    p_data = log_daily_trade(p_data, s_name, "SELL", raw_price, raw_qty, buy_p, status="✅ 체결완료", msg="시장가 매도 접수")
                                    exec_msgs.append(f"▪️ [{q_type}] *{s_name}*: {msg}")
                                    
                                    real_cash_avail += (raw_price * raw_qty)
                                    
                                    pnl = (raw_price - buy_p) * raw_qty
                                    if 'cd_tracker' not in p_data: p_data['cd_tracker'] = {}
                                    cd_i = p_data['cd_tracker'].get(t_code, {'losses': 0, 'until': '2000-01-01'})
                                    if pnl < 0:
                                        cd_i['losses'] += 1
                                        if cd_i['losses'] >= 2:
                                            cd_i['until'] = (datetime.datetime.now(KST).date() + datetime.timedelta(days=cooldown_days)).strftime('%Y-%m-%d')
                                    else: cd_i['losses'] = 0
                                    p_data['cd_tracker'][t_code] = cd_i
                                    if 'ts_tracker' in p_data and t_code in p_data['ts_tracker']: del p_data['ts_tracker'][t_code]
                                    needs_save = True
                                else: 
                                    p_data = log_daily_trade(p_data, s_name, "SELL", raw_price, raw_qty, buy_p, status="❌ 주문실패", msg=msg)
                                    exec_msgs.append(f"❌ [{q_type} 실패] *{s_name}*: {msg}")
                                    needs_save = True
                                    
                            elif "매수" in q_type or "확대" in q_type:
                                aff_qty = int(real_cash_avail // raw_price)
                                final_qty = min(raw_qty, aff_qty)
                                
                                if final_qty > 0:
                                    succ, msg = execute_kis_order(SYS_APP_KEY, SYS_APP_SECRET, kis_token_global, SYS_CANO, SYS_ACNT_PRDT, t_code, final_qty, raw_price, order_type="BUY", is_market=False, is_mock=SYS_IS_MOCK)
                                    if succ:
                                        p_data = log_daily_trade(p_data, s_name, "BUY", raw_price, final_qty, status="✅ 체결완료", msg="지정가 매수 접수")
                                        real_cash_avail -= (final_qty * raw_price)
                                        if final_qty < raw_qty:
                                            exec_msgs.append(f"🔄 [{q_type} 부분 체결] *{s_name}*: 예수금 한도 내 {final_qty}주 매수 완료")
                                        else:
                                            exec_msgs.append(f"▪️ [{q_type}] *{s_name}*: {final_qty}주 매수 완료")
                                        needs_save = True
                                    else: 
                                        p_data = log_daily_trade(p_data, s_name, "BUY", raw_price, final_qty, status="❌ 주문실패", msg=msg)
                                        exec_msgs.append(f"❌ [{q_type} 실패] *{s_name}*: {msg}")
                                        needs_save = True
                                else:
                                    p_data = log_daily_trade(p_data, s_name, "BUY", raw_price, raw_qty, status="⚠️ 매수스킵", msg="예수금 부족으로 차순위 큐 진행")
                                    needs_save = True
                        
                        if needs_save: save_portfolio_to_sheets(selected_port, p_data)
                        if exec_msgs:
                            if tg_noti_order:
                                send_telegram_message("🤖 *[주문 전송 집행 결과]*\n" + "\n".join(exec_msgs))
                            st.success("주문 집행이 완료되었습니다!")
                            time.sleep(1)
                            
                            if cache_key in st.session_state:
                                del st.session_state[cache_key]
                                
                            try: st.query_params["auth"] = daily_token
                            except: st.experimental_set_query_params(auth=daily_token)
                            st.rerun()
        else: st.info("💡 현재 AI 퀀트 엔진이 포착한 신규 매수 또는 매도 시그널이 없습니다.")

    st.markdown("---")
    st.subheader("📜 당일 매매(API 전송) 내역 및 실적 요약")
    today_str = datetime.datetime.now(KST).strftime('%Y-%m-%d')
    if p_data and p_data.get('daily_trades_date') == today_str and p_data.get('daily_trades'):
        trades_df = pd.DataFrame(p_data['daily_trades'])
        
        succ_df = trades_df[trades_df['상태'].str.contains('✅')] if '상태' in trades_df.columns else trades_df
        total_buy = succ_df[succ_df['주문 구분'] == '매수(진입)']['체결 금액'].sum() if not succ_df.empty else 0
        total_sell = succ_df[succ_df['주문 구분'] == '매도(청산)']['체결 금액'].sum() if not succ_df.empty else 0
        total_pnl = succ_df['실현 손익'].sum() if not succ_df.empty else 0
        
        c1, c2, c3 = st.columns(3)
        with c1: st.markdown(mts_metric_html("🛒 당일 체결된 총 매수대금", f"{total_buy:,.0f} 원"), unsafe_allow_html=True)
        with c2: st.markdown(mts_metric_html("💸 당일 체결된 총 매도대금", f"{total_sell:,.0f} 원"), unsafe_allow_html=True)
        with c3: st.markdown(mts_metric_html("🎯 당일 확정 실현손익", f"{total_pnl:+,.0f} 원", f"{total_pnl:+,.0f} 원"), unsafe_allow_html=True)
        
        display_trades = trades_df.copy()
        if '상태' not in display_trades.columns: display_trades['상태'] = "✅ 체결완료"
        if '비고 (API 메시지)' not in display_trades.columns: display_trades['비고 (API 메시지)'] = "-"
            
        display_trades['체결 단가'] = display_trades['체결 단가'].apply(lambda x: f"{x:,.0f} 원")
        display_trades['체결 수량'] = display_trades['체결 수량'].apply(lambda x: f"{x:,} 주")
        display_trades['체결 금액'] = display_trades['체결 금액'].apply(lambda x: f"{x:,.0f} 원")
        display_trades['실현 손익'] = display_trades.apply(lambda row: f"{row['실현 손익']:+,.0f} 원" if row['주문 구분'] == '매도(청산)' and '✅' in row['상태'] else "-", axis=1)
        
        st.dataframe(apply_mts_style(display_trades, ['실현 손익']), use_container_width=True, hide_index=True)
    else: st.info("오늘 KIS API 엔진을 통해 체결 시도된 거래 내역이 없습니다.")

with tab4:
    st.header("🧪 시뮬레이션 및 백테스트 (Simulation & Backtest)")
    if not p_data or not selected_port: 
        st.warning("포트폴리오가 없습니다.")
    else:
        stocks_df = pd.DataFrame(p_data.get('stocks', []))
        today_date = datetime.datetime.now(KST).date()
        
        raw_port_stocks = p_data.get('stocks', [])
        raw_real_stocks = kis_data.get('stocks', []) if kis_data else []
        merged_stocks = []
        for s in raw_port_stocks:
            merged_stocks.append({'종목명': s.get('종목명'), '티커': str(s.get('티커')).strip().zfill(6)})
        for s in raw_real_stocks:
            merged_stocks.append({'종목명': s.get('종목명'), '티커': str(s.get('티커')).strip().zfill(6)})
        stocks_df = pd.DataFrame(merged_stocks).drop_duplicates(subset=['티커']) if merged_stocks else pd.DataFrame()
        
        st.subheader("🎯 Test 1. 포워드 테스트 (관심종목 vs 실전 계좌)")
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

        if st.button("▶️ 포워드 테스트 1:1 비교 실행", type="primary", use_container_width=True):
            if stocks_df.empty: 
                st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner(f"{real_base_date} 부터 현재까지 통합 유니버스 1:1 백테스트 구동 중..."):
                    fw_result = run_quant_simulation(stocks_df, active_strat, total_invested_principal, real_base_date, today_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if fw_result:
                        st.markdown("### 🏆 누적 수익률 비교 (Yield Comparison)")
                        col_fw1, col_fw2 = st.columns(2)
                        with col_fw1: st.markdown(mts_metric_html("📈 AI 포워드 테스트 (이론)", f"{fw_result['final_port_ret']:+.2f}%", f"기말 자산: {fw_result['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                        if SYS_APP_KEY and kis_data:
                            total_pnl_all = real_eval_pnl + cumulative_realized_pnl + manual_offset
                            real_ret_pct_custom = (total_pnl_all / total_invested_principal * 100) if total_invested_principal > 0 else 0
                            with col_fw2: st.markdown(mts_metric_html("🔌 나의 실전 계좌 (실제)", f"{real_ret_pct_custom:+.2f}%", f"현재 자산: {real_total_eval:,.0f} 원"), unsafe_allow_html=True)
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
                        
                        comp_df = pd.DataFrame(comp_data)
                        st.dataframe(apply_mts_style(comp_df, ['🤖 누적손익', '🔌 평가손익']), use_container_width=True, hide_index=True)
                    else: st.warning("데이터가 부족하여 시뮬레이션을 완료할 수 없습니다.")

        st.markdown("---")
        
        st.subheader("📊 Test 2. 장기 초과수익 검증 (관심종목 대상)")
        col_sim1, col_sim2 = st.columns(2)
        with col_sim1: start_date = st.date_input("시작일", datetime.date(2023, 1, 1), key="t2_s")
        with col_sim2: end_date = st.date_input("종료일", today_date, key="t2_e")

        if st.button("🚀 관심종목 대상 장기 Backtest 실행", type="primary", use_container_width=True):
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner(f"통합 유니버스 초고속 벡터 연산 AI 시뮬레이션 중... (약 5초 소요)"):
                    bt_result = run_quant_simulation(stocks_df, active_strat, total_cash, start_date, end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if bt_result:
                        st.success(f"✅ 장기 백테스트 실행 완료!")
                        col_r1, col_r2 = st.columns(2)
                        with col_r1: st.markdown(mts_metric_html("총 초기 투입 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                        with col_r2: st.markdown(mts_metric_html("AI 초과수익 기말 자산", f"{bt_result['final_asset']:,.0f} 원", f"{bt_result['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        
                        summary_df = pd.DataFrame(bt_result['summary_rows'])
                        st.dataframe(apply_mts_style(summary_df, ['총 순수익 (원)', '수익률 (%)']), use_container_width=True, hide_index=True)
                        
                        chart = alt.Chart(bt_result['eom_weights_reset']).mark_bar().encode(
                            x=alt.X('Date:O', title=''), y=alt.Y('Weight:Q', title='비중 (%)', stack='zero'),
                            color=alt.Color('Asset:N', scale=alt.Scale(domain=bt_result['cols_ordered'], range=bt_result['color_range'])), order=alt.Order('Order:Q')
                        ).properties(height=450)
                        st.altair_chart(chart, use_container_width=True)

        st.markdown("---")
        
        st.subheader("💡 Test 3. 동적 유니버스 블라인드 백테스트 (시장 주도주 자율 매매)")
        st.warning("※ 주의: 상장폐지된 종목이 누락되어 성과가 과대 계상될 수 있는 '생존자 편향'이 포함되어 있으므로, 하락장 방어력 검증용으로만 참고하세요.")
        
        if active_strat == '대형주 (Core)':
            univ_text = "**KOSPI 시가총액 상위 50개 대형주**"
        else:
            univ_text = "**KOSDAQ 시가총액 상위 50개 중소형주**"
            
        st.markdown(f"과거 특정 시점의 {univ_text}를 대상으로 AI가 100% 자율 매매했을 때의 실전 운용 성과를 검증합니다.")
        
        col_d1, col_d2 = st.columns(2)
        with col_d1: dyn_start_date = st.date_input("시작일", datetime.date(2023, 1, 1), key="t3_s")
        with col_d2: dyn_end_date = st.date_input("종료일", today_date, key="t3_e")
            
        krx_univ = load_krx_universe()
        
        if st.button("🚀 AI 자율 매매 블라인드 테스트 실행 (약 10초 소요)", type="primary", use_container_width=True):
            if krx_univ.empty: 
                st.error("KRX 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")
            else:
                if 'Market' not in krx_univ.columns: krx_univ['Market'] = 'KOSPI'
                
                if active_strat == '대형주 (Core)':
                    sim_cands = krx_univ[krx_univ['Market'].str.contains('KOSPI', case=False, na=False)]
                else:
                    sim_cands = krx_univ[krx_univ['Market'].str.contains('KOSDAQ', case=False, na=False)]
                    
                if 'Marcap' in sim_cands.columns:
                    sim_cands = sim_cands.sort_values('Marcap', ascending=False).head(50)
                else:
                    sim_cands = sim_cands.head(50)
                
                sim_df_cands = pd.DataFrame({
                    '종목명': sim_cands['Name'],
                    '티커': sim_cands['Code']
                })
                
                with st.spinner(f"시가총액 상위 50개 종목 수집 및 병렬 시뮬레이션 진행 중..."):
                    dyn_result = run_quant_simulation(sim_df_cands, active_strat, total_cash, dyn_start_date, dyn_end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    
                    if dyn_result:
                        st.success(f"✅ 동적 유니버스 블라인드 백테스트 실행 완료!")
                        col_r1, col_r2 = st.columns(2)
                        with col_r1: st.markdown(mts_metric_html("총 초기 투입 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                        with col_r2: st.markdown(mts_metric_html("블라인드 테스트 기말 자산", f"{dyn_result['final_asset']:,.0f} 원", f"{dyn_result['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        
                        dyn_summary_df = pd.DataFrame(dyn_result['summary_rows'])
                        st.dataframe(apply_mts_style(dyn_summary_df, ['총 순수익 (원)', '수익률 (%)']), use_container_width=True, hide_index=True)
                        
                        chart = alt.Chart(dyn_result['eom_weights_reset']).mark_bar().encode(
                            x=alt.X('Date:O', title=''), y=alt.Y('Weight:Q', title='비중 (%)', stack='zero'),
                            color=alt.Color('Asset:N', scale=alt.Scale(domain=dyn_result['cols_ordered'], range=dyn_result['color_range'])), order=alt.Order('Order:Q')
                        ).properties(height=450)
                        st.altair_chart(chart, use_container_width=True)
                    else:
                        st.warning("데이터가 부족하여 시뮬레이션을 완료할 수 없습니다.")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 (v6.13 UI Decoupled)</h1>
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
    *   **📌 타겟 유니버스:** KOSPI 시가총액 상위 100종목
    *   **🟢 AI 진입 조건 (모두 만족 시):**
        1.  **장기 추세 방어:** 현재 주가가 200일 이동평균선(MA200) 이상에 위치.
        2.  **중기 추세 상승:** 60일 이동평균선(MA60)의 기울기가 양수.
        3.  **단기 모멘텀:** 20일 전 주가 대비 현재 주가가 상승.
        4.  **골든크로스 안착:** 20일선이 60일선을 상향 돌파하고 버퍼(1.5%) 이상 확장.
        5.  **시장 안정:** VIX(공포지수)가 30 미만.
    *   **🔴 AI 이탈 조건:**
        1.  **추세 붕괴:** 20일선이 60일선을 하향 이탈 시 전량 매도.
        2.  **강제 손절 컷:** 평단가 대비 수익률 **`-15%`** 도달 시 즉각 손절.
    
    ### 🚀 [전략 B] 중소형주 (Satellite) - 스마트 수급 눌림목
    *   **📌 타겟 유니버스:** KOSDAQ 시가총액 1,000억 원 이상 상위 종목
    *   **🟢 AI 진입 조건 (모두 만족 시):**
        1.  **장기 추세 방어:** 현재 주가가 200일 이동평균선(MA200) 이상에 위치.
        2.  **수급 폭발:** 최근 20일 내 거래량이 5일 평균 거래량 대비 **200% 이상 급증**.
        3.  **스마트 눌림목:** 현재 주가가 20일선 부근(`-5% ~ +3%`)으로 조정 또는 당일 저가가 20일선 터치 후 지지.
        4.  **하방 리스크 제한:** 최근 120일 최고가 대비 하락폭(MDD)이 `-30%` 이내.
        5.  **고점 형성 경과일수:** 최고가 시점이 최근 45일 이내.
        6.  **거래량 감쇄:** 조정 구간의 거래량이 고점 거래량의 50% 이하로 축소.
    *   **🔴 AI 이탈 조건:**
        1.  **추세 붕괴:** 주가가 20일 이동평균선을 하향 이탈 시 전량 매도.
        2.  **강제 손절 컷:** 매수 단가 대비 수익률 **`-12%`** 도달 시 즉각 손절.
    
    ---
    
    ## 3. 🌍 거시경제 및 리스크 관리
    *   **🚨 VIX 공포지수 브레이크:** VIX 지수 30 초과 시 신규 매수 전면 동결.
    *   **🔥 VIX Contrarian:** VIX 25 이상 급등 후 단기 꺾임 시 낙폭과대 역발상 매수 허용.
    *   **🎣 라이브 트레일링 스탑:** 목표 수익(대형주 30%, 중소형주 15%) 달성 후 고점 대비 일정 비율 하락 시 시장가 익절.
    *   **🥶 실전 쿨다운 시스템:** 연속 2회 손실 발생 시 해당 종목 60일간 진입 차단.
    *   **💧 유동성 필터:** 5일 평균 거래대금 50억 원 미만 종목 매수 보류.
    """, unsafe_allow_html=True)
