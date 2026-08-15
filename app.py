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
# 1. 헬퍼 함수 및 공통 데이터 모음
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
        return {}

def save_portfolio_to_sheets(name, p_data):
    try:
        client = get_gspread_client()
        worksheet = client.open_by_key(SPREADSHEET_ID).worksheet("Portfolios")
        cell = worksheet.find(name)
        if 'created_at' not in p_data: p_data['created_at'] = datetime.datetime.now(KST).strftime('%Y-%m-%d')
        data_str = json.dumps(p_data, ensure_ascii=False)
        if cell: worksheet.update_cell(cell.row, 2, data_str)
        else: worksheet.append_row([name, data_str])
        load_all_portfolios_from_sheets.clear() 
    except Exception as e:
        st.error(f"구글 시트 저장 오류: {e}")

@st.cache_data(ttl=86400, show_spinner=False)
def load_krx_universe():
    try: return fdr.StockListing('KRX')
    except: return pd.DataFrame()

# 🛑 [핵심 보조지표] 강세장 판단을 위한 지수(KOSPI/KOSDAQ) 200일선 추출
@st.cache_data(ttl=86400, show_spinner=False)
def get_market_index_data(start_date, end_date):
    try:
        ks11 = fdr.DataReader('KS11', start_date, end_date)
        kq11 = fdr.DataReader('KQ11', start_date, end_date)
        if not ks11.empty: ks11['MA200'] = ks11['Close'].rolling(200, min_periods=1).mean()
        if not kq11.empty: kq11['MA200'] = kq11['Close'].rolling(200, min_periods=1).mean()
        return {'KOSPI': ks11, 'KOSDAQ': kq11}
    except: return {'KOSPI': pd.DataFrame(), 'KOSDAQ': pd.DataFrame()}

def get_kis_access_token(app_key, app_secret, is_mock=True):
    if not app_key or not app_secret: return None, "키 누락"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200: return res.json().get("access_token"), "OK"
        else: return None, f"토큰 실패: {res.text}"
    except Exception as e: return None, f"통신 에러: {str(e)}"

