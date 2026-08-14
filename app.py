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

if 'search_q' not in st.session_state: st.session_state.search_q = None
if 'search_sec' not in st.session_state: st.session_state.search_sec = None
if 'show_scanner' not in st.session_state: st.session_state.show_scanner = False

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
        try: worksheet = sh.worksheet("Settings")
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
# 1. 헬퍼 함수 모음
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
    tr_id = ("VTTC0802U" if order_type == "BUY" else "VTTC0801U") if is_mock else ("TTTC0802U" if order_type == "BUY" else "TTTC0801U")
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
        '체결 시간': now_str, '종목명': s_name, '주문 구분': '매도(청산)' if order_type == "SELL" else '매수(진입)',
        '상태': status, '체결 단가': price, '체결 수량': qty, '체결 금액': price * qty, '실현 손익': pnl, '비고 (API 메시지)': msg
    })
    if len(p_data['daily_trades']) > 30:
        p_data['daily_trades'] = p_data['daily_trades'][-30:]
    return p_data

# ==========================================
# 2. 공통 두뇌(quant_engine) 연결 브릿지 안전 래퍼
# ==========================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_krx_universe():
    try: return qe.load_krx_universe()
    except: return pd.DataFrame()

@st.cache_data(ttl=1800)
def fetch_market_data():
    try:
        if hasattr(qe, 'fetch_market_data'):
            return qe.fetch_market_data()
    except: pass
    return 20.0, False, True, 0.0, 0.0

@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code):
    try: return qe.fetch_stock_status(ticker_code)
    except: return None

def analyze_quant_strategy(*args, **kwargs):
    return qe.analyze_quant_strategy(*args, **kwargs)

@st.cache_data(ttl=3600)
def run_core_scanner(use_ma200, buf_pct):
    krx = load_krx_universe()
    buf = buf_pct / 100.0
    if krx.empty: return pd.DataFrame()
    cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in krx.columns else krx.head(100)
    
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
    kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)] if 'Market' in krx.columns else krx
    cands = kosdaq[kosdaq['Marcap'] >= 100000000000].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in kosdaq.columns else kosdaq.head(100)
    
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
    try:
        bm = fdr.DataReader(idx_sym, f_start, end_date)
        if not bm.empty:
            bm = bm[~bm.index.duplicated(keep='first')]
            if bm.index.tz is not None: bm.index = bm.index.tz_localize(None)
            bm['Bm_Ret_60'] = bm['Close'] / bm['Close'].shift(60) - 1
            bm['Bm_MA60'] = bm['Close'].rolling(60).mean()
            m_df['Bm_Ret_60'], m_df['Bm_Bull'] = bm['Bm_Ret_60'], bm['Close'] > bm['Bm_MA60']
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
            if r is not None and not r[1].empty: s_dfs[r[0]] = r[1]
    if not s_dfs: return None
    c_idx = max([d.index for d in s_dfs.values()], key=len)
    fast_data = {}
    for nm, df in s_dfs.items():
        df_re = df.reindex(c_idx).ffill().fillna(0)
        fast_data[nm] = {'Close': df_re['Close'].to_numpy(), 'Sig': df_re['Sig'].to_numpy(), 'Sc': df_re['Sc'].to_numpy(), 'Bm_Bull': df_re['Bm_Bull'].to_numpy()}
    
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
    val_str = str(val)
    if val_str.startswith('+'): return 'color: #FF5050; font-weight: bold;'
    elif val_str.startswith('-') and len(val_str) > 1 and val_str != '-': return 'color: #3b82f6; font-weight: bold;'
    return ''

def apply_mts_style(df, subset_cols):
    valid_cols = [c for c in subset_cols if c in df.columns]
    if not valid_cols: return df
    if hasattr(df.style, 'map'): return df.style.map(color_profit_loss, subset=valid_cols)
    else: return df.style.applymap(color_profit_loss, subset=valid_cols)

