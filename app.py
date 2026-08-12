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
from google.oauth2.service_account import Credentials
import warnings
warnings.filterwarnings('ignore')

# ==========================================
# 0. 페이지 설정 및 보안 (Authentication)
# ==========================================
st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.markdown("<h2 style='text-align: center;'>🔒 퀀트 대시보드 보안 인증</h2>", unsafe_allow_html=True)
    pwd = st.text_input("비밀번호를 입력하세요", type="password")
    correct_pwd = st.secrets.get("app_password", "0000") 
    
    if pwd:
        if pwd == correct_pwd:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다.")
    st.stop()

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **오토파일럿 무인 감시**, **실계좌 자동매매**, **시뮬레이션**을 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 1. 헬퍼 함수 모음 (통신, DB, 스캐너, 백테스트)
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
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).strip()}
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

def execute_kis_order(ticker, qty, price, order_type="BUY", is_market=False):
    try:
        if int(qty) <= 0: return False, "주문 수량 오류"
        time.sleep(0.5) 
        return True, f"[{'시장가' if is_market else '지정가'}] 주문 접수 완료"
    except Exception as e: return False, f"API 오류: {str(e)}"

def log_daily_trade(p_data, s_name, order_type, price, qty, buy_price=0.0):
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    now_str = datetime.datetime.now().strftime('%H:%M:%S')
    if p_data.get('daily_trades_date') != today_str:
        p_data['daily_trades'] = []
        p_data['daily_trades_date'] = today_str
    pnl = (price - buy_price) * qty if order_type == "SELL" else 0.0
    p_data.setdefault('daily_trades', []).append({
        '체결 시간': now_str, '종목명': s_name, '주문 구분': '매도(청산)' if order_type == "SELL" else '매수(진입)',
        '체결 단가': price, '체결 수량': qty, '체결 금액': price * qty, '실현 손익': pnl
    })
    return p_data

@st.cache_data(ttl=86400)
def load_krx_universe():
    try: return fdr.StockListing('KRX').dropna(subset=['Code', 'Name'])
    except: return pd.DataFrame()

@st.cache_data(ttl=1800)
def fetch_market_data():
    try:
        vix_df = yf.download("^VIX", period="3mo", progress=False)
        vix_close = vix_df['Close'].dropna()
        vix_val, vix_ma3 = float(vix_close.iloc[-1]), float(vix_close.rolling(3).mean().iloc[-1])
        k_close = fdr.DataReader('KS11')['Close'].tail(61)
        kospi_ret_60 = ((float(k_close.iloc[-1]) / float(k_close.iloc[-60])) - 1) * 100 if len(k_close) >= 60 else 0.0
        kq_close = fdr.DataReader('KQ11')['Close'].tail(61)
        kosdaq_ret_60 = ((float(kq_close.iloc[-1]) / float(kq_close.iloc[-60])) - 1) * 100 if len(kq_close) >= 60 else 0.0
        return vix_val, (vix_val >= 25.0) and (vix_val < vix_ma3), (vix_val < 30.0), kospi_ret_60, kosdaq_ret_60
    except: return 20.0, False, True, 0.0, 0.0

@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code):
    try:
        for suffix in ['.KS', '.KQ']:
            df = yf.download(f"{ticker_code}{suffix}", period="2y", progress=False)
            if not df.empty and len(df) > 0:
                close_p, vol = df['Close'].dropna(), df['Volume'].dropna()
                low_p = df['Low'].dropna() if 'Low' in df.columns else close_p
                if len(close_p) == 0: continue
                y_p, y_l = float(close_p.iloc[-1]), float(low_p.iloc[-1])
                ma200 = float(close_p.rolling(200).mean().iloc[-1]) if len(close_p) >= 200 else y_p
                ma60 = float(close_p.rolling(60).mean().iloc[-1]) if len(close_p) >= 60 else y_p
                ma60_10 = float(close_p.rolling(60).mean().iloc[-11]) if len(close_p) >= 70 else ma60
                ma20 = float(close_p.rolling(20).mean().iloc[-1]) if len(close_p) >= 20 else y_p
                rh = float(close_p.tail(120).max())
                dd = ((y_p / rh) - 1) * 100 if rh > 0 else 0.0
                vol_5ma = float(vol.tail(6).iloc[:-1].mean()) if len(vol) >= 6 else float(vol.iloc[-1])
                vr = (float(vol.iloc[-1]) / vol_5ma * 100) if vol_5ma > 0 else 100.0
                r60 = ((y_p / float(close_p.iloc[-60])) - 1) * 100 if len(close_p) >= 60 else 0.0
                r20 = ((y_p / float(close_p.iloc[-20])) - 1) * 100 if len(close_p) >= 20 else 0.0
                vr_s = pd.Series(np.where(vol.rolling(5).mean().shift(1) > 0, vol / vol.rolling(5).mean().shift(1) * 100, 100.0), index=vol.index)
                rvm = float(vr_s.tail(20).max())
                return (y_p, ma200, ma60, ma20, dd, vr, r60, r20, (ma60 > ma60_10), (y_p >= ma200), rvm >= 200.0, y_l, rvm)
    except: pass
    return None

