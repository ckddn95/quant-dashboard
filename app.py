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
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **오토파일럿 무인 감시**, **가상/실계좌 연동**을 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 텔레그램 메시지 발송 함수
# ==========================================
def send_telegram_message(message):
    try:
        tg_token = st.secrets.get("telegram", {}).get("bot_token")
        tg_chat_id = st.secrets.get("telegram", {}).get("chat_id")
        
        if not tg_token or not tg_chat_id:
            return False, "Secrets에 텔레그램 정보가 없습니다."
            
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        payload = {
            "chat_id": tg_chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            return True, "발송 성공"
        else:
            return False, f"API 오류: {res.text}"
    except Exception as e:
        return False, str(e)

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
# 한국투자증권 Open API 연동 로직
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
        pass
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
        "custtype": "P"
    }
    
    safe_cano = str(cano).replace("-", "").strip()[:8]
    safe_prdt = str(acnt_prdt_cd).strip().zfill(2)
    
    params = {
        "CANO": safe_cano, "ACNT_PRDT_CD": safe_prdt, "AFHR_FLPR_YN": "N",
        "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", 
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
    }
    
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('rt_cd') == '0':
                return data.get('output1', []), data.get('output2', [])
    except Exception as e:
        pass
    return None, None

# ==========================================
# 1. 데이터 수집 함수 모음
# ==========================================
@st.cache_data(ttl=86400)
def load_krx_universe():
    try:
        df = fdr.StockListing('KRX')
        return df.dropna(subset=['Code', 'Name'])
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

# ==========================================
# 2. 세션 트래킹 초기화
# ==========================================
if 'auto_diagnose' not in st.session_state:
    st.session_state.auto_diagnose = False
if 'show_scanner' not in st.session_state:
    st.session_state.show_scanner = False

# ==========================================
# 3. 사이드바: 포트폴리오 관리 및 세팅
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
else:
    st.sidebar.info("👈 포트폴리오를 추가해 주세요.")
    active_strat = "대형주 (Core)"

st.sidebar.markdown("---")
st.sidebar.header("🔌 한국투자증권 실계좌 연동")
SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, SYS_IS_MOCK = None, None, None, None, True

if p_data:
    kis_secret_key = "core" if active_strat == "대형주 (Core)" else "satellite"
    try:
        kis_account_data = st.secrets.get("kis_accounts", {}).get(kis_secret_key, None)
    except: kis_account_data = None
        
    if kis_account_data:
        SYS_APP_KEY = kis_account_data.get("app_key")
        SYS_APP_SECRET = kis_account_data.get("app_secret")
        SYS_CANO = str(kis_account_data.get("cano"))
        SYS_ACNT_PRDT = str(kis_account_data.get("acnt_prdt", "01"))
        SYS_IS_MOCK = kis_account_data.get("is_mock", False)
        acc_name = kis_account_data.get("name", f"{active_strat} 계좌")
        st.sidebar.success(f"✅ **{acc_name}** 자동 매칭됨")
    else:
        st.sidebar.warning(f"🔑 **KIS API 미연동**")

# ==========================================
# 텔레그램 연동 상태 및 오토파일럿 (V2.3 NEW!)
# ==========================================
st.sidebar.markdown("---")
st.sidebar.header("📱 텔레그램 알림 봇 연동")
tg_token = st.secrets.get("telegram", {}).get("bot_token", "")
tg_chat_id = st.secrets.get("telegram", {}).get("chat_id", "")

# 오토파일럿 전역 변수
auto_pilot = False 