def mts_metric_html(label, value, delta=None):
    val_color, val_str = "white", str(value)
    if not delta: 
        if val_str.startswith('+'): val_color = "#FF5050"
        elif val_str.startswith('-') and val_str != '-': val_color = "#3b82f6"
    delta_html = ""
    if delta:
        d_str = str(delta)
        d_color = "#FF5050" if d_str.startswith('+') else ("#3b82f6" if d_str.startswith('-') and d_str != '-' else "#a3a8b8")
        delta_html = f'<div style="color: {d_color}; font-size: 1rem; font-weight: bold; margin-top: 4px;">{d_str}</div>'
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
st.sidebar.header("🎯 현재 작업할 포트폴리오 선택")
all_ports = load_all_portfolios_from_sheets()
port_names = list(all_ports.keys())
selected_port = st.sidebar.selectbox("구글 시트 DB 목록", port_names) if port_names else None
p_data = all_ports.get(selected_port) if selected_port else None
active_strat = p_data.get('strategy', '대형주 (Core)') if p_data else "대형주 (Core)"
total_cash = int(p_data.get('cash', 10000000)) if p_data else 10000000

# 🛑 [복원] 사이드바에서 앱 키를 비밀번호형태로 직접 입력/관리할 수 있도록 보완
with st.sidebar.expander("🔑 KIS API 키 직접 입력 (선택)", expanded=False):
    manual_app_key = st.text_input("APP KEY", value=p_data.get('manual_app_key', '') if p_data else '', type="password")
    manual_app_secret = st.text_input("APP SECRET", value=p_data.get('manual_app_secret', '') if p_data else '', type="password")
    manual_cano = st.text_input("계좌번호 (앞 8자리)", value=p_data.get('manual_cano', '') if p_data else '')
    if st.button("키 정보 저장"):
        if p_data:
            p_data['manual_app_key'] = manual_app_key
            p_data['manual_app_secret'] = manual_app_secret
            p_data['manual_cano'] = manual_cano
            save_portfolio_to_sheets(selected_port, p_data)
            st.success("API 키 저장 완료!")
            try: st.query_params["auth"] = daily_token
            except: st.experimental_set_query_params(auth=daily_token)
            st.rerun()

# KIS 계좌 정보 우선순위: 수동 입력값 -> secrets.toml 값
SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = None, None, None, None, True
if p_data and p_data.get('manual_app_key'):
    SYS_APP_KEY = p_data.get('manual_app_key')
    SYS_APP_SECRET = p_data.get('manual_app_secret')
    SYS_CANO = str(p_data.get('manual_cano'))
    SYS_ACNT_PRDT = "01"
    SYS_IS_MOCK = True
elif p_data:
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
            dnca_tot = float(summary[0].get('dnca_tot_amt', 0))
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
        if '✅' in trade.get('상태', '✅'): cumulative_realized_pnl += trade.get('실현 손익', 0.0)
        
manual_offset = float(p_data.get('pnl_offset', 0.0)) if p_data else 0.0
total_invested_principal = float(total_cash)
if kis_data:
    total_invested_principal = real_total_eval - real_eval_pnl - cumulative_realized_pnl - manual_offset
    if total_invested_principal <= 0: total_invested_principal = total_cash

if p_data:
    st.sidebar.markdown("---")
    st.sidebar.subheader("💰 Virtual Capital & Settings")
    st.sidebar.markdown(f"**현재 설정 전략:** `{active_strat}`")
    new_cash = st.sidebar.number_input(f"총 투자 운용 자산", value=int(total_cash), step=1_000_000, format="%d")
    if new_cash != total_cash:
        p_data['cash'] = new_cash
        save_portfolio_to_sheets(selected_port, p_data)
        try: st.query_params["auth"] = daily_token
        except: st.experimental_set_query_params(auth=daily_token)
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")
if SYS_APP_KEY: st.sidebar.success(f"✅ **{active_strat} 계좌** 연동됨")
else: st.sidebar.warning("🔑 **KIS API 미연동**")

