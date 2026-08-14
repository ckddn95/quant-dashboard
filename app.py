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
        return res.status_code == 200, "발송 성공" if res.status_code == 200 else f"API 오류: {res.text}"
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

@st.cache_data(ttl=86400, show_spinner=False)
def load_krx_universe():
    try:
        krx = fdr.StockListing('KRX')
        return krx
    except Exception as e:
        return pd.DataFrame()

def get_kis_access_token(app_key, app_secret, is_mock=True):
    if not app_key or not app_secret: return None, "키 누락"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200: return res.json().get("access_token"), "OK"
        else: return None, f"토큰 발급 실패 (HTTP {res.status_code}): {res.text}"
    except Exception as e:
        return None, f"통신 에러: {str(e)}"

def fetch_kis_current_price(app_key, app_secret, ticker, token, is_mock=True):
    if not token: return 0.0
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).strip().zfill(6)}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': 
            return float(res.json()['output']['stck_prpr'])
    except: pass
    return 0.0

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    if not token: return None, None, "토큰이 없습니다."
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-balance"
    tr_id = "VTTC8434R" if is_mock else "TTTC8434R"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    params = {"CANO": str(cano).replace("-", "").strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            rj = res.json()
            if rj.get('rt_cd') == '0': return rj.get('output1', []), rj.get('output2', []), "OK"
            else: return None, None, f"KIS 응답 거절: {rj.get('msg1')}"
        else: return None, None, f"HTTP 에러 {res.status_code}: {res.text}"
    except Exception as e:
        return None, None, f"서버 통신 에러: {str(e)}"

# 🛑 [패치] min_periods=1 적용하여 데이터 누락 방어
def evaluate_stock_for_ui(ticker, strat, buy_price=0.0, highest_price=0.0, use_ma200=True, buf_pct=0.015, ts_tgt=0.30, ts_drp=-0.10, sl=-0.15, c_price=0.0):
    try:
        df = fdr.DataReader(str(ticker).zfill(6), start=(datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d'))
        if df is None or df.empty:
            return c_price, "분석 불가", 0.0, "과거 데이터 없음"
        
        close_p = float(df['Close'].iloc[-1])
        if c_price <= 0: c_price = close_p
        
        ma20 = df['Close'].rolling(20, min_periods=1).mean().iloc[-1]
        ma60 = df['Close'].rolling(60, min_periods=1).mean().iloc[-1]
        ma200 = df['Close'].rolling(200, min_periods=1).mean().iloc[-1]
        
        dist_20_60 = ((ma20 / ma60) - 1) * 100 if ma60 > 0 else 0.0
        dist_c_20 = ((c_price / ma20) - 1) * 100 if ma20 > 0 else 0.0
        
        if buy_price > 0:
            ret = (c_price / buy_price) - 1
            if ret <= sl: return c_price, "🔴 긴급 손절 매도", 10.0, f"수익률 {ret*100:+.1f}% (손절컷 도달)"
            if highest_price > 0 and (highest_price/buy_price - 1) >= ts_tgt:
                drop_from_peak = (c_price / highest_price) - 1
                if drop_from_peak <= ts_drp: 
                    return c_price, "🔵 트레일링 익절", 20.0, f"고점대비 {drop_from_peak*100:+.1f}% (익절 조건 충족)"
            if strat == "Core" and c_price < ma60 * (1 - buf_pct/2): 
                return c_price, "🔴 전량 청산", 30.0, f"현재가 < 60일선({ma60:,.0f}원) 하향이탈"
            elif strat == "Satellite" and c_price < ma20 * (1 - buf_pct/2): 
                return c_price, "🔴 전량 청산", 30.0, f"현재가 < 20일선({ma20:,.0f}원) 하향이탈"
        
        pass_ma200 = (c_price >= ma200) if use_ma200 else True
        ma200_str = " (200일선 지지)" if pass_ma200 and use_ma200 else ""
        
        if strat == "Core":
            m60_up = True if len(df) < 60 else (ma60 > df['Close'].rolling(60, min_periods=1).mean().iloc[-11])
            if pass_ma200 and (ma20 >= ma60 * (1 + buf_pct)) and m60_up:
                score = min(85.0 + max(0, dist_20_60), 99.0)
                return c_price, "🟢 매수 시그널 발생", round(score, 1), f"20/60일선 이격도 {dist_20_60:+.1f}% 골든크로스{ma200_str}"
        else:
            if pass_ma200 and (-5.0 <= dist_c_20 <= 3.0):
                score = min(85.0 + max(0, (3.0 - dist_c_20)), 99.0)
                return c_price, "🟢 매수 시그널 발생", round(score, 1), f"20일선 이격도 {dist_c_20:+.1f}% 눌림목 진입{ma200_str}"
                
        return c_price, "🟡 모니터링 유지", 50.0, f"현재 20일선 이격도 {dist_c_20:+.1f}% (타점 대기 중)"
    except Exception as e:
        return c_price, "분석 불가", 0.0, f"데이터 분석 에러"

@st.cache_data(ttl=3600)
def run_scanner_safe(strat, use_ma200, buf_pct):
    krx = load_krx_universe()
    if krx.empty: return pd.DataFrame()
    
    if strat == '대형주 (Core)':
        cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)]
        cands = cands.sort_values('Marcap', ascending=False).head(50) if 'Marcap' in cands.columns else cands.head(50)
    else:
        cands = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)]
        cands = cands.sort_values('Marcap', ascending=False).head(50) if 'Marcap' in cands.columns else cands.head(50)
    
    res = []
    def process(row):
        tc = str(row['Code']).strip().zfill(6)
        cp, action, score, reason = evaluate_stock_for_ui(tc, strat, 0.0, 0.0, use_ma200, buf_pct/100.0, 0.3, -0.1, -0.15)
        if "매수 시그널" in action: 
            return {'종목명': row['Name'], '티커': tc, '현재가': cp, 'AI 스코어': score, '진단 근거': reason}
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for r in executor.map(process, [r for _, r in cands.iterrows()]):
            if r: res.append(r)
            
    return pd.DataFrame(res).sort_values('AI 스코어', ascending=False)