if tg_token and tg_chat_id:
    st.sidebar.success("✅ 텔레그램 봇 연동 완료")
    if st.sidebar.button("🔔 봇 연동 테스트 알림 발송"):
        success, msg = send_telegram_message("🤖 *Core-Satellite Quant System*\n텔레그램 알림 봇이 정상적으로 연결되었습니다! 앞으로 새로운 매매 시그널을 보내드립니다.")
        if success:
            st.toast("텔레그램 알림 발송 성공!")  # <--- 에러 났던 부분 수정 완료!
        else:
            st.sidebar.error(f"발송 실패: {msg}")
            
    st.sidebar.markdown("---")
    st.sidebar.subheader("🤖 오토파일럿 (무인 감시 모드)")
    st.sidebar.caption("브라우저 창을 켜두면 지정된 주기마다 스스로 진단하고, **새로운 매수/매도 시그널** 발생 시에만 알림을 보냅니다.")
    
    auto_pilot = st.sidebar.toggle("오토파일럿 켜기 (Auto-Refresh)", key='auto_pilot_toggle')
    
    if auto_pilot:
        check_min = st.sidebar.number_input("감시 주기 (분)", min_value=1, max_value=60, value=10)
        st.sidebar.info(f"🔄 {check_min}분 주기로 백그라운드 자동 감시 중...\n(알림 테러 방지를 위해 액션이 변한 종목만 알림을 발송합니다.)")
        
        # 브라우저 자동 새로고침 자바스크립트 주입
        components.html(
            f"""
            <script>
            setTimeout(function(){{
                window.parent.location.reload();
            }}, {check_min * 60000});
            </script>
            """,
            height=0
        )
else:
    st.sidebar.warning("🔑 `Secrets`에 `[telegram]` 정보 미등록. 푸시 알림 비활성화됨.")


# ==========================================
# 파라미터 세팅
# ==========================================
vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60 = fetch_market_data()
st.sidebar.markdown("---")
st.sidebar.header("⚙️ Advanced Strategy Parameters")

def_sl = -15 if active_strat == '대형주 (Core)' else -12
def_alloc = 35 if active_strat == '대형주 (Core)' else 20
def_ts_target = 30 if active_strat == '대형주 (Core)' else 15
def_ts_drop = -10 if active_strat == '대형주 (Core)' else -5