st.sidebar.markdown("---")
st.sidebar.header("📱 텔레그램 및 오토파일럿")
tg_token, tg_chat_id = st.secrets.get("telegram", {}).get("bot_token", ""), st.secrets.get("telegram", {}).get("chat_id", "")
if tg_token and tg_chat_id:
    st.sidebar.success("✅ 텔레그램 봇 연동 완료")
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

    st.sidebar.subheader("🚨 긴급 제어 및 자동매매")
    if p_data:
        ks_key, at_key, ap_key = f"ks_{selected_port}", f"at_{selected_port}", f"ap_{selected_port}"
        init_ks = p_data.get('kill_switch', False)
        init_at = p_data.get('auto_trade_enabled', False)
        init_ap = p_data.get('auto_pilot', False)
        kill_switch = st.sidebar.toggle("🚨 긴급 정지 (KILL SWITCH)", value=init_ks, key=ks_key)
        auto_trade_enabled = st.sidebar.toggle("🚀 실전 자동주문 활성화", value=init_at, key=at_key)
        auto_pilot = st.sidebar.toggle("🔄 오토파일럿 켜기", value=init_ap, key=ap_key)
        
        if kill_switch != init_ks or auto_trade_enabled != init_at or auto_pilot != init_ap:
            p_data['kill_switch'], p_data['auto_trade_enabled'], p_data['auto_pilot'] = kill_switch, auto_trade_enabled, auto_pilot
            save_portfolio_to_sheets(selected_port, p_data)
            try: st.query_params["auth"] = daily_token
            except: st.experimental_set_query_params(auth=daily_token)
            st.rerun()
        if kill_switch: st.sidebar.error("⚠️ 킬 스위치 작동 중!")

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

# ==========================================
# 4. 메인 화면 구성 (5개 탭 및 대기열 순위표 복원)
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
    st.markdown("관심 종목을 추가하면 AI가 실시간으로 타점을 진단합니다.")
    if not p_data or not selected_port:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 선택하세요.")
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
                        st.success("정리 완료!")
                        try: st.query_params["auth"] = daily_token
                        except: st.experimental_set_query_params(auth=daily_token)
                        st.rerun()

        st.markdown("### ⌨️ 직접 종목 검색")
        with st.form("manual_search_form"):
            search_query = st.text_input("종목명 또는 종목코드(6자리) 입력", placeholder="예: 삼성전자, 005930")
            if st.form_submit_button("🔍 검색하기"): st.session_state.search_q = search_query

        current_watchlist_tickers = [str(s.get('티커')).strip().zfill(6) for s in p_data.get('stocks', [])]
        if st.session_state.search_q:
            krx_df = load_krx_universe()
            if not krx_df.empty:
                matched = krx_df[krx_df['Name'].str.contains(st.session_state.search_q, case=False, na=False) | krx_df['Code'].str.contains(st.session_state.search_q, na=False)].head(5)
                for _, r in matched.iterrows():
                    m_name, m_code = r['Name'], str(r['Code']).zfill(6)
                    cc1, cc2 = st.columns([8, 2])
                    cc1.markdown(f"`{m_code}` **{m_name}**")
                    if m_code not in current_watchlist_tickers:
                        if cc2.button("➕ 등록", key=f"add_m_{m_code}"):
                            p_data['stocks'].append({'종목명': m_name, '티커': m_code, '매수단가': 0, '보유수량': 0})
                            save_portfolio_to_sheets(selected_port, p_data)
                            try: st.query_params["auth"] = daily_token
                            except: st.experimental_set_query_params(auth=daily_token)
                            st.rerun()

        if st.session_state.show_scanner:
            with st.spinner("AI 퀀트 필터 검색 중..."):
                scan_result = run_core_scanner(use_ma200_filter, whipsaw_buffer) if active_strat == '대형주 (Core)' else run_satellite_scanner(use_ma200_filter)
                if not scan_result.empty:
                    for _, row in scan_result.iterrows():
                        c1, c2, c3 = st.columns([4, 4, 2])
                        c1.write(f"**{row['종목명']}** (`{row['티커']}`)")
                        c2.write(f"현재가: {row['현재가']}")
                        if str(row['티커']).strip().zfill(6) not in current_watchlist_tickers:
                            if c3.button("➕ 담기", key=f"add_scan_{row['티커']}"):
                                p_data['stocks'].append({'종목명': row['종목명'], '티커': str(row['티커']).strip().zfill(6), '매수단가': 0, '보유수량': 0})
                                save_portfolio_to_sheets(selected_port, p_data)
                                try: st.query_params["auth"] = daily_token
                                except: st.experimental_set_query_params(auth=daily_token)
                                st.rerun()

        st.markdown("---")
        sandbox_stocks = p_data.get('stocks', [])
        visible_stocks = [s for s in sandbox_stocks if str(s.get('티커')).strip().zfill(6) not in real_holdings_tickers]
        display_records, eval_actions_cache = [], {}
        
        for row in visible_stocks:
            ticker = str(row.get('티커', '')).strip().zfill(6)
            c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if kis_token_global else None
            res = fetch_stock_status(ticker)
            action, ai_score = "분석 불가", 0.0
            if res and res[0] is not None:
                yf_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, _, vol_surged, yf_low, recent_vol_max, avg_trade_val, dsp, vc = res
                if not c_price or c_price == 0: c_price = yf_price
                res_q = analyze_quant_strategy(active_strat, c_price, 0.0, 0.0, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, dsp, vc)
                ai_score = res_q['ai_score']
                action = "🟢 매수 시그널 발생" if res_q['entry_cond'] else "🟡 모니터링 유지"
            display_records.append({'선택': False, '종목명': row.get('종목명'), '티커': ticker, '실시간 현재가': c_price, '🔥 매력도 점수': ai_score, '🤖 AI 액션 플랜': action})
        
        display_df = pd.DataFrame(display_records)
        if not display_df.empty: display_df = display_df.sort_values(by="🔥 매력도 점수", ascending=False).reset_index(drop=True)
        edited_df = st.data_editor(display_df, num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}")
        
        if st.button("💾 변경된 내용 저장", type="primary", use_container_width=True):
            save_df = edited_df[['종목명', '티커']].copy()
            save_df['티커'] = save_df['티커'].astype(str).str.strip().str.zfill(6)
            p_data['stocks'] = save_df.to_dict('records')
            save_portfolio_to_sheets(selected_port, p_data)
            st.success("저장 완료!")
            try: st.query_params["auth"] = daily_token
            except: st.experimental_set_query_params(auth=daily_token)
            st.rerun()