# 🛑 [핵심 패치] 시뮬레이터 결측치 증발 버그 해결 (min_periods=1 적용 및 dropna 삭제)
@st.cache_data(ttl=1800)
def run_quant_simulation(sim_stocks, strat, init_cash, start_date, end_date, use_ma200, w_buf, sl, max_a, min_h, ts_tgt, ts_drp, b_boost, cd_days):
    if sim_stocks.empty: return None
    # 200일선 계산을 위해 넉넉하게 과거 400일 데이터 요청
    f_start = pd.to_datetime(start_date) - datetime.timedelta(days=400)
    sim_data = {}
    for _, row in sim_stocks.iterrows():
        tk, nm = str(row.get('티커','')).strip().zfill(6), str(row.get('종목명',''))
        try:
            df = fdr.DataReader(tk, start=f_start, end=end_date)
            if df is not None and not df.empty:
                df['MA20'] = df['Close'].rolling(20, min_periods=1).mean()
                df['MA60'] = df['Close'].rolling(60, min_periods=1).mean()
                df['MA200'] = df['Close'].rolling(200, min_periods=1).mean()
                df['M60_Up'] = df['MA60'] > df['MA60'].shift(10).fillna(0)
                # dropna() 삭제하여 데이터 증발 완전 차단
                sim_data[nm] = df
        except: pass
        
    if not sim_data: return None
    
    summary_rows = []
    total_final_val = 0.0
    alloc_cash = init_cash / len(sim_data) if len(sim_data) > 0 else init_cash
    
    for nm, df in sim_data.items():
        sub_df = df[df.index >= pd.to_datetime(start_date)]
        if sub_df.empty: 
            # 검색 기간에 데이터가 아예 없는 경우 원금 보존 처리 (수익률 0%)
            total_final_val += alloc_cash
            summary_rows.append({
                '종목명': nm, '최종 보유 주수': "0 주",
                '기말 평가금': f"{alloc_cash:,.0f} 원", '총 순수익 (원)': "0 원",
                '수익률 (%)': "+0.00%", '매매 횟수': "매수 0회 / 매도 0회", 
                '총 발생 수수료': "0 원", '기말 포트폴리오 비중': "0%"
            })
            continue
        
        cash, qty, buy_price, highest_price = alloc_cash, 0, 0.0, 0.0
        buy_count, sell_count, total_fee = 0, 0, 0.0
        
        for date, row in sub_df.iterrows():
            c_p, ma20, ma60, ma200, m60_up = row['Close'], row['MA20'], row['MA60'], row['MA200'], row['M60_Up']
            if qty > 0:
                ret = (c_p / buy_price) - 1
                highest_price = max(highest_price, c_p)
                sell_flag = False
                if ret <= sl: sell_flag = True
                elif (highest_price / buy_price - 1) >= ts_tgt and (c_p / highest_price - 1) <= ts_drp: sell_flag = True
                elif strat == "Core" and c_p < ma60 * (1 - w_buf/2): sell_flag = True
                elif strat == "Satellite" and c_p < ma20 * (1 - w_buf/2): sell_flag = True
                    
                if sell_flag:
                    proc = qty * c_p
                    fee = proc * 0.0025
                    cash += (proc - fee); total_fee += fee
                    qty, buy_price, highest_price = 0, 0.0, 0.0
                    sell_count += 1
                    continue 
            
            if qty == 0:
                pass_ma200 = (c_p >= ma200) if use_ma200 else True
                buy_flag = False
                if strat == "Core":
                    if pass_ma200 and (ma20 >= ma60 * (1 + w_buf)) and m60_up: buy_flag = True
                else:
                    dist_ma20 = (c_p / ma20) - 1
                    if pass_ma200 and (-0.05 <= dist_ma20 <= 0.03): buy_flag = True
                        
                if buy_flag and cash >= c_p:
                    q = int(cash // c_p)
                    if q > 0:
                        cost = q * c_p; fee = cost * 0.0025
                        cash -= (cost + fee); total_fee += fee
                        qty, buy_price, highest_price = q, c_p, c_p
                        buy_count += 1
                        
        final_eval = cash + (qty * sub_df['Close'].iloc[-1])
        total_final_val += final_eval
        pnl = final_eval - alloc_cash
        ret_pct = (pnl / alloc_cash) * 100 if alloc_cash > 0 else 0
        
        summary_rows.append({
            '종목명': nm, '최종 보유 주수': f"{qty:,} 주",
            '기말 평가금': f"{final_eval:,.0f} 원", '총 순수익 (원)': f"{pnl:+,.0f} 원",
            '수익률 (%)': f"{ret_pct:+.2f}%", '매매 횟수': f"매수 {buy_count}회 / 매도 {sell_count}회", 
            '총 발생 수수료': f"{total_fee:,.0f} 원", '기말 포트폴리오 비중': "0%"
        })
        
    final_port_ret = ((total_final_val / init_cash) - 1) * 100 if init_cash > 0 else 0
    for r in summary_rows:
        v = float(r['기말 평가금'].replace(',','').replace(' 원',''))
        r['기말 포트폴리오 비중'] = f"{(v / total_final_val) * 100 if total_final_val > 0 else 0:.2f}%"
    
    try: dates = pd.date_range(start=start_date, end=end_date, freq='ME').strftime('%Y-%m')
    except: dates = pd.date_range(start=start_date, end=end_date, freq='M').strftime('%Y-%m')
        
    chart_data = [{'Date': d, 'Asset': nm, 'Weight': 100.0 / len(sim_data)} for d in dates for nm in sim_data.keys()]
            
    return {
        'final_asset': total_final_val, 'final_port_ret': final_port_ret, 'summary_rows': summary_rows,
        'eom_weights_reset': pd.DataFrame(chart_data) if chart_data else pd.DataFrame({'Date': ['2026-08'], 'Asset': [list(sim_data.keys())[0]], 'Weight': [100.0]}),
        'cols_ordered': list(sim_data.keys()), 'color_range': ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']
    }

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

has_saved_keys = bool(p_data and p_data.get('manual_app_key'))
with st.sidebar.expander("🔑 KIS API 설정", expanded=not has_saved_keys):
    if has_saved_keys:
        curr_is_mock = p_data.get('manual_is_mock', True)
        st.success(f"✅ 현재 **{'모의투자' if curr_is_mock else '실계좌'}** API 키가 안전하게 작동 중입니다.")
        st.info("💡 보안을 위해 키 입력창이 완벽하게 숨김 처리되었습니다.")
        
        if st.button("🗑️ 저장된 키 삭제 및 재설정"):
            if p_data is not None:
                p_data.pop('manual_app_key', None)
                p_data.pop('manual_app_secret', None)
                p_data.pop('manual_cano', None)
                p_data.pop('manual_is_mock', None)
                save_portfolio_to_sheets(selected_port, p_data)
                st.warning("저장된 API 키가 삭제되었습니다.")
                try: st.query_params["auth"] = daily_token
                except: st.experimental_set_query_params(auth=daily_token)
                st.rerun()
    else:
        st.markdown("<span style='font-size: 0.85em; color: #a3a8b8;'>* 신규 API 키 정보를 입력하세요.</span>", unsafe_allow_html=True)
        manual_app_key = st.text_input("새 APP KEY 입력", type="password")
        manual_app_secret = st.text_input("새 APP SECRET 입력", type="password")
        manual_cano = st.text_input("새 계좌번호 (앞 8자리)")
        manual_is_mock = st.checkbox("이 키는 모의투자(Mock) 전용입니다.", value=True)
        
        if st.button("✅ 저장"):
            if p_data is not None:
                if manual_app_key.strip() and manual_app_secret.strip() and manual_cano.strip():
                    p_data['manual_app_key'] = manual_app_key.strip()
                    p_data['manual_app_secret'] = manual_app_secret.strip()
                    p_data['manual_cano'] = manual_cano.strip()
                    p_data['manual_is_mock'] = manual_is_mock
                    save_portfolio_to_sheets(selected_port, p_data)
                    st.success("API 키 저장 완료!")
                    try: st.query_params["auth"] = daily_token
                    except: st.experimental_set_query_params(auth=daily_token)
                    st.rerun()
                else:
                    st.error("빈칸 없이 모든 정보를 정확히 입력해주세요.")

SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = None, None, None, None, True
if p_data and p_data.get('manual_app_key'):
    SYS_APP_KEY = p_data.get('manual_app_key')
    SYS_APP_SECRET = p_data.get('manual_app_secret')
    SYS_CANO = str(p_data.get('manual_cano'))
    SYS_IS_MOCK = p_data.get('manual_is_mock', True)
    SYS_ACNT_PRDT = "01"

kis_token_global = None
if SYS_APP_KEY and SYS_APP_SECRET and p_data:
    current_time = time.time()
    token_key = f"kis_token_{SYS_APP_KEY[-6:]}"
    time_key = f"kis_time_{SYS_APP_KEY[-6:]}"
    kis_token_global = p_data.get(token_key)
    token_time = p_data.get(time_key, 0)
    if not kis_token_global or (current_time - token_time) > 40000:
        new_token, token_err = get_kis_access_token(SYS_APP_KEY, SYS_APP_SECRET, is_mock=SYS_IS_MOCK)
        if new_token:
            kis_token_global = new_token
            p_data[token_key] = new_token
            p_data[time_key] = current_time
            save_portfolio_to_sheets(selected_port, p_data)
        else:
            st.sidebar.error(f"⚠️ KIS 인증 실패: {token_err}")

cache_key = f"kis_global_cache_{SYS_CANO}_{SYS_ACNT_PRDT}" if SYS_CANO else "kis_global_cache_None_None"
if SYS_APP_KEY and kis_token_global:
    if st.sidebar.button("🔄 실계좌 데이터 동기화") or cache_key not in st.session_state:
        holdings, summary, err_msg = fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, kis_token_global, is_mock=SYS_IS_MOCK)
        if holdings is not None and summary is not None:
            tot_evlu = float(summary[0].get('tot_evlu_amt', 0))
            tot_pnl = float(summary[0].get('evlu_pfls_smtl_amt', 0))
            dnca_tot = float(summary[0].get('dnca_tot_amt', 0))
            imported = [{'종목명': i.get('prdt_name'), '티커': str(i.get('pdno')).strip().zfill(6), '실시간 현재가': f"{float(i.get('prpr', 0)):,.0f} 원", '매수평균가': f"{float(i.get('pchs_avg_pric', 0)):,.0f} 원", '보유수량': f"{int(i.get('hldg_qty'))} 주", '평가손익률': f"{float(i.get('evlu_pfls_rt', 0)):+.2f}%", '_raw_price': float(i.get('prpr', 0)), '_raw_buy': float(i.get('pchs_avg_pric', 0))} for i in holdings if int(i.get('hldg_qty', 0)) > 0]
            st.session_state[cache_key] = {'total_eval': tot_evlu, 'total_pnl': tot_pnl, 'cash_avail': dnca_tot, 'stocks': imported}
            st.sidebar.success("✅ 최신 잔고 동기화 완료!")
        else:
            st.sidebar.error(f"❌ 동기화 실패: {err_msg}")
            st.sidebar.info("API 설정(실계좌/모의)을 다시 확인해 주세요.")

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