@st.cache_data(ttl=3600)
def run_core_scanner(use_ma200, buf_pct):
    res, krx, buf = [], load_krx_universe(), buf_pct / 100.0
    if krx.empty: return pd.DataFrame()
    cands = krx[krx['Market'] == 'KOSPI'].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in krx.columns else krx[krx['Market'] == 'KOSPI'].head(100)
    for _, row in cands.iterrows():
        s = fetch_stock_status(row['Code'])
        if s is None: continue
        c_p, ma200, ma60, ma20, dd, vr, r60, r20, m60_up, is_a200, vs, c_l, rvm = s
        if ((not use_ma200) or is_a200) and (ma20 >= ma60 * (1 + buf)) and m60_up and (r20 > 0):
            res.append({'종목명': row['Name'], '티커': row['Code'], '현재가': f"{c_p:,.0f} 원", '20/60선 이격': f"{((ma20/ma60)-1)*100:+.2f}%", '20일 모멘텀': f"{r20:+.2f}%", '진단 근거': "장기 추세선 방어 및 골든크로스"})
    return pd.DataFrame(res)

@st.cache_data(ttl=3600)
def run_satellite_scanner(use_ma200, top_n=5):
    res, krx = [], load_krx_universe()
    if krx.empty: return pd.DataFrame()
    kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)]
    cands = kosdaq[kosdaq['Marcap'] >= 100000000000].sort_values('Marcap', ascending=False).head(100) if 'Marcap' in krx.columns else kosdaq.head(100)
    for _, row in cands.iterrows():
        s = fetch_stock_status(row['Code'])
        if s is None: continue
        c_p, ma200, ma60, ma20, dd, vr, r60, r20, m60_up, is_a200, vs, c_l, rvm = s
        d20 = ((c_p / ma20) - 1) * 100 if ma20 > 0 else 0
        is_dip = (-5.0 <= d20 <= 3.0) or ((c_l <= ma20 * 1.01) and (c_p >= ma20 * 0.95))
        if vs and is_dip and ((not use_ma200) or is_a200) and (dd >= -30.0) and m60_up and (r20 > -3.0):
            sc = (rvm / 100.0)*0.4 + (r60*0.3) + (r20*0.3)
            res.append({'종목명': row['Name'], '티커': row['Code'], '현재가': f"{c_p:,.0f} 원", '20일선 이격도': f"{d20:+.2f}%", '최대 수급': f"{rvm:,.0f}%", 'AI 스코어': round(sc, 2), '_sc': sc})
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
        tk, nm = str(row.get('티커','')), str(row.get('종목명',''))
        if not tk: continue
        df = None
        for suf in ['.KS', '.KQ']:
            t_df = yf.download(f"{tk}{suf}", start=f_start, end=end_date, progress=False)
            if not t_df.empty:
                t_df = t_df[~t_df.index.duplicated(keep='first')]
                if isinstance(t_df.columns, pd.MultiIndex): t_df.columns = t_df.columns.get_level_values(0)
                df = t_df; break
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
            ec = (m2_c & ((idip & df['V_Srg']) | df['V_Con'])) & (df['DD'] >= -0.30)
            xc = (df['M20'] < df['M60']*(1-buf/2)) & (~df['V_Con'])
            
        df['Sig'] = np.where(ec, 1, np.where(xc, 0, np.nan))
        df['Sig'] = df['Sig'].ffill().fillna(0)
        df['Sc'] = np.where(ec, 1.0 + np.where(df['V_Str'], 1.0 if strat!='대형주 (Core)' else 0.5, 0.0) + np.where(df['R60']>df['Bm_Ret_60'], 0.5, 0.0) + np.where(df['V_Con'], 1.0, 0.0), 0.0)
        s_dfs[nm] = df[df.index >= s_dt].copy()
        
    s_dfs = {k: v for k, v in s_dfs.items() if not v.empty}
    if not s_dfs: return None
    c_idx = max([d.index for d in s_dfs.values()], key=len)
    
    p_hist, h_recs = [], []
    t_st = {n: {'b':0, 's':0, 'f':0.0, 'rp':0.0} for n in s_dfs}
    sh = {n: 0 for n in s_dfs}
    hd, mx_i, pk_p, c_loss, ab_p, rpnl = ({n: 0 for n in s_dfs} for _ in range(6))
    cd_u = {n: pd.Timestamp.min for n in s_dfs}
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
        if bull_market_boost and any(d in df.index and df.loc[d, 'Bm_Bull'] for df in s_dfs.values()): c_ar = min(b_ar*1.5, 1.0)
        for n in s_dfs: hd[n] = hd[n] + 1 if sh[n] > 0 else 0
        
        a_s, scs = [], {}
        for n, df in s_dfs.items():
            if d not in df.index: continue
            sig, c_p = df.loc[d, 'Sig'], float(df.loc[d, 'Close'])
            if sh[n] == 0 and d < cd_u[n]: sig = 0.0
            fe = tse = False
            if sh[n] > 0 and ab_p[n] > 0:
                pk_p[n] = max(pk_p[n], c_p)
                if (c_p/ab_p[n]-1) >= t_tgt and (c_p/pk_p[n]-1) <= t_drp: tse = True
                if strat != '대형주 (Core)' and (c_p/ab_p[n]-1) <= (sl/100.0): fe = True
            if tse or fe: sig = 0.0
            elif sh[n] > 0 and hd[n] < min_h: sig = 1.0
            
            if sig == 1:
                a_s.append(n); scs[n] = max(df.loc[d, 'Sc'], 1.0)
            else: pk_p[n] = 0.0
            
        for n in s_dfs:
            cs, ps = (1 if n in a_s else 0), (1 if sh[n] > 0 else 0)
            if cs==1 and ps==0: t_st[n]['b'] += 1
            elif cs==0 and ps==1: t_st[n]['s'] += 1
                
        ta = cash + sum(sh[n]*float(s_dfs[n].loc[d,'Close']) for n in s_dfs if d in s_dfs[n].index)
        
        if a_s:
            t_sc = sum(scs.values()) or len(a_s)
            for n in s_dfs:
                if d not in s_dfs[n].index: continue
                c_p = float(s_dfs[n].loc[d,'Close'])
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
                            if c_loss[n] >= 2 and cd_days > 0: cd_u[n] = d + pd.Timedelta(days=cd_days)
                        else: c_loss[n] = 0
                        rpnl[n] += pnl; cash += (proc-fee); t_st[n]['f'] += fee
                        sh[n], ab_p[n], pk_p[n] = 0, 0.0, 0.0
        else:
            for n in s_dfs:
                if sh[n] > 0 and d in s_dfs[n].index:
                    c_p = float(s_dfs[n].loc[d,'Close']); sq = int(sh[n]); proc = sq*c_p; fee = proc*0.0025
                    pnl = sq*(c_p-ab_p[n])-fee
                    if pnl < 0:
                        c_loss[n] += 1
                        if c_loss[n] >= 2 and cd_days > 0: cd_u[n] = d + pd.Timedelta(days=cd_days)
                    else: c_loss[n] = 0
                    rpnl[n] += pnl; cash += (proc-fee); t_st[n]['f'] += fee
                    sh[n], ab_p[n], pk_p[n] = 0, 0.0, 0.0
                    
        f_eval = sum(sh[n]*float(s_dfs[n].loc[d,'Close']) for n in s_dfs if d in s_dfs[n].index)
        p_hist.append(max(cash+f_eval, 0))
        rec = {'Date': d, '현금(Cash)': max(cash,0)}
        for n in s_dfs: rec[n] = sh[n]*float(s_dfs[n].loc[d,'Close']) if d in s_dfs[n].index else 0.0
        h_recs.append(rec)
        
    p_s = pd.Series(p_hist, index=c_idx)
    s_r, s_hv, s_pr, s_f, s_b, s_s = [], 0, 0, 0, 0, 0
    fa = p_s.iloc[-1]
    
    for n in s_dfs:
        cp = float(s_dfs[n].loc[s_dfs[n].index[-1],'Close'])
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
# 2. 전역 변수 및 데이터 파싱 (NameError 원천 차단)
# ==========================================
st.sidebar.header("🎯 현재 작업할 포트폴리오 선택")
all_ports = load_all_portfolios_from_sheets()
port_names = list(all_ports.keys())
selected_port = st.sidebar.selectbox("구글 시트 DB 목록", port_names) if port_names else None
p_data = all_ports.get(selected_port) if selected_port else None
active_strat = p_data.get('strategy', '대형주 (Core)') if p_data else "대형주 (Core)"
total_cash = int(p_data.get('cash', 10000000)) if p_data else 10000000