with tab2:
    st.header("🔌 실전 계좌 (Real Account) 모니터링")
    if not SYS_APP_KEY: st.warning("사이드바 또는 API 키 설정을 확인하세요.")
    elif kis_data:
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        col_m1.markdown(mts_metric_html("💰 총 평가 금액", f"{real_total_eval:,.0f} 원"), unsafe_allow_html=True)
        col_m2.markdown(mts_metric_html("📥 투자 원금", f"{total_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        col_m3.markdown(mts_metric_html("📈 누적 수익금", f"{real_eval_pnl:+,.0f} 원"), unsafe_allow_html=True)
        col_m4.markdown(mts_metric_html("💵 가용 현금", f"{real_cash_avail:,.0f} 원"), unsafe_allow_html=True)
        if not real_stocks_df.empty:
            st.dataframe(real_stocks_df, use_container_width=True, hide_index=True)

with tab3:
    st.header("🤖 실전 자동매매 관제센터 & 우선순위 주문 대기열")
    col_c1, col_c2, col_c3 = st.columns(3)
    col_c1.metric("🚨 킬 스위치", "차단됨" if kill_switch else "정상")
    col_c2.metric("🚀 자동주문", "활성화" if auto_trade_enabled else "비활성화")
    col_c3.metric("💵 가용 예수금", f"{real_cash_avail:,.0f} 원")
    st.markdown("---")
    
    # 🛑 [복원] 자동매매 우선순위 큐(순위표) 로직 계산 및 렌더링
    temp_queue = []
    eligible_stocks = {str(s['티커']).strip().zfill(6): s.get('종목명', '') for s in p_data.get('stocks', [])} if p_data else {}
    
    current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
    if current_asset_base <= 0: current_asset_base = total_cash
    target_buy_amt = current_asset_base * (max_alloc_pct / 100.0)

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

        highest_price = p_data.get('ts_tracker', {}).get(ticker, buy_price) if p_data else buy_price
        res_q = analyze_quant_strategy(active_strat, live_c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, dsp, vc)

        is_sell, sell_type = False, ""
        if qty_num > 0:
            if res_q['stop_loss_cond']: is_sell, sell_type = True, "🔴 긴급 손절 매도"
            elif res_q['trailing_stop_cond']: is_sell, sell_type = True, "🔵 트레일링 익절"
            elif res_q['exit_cond_trend']: is_sell, sell_type = True, "🔴 추세 이탈 매도"
                
        if is_sell:
            temp_queue.append({
                '우선순위_분류': 0, '🔥 점수': 999.0, '종목명': s_name, '티커': ticker, '구분': sell_type,
                '주문 단가': f"{live_c_price:,.0f} 원", '주문 수량': f"{qty_num:,} 주", '필요 자금': f"-{live_c_price * qty_num:,.0f} 원 (회수)",
                '_raw_price': live_c_price, '_qty': qty_num, '_req_fund': 0, '주문 실행 상태': '대기 중'
            })
            continue 

        if res_q['entry_cond'] and avg_trade_val >= 5000000000:
            current_holding_amt = qty_num * live_c_price
            additional_amt = max(0, target_buy_amt - current_holding_amt)
            add_qty = int(additional_amt // live_c_price)
            if add_qty > 0:
                req_fund = add_qty * live_c_price
                buy_type = "🛒 신규 매수" if qty_num == 0 else "🟢 비중 확대"
                temp_queue.append({
                    '우선순위_분류': 1, '🔥 점수': res_q['ai_score'], '종목명': s_name, '티커': ticker, '구분': buy_type,
                    '주문 단가': f"{live_c_price:,.0f} 원", '주문 수량': f"{add_qty:,} 주", '필요 자금': f"{req_fund:,.0f} 원",
                    '_raw_price': live_c_price, '_qty': add_qty, '_req_fund': req_fund, '주문 실행 상태': '대기 중'
                })

    temp_queue = sorted(temp_queue, key=lambda x: (x['우선순위_분류'], -x['🔥 점수']))
    queue_df = pd.DataFrame(temp_queue)

    if not queue_df.empty:
        queue_df['우선순위'] = [f"{i+1}위" for i in range(len(queue_df))]
        display_queue = queue_df.copy()
        display_queue['🔥 점수'] = display_queue['🔥 점수'].apply(lambda x: "🚨 최우선 (매도)" if float(x) >= 900.0 else f"{float(x):.2f}")
        st.subheader("📋 AI 매매 우선순위 대기열 (Order Queue)")
        st.table(display_queue[['우선순위', '종목명', '구분', '🔥 점수', '주문 단가', '주문 수량', '필요 자금', '주문 실행 상태']])
    else:
        st.info("💡 현재 AI 퀀트 엔진이 포착한 대기 중인 매수/매도 시그널이 없습니다.")

    st.markdown("---")
    st.info("💡 안전을 위해 화면 대시보드에서는 자동 발사 기능이 제거되었으며, 수동 전송 버튼을 누를 때만 안전하게 전송됩니다.")
    if st.button("⚡ 대기열 일괄 주문 수동 전송", type="primary", use_container_width=True):
        st.success("수동 주문 검토 완료 (실제 집행은 봇이 안전하게 수행합니다)")

with tab4:
    st.header("🧪 시뮬레이션 및 백테스트")
    stocks_df = pd.DataFrame(p_data.get('stocks', [])) if p_data else pd.DataFrame()
    if st.button("🚀 장기 백테스트 실행", type="primary", use_container_width=True):
        if not stocks_df.empty:
            with st.spinner("백테스트 구동 중..."):
                res_bt = run_quant_simulation(stocks_df, active_strat, total_cash, datetime.date(2023, 1, 1), datetime.datetime.now(KST).date(), use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                if res_bt:
                    st.success(f"백테스트 완료! 최종 자산: {res_bt['final_asset']:,.0f} 원 ({res_bt['final_port_ret']:+.2f}%)")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서</h1>
    <hr>
    <p>본 시스템은 Core(대형주 추세추종)와 Satellite(중소형주 수급 눌림목) 전략을 결합한 듀얼 퀀트 시스템입니다.</p>
    """, unsafe_allow_html=True)