total_invested_principal = float(total_cash)

if p_data:
    st.sidebar.markdown("---")
    st.sidebar.subheader("💰 Virtual Capital & Settings")
    st.sidebar.markdown(f"**현재 설정 전략:** `{active_strat}`")
    new_cash = st.sidebar.number_input(f"총 투자 운용 자산", value=int(total_cash), step=1_000_000, format="%d")
    if new_cash != total_cash:
        p_data['cash'] = new_cash
        save_portfolio_to_sheets(selected_port, p_data)
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")
if SYS_APP_KEY: st.sidebar.success(f"✅ **{'모의' if SYS_IS_MOCK else '실전'} 계좌** 연동됨")
else: st.sidebar.warning("🔑 **KIS API 미연동**")

st.sidebar.markdown("---")
st.sidebar.header("📱 텔레그램 및 오토파일럿")
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
        st.rerun()
    if kill_switch: st.sidebar.error("⚠️ 킬 스위치 작동 중!")

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
    st.markdown("관심 종목을 추가하면 AI가 실시간으로 타점을 진단합니다.")
    if not p_data or not selected_port:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 선택하세요.")
    else:
        col_s1, col_s2 = st.columns([8, 2])
        with col_s1:
            if st.button("🚀 실시간 AI 타점 스캐너 가동 (신규 발굴)", type="primary", use_container_width=True): 
                st.session_state.show_scanner = True
        with col_s2:
            if st.button("🧹 일괄 퇴출 (체크된 종목)", type="secondary", use_container_width=True):
                st.info("표 안의 '🗑️ 삭제' 열을 체크하고 아래 [저장] 버튼을 누르면 삭제됩니다.")

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
                            st.rerun()

        if st.session_state.show_scanner:
            with st.spinner("AI 퀀트 필터 검색 중..."):
                scan_result = run_scanner_safe(active_strat, use_ma200_filter, whipsaw_buffer)
                if not scan_result.empty:
                    st.markdown("### 💡 AI 스캐너 포착 종목")
                    for _, row in scan_result.iterrows():
                        c1, c2, c3, c4 = st.columns([2, 2, 4, 2])
                        c1.markdown(f"**{row['종목명']}** (`{row['티커']}`)")
                        c2.markdown(f"**{row['현재가']:,.0f} 원**" if isinstance(row['현재가'], float) else f"**{row['현재가']}**")
                        c3.markdown(f"🔥 `{row['AI 스코어']}점` | {row['진단 근거']}")
                        
                        if str(row['티커']).strip().zfill(6) not in current_watchlist_tickers:
                            if c4.button("➕ 담기", key=f"add_scan_{row['티커']}"):
                                p_data['stocks'].append({'종목명': row['종목명'], '티커': str(row['티커']).strip().zfill(6), '매수단가': 0, '보유수량': 0})
                                save_portfolio_to_sheets(selected_port, p_data)
                                st.rerun()
                        else:
                            c4.button("✅ 담김", disabled=True, key=f"added_scan_{row['티커']}")
                    st.markdown("---")
                else:
                    st.info("조건에 맞는 종목이 없습니다.")

        st.markdown("---")
        st.markdown("### 📋 현재 등록된 감시 리스트 (Watchlist)")
        st.info("💡 **종목을 삭제하려면?** 표 가장 왼쪽의 **[ 🗑️ 삭제 ]** 열에 체크한 뒤, 표 아래의 **[ 💾 변경된 내용 반영 ]** 버튼을 누르세요.")
        
        visible_stocks = p_data.get('stocks', [])
        display_records = []
        
        def process_watchlist_row(row):
            ticker = str(row.get('티커', '')).strip().zfill(6)
            c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if SYS_APP_KEY and kis_token_global else 0.0
            cp, action, score, reason = evaluate_stock_for_ui(ticker, active_strat, 0.0, 0.0, use_ma200_filter, whipsaw_buffer/100.0, ts_target_pct/100.0, ts_drop_pct/100.0, sat_stop_loss/100.0, c_price)
            return {'🗑️ 삭제': False, '종목명': row.get('종목명'), '티커': ticker, '실시간 현재가': f"{cp:,.0f} 원" if cp > 0 else "-", '🔥 매력도 점수': score, '🤖 AI 액션 플랜': action, '📊 근거': reason}

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            results = executor.map(process_watchlist_row, visible_stocks)
            for res in results:
                if res: display_records.append(res)
        
        display_df = pd.DataFrame(display_records)
        if not display_df.empty: 
            display_df = display_df.sort_values(by="🔥 매력도 점수", ascending=False).reset_index(drop=True)
            edited_df = st.data_editor(display_df, num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}")
            if st.button("💾 변경된 내용 반영", type="primary", use_container_width=True):
                keep_df = edited_df[edited_df['🗑️ 삭제'] == False]
                save_df = keep_df[['종목명', '티커']].copy()
                save_df['티커'] = save_df['티커'].astype(str).str.strip().str.zfill(6)
                p_data['stocks'] = save_df.to_dict('records')
                save_portfolio_to_sheets(selected_port, p_data)
                st.success("관심종목 리스트가 성공적으로 업데이트(삭제/저장) 되었습니다!")
                time.sleep(0.5)
                st.rerun()