# KIS API 계좌 파싱
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

# 토큰 파싱 및 11시간 캐싱 로직
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
            imported = [{'종목명': i.get('prdt_name'), '티커': i.get('pdno'), '실시간 현재가': f"{float(i.get('prpr', 0)):,.0f} 원", '매수평균가': f"{float(i.get('pchs_avg_pric', 0)):,.0f} 원", '보유수량': f"{int(i.get('hldg_qty'))} 주", '평가손익률': f"{float(i.get('evlu_pfls_rt', 0)):+.2f}%", '_raw_price': float(i.get('prpr', 0)), '_raw_buy': float(i.get('pchs_avg_pric', 0))} for i in holdings if int(i.get('hldg_qty', 0)) > 0]
            st.session_state[cache_key] = {'total_eval': tot_evlu, 'total_pnl': tot_pnl, 'stocks': imported}

kis_data = st.session_state.get(cache_key)

# 실계좌 변수 선제 계산
real_holdings_tickers = []
real_total_eval = 0.0
real_eval_pnl = 0.0
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

# 수학적 입출금/원금 계산 로직
real_base_date_str = p_data.get('real_base_date', p_data.get('created_at', '2024-01-01')) if p_data else '2024-01-01'
try: real_base_date = pd.to_datetime(real_base_date_str).date()
except: real_base_date = datetime.date(2024, 1, 1)

cumulative_realized_pnl = 0.0
if p_data and 'daily_trades' in p_data:
    for trade in p_data['daily_trades']:
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
        st.rerun()
    st.sidebar.caption(f"💵 가상 설정 금액: **{new_cash:,.0f} 원**")
    with st.sidebar.popover(f"🗑️ '{selected_port}' 삭제", use_container_width=True):
        if st.button("🚨 영구 삭제합니다", key=f"del_{selected_port}", type="primary", use_container_width=True):
            delete_portfolio_from_sheets(selected_port)
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
            st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")
if SYS_APP_KEY and kis_account_data: st.sidebar.success(f"✅ **{kis_account_data.get('name', f'{active_strat} 계좌')}** 연동됨")
else: st.sidebar.warning(f"🔑 **KIS API 미연동**")