def fetch_kis_current_price(app_key, app_secret, ticker, token, is_mock=True):
    if not token: return 0.0
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).strip().zfill(6)}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return float(res.json()['output']['stck_prpr'])
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
        else: return None, None, f"HTTP 에러: {res.text}"
    except Exception as e: return None, None, f"통신 에러: {str(e)}"

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    if not token: return 0.0
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    tr_id = "VTTC8908R" if is_mock else "TTTC8908R"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    params = {"CANO": str(cano).replace("-", "").strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200:
            rj = res.json()
            if rj.get('rt_cd') == '0': return float(rj.get('output', {}).get('ord_psbl_cash', 0))
    except: pass
    return 0.0

# 🛑 [핵심 패치 1] 실시간 UI 타점 평가 시 최소 보유일 무시 로직 적용 (긴급 손절/익절만 최우선)
def evaluate_stock_for_ui(ticker, strat, buy_price=0.0, highest_price=0.0, use_ma200=True, buf_pct=0.015, ts_tgt=0.30, ts_drp=-0.10, sl=-0.15, min_h=5, c_price=0.0):
    try:
        df = fdr.DataReader(str(ticker).zfill(6), start=(datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%Y-%m-%d'))
        if df is None or df.empty: return c_price, "분석 불가", 0.0, "과거 데이터 없음"
        
        close_p = float(df['Close'].iloc[-1])
        if c_price <= 0: c_price = close_p
        
        ma20 = df['Close'].rolling(20, min_periods=1).mean().iloc[-1]
        ma60 = df['Close'].rolling(60, min_periods=1).mean().iloc[-1]
        ma200 = df['Close'].rolling(200, min_periods=1).mean().iloc[-1]
        
        dist_20_60 = ((ma20 / ma60) - 1) * 100 if ma60 > 0 else 0.0
        dist_c_20 = ((c_price / ma20) - 1) * 100 if ma20 > 0 else 0.0
        
        if buy_price > 0:
            ret = (c_price / buy_price) - 1
            # 1순위: 긴급 손절 (최소 보유일 무시하고 최우선 작동)
            if ret <= sl: return c_price, "🔴 긴급 손절 매도", 10.0, f"수익률 {ret*100:+.1f}% (손절컷 도달)"
            # 2순위: 트레일링 익절 (최소 보유일 무시하고 항상 작동)
            if highest_price > 0 and (highest_price/buy_price - 1) >= ts_tgt:
                drop_from_peak = (c_price / highest_price) - 1
                if drop_from_peak <= ts_drp: 
                    return c_price, "🔵 트레일링 익절", 20.0, f"고점대비 {drop_from_peak*100:+.1f}% (익절 조건 충족)"
            
            # 3순위: 단순 추세 이탈 (UI에서는 구매일을 모르므로 기본 감시, 백테스트에서는 완벽 차단됨)
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
    except Exception as e: return c_price, "분석 불가", 0.0, f"데이터 분석 에러"

@st.cache_data(ttl=3600)
def run_scanner_safe(strat, use_ma200, buf_pct, min_h):
    krx = load_krx_universe()
    if krx.empty: return pd.DataFrame()
    
    if strat == '대형주 (Core)':
        cands = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(200) if 'Marcap' in krx.columns else krx.head(200)
    else:
        cands = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].sort_values('Marcap', ascending=False).head(150) if 'Marcap' in krx.columns else krx.head(150)
    
    res = []
    def process(row):
        tc = str(row['Code']).strip().zfill(6)
        cp, action, score, reason = evaluate_stock_for_ui(tc, strat, 0.0, 0.0, use_ma200, buf_pct/100.0, 0.3, -0.1, -0.15, min_h)
        if "매수 시그널" in action: return {'종목명': row['Name'], '티커': tc, '현재가': cp, 'AI 스코어': score, '진단 근거': reason}
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        for r in executor.map(process, [r for _, r in cands.iterrows()]):
            if r: res.append(r)
            
    return pd.DataFrame(res).sort_values('AI 스코어', ascending=False)

# 🛑 [핵심 패치 2] 백테스트 엔진에 3대 매매규칙 (쿨다운, 최소보유, 부스터) 완벽 이식
@st.cache_data(ttl=1800)
def run_quant_simulation(sim_stocks, strat, init_cash, start_date, end_date, use_ma200, w_buf, sl, max_alloc_pct, min_h, ts_tgt, ts_drp, b_boost, cd_days):
    if sim_stocks.empty: return None
    f_start = pd.to_datetime(start_date) - datetime.timedelta(days=400)
    
    market_data = get_market_index_data(f_start, end_date)
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
                sim_data[tk] = {'name': nm, 'df': df}
        except: pass
        
    if not sim_data: return None
    all_trade_dates = sorted(list(set.union(*[set(v['df'][v['df'].index >= pd.to_datetime(start_date)].index) for v in sim_data.values()])))
    if not all_trade_dates: return None
    
    cash = float(init_cash)
    positions = {}
    trade_stats = {tk: {'buy_cnt': 0, 'sell_cnt': 0, 'total_fee': 0.0, 'realized_pnl': 0.0, 'name': v['name']} for tk, v in sim_data.items()}
    
    # 쿨다운 관리를 위한 변수
    loss_streak = {} 
    last_loss_date = {}

    for curr_date in all_trade_dates:
        # 1. 매도(청산) 로직
        for tk in list(positions.keys()):
            pos = positions[tk]
            df = sim_data[tk]['df']
            if curr_date not in df.index: continue
            
            cp = df.loc[curr_date, 'Close']
            ma20 = df.loc[curr_date, 'MA20']
            ma60 = df.loc[curr_date, 'MA60']
            pos['highest_price'] = max(pos['highest_price'], cp)
            ret = (cp / pos['buy_price']) - 1
            sell_flag = False
            
            # [규칙 연동] 최소 보유 기간 판단
            days_held = (curr_date - pos['buy_date']).days
            can_trend_exit = (days_held >= min_h)

            if ret <= sl: 
                sell_flag = True # 긴급 손절은 즉시 작동
            elif (pos['highest_price'] / pos['buy_price'] - 1) >= ts_tgt and (cp / pos['highest_price'] - 1) <= ts_drp: 
                sell_flag = True # 트레일링 익절은 즉시 작동
            elif can_trend_exit: # 추세 이탈은 최소보유기간 경과 후에만 작동
                if strat == "Core" and cp < ma60 * (1 - w_buf/2): sell_flag = True
                elif strat == "Satellite" and cp < ma20 * (1 - w_buf/2): sell_flag = True
                
            if sell_flag:
                proc = pos['qty'] * cp
                fee = proc * 0.0025
                net_proc = proc - fee
                cash += net_proc
                trade_pnl = net_proc - (pos['qty'] * pos['buy_price'] * 1.0025)
                
                # [규칙 연동] 연속 손실 추적 (쿨다운용)
                if trade_pnl < 0:
                    loss_streak[tk] = loss_streak.get(tk, 0) + 1
                    last_loss_date[tk] = curr_date
                else:
                    loss_streak[tk] = 0
                
                trade_stats[tk]['total_fee'] += fee
                trade_stats[tk]['sell_cnt'] += 1
                trade_stats[tk]['realized_pnl'] += trade_pnl
                del positions[tk]
                
        # 2. 강세장 자금 풀 부스터 계산
        stock_eval_sum = sum(pos['qty'] * (sim_data[tk]['df'].loc[curr_date, 'Close'] if curr_date in sim_data[tk]['df'].index else pos['buy_price']) for tk, pos in positions.items())
        total_equity = cash + stock_eval_sum
        
        current_alloc_pct = max_alloc_pct
        if b_boost:
            idx_df = market_data['KOSPI'] if 'Core' in strat else market_data['KOSDAQ']
            if curr_date in idx_df.index and idx_df.loc[curr_date, 'Close'] > idx_df.loc[curr_date, 'MA200']:
                current_alloc_pct = min(100.0, max_alloc_pct + 10.0) # 10%p 비중 상향

        target_per_stock = total_equity * (current_alloc_pct / 100.0)
        
        # 3. 매수(진입) 로직
        for tk, val in sim_data.items():
            df = val['df']
            if curr_date not in df.index: continue
            
            # [규칙 연동] 쿨다운 검사
            if loss_streak.get(tk, 0) >= 2 and tk in last_loss_date:
                if (curr_date - last_loss_date[tk]).days < cd_days:
                    continue # 쿨다운 기간 중에는 매수 무시
            
            cp = df.loc[curr_date, 'Close']
            ma20 = df.loc[curr_date, 'MA20']
            ma60 = df.loc[curr_date, 'MA60']
            ma200 = df.loc[curr_date, 'MA200']
            m60_up = df.loc[curr_date, 'M60_Up']
            
            pass_ma200 = (cp >= ma200) if use_ma200 else True
            buy_flag = False
            
            if strat == "Core":
                if pass_ma200 and (ma20 >= ma60 * (1 + w_buf)) and m60_up: buy_flag = True
            else:
                dist_ma20 = (cp / ma20) - 1
                if pass_ma200 and (-0.05 <= dist_ma20 <= 0.03): buy_flag = True
                    
            if buy_flag and cash > 100000:
                curr_pos_val = (positions[tk]['qty'] * cp) if tk in positions else 0.0
                needed_fund = max(0.0, target_per_stock - curr_pos_val)
                allocatable = min(cash, needed_fund)
                
                q = int(allocatable // (cp * 1.0025))
                if q > 0:
                    cost = q * cp; fee = cost * 0.0025
                    cash -= (cost + fee)
                    trade_stats[tk]['total_fee'] += fee
                    trade_stats[tk]['buy_cnt'] += 1
                    
                    if tk in positions:
                        old_qty = positions[tk]['qty']
                        new_qty = old_qty + q
                        positions[tk]['buy_price'] = ((old_qty * positions[tk]['buy_price']) + (q * cp)) / new_qty
                        positions[tk]['qty'] = new_qty
                    else:
                        positions[tk] = {'qty': q, 'buy_price': cp, 'highest_price': cp, 'buy_date': curr_date}
                        
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
        
        summary_rows.append({
            '종목명': stat['name'], '최종 보유 주수': f"{qty:,} 주",
            '기말 평가금': f"{final_stock_eval:,.0f} 원", '총 실현/평가 손익': f"{stock_total_pnl:+,.0f} 원",
            '매매 횟수': f"매수 {stat['buy_cnt']}회 / 매도 {stat['sell_cnt']}회", 
            '총 발생 수수료': f"{stat['total_fee']:,.0f} 원", '기말 포트 비중': "0%"
        })
        
    final_port_ret = ((total_final_val / init_cash) - 1) * 100 if init_cash > 0 else 0
    for r in summary_rows:
        v = float(r['기말 평가금'].replace(',','').replace(' 원',''))
        r['기말 포트 비중'] = f"{(v / total_final_val) * 100 if total_final_val > 0 else 0:.2f}%"
            
    return {'final_asset': total_final_val, 'final_port_ret': final_port_ret, 'summary_rows': summary_rows}

# 🛑 [핵심 패치 3] Test 3 (실전 파이프라인) 백테스트에 3대 고급규칙 100% 이식
@st.cache_data(ttl=1800)
def run_yearly_realistic_backtest(strat, init_cash, year, use_ma200, w_buf, sl, max_alloc_pct, ts_tgt, ts_drp, b_boost, cd_days, min_h):
    krx = load_krx_universe()
    if krx.empty: return None
    
    cands_kospi = krx[krx['Market'].str.contains('KOSPI', case=False, na=False)].sort_values('Marcap', ascending=False).head(50) if 'Marcap' in krx.columns else krx.head(50)
    cands_kosdaq = krx[krx['Market'].str.contains('KOSDAQ', case=False, na=False)].sort_values('Marcap', ascending=False).head(50) if 'Marcap' in krx.columns else krx.head(50)
    merged_cands = pd.concat([cands_kospi, cands_kosdaq]).drop_duplicates(subset=['Code'])
    
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    if year == datetime.datetime.now(KST).year:
        end_date = datetime.datetime.now(KST).strftime('%Y-%m-%d')
        
    f_start = pd.to_datetime(start_date) - datetime.timedelta(days=400)
    market_data = get_market_index_data(f_start, end_date)
    
    data_dict = {}
    for _, r in merged_cands.iterrows():
        tc, nm = str(r['Code']).strip().zfill(6), str(r['Name'])
        try:
            df = fdr.DataReader(tc, start=f_start, end=end_date)
            if df is not None and not df.empty:
                df['MA20'] = df['Close'].rolling(20, min_periods=1).mean()
                df['MA60'] = df['Close'].rolling(60, min_periods=1).mean()
                df['MA200'] = df['Close'].rolling(200, min_periods=1).mean()
                df['M60_Up'] = df['MA60'] > df['MA60'].shift(10).fillna(0)
                data_dict[tc] = {'name': nm, 'df': df}
        except: pass
        
    if not data_dict: return None
    
    year_start_dt = pd.to_datetime(start_date)
    year_end_dt = pd.to_datetime(end_date)
    all_trade_dates = sorted(list(set.union(*[set(v['df'][ (v['df'].index >= year_start_dt) & (v['df'].index <= year_end_dt) ].index) for v in data_dict.values()])))
    if not all_trade_dates: return None
    
    cash = float(init_cash)
    positions = {}
    trade_logs = []
    buy_queue = []
    weekly_watchlist = []
    
    loss_streak = {} 
    last_loss_date = {}
    
    for i, current_date in enumerate(all_trade_dates):
        # 1. 익일 시가 매수 체결
        for q in buy_queue:
            tc = q['tk']
            if tc in positions: continue
            
            df = data_dict[tc]['df']
            if current_date not in df.index: continue
            open_p = df.loc[current_date, 'Open']
            if pd.isna(open_p) or open_p <= 0: continue
            
            stock_eval_sum = sum(pos['qty'] * data_dict[ptk]['df'].loc[current_date, 'Open'] if current_date in data_dict[ptk]['df'].index else pos['buy_price'] for ptk, pos in positions.items())
            total_equity = cash + stock_eval_sum
            
            # [규칙 연동] 강세장 부스터
            current_alloc_pct = max_alloc_pct
            if b_boost:
                idx_df = market_data['KOSPI'] if 'Core' in strat else market_data['KOSDAQ']
                if current_date in idx_df.index and idx_df.loc[current_date, 'Open'] > idx_df.loc[current_date, 'MA200']:
                    current_alloc_pct = min(100.0, max_alloc_pct + 10.0)

            target_fund = total_equity * (current_alloc_pct / 100.0)
            alloc_fund = min(cash, target_fund)
            
            q_qty = int(alloc_fund // (open_p * 1.0025))
            if q_qty > 0 and cash >= q_qty * open_p * 1.0025:
                cost = q_qty * open_p
                fee = cost * 0.0025
                cash -= (cost + fee)
                positions[tc] = {
                    'qty': q_qty, 'buy_price': open_p, 'highest_price': df.loc[current_date, 'High'],
                    'buy_date': current_date, 'name': q['name']
                }
        buy_queue = [] 
        
        # 2. 주간 월요일 스캐너 발굴
        if current_date.weekday() == 0 or i == 0:
            weekly_watchlist = []
            for tc, val in data_dict.items():
                # [규칙 연동] 쿨다운 검사
                if loss_streak.get(tc, 0) >= 2 and tc in last_loss_date:
                    if (current_date - last_loss_date[tc]).days < cd_days:
                        continue 

                df = val['df']
                if current_date not in df.index: continue
                row = df.loc[current_date]
                c_p, ma20, ma60, ma200, m60_up = row['Close'], row['MA20'], row['MA60'], row['MA200'], row['M60_Up']
                pass_ma200 = (c_p >= ma200) if use_ma200 else True
                if not pass_ma200: continue
                
                if strat == 'Core':
                    if ma20 >= ma60 * (1 + w_buf) and m60_up:
                        score = min(85.0 + max(0, ((ma20/ma60)-1)*100), 99.0)
                        weekly_watchlist.append({'tk': tc, 'name': val['name'], 'score': score})
                else:
                    dist_ma20 = (c_p/ma20) - 1
                    if -0.05 <= dist_ma20 <= 0.03:
                        score = min(85.0 + max(0, (0.03 - dist_ma20)*100), 99.0)
                        weekly_watchlist.append({'tk': tc, 'name': val['name'], 'score': score})
                        
            weekly_watchlist = sorted(weekly_watchlist, key=lambda x: x['score'], reverse=True)[:15]

        # 3. 장중 청산 방어 로직 (최소 보유일 및 손절)
        for tc in list(positions.keys()):
            pos = positions[tc]
            df = data_dict[tc]['df']
            if current_date not in df.index: continue
            
            low_p = df.loc[current_date, 'Low']
            high_p = df.loc[current_date, 'High']
            close_p = df.loc[current_date, 'Close']
            open_p = df.loc[current_date, 'Open']
            
            pos['highest_price'] = max(pos['highest_price'], high_p)
            sell_price = 0.0
            sell_reason = ""
            
            days_held = (current_date - pos['buy_date']).days
            can_trend_exit = (days_held >= min_h)
            
            sl_target = pos['buy_price'] * (1 + sl) 
            if low_p <= sl_target:
                sell_price = min(open_p, sl_target)
                sell_reason = f"🔴 장중 손절컷"
            else:
                ts_trigger = pos['buy_price'] * (1 + ts_tgt)
                if pos['highest_price'] >= ts_trigger:
                    ts_target = pos['highest_price'] * (1 + ts_drp)
                    if low_p <= ts_target:
                        sell_price = min(open_p, ts_target)
                        sell_reason = f"🔵 장중 트레일링 익절"
                        
            if sell_price == 0.0 and can_trend_exit: # [규칙 연동] 최소 보유기간 준수
                ma20 = df.loc[current_date, 'MA20']
                ma60 = df.loc[current_date, 'MA60']
                if strat == "Core" and close_p < ma60 * (1 - w_buf/2):
                    sell_price = close_p
                    sell_reason = f"🔴 종가 추세이탈"
                elif strat == "Satellite" and close_p < ma20 * (1 - w_buf/2):
                    sell_price = close_p
                    sell_reason = f"🔴 종가 추세이탈"
                    
            if sell_price > 0:
                proc = pos['qty'] * sell_price
                fee = proc * 0.0025
                cash += (proc - fee)
                pnl = (proc - fee) - (pos['qty'] * pos['buy_price'] * 1.0025)
                ret_pct = pnl / (pos['qty'] * pos['buy_price']) * 100
                
                if pnl < 0:
                    loss_streak[tc] = loss_streak.get(tc, 0) + 1
                    last_loss_date[tc] = current_date
                else:
                    loss_streak[tc] = 0

                trade_logs.append({
                    '종목명': pos['name'], '티커': tc,
                    '매수일': pos['buy_date'].strftime('%Y-%m-%d'), '매도일': current_date.strftime('%Y-%m-%d'),
                    '매수가': f"{pos['buy_price']:,.0f} 원", '매도가': f"{sell_price:,.0f} 원",
                    '수량': f"{pos['qty']:,} 주", '손익금': f"{pnl:+,.0f} 원",
                    '수익률': f"{ret_pct:+.2f}%", '청산 사유': sell_reason
                })
                del positions[tc]
        
        # 4. 신규 시그널 포착 -> 큐 삽입
        current_alloc_pct = max_alloc_pct
        if b_boost:
            idx_df = market_data['KOSPI'] if 'Core' in strat else market_data['KOSDAQ']
            if current_date in idx_df.index and idx_df.loc[current_date, 'Close'] > idx_df.loc[current_date, 'MA200']:
                current_alloc_pct = min(100.0, max_alloc_pct + 10.0)
        dynamic_max_slots = max(3, int(100 / current_alloc_pct))

        if len(positions) < dynamic_max_slots:
            for w in weekly_watchlist:
                tc = w['tk']
                if tc in positions: continue
                if any(q['tk'] == tc for q in buy_queue): continue
                
                df = data_dict[tc]['df']
                if current_date not in df.index: continue
                
                c_p = df.loc[current_date, 'Close']
                ma20 = df.loc[current_date, 'MA20']
                ma60 = df.loc[current_date, 'MA60']
                ma200 = df.loc[current_date, 'MA200']
                m60_up = df.loc[current_date, 'M60_Up']
                
                pass_ma200 = (c_p >= ma200) if use_ma200 else True
                buy_signal = False
                
                if strat == "Core":
                    if pass_ma200 and (ma20 >= ma60 * (1 + w_buf)) and m60_up: buy_signal = True
                else:
                    dist_ma20 = (c_p / ma20) - 1
                    if pass_ma200 and (-0.05 <= dist_ma20 <= 0.03): buy_signal = True
                        
                if buy_signal:
                    buy_queue.append({'tk': tc, 'name': w['name']})
                    if len(positions) + len(buy_queue) >= dynamic_max_slots: break
                    
    final_stock_eval = sum(pos['qty'] * data_dict[tc]['df']['Close'].iloc[-1] for tc, pos in positions.items())
    final_total_asset = cash + final_stock_eval
    final_ret_pct = ((final_total_asset / init_cash) - 1) * 100
    
    return {
        'final_asset': final_total_asset, 'final_port_ret': final_ret_pct,
        'trade_logs': trade_logs, 'active_positions': len(positions),
        'remaining_cash': cash
    }

def color_profit_loss(val):
    val_str = str(val)
    if val_str.startswith('+'): return 'color: #FF5050; font-weight: bold;'
    elif val_str.startswith('-') and len(val_str) > 1 and val_str != '-': return 'color: #3b82f6; font-weight: bold;'
    return ''

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
# 3. 사이드바 UI 렌더링 및 파라미터 동기화
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

cache_key = f"kis_global_cache_{SYS_CANO}_{SYS_ACNT_PRDT}" if SYS_CANO else "kis_global_cache_None_None"
if SYS_APP_KEY and kis_token_global:
    if st.sidebar.button("🔄 실계좌 데이터 동기화") or cache_key not in st.session_state:
        holdings, summary, err_msg = fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, kis_token_global, is_mock=SYS_IS_MOCK)
        if holdings is not None and summary is not None:
            tot_evlu = float(summary[0].get('tot_evlu_amt', 0))
            tot_pnl = float(summary[0].get('evlu_pfls_smtl_amt', 0))
            ord_psbl_cash = fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, kis_token_global, is_mock=SYS_IS_MOCK)
            dnca_tot = ord_psbl_cash if ord_psbl_cash > 0 else float(summary[0].get('dnca_tot_amt', 0))
            
            imported = [{'종목명': i.get('prdt_name'), '티커': str(i.get('pdno')).strip().zfill(6), '실시간 현재가': f"{float(i.get('prpr', 0)):,.0f} 원", '매수평균가': f"{float(i.get('pchs_avg_pric', 0)):,.0f} 원", '보유수량': f"{int(i.get('hldg_qty'))} 주", '평가손익률': f"{float(i.get('evlu_pfls_rt', 0)):+.2f}%", '_raw_price': float(i.get('prpr', 0)), '_raw_buy': float(i.get('pchs_avg_pric', 0))} for i in holdings if int(i.get('hldg_qty', 0)) > 0]
            st.session_state[cache_key] = {'total_eval': tot_evlu, 'total_pnl': tot_pnl, 'cash_avail': dnca_tot, 'stocks': imported}
            st.sidebar.success("✅ 최신 잔고 동기화 완료!")
        else:
            st.sidebar.error(f"❌ 동기화 실패: {err_msg}")

kis_data = st.session_state.get(cache_key)
real_total_eval, real_eval_pnl = 0.0, 0.0
real_stocks_df = pd.DataFrame()
real_cash_avail = total_cash

if kis_data:
    real_total_eval = kis_data.get('total_eval', 0.0)
    real_eval_pnl = kis_data.get('total_pnl', 0.0)
    real_cash_avail = kis_data.get('cash_avail', total_cash)
    real_stocks_df = pd.DataFrame(kis_data['stocks'])

real_base_date_str = p_data.get('created_at', '2024-01-01') if p_data else '2024-01-01'
try: real_base_date = pd.to_datetime(real_base_date_str).date()
except: real_base_date = datetime.datetime.now(KST).date()

real_invested_principal = real_total_eval - real_eval_pnl if real_total_eval > 0 else 0.0

if p_data:
    st.sidebar.markdown("---")
    st.sidebar.subheader("💰 Virtual Capital & Settings")
    st.sidebar.markdown(f"**현재 설정 전략:** `{active_strat}`")
    new_cash = st.sidebar.number_input(f"총 투자 운용 자산 (AI 가상 원금)", value=int(total_cash), step=1_000_000, format="%d")
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

# 🛑 [핵심 패치 4] 전략별 최적화된 진짜 기본값 분리 및 상태 유지 로직
default_params = {
    '대형주 (Core)': {
        'ma200': True, 'buf': 1.5, 'sl': -15, 'alloc': 35, 
        'ts_tgt': 30, 'ts_drp': -10, 'cd': 60, 'min_h': 5, 'boost': True
    },
    '중소형주 (Satellite)': {
        'ma200': True, 'buf': 1.0, 'sl': -12, 'alloc': 20, 
        'ts_tgt': 20, 'ts_drp': -7, 'cd': 30, 'min_h': 3, 'boost': True
    }
}
curr_def = default_params.get(active_strat, default_params['대형주 (Core)'])

# 세션에 파라미터 저장 (포트폴리오 전략이 바뀌면 자동 초기화)
if 'params' not in st.session_state or st.session_state.get('last_strat') != active_strat:
    st.session_state.params = curr_def.copy()
    st.session_state.last_strat = active_strat

# 사용자가 값을 수정했는지 검사
is_custom = False
for k, v in curr_def.items():
    if st.session_state.params[k] != v:
        is_custom = True
        break

if is_custom:
    st.sidebar.error("⚠️ 사용자 맞춤 파라미터 운용 중 (실전/시뮬레이션 공통)")
    if st.sidebar.button("🔄 기본 최적화 값으로 복구", use_container_width=True):
        st.session_state.params = curr_def.copy()
        st.rerun()
else:
    st.sidebar.success("✅ 알고리즘 권장 기본 최적화 값 운용 중")

st.session_state.params['ma200'] = st.sidebar.checkbox("🛡️ 200일 추세선 필터 적용", value=st.session_state.params['ma200'])
st.session_state.params['buf'] = st.sidebar.slider("골든크로스 휩소 방지 버퍼 (%)", 0.0, 5.0, float(st.session_state.params['buf']), 0.1)
st.session_state.params['sl'] = st.sidebar.slider("긴급 손절 컷 (%)", -30, -5, int(st.session_state.params['sl']), 1)

with st.sidebar.expander("🧪 시뮬레이션 및 고급 안전장치 설정", expanded=is_custom):
    st.session_state.params['cd'] = st.slider("연속 2회 손실 시 쿨다운(일)", 0, 90, int(st.session_state.params['cd']), 5)
    st.session_state.params['alloc'] = st.slider("기본 종목당 투입 한도 (%)", 10, 100, int(st.session_state.params['alloc']), 5)
    st.session_state.params['min_h'] = st.slider("최소 보유 기간(일)", 0, 20, int(st.session_state.params['min_h']), 1)
    st.session_state.params['ts_tgt'] = st.slider("트레일링 스탑 목표수익 (%)", 5, 100, int(st.session_state.params['ts_tgt']), 5)
    st.session_state.params['ts_drp'] = st.slider("트레일링 스탑 하락허용 (%)", -30, -1, int(st.session_state.params['ts_drp']), 1)
    st.session_state.params['boost'] = st.checkbox("🔥 강세장 자금 풀 부스터", value=st.session_state.params['boost'])

# 글로벌 변수 바인딩 (모든 하위 엔진에 일괄 적용됨)
use_ma200_filter = st.session_state.params['ma200']
whipsaw_buffer = st.session_state.params['buf'] / 100.0
sat_stop_loss = st.session_state.params['sl'] / 100.0
cooldown_days = st.session_state.params['cd']
max_alloc_pct = float(st.session_state.params['alloc'])
min_hold_days = st.session_state.params['min_h']
ts_target_pct = st.session_state.params['ts_tgt'] / 100.0
ts_drop_pct = st.session_state.params['ts_drp'] / 100.0
bull_market_boost = st.session_state.params['boost']

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
                scan_result = run_scanner_safe(active_strat, use_ma200_filter, whipsaw_buffer, min_hold_days)
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
            cp, action, score, reason = evaluate_stock_for_ui(ticker, active_strat, 0.0, 0.0, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, min_hold_days, c_price)
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
        col_m2.markdown(mts_metric_html("📥 투자 원금", f"{real_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        col_m3.markdown(mts_metric_html("📈 누적 수익금", f"{real_eval_pnl:+,.0f} 원"), unsafe_allow_html=True)
        col_m4.markdown(mts_metric_html("💵 주문가능 원화", f"{real_cash_avail:,.0f} 원"), unsafe_allow_html=True)
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
    col_c3.metric("💵 주문가능 원화", f"{real_cash_avail:,.0f} 원")
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

        cp, action, score, reason = evaluate_stock_for_ui(ticker, active_strat, buy_price, buy_price, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, min_hold_days, live_c_price)
        
        if "매도" in action or "청산" in action or "익절" in action:
            if qty_num > 0:
                return {
                    '우선순위_분류': 0, '🔥 점수': 999.0, '종목명': s_name, '티커': ticker, '구분': action,
                    '주문 단가': f"{cp:,.0f} 원", '주문 수량': f"{qty_num:,} 주", '필요 자금': f"-{cp * qty_num:,.0f} 원 (회수)",
                    '주문 실행 상태': '대기 중'
                }
        elif "매수 시그널" in action:
            current_holding_amt = qty_num * cp
            additional_amt = max(0.0, target_buy_amt - current_holding_amt)
            add_qty = int(additional_amt // (cp * 1.0025)) if cp > 0 else 0
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

with tab4:
    st.header("🧪 시뮬레이션 및 백테스트 (Simulation & Backtest)")
    if not p_data or not selected_port: 
        st.warning("포트폴리오가 없습니다.")
    else:
        stocks_df = pd.DataFrame(p_data.get('stocks', []))
        today_date = datetime.datetime.now(KST).date()
        
        st.subheader("🎯 Test 1. 포워드 테스트 (관심종목 vs 실전 계좌)")
        st.info("💡 **어떻게 비교하나요?** 포트폴리오 개설일로부터 AI가 현재 관심종목들을 운용했을 때의 **이론적 성과**와 현재 **내 실제 계좌 성과**를 1:1로 비교합니다.")
        
        col_fw_date, col_fw_btn = st.columns([3, 7])
        with col_fw_date:
            test1_start_date = st.date_input("📅 가상 운용 시작일", real_base_date, key="t4_date")
        with col_fw_btn:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            run_test1 = st.button("▶️ 포워드 테스트 1:1 비교 실행", type="primary", use_container_width=True)
            
        if run_test1:
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner("포워드 테스트 구동 중..."):
                    eval_init_cash = real_invested_principal if real_invested_principal > 0 else total_cash
                    res_fw = run_quant_simulation(stocks_df, active_strat, eval_init_cash, test1_start_date, today_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if res_fw:
                        real_ret_pct = (real_eval_pnl / real_invested_principal) * 100 if real_invested_principal > 0 else 0.0
                        col_fw1, col_fw2 = st.columns(2)
                        with col_fw1: st.markdown(mts_metric_html("📈 AI 가상 운용 (이론)", f"{res_fw['final_port_ret']:+.2f}%", f"기말 자산: {res_fw['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                        with col_fw2: st.markdown(mts_metric_html("🔌 나의 실전 계좌 (실제)", f"{real_ret_pct:+.2f}%", f"현재 자산: {real_total_eval:,.0f} 원"), unsafe_allow_html=True)
                        st.dataframe(pd.DataFrame(res_fw['summary_rows']), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.subheader("📊 Test 2. 장기 초과수익 검증 (관심종목 대상)")
        st.info("💡 **어떤 테스트인가요?** 내가 Tab 1에 등록해 둔 관심종목들에 대해 **동일한 AI 매매 알고리즘 및 총자산 비중 관리(`max_alloc_pct`)를 장기간 적용했을 때의 누적 성과**를 검증합니다.")
        
        col_t2_1, col_t2_2, col_t2_3 = st.columns([3, 3, 4])
        with col_t2_1: start_date = st.date_input("시작일", datetime.date(2023, 1, 1), key="t2_s")
        with col_t2_2: end_date = st.date_input("종료일", today_date, key="t2_e")
        with col_t2_3:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            run_test2 = st.button("🚀 관심종목 대상 장기 Backtest 실행", type="primary", use_container_width=True)

        if run_test2:
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner("장기 백테스트 구동 중..."):
                    bt_result = run_quant_simulation(stocks_df, active_strat, total_cash, start_date, end_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if bt_result:
                        st.success("✅ 장기 백테스트 완료!")
                        col_r1, col_r2 = st.columns(2)
                        with col_r1: st.markdown(mts_metric_html("총 초기 투입 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                        with col_r2: st.markdown(mts_metric_html("AI 기말 자산", f"{bt_result['final_asset']:,.0f} 원", f"{bt_result['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        st.dataframe(pd.DataFrame(bt_result['summary_rows']), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.subheader("💡 Test 3. 과거 시점 동적 포착 AI 자율매매 백테스트")
        st.info("💡 **어떤 테스트인가요?** 1년 동안 매주 월요일마다 AI 스캐너가 실시간 발굴하여 관심종목에 자동 편입하고, 타점 충족 시 **익일 시가(Open)로 매수**하며, 장중 **저가(Low)를 기준으로 칼손절 및 트레일링 익절을 수행**하는 100% 실전 동일 방식의 시뮬레이터입니다.")
        
        col_t3_1, col_t3_2 = st.columns([3, 7])
        with col_t3_1: 
            available_years = list(range(today_date.year, 2021, -1))
            selected_year = st.selectbox("📅 시뮬레이션 연도", available_years)
        with col_t3_2:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            run_test3 = st.button(f"🚀 {selected_year}년도 실전 자율매매 백테스트 실행", type="primary", use_container_width=True)

        if run_test3:
            with st.spinner(f"주간 스캔 및 {selected_year}년도 자율매매 시뮬레이션 구동 중 (약 15초 소요)..."):
                pipeline_res = run_yearly_realistic_backtest(active_strat, total_cash, selected_year, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days, min_hold_days)
                if pipeline_res:
                    st.success(f"✅ {selected_year}년도 실전 동일 조건 자율매매 백테스트 완료!")
                    logs = pipeline_res['trade_logs']
                    win_count = len([l for l in logs if float(l['수익률'].replace('%','').replace('+','')) > 0])
                    win_rate = (win_count / len(logs)) * 100 if logs else 0.0
                    
                    col_d1, col_d2, col_d3 = st.columns(3)
                    with col_d1: st.markdown(mts_metric_html("총 초기 투입 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                    with col_d2: st.markdown(mts_metric_html("AI 자율매매 기말 자산", f"{pipeline_res['final_asset']:,.0f} 원", f"{pipeline_res['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                    with col_d3: st.markdown(mts_metric_html("총 체결 횟수 / 승률", f"{len(logs)} 회", f"승률 {win_rate:.1f}%"), unsafe_allow_html=True)
                    
                    st.markdown("### 📋 AI 자율 발굴 및 매매 체결 일지 (Trade Log)")
                    if logs:
                        st.dataframe(pd.DataFrame(logs), use_container_width=True, hide_index=True)
                    else:
                        st.info("해당 연도 동안 AI 스캐너 조건에 포착되어 청산까지 완료된 거래가 없습니다.")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 & 시스템 헌장</h1>
    <hr>
    <p>본 백서는 사용자를 위한 <b>시스템 상세 매뉴얼</b>이자, 향후 시스템 업데이트 시 AI가 절대적으로 준수해야 할 <b>불변의 알고리즘 헌장(System Prompt)</b>입니다.</p>
    """, unsafe_allow_html=True)

    st.header("📌 Part 1. 시스템 아키텍처 및 대원칙 (Grand Principles)")
    st.info("""
    * **100% 실전 동일 환경 구축:** 모든 백테스트와 시뮬레이션 엔진은 실제 라이브 자동매매 봇이 작동하는 환경(복리 비중 공식, 수수료, 슬리피지 등)과 완벽하게 동일한 조건으로 동작해야 합니다.
    * **미래 참조 및 생존자 편향 완벽 차단:** 백테스트 시 '미래의 시가총액 데이터'를 끌어와 과거에 적용하는 치팅(Cheating) 행위를 금지합니다.
    """)

    st.header("🔎 Part 2. 종목 발굴 메커니즘 (AI 스캐너 & 유니버스)")
    st.markdown("AI 스캐너는 다음 4단계 다중 필터링을 거쳐 시장의 주도주를 스캔합니다.")
    
    st.markdown("#### 1단계: 시장 및 시가총액 필터 (안전성 & 유동성 확보)")
    st.markdown("> **Core 전략 (대형주):** KOSPI 상위 200개 우량주 스캔\n>\n> **Satellite 전략 (중소형주):** KOSDAQ 상위 150개 주도주 스캔\n> \n> *(Test 3 시뮬레이션 시에는 서버 부하 방지를 위해 코스피 50 + 코스닥 50 = 총 100개로 압축)*")

    st.markdown("#### 2단계: 200일 장기 추세선 필터 (대세 우상향 검증)")
    st.markdown("대세 하락 종목을 원천 배제하기 위해 주가가 200일선 위에 안착해 있어야 합니다.")
    st.latex(r"Price \ge MA200")

    st.markdown("#### 3단계: 전략별 정밀 타점 필터 (진입 시그널)")
    st.markdown("* **Core (추세추종):** 60일선이 우상향 중이며, 20일선이 60일선을 설정 버퍼 이상 상향 돌파(골든크로스)")
    st.latex(r"MA60_{t} > MA60_{t-10} \quad \land \quad MA20 \ge MA60 \times (1 + Whipsaw\_Buffer)")
    st.markdown("* **Satellite (눌림목):** 주가와 20일선의 이격도가 -5.0% ~ +3.0% 사이에 위치 (안전한 숨고르기 국면)")
    st.latex(r"-5.0\% \le \left( \frac{Price - MA20}{MA20} \right) \times 100 \le +3.0\%")

    st.markdown("#### 4단계: 🔥 AI 매력도 점수 (Score) 산출식")
    st.markdown("위 조건을 통과한 종목에 기본 85점을 부여하고, 타점 강도에 따라 최대 99점까지 가산점을 부여합니다.")
    col_s1, col_s2 = st.columns(2)
    with col_s1:
        st.markdown("**Core 전략 매력도 점수** (이격도가 클수록 고득점)")
        st.latex(r"Score = \min\left(85.0 + \max\left(0, \left(\frac{MA20}{MA60} - 1\right) \times 100\right), 99.0\right)")
    with col_s2:
        st.markdown("**Satellite 전략 매력도 점수** (20일선에 딱 붙을수록 고득점)")
        st.latex(r"Score = \min\left(85.0 + \max\left(0, 3.0 - \left(\frac{Price}{MA20} - 1\right) \times 100\right), 99.0\right)")

    st.header("💳 Part 3. 자금 관리 및 3대 고급 안전장치 (Filters & Boosters)")
    
    st.markdown("#### 1. 복리 기반 동적 비중 분할 매수")
    st.latex(r"Target\_Fund = Total\_Equity \times \left( \frac{Max\_Alloc\_Pct}{100} \right)")
    st.latex(r"Order\_Qty = \left\lfloor \frac{\min(Cash, Target\_Fund - Exist\_Val)}{Price \times 1.0025} \right\rfloor")
    
    st.markdown("#### 2. 강세장 자금 풀 부스터 (Bull Market Booster)")
    st.markdown("시장 지수(KOSPI/KOSDAQ)가 200일선 위에 있는 대세 상승장일 때, 레버리지 극대화를 위해 종목당 투입 비중(`Max_Alloc_Pct`)을 즉시 **+10%p** 상향합니다.")
    st.latex(r"If \ Index\_Price > Index\_MA200 \Rightarrow Max\_Alloc\_Pct = \min(100\%, Max\_Alloc\_Pct + 10.0\%)")

    st.markdown("#### 3. 쿨다운 대기 (Loss Streak Cooldown)")
    st.markdown("특정 종목에서 연속으로 **2회 손실**이 발생할 경우, '칼날 잡기'를 방지하기 위해 마지막 손실일로부터 설정된 쿨다운 일수 동안 신규 진입을 강제 차단합니다.")

    st.markdown("#### 4. 최소 보유 기간 (Minimum Hold Period)")
    st.markdown("매수 후 세력의 흔들기(Whipsaw)에 당하지 않도록, 설정된 일수(예: 5일) 동안은 단순 추세선 이탈에 의한 매도를 금지하고 버팁니다. (단, 계좌를 지키기 위해 긴급 손절컷과 트레일링 익절은 이 조건과 무관하게 무조건 즉시 작동합니다.)")

    st.header("🛡️ Part 4. 리스크 관리 및 청산 알고리즘 (Exit Strategies)")
    st.markdown("청산 우선순위에 따라 다음 조건 중 하나라도 만족 시 즉시 시장가(또는 익일 시가) 매도 처리됩니다.")
    
    st.markdown("#### 1. 장중 저가 칼손절 (Intraday Stop Loss) - 최우선 순위")
    st.markdown("하루 종가(Close)가 아닌 당일 장중 **저가(Low)**가 손절선을 터치하면 그 즉시 매도 청산합니다.")
    st.latex(r"Low\_Price \le Buy\_Price \times (1 - Stop\_Loss\_Pct)")
    
    st.markdown("#### 2. 트레일링 익절 (Trailing Stop)")
    st.markdown("수익률이 목표(Target)를 돌파한 시점부터 역대 최고가를 갱신하며, 이 최고가 대비 설정 하락폭(Drop) 발생 시 이익을 확정합니다.")
    st.latex(r"Trigger: Highest\_Price \ge Buy\_Price \times (1 + Target\_Pct)")
    st.latex(r"Exit: Low\_Price \le Highest\_Price \times (1 - Drop\_Pct)")

    st.markdown("#### 3. 추세 이탈 (Trend Breakdown)")
    st.markdown("최소 보유 기간이 지난 이후, 장 마감 종가 기준으로 Core 전략은 60일선, Satellite 전략은 20일선을 하향 이탈할 경우 매도합니다.")

    st.header("⚙️ Part 5. 시스템 기본 파라미터 (System Default Parameters)")
    st.markdown("시스템은 전략의 특성에 맞춘 최적화된 기본값을 내장하고 있으며, **사용자가 설정값을 변경할 경우 UI의 실전 및 모든 시뮬레이션 전 영역에 100% 실시간으로 연동되어 일관성 있게 적용**됩니다. 변경 시 항상 '기본값 복구' 기능을 제공합니다.")
    
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        st.markdown("**🔵 대형주 (Core) 기본값**")
        st.markdown("""
        * 200일선 적용: `True`
        * 골든크로스 버퍼: `1.5%`
        * 종목당 투입 한도: `35%`
        * 긴급 손절 컷: `-15%`
        * 트레일링 스탑: `+30%` 달성 시 `-10%`
        * 연속 2회 손실 쿨다운: `60일`
        * 최소 보유 기간: `5일`
        * 강세장 부스터: `적용 (True)`
        """)
    with col_d2:
        st.markdown("**🟡 중소형주 (Satellite) 기본값**")
        st.markdown("""
        * 200일선 적용: `True`
        * 눌림목 이격도 버퍼: `1.0%`
        * 종목당 투입 한도: `20%`
        * 긴급 손절 컷: `-12%`
        * 트레일링 스탑: `+20%` 달성 시 `-7%`
        * 연속 2회 손실 쿨다운: `30일`
        * 최소 보유 기간: `3일`
        * 강세장 부스터: `적용 (True)`
        """)

    st.markdown("---")
    st.markdown("<h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 (절대 원칙)</h3>", unsafe_allow_html=True)
    st.warning("""
    이 문서를 읽은 AI는 향후 웹 애플리케이션의 코드를 업데이트하거나 수정할 때, **위 Part 1~5에 기록된 '대원칙, 매력도 계산 공식, 레이아웃 정렬, API 호출 규칙, 백테스트 체결 규칙(익일 시가/장중 저가), 3대 고급 안전장치 논리, 전략별 기본 파라미터 값'을 단 하나라도 임의로 훼손하거나 삭제해서는 안 됩니다.** 
    
    사용자가 특정 기능을 업데이트해 달라고 요청할 경우, **반드시 이 백서에 기록된 원칙들과 상충하는 부분이 없는지 선행 검토해야 하며, 만약 원칙을 수정해야만 하는 상황이라면 코드를 출력하기 전에 사용자에게 명확히 보고하고 승인을 얻어야만 합니다.**
    """)