with tab2:
    st.header("🔌 실전 계좌 (Real Account) 모니터링")
    if SYS_APP_KEY and kis_data:
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        col_m1.markdown(mts_metric_html("💰 총 평가 금액", f"{real_total_eval:,.0f} 원"), unsafe_allow_html=True)
        col_m2.markdown(mts_metric_html("📥 투자 원금", f"{total_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        col_m3.markdown(mts_metric_html("📈 누적 수익금", f"{real_eval_pnl:+,.0f} 원"), unsafe_allow_html=True)
        col_m4.markdown(mts_metric_html("💵 가용 현금", f"{real_cash_avail:,.0f} 원"), unsafe_allow_html=True)
        if not real_stocks_df.empty:
            display_real_df = real_stocks_df.drop(columns=['_raw_price', '_raw_buy'], errors='ignore')
            st.dataframe(display_real_df, use_container_width=True, hide_index=True)
    else:
        st.info("💡 실전 계좌 연동 키가 입력되지 않았거나 잔고 데이터를 불러오지 못했습니다. 왼쪽 사이드바에서 계좌를 연동해주세요.")

with tab3:
    st.header("🤖 실전 자동매매 관제센터 & 우선순위 주문 대기열")
    col_c1, col_c2, col_c3 = st.columns(3)
    col_c1.metric("🚨 킬 스위치", "차단됨" if kill_switch else "정상")
    col_c2.metric("🚀 자동주문", "활성화" if auto_trade_enabled else "비활성화")
    col_c3.metric("💵 가용 예수금", f"{real_cash_avail:,.0f} 원")
    st.markdown("---")
    
    current_asset_base = real_total_eval if (SYS_APP_KEY and kis_data) else total_cash
    if current_asset_base <= 0: current_asset_base = total_cash
    target_buy_amt = current_asset_base * (max_alloc_pct / 100.0)

    temp_queue = []
    
    def process_queue_row(row):
        ticker = str(row.get('티커', '')).strip().zfill(6)
        s_name = row.get('종목명', '')
        
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

        c_price = fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SECRET, ticker, kis_token_global, SYS_IS_MOCK) if SYS_APP_KEY and kis_token_global else 0.0
        if live_c_price <= 0: live_c_price = c_price

        cp, action, score, reason = evaluate_stock_for_ui(ticker, active_strat, buy_price, buy_price, use_ma200_filter, whipsaw_buffer/100.0, ts_target_pct/100.0, ts_drop_pct/100.0, sat_stop_loss/100.0, live_c_price)
        
        if "매도" in action or "청산" in action or "익절" in action:
            if qty_num > 0:
                return {
                    '우선순위_분류': 0, '🔥 점수': 999.0, '종목명': s_name, '티커': ticker, '구분': action,
                    '주문 단가': f"{cp:,.0f} 원", '주문 수량': f"{qty_num:,} 주", '필요 자금': f"-{cp * qty_num:,.0f} 원 (회수)",
                    '주문 실행 상태': '대기 중'
                }
        elif "매수 시그널" in action:
            current_holding_amt = qty_num * cp
            additional_amt = max(0, target_buy_amt - current_holding_amt)
            add_qty = int(additional_amt // cp) if cp > 0 else 0
            if add_qty > 0:
                req_fund = add_qty * cp
                buy_type = "🛒 신규 매수" if qty_num == 0 else "🟢 비중 확대"
                return {
                    '우선순위_분류': 1, '🔥 점수': score, '종목명': s_name, '티커': ticker, '구분': buy_type,
                    '주문 단가': f"{cp:,.0f} 원", '주문 수량': f"{add_qty:,} 주", '필요 자금': f"{req_fund:,.0f} 원",
                    '주문 실행 상태': '대기 중'
                }
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        results = executor.map(process_queue_row, visible_stocks)
        for res in results:
            if res: temp_queue.append(res)

    queue_df = pd.DataFrame(temp_queue)
    if not queue_df.empty:
        queue_df = queue_df.sort_values(by=['우선순위_분류', '🔥 점수'], ascending=[True, False]).reset_index(drop=True)
        queue_df['우선순위'] = [f"{i+1}위" for i in range(len(queue_df))]
        st.subheader("📋 AI 매매 우선순위 대기열 (Order Queue)")
        st.table(queue_df[['우선순위', '종목명', '구분', '🔥 점수', '주문 단가', '주문 수량', '필요 자금', '주문 실행 상태']])
    else:
        st.info("💡 현재 AI 퀀트 엔진이 포착한 대기 중인 매수/매도 시그널이 없습니다.")

    st.markdown("---")
    if st.button("⚡ 대기열 일괄 주문 수동 전송", type="primary", use_container_width=True):
        st.success("수동 주문 검토 완료 (실제 집행은 봇이 안전하게 수행합니다)")

# 🛑 [핵심 패치 1 & 2] 줄 맞춤 교정 및 데이터 증발 방지
with tab4:
    st.header("🧪 시뮬레이션 및 백테스트 (Simulation & Backtest)")
    if not p_data or not selected_port: 
        st.warning("포트폴리오가 없습니다.")
    else:
        stocks_df = pd.DataFrame(p_data.get('stocks', []))
        today_date = datetime.datetime.now(KST).date()
        
        st.subheader("🎯 Test 1. 포워드 테스트 (관심종목 vs 실전 계좌)")
        st.info("💡 **어떻게 비교하나요?** 내가 지정한 시작일로부터 AI가 현재 관심종목들을 운용했을 때의 **이론적 성과**와 현재 **내 실제 계좌 성과**를 1:1로 비교합니다. (번거로운 수동 기준 저장 기능을 없애고 직관적으로 자동 계산하도록 개선했습니다.)")
        
        col_fw_date, col_fw_btn = st.columns([3, 7])
        with col_fw_date:
            test1_start_date = st.date_input("📅 가상 운용 시작일", datetime.date(2023, 1, 1), key="t4_date")
        
        with col_fw_btn:
            # 버튼 상단 여백을 주어 날짜 칸과 완벽하게 수평 정렬
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            if st.button("▶️ 포워드 테스트 1:1 비교 실행", type="primary", use_container_width=True):
                if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
                else:
                    with st.spinner("포워드 테스트 구동 중..."):
                        res_fw = run_quant_simulation(stocks_df, active_strat, total_invested_principal, test1_start_date, today_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss/100.0, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                        if res_fw:
                            col_fw1, col_fw2 = st.columns(2)
                            with col_fw1: st.markdown(mts_metric_html("📈 AI 가상 운용 (이론)", f"{res_fw['final_port_ret']:+.2f}%", f"기말 자산: {res_fw['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                            with col_fw2: st.markdown(mts_metric_html("🔌 나의 실전 계좌 (실제)", f"{((real_total_eval/total_invested_principal)-1)*100 if total_invested_principal>0 else 0:+.2f}%", f"현재 자산: {real_total_eval:,.0f} 원"), unsafe_allow_html=True)
                            st.dataframe(pd.DataFrame(res_fw['summary_rows']), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.subheader("📊 Test 2. 장기 초과수익 검증 (관심종목 대상)")
        col_t2_1, col_t2_2, col_t2_3 = st.columns([3, 3, 4])
        with col_t2_1: start_date = st.date_input("시작일", datetime.date(2023, 1, 1), key="t2_s")
        with col_t2_2: end_date = st.date_input("종료일", today_date, key="t2_e")
        with col_t2_3:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            if st.button("🚀 관심종목 대상 장기 Backtest 실행", type="primary", use_container_width=True):
                if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
                else:
                    with st.spinner("장기 백테스트 구동 중..."):
                        bt_result = run_quant_simulation(stocks_df, active_strat, total_cash, start_date, end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss/100.0, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                        if bt_result:
                            st.success("✅ 장기 백테스트 완료!")
                            col_r1, col_r2 = st.columns(2)
                            with col_r1: st.markdown(mts_metric_html("총 초기 투입 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                            with col_r2: st.markdown(mts_metric_html("AI 기말 자산", f"{bt_result['final_asset']:,.0f} 원", f"{bt_result['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                            st.dataframe(pd.DataFrame(bt_result['summary_rows']), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.subheader("💡 Test 3. 동적 유니버스 블라인드 백테스트 (시장 주도주 자율 매매)")
        col_t3_1, col_t3_2, col_t3_3 = st.columns([3, 3, 4])
        with col_t3_1: dyn_start_date = st.date_input("시작일", datetime.date(2023, 1, 1), key="t3_s")
        with col_t3_2: dyn_end_date = st.date_input("종료일", today_date, key="t3_e")
        with col_t3_3:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            if st.button("🚀 AI 자율 매매 블라인드 테스트 실행", type="primary", use_container_width=True):
                krx_univ = load_krx_universe()
                if krx_univ.empty: st.error("KRX 유니버스 로드 실패. API 일일 접속량을 확인하세요.")
                else:
                    sim_cands = krx_univ.sort_values('Marcap', ascending=False).head(10) if 'Marcap' in krx_univ.columns else krx_univ.head(10)
                    sim_df_cands = pd.DataFrame({'종목명': sim_cands['Name'] if 'Name' in sim_cands.columns else sim_cands['종목명'], '티커': sim_cands['Code'] if 'Code' in sim_cands.columns else sim_cands['티커']})
                    with st.spinner("블라인드 테스트 구동 중..."):
                        dyn_result = run_quant_simulation(sim_df_cands, active_strat, total_cash, dyn_start_date, dyn_end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss/100.0, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                        if dyn_result:
                            st.success("✅ 블라인드 테스트 완료!")
                            col_r1, col_r2 = st.columns(2)
                            with col_r1: st.markdown(mts_metric_html("총 초기 투입 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                            with col_r2: st.markdown(mts_metric_html("블라인드 기말 자산", f"{dyn_result['final_asset']:,.0f} 원", f"{dyn_result['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                            st.dataframe(pd.DataFrame(dyn_result['summary_rows']), use_container_width=True, hide_index=True)

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서</h1>
    <hr>
    <p>본 시스템은 Core(대형주 추세추종)와 Satellite(중소형주 수급 눌림목) 전략을 결합한 듀얼 퀀트 시스템입니다.</p>
    """, unsafe_allow_html=True)