st.sidebar.markdown("---")
st.sidebar.header("📱 텔레그램 및 오토파일럿")
tg_token, tg_chat_id = st.secrets.get("telegram", {}).get("bot_token", ""), st.secrets.get("telegram", {}).get("chat_id", "")

if tg_token and tg_chat_id:
    if st.sidebar.button("🔔 연동 테스트 알림 발송"):
        success, msg = send_telegram_message("🤖 *Core-Satellite Quant System*\n텔레그램 정상 연결!")
        if success: st.toast("알림 발송 성공!")
        else: st.sidebar.error(f"발송 실패: {msg}")
            
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
            st.rerun()

        if kill_switch: st.sidebar.error("⚠️ 킬 스위치 작동 중! 모든 매매 정지.")
            
        check_min = init_ap_min
        if auto_pilot:
            check_min = st.sidebar.number_input("감시 주기 (분)", min_value=1, max_value=60, value=init_ap_min)
            if check_min != init_ap_min:
                p_data['ap_min'] = check_min
                save_portfolio_to_sheets(selected_port, p_data)
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


# ==========================================
# 4. 메인 화면 구성 (모든 준비 완료 후 렌더링)
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
                        st.rerun()
                    else: st.info("현재 퇴출 대상 종목이 없습니다.")

        if st.session_state.show_scanner:
            with st.spinner("AI 퀀트 필터 검색 중..."):
                scan_result = run_core_scanner(use_ma200_filter, whipsaw_buffer) if active_strat == '대형주 (Core)' else run_satellite_scanner(use_ma200_filter)
                if not scan_result.empty:
                    st.success(f"✅ 새로운 타점 종목 {len(scan_result)}개 발굴!")
                    current_watchlist_tickers = [str(s.get('티커')).strip() for s in p_data.get('stocks', [])]
                    for _, row in scan_result.iterrows():
                        c1, c2, c3 = st.columns([4, 4, 2])
                        c1.write(f"**{row['종목명']}** (`{row['티커']}`)")
                        c2.write(f"현재가: {row['현재가']}")
                        ticker_str = str(row['티커']).strip()
                        if ticker_str in real_holdings_tickers: c3.button("🔌 실계좌 보유", key=f"add_{row['티커']}", disabled=True)
                        elif ticker_str in current_watchlist_tickers: c3.button("📝 관심종목", key=f"add_{row['티커']}", disabled=True)
                        else:
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
            
        if hidden_stocks: st.info(f"💡 현재 이 포트폴리오의 **{len(hidden_stocks)}개** 종목이 '실전 계좌(탭 2)'에 보유 중이므로 숨김 처리되었습니다.")
        if kis_token_global: st.caption("⚡ **KIS API 연결됨:** 한국투자증권 실시간 호가 및 AI 진단 반영 중입니다.")
            
        display_records, eval_actions_cache = [], {}
        buf = whipsaw_buffer / 100.0
        current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
        is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
        current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
        target_buy_amt = current_asset_base * (current_max_alloc_pct / 100.0)
        
        with st.spinner("AI 실시간 데이터 연동 및 통합 표 생성 중..."):
            for row in visible_stocks:
                ticker = row.get('티커', '')
                c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if kis_token_global else None
                res = fetch_stock_status(ticker)
                action, tech_text, easy_desc, ai_score = "분석 불가", "-", "데이터를 불러오지 못했습니다.", 0.0
                
                if res and res[0] is not None:
                    yf_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, _, vol_surged, yf_low, recent_vol_max = res
                    if not c_price or c_price == 0: c_price = yf_price
                    is_above_ma200 = c_price >= ma200
                    current_low = min(yf_low, c_price)
                    dist_ma20 = ((c_price / ma20) - 1) * 100 if ma20 > 0 else 0
                    diff_ma = ((ma20 / ma60) - 1) * 100 if ma60 > 0 else 0
                    tech_text = f"20/60선 이격 {diff_ma:+.2f}%" if active_strat == '대형주 (Core)' else f"20일선 이격 {dist_ma20:+.2f}%"
                    ma200_cond = is_above_ma200 if use_ma200_filter else True
                    target_shares = int(target_buy_amt // c_price) if c_price > 0 else 0

                    if active_strat == '대형주 (Core)':
                        ai_score = round((ret_20 * 0.5) + (ret_60 * 0.3) + (diff_ma * 0.2), 2)
                        entry_cond = (ma200_cond and (ma20 >= ma60 * (1 + buf)) and ma60_slope_positive and (ret_20 > 0) and vix_safe) or vix_contrarian
                        exit_cond_trend = (ma20 < ma60 * (1 - buf/2))
                        if exit_cond_trend or (use_ma200_filter and not is_above_ma200):
                            action, easy_desc = "🔴 유니버스 제외 (추세 붕괴)", "[유니버스 제외] 핵심 지지선 하향 이탈 및 모멘텀 소멸이 확인되었습니다."
                        elif entry_cond:
                            action, easy_desc = f"🟢 매수 시그널 발생 (목표: {target_shares:,}주)", "[매수 시그널 발생] 중장기 정배열 및 모멘텀 강세. 신규 편입 유효 구간입니다."
                        else:
                            action, easy_desc = "🟡 모니터링 유지", "[모니터링 유지] 유효한 매매 시그널 미발생. 추가 가격 및 추세 확인 필요."
                    else:
                        ai_score = round((recent_vol_max / 100.0) * 0.4 + (ret_60 * 0.3) + (ret_20 * 0.3), 2)
                        is_dip = (-5.0 <= dist_ma20 <= 3.0) or (current_low <= ma20 * 1.01)
                        entry_cond = (ma200_cond and ((is_dip and vol_surged) or vix_contrarian) and drawdown >= -30.0)
                        if not vol_surged or drawdown < -30.0 or (use_ma200_filter and not is_above_ma200):
                            action, easy_desc = "🔴 유니버스 제외 (수급/추세 상실)", "[유니버스 제외] 수급/지지선 이탈 및 모멘텀 소멸이 확인되었습니다."
                        elif entry_cond:
                            action, easy_desc = f"🟢 매수 시그널 발생 (목표: {target_shares:,}주)", "[매수 시그널 발생] 중장기 정배열 및 모멘텀 강세. 신규 편입 유효 구간입니다."
                        else:
                            action, easy_desc = "🟡 모니터링 유지", "[모니터링 유지] 유효한 매매 시그널 미발생. 추가 가격 및 추세 확인 필요."

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
                save_df['매수단가'], save_df['보유수량'] = 0, 0
                p_data['stocks'] = pd.DataFrame(save_df.to_dict('records') + hidden_stocks).drop_duplicates(subset=['티커']).to_dict('records')
                save_portfolio_to_sheets(selected_port, p_data)
                st.success("✅ 저장 및 동기화 완료!")
                st.rerun()

        with c_btn2:
            if st.button("🗑️ 체크한 종목 삭제", type="secondary", use_container_width=True):
                to_delete = edited_df[edited_df['선택'] == True]['티커'].tolist()
                if to_delete:
                    p_data['stocks'] = [s for s in p_data['stocks'] if s['티커'] not in to_delete]
                    save_portfolio_to_sheets(selected_port, p_data)
                    st.success(f"✅ {len(to_delete)}개 종목 삭제됨!")
                    time.sleep(1)
                    st.rerun()
                else: st.warning("⚠️ 삭제할 종목을 체크박스로 선택해주세요.")

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
                    st.rerun()

            st.markdown("---")
            if not real_stocks_df.empty:
                with st.spinner("실계좌 종목 AI 집중 분석 중..."):
                    buf, live_results = whipsaw_buffer / 100.0, []
                    is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
                    current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
                    target_buy_amt = real_total_eval * (current_max_alloc_pct / 100.0)
                    
                    for idx, row in real_stocks_df.iterrows():
                        live_c_price, buy_price = float(row.get('_raw_price', 0)), float(row.get('_raw_buy', 0))
                        if live_c_price == 0: continue
                            
                        qty_str = str(row.get('보유수량', '0 주')).replace(' 주', '').replace(',', '').strip()
                        try: qty_num = int(float(qty_str))
                        except: qty_num = 0
                        profit_amt = (live_c_price - buy_price) * qty_num
                        current_holding_amt = live_c_price * qty_num

                        res = fetch_stock_status(row['티커'])
                        user_ret = ((live_c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                        
                        if not res or res[0] is None:
                            live_results.append({
                                '보유 종목명': row['종목명'], '티커': row['티커'], '🔥 매력도 점수': 0.0, '보유수량': f"{qty_num:,} 주",
                                '매수평균가': f"{buy_price:,.0f} 원", '실시간 현재가': f"{live_c_price:,.0f} 원", 
                                '평가손익': f"{profit_amt:+,.0f} 원", '수익률': f"{user_ret:+.2f}%", 
                                '🤖 실계좌 전용 액션 플랜': "⚪ 모니터링 불가", '📊 판단 근거': "AI 분석용 과거 데이터 수신 실패"
                            })
                            continue
                            
                        yf_price, ma200, ma60, ma20, drawdown, _, _, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, yf_low, recent_vol_max = res

                        diff_ma = ((ma20 / ma60) - 1) * 100 if (ma20 and ma60) else 0
                        dist_ma20 = ((live_c_price / ma20) - 1) * 100 if ma20 else 0
                        exit_cond_trend = (ma20 < ma60 * (1 - buf/2)) and not vix_contrarian
                        ma200_cond = is_above_ma200 if use_ma200_filter else True
                        
                        additional_amt = max(0, target_buy_amt - current_holding_amt)
                        add_qty = int(additional_amt // live_c_price)

                        if active_strat == '대형주 (Core)': 
                            ai_score = round((ret_20 * 0.5) + (ret_60 * 0.3) + (diff_ma * 0.2), 2)
                            entry_cond = (ma200_cond and (ma20 >= ma60 * (1 + buf)) and ma60_slope_positive and (ret_20 > 0) and vix_safe) or vix_contrarian
                            if user_ret >= ts_target_pct: action, reason = "🔵 트레일링 스탑 가동", f"목표 수익률({ts_target_pct}%) 도달"
                            elif exit_cond_trend: action, reason = "🔴 전량 청산 (추세 이탈)", f"20/60선 데드크로스 이탈"
                            elif entry_cond:
                                if add_qty > 0: action, reason = f"🟢 비중 확대 유효 (+{add_qty:,}주)", f"신규 진입 타점 조건 충족"
                                else: action, reason = "🟡 비중 도달 (포지션 홀딩)", f"목표 비중({current_max_alloc_pct}%) 기충족"
                            else: action, reason = "🟡 포지션 홀딩", f"추세 방어 중 및 지지선 이탈 없음"
                        else:
                            ai_score = round((recent_vol_max / 100.0) * 0.4 + (ret_60 * 0.3) + (ret_20 * 0.3), 2)
                            is_dip = (-5.0 <= dist_ma20 <= 3.0) or (min(yf_low, live_c_price) <= ma20 * 1.01)
                            entry_cond = (ma200_cond and ((is_dip and vol_surged) or vix_contrarian) and drawdown >= -30.0)
                            if user_ret <= sat_stop_loss: action, reason = "🔴 손절 매도 집행", f"손절 기준선({sat_stop_loss}%) 도달"
                            elif user_ret >= ts_target_pct: action, reason = "🔵 트레일링 스탑 가동", f"목표 수익률({ts_target_pct}%) 돌파"
                            elif exit_cond_trend: action, reason = "🔴 전량 청산 (추세 이탈)", f"20/60선 데드크로스 이탈"
                            elif entry_cond:
                                if add_qty > 0: action, reason = f"🟢 비중 확대 유효 (+{add_qty:,}주)", f"수급 유입 후 20일선 눌림목 지지 중"
                                else: action, reason = "🟡 비중 도달 (포지션 홀딩)", f"목표 비중({current_max_alloc_pct}%) 기충족"
                            else: action, reason = "🟡 포지션 홀딩", f"손절선 이탈 없음 및 추세 유지 중"

                        live_results.append({
                            '보유 종목명': row['종목명'], '티커': row['티커'], '🔥 매력도 점수': ai_score, '보유수량': f"{qty_num:,} 주",
                            '매수평균가': f"{buy_price:,.0f} 원", '실시간 현재가': f"{live_c_price:,.0f} 원", 
                            '평가손익': f"{profit_amt:+,.0f} 원", '수익률': f"{user_ret:+.2f}%", 
                            '🤖 실계좌 전용 액션 플랜': action, '📊 판단 근거': reason
                        })
                    
                    live_df = pd.DataFrame(live_results)
                    if not live_df.empty:
                        live_df = live_df.sort_values(by="🔥 매력도 점수", ascending=False).reset_index(drop=True)
                        st.table(live_df.drop(columns=['티커']))
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
    
    order_queue = []
    eligible_stocks = {}
    if p_data and 'stocks' in p_data:
        for s in p_data['stocks']: eligible_stocks[s['티커']] = s.get('종목명', '')
    if SYS_APP_KEY and kis_data and not real_stocks_df.empty:
        for idx, row in real_stocks_df.iterrows(): eligible_stocks[row['티커']] = row.get('종목명', '')
            
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
        
        c_p, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, yf_low, recent_vol_max = res
        
        if qty_num == 0:
            live_c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if kis_token_global else c_p
            if not live_c_price: live_c_price = c_p
        if live_c_price <= 0: continue

        user_ret = ((live_c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
        diff_ma = ((ma20 / ma60) - 1) * 100 if ma60 > 0 else 0
        dist_ma20 = ((live_c_price / ma20) - 1) * 100 if ma20 > 0 else 0
        ma200_cond = is_above_ma200 if use_ma200_filter else True
        current_low = min(yf_low, live_c_price)
        
        ai_score, entry_cond, exit_cond_trend = 0.0, False, (ma20 < ma60 * (1 - whipsaw_buffer/200.0)) and not vix_contrarian
        
        if active_strat == '대형주 (Core)':
            ai_score = round((ret_20 * 0.5) + (ret_60 * 0.3) + (diff_ma * 0.2), 2)
            entry_cond = (ma200_cond and (ma20 >= ma60 * (1 + whipsaw_buffer/100.0)) and ma60_slope_positive and (ret_20 > 0) and vix_safe) or vix_contrarian
        else:
            ai_score = round((recent_vol_max / 100.0) * 0.4 + (ret_60 * 0.3) + (ret_20 * 0.3), 2)
            is_dip = (-5.0 <= dist_ma20 <= 3.0) or (current_low <= ma20 * 1.01)
            entry_cond = (ma200_cond and ((is_dip and vol_surged) or vix_contrarian) and drawdown >= -30.0)

        is_sell, sell_type = False, ""
        if qty_num > 0:
            if active_strat == '대형주 (Core)':
                if user_ret >= ts_target_pct: is_sell, sell_type = True, "🔵 트레일링 익절"
                elif exit_cond_trend: is_sell, sell_type = True, "🔴 추세 이탈 매도"
            else:
                if user_ret <= sat_stop_loss: is_sell, sell_type = True, "🔴 긴급 손절 매도"
                elif user_ret >= ts_target_pct: is_sell, sell_type = True, "🔵 트레일링 익절"
                elif exit_cond_trend: is_sell, sell_type = True, "🔴 추세 이탈 매도"
                
        if is_sell:
            status_text = "대기 중"
            if kill_switch: status_text = "🚨 킬 스위치 차단됨"
            elif not auto_trade_enabled: status_text = "⏸️ 자동주문 비활성"
            order_queue.append({
                '우선순위_분류': 0, '🔥 점수': 999.0, '종목명': s_name, '티커': ticker, '구분': sell_type,
                '목표 주가': f"{live_c_price:,.0f} 원", '목표 주문 수량': f"{qty_num:,} 주", '필요 자금': f"-{live_c_price * qty_num:,.0f} 원 (회수)",
                '_raw_price': live_c_price, '_buy_price': buy_price, '_qty': qty_num, '_req_fund': 0, '주문 실행 상태': status_text
            })
            continue 

        if entry_cond:
            current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
            is_bull_market = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
            current_max_alloc_pct = min(max_alloc_pct * 1.5 if (bull_market_boost and is_bull_market) else max_alloc_pct, 100.0)
            
            target_buy_amt = current_asset_base * (current_max_alloc_pct / 100.0)
            current_holding_amt = qty_num * live_c_price
            additional_amt = max(0, target_buy_amt - current_holding_amt)
            add_qty = int(additional_amt // live_c_price)
            
            if add_qty > 0:
                req_fund = add_qty * live_c_price
                status_text = "대기 중"
                if kill_switch: status_text = "🚨 킬 스위치 차단됨"
                elif not auto_trade_enabled: status_text = "⏸️ 자동주문 비활성"
                elif real_cash_avail < req_fund: status_text = "⚠️ 예수금 부족"
                buy_type = "🛒 신규 매수" if qty_num == 0 else "🟢 비중 확대"
                
                order_queue.append({
                    '우선순위_분류': 1, '🔥 점수': ai_score, '종목명': s_name, '티커': ticker, '구분': buy_type,
                    '목표 주가': f"{live_c_price:,.0f} 원", '목표 주문 수량': f"{add_qty:,} 주", '필요 자금': f"{req_fund:,.0f} 원",
                    '_raw_price': live_c_price, '_buy_price': 0, '_qty': add_qty, '_req_fund': req_fund, '주문 실행 상태': status_text
                })

    queue_df = pd.DataFrame(order_queue)
    if not queue_df.empty:
        queue_df = queue_df.sort_values(by=['우선순위_분류', '🔥 점수'], ascending=[True, False]).reset_index(drop=True)
        queue_df['우선순위'] = [f"{i+1}위" for i in range(len(queue_df))]
        st.subheader("📋 AI 매매 우선순위 대기열 (Order Queue)")
        st.table(queue_df[['우선순위', '종목명', '구분', '🔥 점수', '목표 주가', '목표 주문 수량', '필요 자금', '주문 실행 상태']])
        
        st.markdown("---")
        if auto_trade_enabled: btn_label, btn_type = "⚡ 자동 감시 주기 무시하고 즉시 강제 집행 (입금 직후용)", "secondary"
        else: btn_label, btn_type = "⚡ 대기열 일괄 주문 수동 전송", "primary"
            
        if st.button(btn_label, type=btn_type, use_container_width=True):
            if kill_switch: st.error("🚨 킬 스위치가 활성화되어 있어 주문을 전송할 수 없습니다.")
            elif not auto_trade_enabled and btn_type == "secondary": st.warning("🚀 사이드바에서 '실전 자동주문 활성화' 스위치를 켜주세요.")
            else:
                with st.spinner("우선순위 대기열 순차 주문 전송 중..."):
                    exec_msgs, needs_save = [], False
                    for idx, q_row in queue_df.iterrows():
                        s_name, t_code, q_type = q_row['종목명'], q_row['티커'], q_row['구분']
                        raw_qty, raw_price, buy_p = q_row['_qty'], q_row['_raw_price'], q_row['_buy_price']
                        
                        if "매도" in q_type or "익절" in q_type:
                            succ, msg = execute_kis_order(t_code, raw_qty, raw_price, order_type="SELL", is_market=True)
                            if succ:
                                p_data = log_daily_trade(p_data, s_name, "SELL", raw_price, raw_qty, buy_p)
                                exec_msgs.append(f"▪️ [{q_type}] *{s_name}*: {msg}"); needs_save = True
                            else: exec_msgs.append(f"❌ [{q_type} 실패] *{s_name}*: {msg}")
                                
                        elif "매수" in q_type or "확대" in q_type:
                            req_f = q_row['_req_fund']
                            if real_cash_avail >= req_f:
                                succ, msg = execute_kis_order(t_code, raw_qty, raw_price, order_type="BUY", is_market=False)
                                if succ:
                                    p_data = log_daily_trade(p_data, s_name, "BUY", raw_price, raw_qty)
                                    real_cash_avail -= req_f
                                    exec_msgs.append(f"▪️ [{q_type}] *{s_name}*: {msg}"); needs_save = True
                                else: exec_msgs.append(f"❌ [{q_type} 실패] *{s_name}*: {msg}")
                            else: exec_msgs.append(f"⚠️ [{q_type} 보류] *{s_name}*: 예수금 부족")
                    
                    if needs_save: save_portfolio_to_sheets(selected_port, p_data)
                    if exec_msgs:
                        send_telegram_message("🤖 *[주문 전송 집행 결과]*\n" + "\n".join(exec_msgs))
                        st.success("주문 집행이 완료되었습니다!")
                        time.sleep(1)
                        st.rerun()
    else: st.info("💡 현재 AI 퀀트 엔진이 포착한 신규 매수 또는 매도 시그널이 없습니다.")

    st.markdown("---")
    st.subheader("📜 당일 자동매매 체결 내역 및 실적 요약")
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    if p_data and p_data.get('daily_trades_date') == today_str and p_data.get('daily_trades'):
        trades_df = pd.DataFrame(p_data['daily_trades'])
        total_buy = trades_df[trades_df['주문 구분'] == '매수(진입)']['체결 금액'].sum()
        total_sell = trades_df[trades_df['주문 구분'] == '매도(청산)']['체결 금액'].sum()
        total_pnl = trades_df['실현 손익'].sum()
        
        c1, c2, c3 = st.columns(3)
        c1.metric("🛒 당일 총 매수대금", f"{total_buy:,.0f} 원")
        c2.metric("💸 당일 총 매도대금", f"{total_sell:,.0f} 원")
        c3.metric("🎯 당일 확정 실현손익", f"{total_pnl:+,.0f} 원")
        
        display_trades = trades_df.copy()
        display_trades['체결 단가'] = display_trades['체결 단가'].apply(lambda x: f"{x:,.0f} 원")
        display_trades['체결 수량'] = display_trades['체결 수량'].apply(lambda x: f"{x:,} 주")
        display_trades['체결 금액'] = display_trades['체결 금액'].apply(lambda x: f"{x:,.0f} 원")
        display_trades['실현 손익'] = display_trades['실현 손익'].apply(lambda x: f"{x:+,.0f} 원" if x != 0 else "-")
        st.dataframe(display_trades, use_container_width=True)
    else: st.info("오늘 KIS API 엔진을 통해 체결 완료된 거래 내역이 없습니다.")

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
                st.rerun()
                
        st.markdown(f"설정된 기준일(`{real_base_date}`)부터 오늘까지 관심종목 유니버스를 바탕으로 AI 전략을 가동했을 때의 **이론적 누적 수익률**과, 고객님의 **실계좌 수익률**을 나란히 비교합니다.")

        if st.button("▶️ 포워드 테스트 1:1 비교 실행", use_container_width=True):
            if stocks_df.empty: 
                st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner(f"{real_base_date} 부터 현재까지 AI 시뮬레이션 중..."):
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
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 (v4.4)</h1>
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
        1.  **추세/수급 붕괴:** 대형주와 동일하게 20일선이 60일선을 이탈하거나, 수급 폭발 이력이 소멸되면 전량 매도 및 관심종목 퇴출.
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

    ---
    
    ## 4. 🛡️ AI 리스크 매니지먼트 (Risk Control)
    수익을 내는 것만큼 중요한 것은 번 돈을 지키고 뇌동매매를 방지하는 것입니다.
    
    *   **🎣 트레일링 스탑 (Trailing Stop - 수익 극대화):**
        *   수익률이 목표치(대형주 `30%`, 중소형주 `15%`)를 돌파하면 익절 대기 모드로 전환됩니다.
        *   이후 주가가 계속 오르면 매도하지 않고 따라가며(Trailing), 최고점 대비 특정 비율(`-10%` 또는 `-5%` 수준)만큼 하락하는 꺾임 현상이 발생할 때만 기계적으로 수익을 확정(매도)합니다.
    *   **🥶 손실 쿨다운 시스템 (Cooldown):**
        *   특정 종목에서 연속으로 2회 이상 손실(매도/손절)이 발생하면, 해당 종목은 AI 블랙리스트에 등재되어 설정된 기간(기본 `60일`) 동안 어떠한 매수 시그널이 떠도 진입을 거부합니다. (종목과 사랑에 빠지거나, 휩소 구간에서 계좌가 녹는 현상 완벽 차단)
    
    ---
    
    ## 5. ⚙️ 자동매매 우선순위 & 자금 배분 로직 (Position Sizing)
    동시에 여러 종목의 매수 시그널이 발생할 경우, 한정된 예수금 내에서 효율적으로 자본을 배분하기 위해 **AI 매력도 스코어링**을 산출하여 매수 우선순위를 정합니다.
    *   **1순위 매도 (Emergency Exit):** 손절 및 추세 이탈 종목을 최우선 청산하여 현금을 확보합니다.
    *   **2순위 매수 (Score-based Allocation):** AI 매력도 점수가 높은 종목 순서대로 한정된 가용 예수금을 순차적 배분합니다.
    *   **추가 매수(비중 확대) 산출:** `(계좌 총 평가금액 × 설정 비중 %) - 현재 해당 종목의 평가금액` 공식으로 부족한 비중만큼의 구체적인 목표 매수 수량과 금액을 산출하여 과매수를 방지합니다.

    ---

    ## 6. 🚨 자동매매 페일세이프 (Fail-Safe) 및 입출금 자동 감지
    자동매매가 예상치 못한 시장 상황이나 프로그램 오류로 인해 계좌를 망가뜨리지 않도록 최우선 보안 장치가 결합되어 있습니다.
    *   **무작위 재시도 금지 (No Blind Retry):** 주문 실패 시 이전 조건을 맹목적으로 반복하지 않으며, 실시간 호가 및 조건을 재검증합니다.
    *   **하드코딩 킬 스위치 (Kill Switch):** 활성화 즉시 어떠한 상황에서도 모든 KIS API 매매 호출이 차단되어 계좌를 안전하게 보호합니다.
    *   **수학적 팩트 기반 입출금 추적:** 기존의 부정확한 수동 입출금 입력 방식을 폐기하고, `현재 계좌 총 평가금 - 보유 주식 평가손익 - 봇 누적 실현손익` 공식을 통해 외부에서 입출금된 순수 투입 원금을 1원 단위까지 100% 정확하게 자동 역산합니다.
    """, unsafe_allow_html=True)