use_ma200_filter = st.sidebar.checkbox("🛡️ 200일 대장기 추세선 필터 적용", value=True)
whipsaw_buffer = st.sidebar.slider("골든크로스 휩소 방지 버퍼 (%)", min_value=0.0, max_value=5.0, value=1.5, step=0.5)
sat_stop_loss = st.sidebar.slider("긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=def_sl, step=1)
max_alloc_pct = st.sidebar.slider("기본 종목당 투입 한도 (%)", min_value=10, max_value=60, value=def_alloc, step=5)
ts_target_pct = st.sidebar.slider("트레일링 스탑 목표 수익률 (%)", min_value=10, max_value=100, value=def_ts_target, step=5)
bull_market_boost = st.sidebar.checkbox("🔥 강세장 자금 풀 부스터", value=True)

# ==========================================
# 4. 탭 구성
# ==========================================
tab1, tab2 = st.tabs(["📝 가상 샌드박스", "🔌 KIS 실전 계좌"])

with tab1:
    st.header("📝 가상 포트폴리오 샌드박스 (Google Sheets DB 연동)")
    
    if not p_data or not selected_port:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 생성하거나 선택하세요.")
    else:
        current_strategy = p_data.get('strategy', '대형주 (Core)')
        total_cash = p_data.get('cash', 10000000)
        stocks_df = pd.DataFrame(p_data.get('stocks', []))
        if stocks_df.empty: stocks_df = pd.DataFrame(columns=['종목명', '티커', '매수단가', '보유수량'])

        st.markdown("**가상 포트폴리오 내역 (매수단가 및 보유수량 테스트 입력)**")
        edited_df = st.data_editor(stocks_df, num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}")
        
        if st.button("💾 표 데이터 수정 후 덮어쓰기 (Quick Save)", type="primary"):
            p_data['stocks'] = edited_df.to_dict(orient='records')
            save_portfolio_to_sheets(selected_port, p_data)
            st.success("✅ 구글 시트 DB에 저장되었습니다!")

        st.markdown("---")
        st.subheader("🩺 가상 포트폴리오 AI 진단 결과")
        
        run_btn = st.button("🚀 가상 종목 진단 실행", type="secondary")
        
        # 버튼을 누르거나, 사이드바에서 오토파일럿이 켜져있으면 자동으로 실행
        if run_btn or auto_pilot:
            if edited_df.empty:
                st.warning("진단할 종목이 없습니다.")
            else:
                with st.spinner("AI 퀀트 필터링 분석 중..."):
                    market_ret_60 = kospi_ret_60 if current_strategy == '대형주 (Core)' else kosdaq_ret_60
                    buf = whipsaw_buffer / 100.0
                    
                    buy_scores = {}
                    stock_data_cache = {}
                    
                    for idx, row in edited_df.iterrows():
                        s_ticker = row['티커']
                        s_name = row['종목명']
                        buy_price = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                        quantity = pd.to_numeric(row.get('보유수량', 0), errors='coerce')
                        is_holding = (quantity > 0) and (buy_price > 0)
                        
                        c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_vol_max = fetch_stock_status(s_ticker)
                        if c_price is None: continue
                        
                        stock_data_cache[s_name] = {
                            'price': c_price, 'ma200': ma200, 'ma60': ma60, 'ma20': ma20, 
                            'drawdown': drawdown, 'vol_ratio': vol_ratio, 'ret_60': ret_60, 'ret_20': ret_20,
                            'ma60_slope': ma60_slope_positive, 'is_above_ma200': is_above_ma200, 'is_holding': is_holding,
                            'qty': quantity, 'vol_surged': vol_surged, 'low': current_low
                        }
                        buy_scores[s_name] = 1.0 # 기본 스코어 단순화 적용

                    current_max_alloc_ratio = max_alloc_pct / 100.0
                    current_stock_eval = sum((data['qty'] * data['price']) for data in stock_data_cache.values() if data['is_holding'])
                    available_cash = total_cash - current_stock_eval
                    current_cash = max(available_cash, 0)

                    results = []
                    for idx, row in edited_df.iterrows():
                        s_name = row['종목명']
                        if s_name not in stock_data_cache: continue
                        data = stock_data_cache[s_name]
                        c_price = data['price']
                        is_holding = data['is_holding']
                        
                        ma20, ma60, ret_20, ma60_slope_positive = data['ma20'], data['ma60'], data['ret_20'], data['ma60_slope']
                        diff_ma = ((ma20 / ma60) - 1) * 100
                        dist_ma20 = ((c_price / ma20) - 1) * 100
                        
                        tech_text = f"20/60선 이격 {diff_ma:+.2f}%, 20일 모멘텀 {ret_20:+.2f}%" if current_strategy == '대형주 (Core)' else f"20일선 이격 {dist_ma20:+.2f}%"

                        if current_strategy == '대형주 (Core)':
                            if is_holding: 
                                if ma20 >= ma60 * (1 - buf/2): 
                                    action = "🟢 보유 유지"
                                else: 
                                    action = "🔴 전량 매도 (추세 이탈)"
                            else: 
                                if (use_ma200_filter and not data['is_above_ma200']): action = "🔴 진입 보류 (200일선 하회)"
                                elif ((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) and vix_safe) or vix_contrarian: action = f"🟢 적극 신규 진입 권장"
                                else: action = "🟡 관망 (타점 대기)"
                        else:
                            if is_holding: 
                                buy_price_val = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                                user_ret = ((c_price / buy_price_val) - 1) * 100 if buy_price_val > 0 else 0
                                if user_ret <= (sat_stop_loss): action = "🔴 강제 손절 집행"
                                elif ma20 >= ma60 * (1 - buf/2): action = "🟢 보유 유지"
                                else: action = "🔴 전량 매도"
                            else:
                                if (use_ma200_filter and not data['is_above_ma200']): action = "🔴 진입 보류 (200일선 하회)"
                                else:
                                    low_ma20_touch = (data['low'] <= ma20 * 1.01) and (c_price >= ma20 * 0.95)
                                    if ((-5.0 <= dist_ma20 <= 3.0) or low_ma20_touch or vix_contrarian) and data['drawdown'] >= -30.0: action = f"🟢 적극 신규 진입 권장"
                                    else: action = "🟡 관망 (타점 대기)"
                                
                        results.append({'종목명': s_name, '현재가': f"{c_price:,.0f} 원", '액션 플랜': action, '상세 판단 근거': tech_text})
                    
                    st.info(f"📊 **[가상 자금 리밸런싱 현황]** 가용 현금 잔고: `{current_cash:,.0f} 원` (보유 주식 평가액: `{current_stock_eval:,.0f} 원`)")
                    st.table(pd.DataFrame(results))

                    # -------------------------------------------------------------
                    # [V2.3 오토파일럿 핵심] 구글 시트에 상태 저장 및 변화 감지
                    # -------------------------------------------------------------
                    if auto_pilot:
                        changed_msgs = []
                        needs_save = False
                        
                        for idx, r_dict in enumerate(p_data['stocks']):
                            s_name = r_dict['종목명']
                            curr_action = next((r['액션 플랜'] for r in results if r['종목명'] == s_name), "기록없음")
                            old_action = r_dict.get('last_action', "기록없음")
                            
                            if curr_action != old_action:
                                p_data['stocks'][idx]['last_action'] = curr_action # 시트에 저장할 상태 업데이트
                                needs_save = True
                                
                                # 단순 유지/관망이 아닌 유의미한 시그널 변경일 때만 알림 전송
                                if "유지" not in curr_action and "관망" not in curr_action and "보류" not in curr_action:
                                    changed_msgs.append(f"▪️ *{s_name}*: {curr_action}")
                                    
                        if changed_msgs:
                            tg_msg = f"🤖 *[{selected_port} 가상포트] 시그널 감지!*\n" + "\n".join(changed_msgs)
                            send_telegram_message(tg_msg)
                            st.toast("오토파일럿 알림 발송 완료!")
                            
                        if needs_save:
                            save_portfolio_to_sheets(selected_port, p_data) # 변경된 상태를 구글 시트에 영구 저장
                    # -------------------------------------------------------------

                    if tg_token and tg_chat_id and len(results) > 0 and not auto_pilot:
                        if st.button("📲 이 진단 결과를 텔레그램으로 수동 전송", key="send_tg_virtual"):
                            msg_lines = [f"📊 *[{selected_port}] 가상 포트폴리오 수동 진단 결과*"]
                            for r in results:
                                if "보유 유지" not in r['액션 플랜'] and "관망" not in r['액션 플랜'] and "보류" not in r['액션 플랜']:
                                    msg_lines.append(f"▪️ *{r['종목명']}*: {r['액션 플랜']}")
                            success, msg = send_telegram_message("\n".join(msg_lines))
                            if success: st.toast("수동 전송 성공!")

with tab2:
    st.header("🔌 실전 계좌(API) 연동 현황")
    if not SYS_APP_KEY:
        st.warning("API 키를 등록해주세요.")
    else:
        st.success(f"✅ 연동 계좌: **`{SYS_CANO[:4]}****-{SYS_ACNT_PRDT}`**")
        
        cache_key = f"kis_global_cache_{SYS_CANO}_{SYS_ACNT_PRDT}"
        if st.button("🔄 잔고 실시간 새로고침") or auto_pilot or cache_key not in st.session_state:
            token = get_kis_access_token(SYS_APP_KEY, SYS_APP_SECRET, is_mock=SYS_IS_MOCK)
            if token:
                holdings, summary = fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SECRET, SYS_CANO, SYS_ACNT_PRDT, token, is_mock=SYS_IS_MOCK)
                if holdings is not None and summary is not None:
                    tot_evlu = float(summary[0].get('tot_evlu_amt', 0)) if summary else 0
                    imported = []
                    for item in holdings:
                        qty = int(item.get('hldg_qty', 0))
                        if qty > 0:
                            imported.append({
                                '종목명': item.get('prdt_name', ''), '티커': item.get('pdno', ''),
                                '실시간 현재가': f"{float(item.get('prpr', 0)):,.0f} 원",
                                '매수평균가': f"{float(item.get('pchs_avg_pric', 0)):,.0f} 원",
                                '보유수량': f"{qty:,} 주",
                                '평가손익률': f"{float(item.get('evlu_pfls_rt', 0)):+.2f}%",
                                '_raw_price': float(item.get('prpr', 0)), '_raw_buy': float(item.get('pchs_avg_pric', 0))
                            })
                    st.session_state[cache_key] = {'total_eval': tot_evlu, 'stocks': imported}

        kis_data = st.session_state.get(cache_key)
        if kis_data:
            real_total_eval = kis_data['total_eval']
            real_stocks_df = pd.DataFrame(kis_data['stocks'])
            st.metric("💰 계좌 총 평가 금액 (현금+주식)", f"{real_total_eval:,.0f} 원")
            
            if not real_stocks_df.empty:
                display_df = real_stocks_df[['종목명', '티커', '실시간 현재가', '매수평균가', '보유수량', '평가손익률']]
                st.dataframe(display_df, use_container_width=True)

            st.markdown("---")
            run_real_btn = st.button("🚀 실전 계좌 종목 진단 실행", type="secondary")
            
            if run_real_btn or auto_pilot:
                if real_stocks_df.empty:
                    st.warning("진단할 보유 종목이 없습니다.")
                else:
                    with st.spinner("실계좌 종목 분석 중..."):
                        buf = whipsaw_buffer / 100.0
                        live_results = []
                        for idx, row in real_stocks_df.iterrows():
                            s_ticker = row['티커']
                            s_name = row['종목명']
                            buy_price = float(row.get('_raw_buy', 0))
                            
                            c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200, vol_surged, current_low, recent_vol_max = fetch_stock_status(s_ticker)
                            live_c_price = float(row.get('_raw_price', c_price if c_price else 0))
                            if live_c_price == 0: continue

                            if active_strat == '대형주 (Core)':
                                if ma20 and ma60 and (ma20 >= ma60 * (1 - buf/2)): action = "🟢 보유 유지"
                                else: action = "🔴 즉각 매도 (추세 이탈)"
                            else:
                                user_ret = ((live_c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                                if user_ret <= (sat_stop_loss): action = "🔴 강제 손절 집행"
                                elif ma20 and ma60 and (ma20 >= ma60 * (1 - buf/2)): action = "🟢 보유 유지"
                                else: action = "🔴 전량 매도"

                            live_results.append({
                                '보유 종목명': s_name, 
                                '한투 실시간 현재가': f"{live_c_price:,.0f} 원",
                                'AI 액션 플랜 (권장)': action
                            })
                        
                        st.table(pd.DataFrame(live_results))
                        
                        # -------------------------------------------------------------
                        # [V2.3 실계좌 오토파일럿 핵심] 구글 시트에 상태 저장 및 변화 감지
                        # -------------------------------------------------------------
                        if auto_pilot:
                            changed_msgs = []
                            needs_save = False
                            
                            if 'real_last_actions' not in p_data:
                                p_data['real_last_actions'] = {}
                                
                            for r in live_results:
                                s_name = r['보유 종목명']
                                curr_action = r['AI 액션 플랜 (권장)']
                                old_action = p_data['real_last_actions'].get(s_name, "기록없음")
                                
                                if curr_action != old_action:
                                    p_data['real_last_actions'][s_name] = curr_action
                                    needs_save = True
                                    
                                    if "유지" not in curr_action:
                                        changed_msgs.append(f"▪️ *{s_name}*: {curr_action}")
                                        
                            if changed_msgs:
                                tg_msg = f"🤖 *[{acc_name} 실전계좌] 긴급 시그널 감지!*\n" + "\n".join(changed_msgs)
                                send_telegram_message(tg_msg)
                                st.toast("실계좌 오토파일럿 알림 발송 완료!")
                                
                            if needs_save:
                                save_portfolio_to_sheets(selected_port, p_data)
                        # -------------------------------------------------------------
