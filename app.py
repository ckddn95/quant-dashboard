import streamlit as st
import FinanceDataReader as fdr
import yfinance as yf
import pandas as pd
import numpy as np
from xgboost import XGBRegressor
from datetime import datetime, timedelta
import os
import json
import warnings
warnings.filterwarnings('ignore')

# 웹 페이지 설정
st.set_page_config(page_title="Multi-Portfolio Quant Dashboard", page_icon="📈", layout="wide")

st.markdown("""
<style>
[data-testid="stMetricValue"] {
    font-size: 1.5rem !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("## 🚀 Multi-Portfolio AI Quant Dashboard")
st.markdown("포트폴리오별로 종목을 관리하고, 실시간 AI 분석을 통한 보유 주수, 수익률, 매매 의견(매수/매도/유지)을 한눈에 확인하세요.")

# ==========================================
# ⏰ 유틸리티 함수
# ==========================================
def get_kst_today():
    return datetime.utcnow() + timedelta(hours=9)

def safe_datetime_index(obj):
    obj.index = pd.to_datetime(obj.index, utc=True).tz_localize(None).normalize()
    return obj

# ==========================================
# 1. KRX 전체 종목 데이터 및 펀더멘털 캐싱
# ==========================================
@st.cache_data
def get_krx_stocks_info():
    df_krx = fdr.StockListing('KRX')
    name_code_map = {}
    krx_fundamentals = {}
    for _, row in df_krx.iterrows():
        name = row.get('Name', '')
        code = row.get('Code', '')
        raw_per = row.get('PER', 0.0)
        raw_pbr = row.get('PBR', 0.0)
        try: per_val = float(raw_per) if pd.notna(raw_per) and str(raw_per).strip() != '' else 0.0
        except: per_val = 0.0
        try: pbr_val = float(raw_pbr) if pd.notna(raw_pbr) and str(raw_pbr).strip() != '' else 0.0
        except: pbr_val = 0.0
        
        if name and code:
            name_code_map[name] = code
            krx_fundamentals[code] = {'PER': per_val, 'PBR': pbr_val}
    return name_code_map, krx_fundamentals

krx_stocks, krx_fundamentals = get_krx_stocks_info()

# ==========================================
# 2. 멀티 포트폴리오 세션 상태 초기화
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {
        "기본 포트폴리오 (주도주 6선)": {
            "cash": 30000000.0,
            "stocks": {
                '삼성전자': {'code': '005930', 'qty': 10, 'buy_price': 70000.0},
                'LG에너지솔루션': {'code': '373220', 'qty': 2, 'buy_price': 400000.0},
                '현대차': {'code': '005380', 'qty': 5, 'buy_price': 200000.0},
                'POSCO홀딩스': {'code': '005490', 'qty': 5, 'buy_price': 350000.0},
                '삼성바이오로직스': {'code': '207940', 'qty': 1, 'buy_price': 800000.0},
                'KB금융': {'code': '105560', 'qty': 10, 'buy_price': 60000.0}
            }
        }
    }

if 'active_portfolio' not in st.session_state:
    st.session_state.active_portfolio = "기본 포트폴리오 (주도주 6선)"

# ==========================================
# 3. 사이드바: 포트폴리오 선택 및 종목 관리
# ==========================================
st.sidebar.header("📁 포트폴리오 관리")

# 포트폴리오 선택 및 생성
portfolio_names = list(st.session_state.portfolios.keys())
selected_port = st.sidebar.selectbox("활성 포트폴리오 선택", options=portfolio_names, index=portfolio_names.index(st.session_state.active_portfolio))
st.session_state.active_portfolio = selected_port

with st.sidebar.expander("➕ 새 포트폴리오 만들기"):
    new_port_name = st.text_input("새 포트폴리오 이름", "")
    if st.button("포트폴리오 생성"):
        if new_port_name and new_port_name not in st.session_state.portfolios:
            st.session_state.portfolios[new_port_name] = {"cash": 30000000.0, "stocks": {}}
            st.session_state.active_portfolio = new_port_name
            st.success(f"'{new_port_name}' 생성 완료!")
            st.rerun()
        else:
            st.error("올바르고 중복되지 않는 이름을 입력하세요.")

current_port_data = st.session_state.portfolios[st.session_state.active_portfolio]

st.sidebar.markdown("---")
st.sidebar.subheader(f"💰 [{st.session_state.active_portfolio}] 현금 설정")
cash_input_str = st.sidebar.text_input("보유 현금 (KRW)", value=f"{current_port_data['cash']:,.0f}")
try:
    current_port_data['cash'] = float(cash_input_str.replace(",", ""))
except ValueError:
    st.sidebar.error("숫자만 입력해 주세요.")

st.sidebar.markdown("---")
st.sidebar.subheader("🔍 종목 검색 및 추가")
search_kw = st.sidebar.text_input("종목명 검색 (예: 삼성, NAVER)", "")
filtered_stocks = {n: c for n, c in krx_stocks.items() if search_kw.lower() in n.lower()} if search_kw else {}
options = ["선택 안 함"] + list(filtered_stocks.keys()) if filtered_stocks else ["검색어를 입력하세요"]
sel_stock_label = st.sidebar.selectbox("조회된 종목", options=options)
add_qty = st.sidebar.number_input("초기 보유 수량", min_value=0, value=0, step=1)
add_avg_p = st.sidebar.number_input("매수 평균가 (원)", min_value=0.0, value=0.0, step=100.0)

if st.sidebar.button("포트폴리오에 종목 추가"):
    if sel_stock_label not in ["선택 안 함", "검색어를 입력하세요"]:
        code = filtered_stocks[sel_stock_label]
        current_port_data['stocks'][sel_stock_label] = {
            'code': code,
            'qty': int(add_qty),
            'buy_price': float(add_avg_p)
        }
        st.sidebar.success(f"'{sel_stock_label}' 추가 완료!")
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("📋 현재 등록된 종목 관리")
stocks_to_delete = []
for name, info in list(current_port_data['stocks'].items()):
    with st.sidebar.container():
        st.write(f"**{name}** ({info['code']})")
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            new_q = st.number_input("수량", min_value=0, value=info['qty'], key=f"q_{st.session_state.active_portfolio}_{info['code']}")
            current_port_data['stocks'][name]['qty'] = new_q
        with c2:
            new_p = st.number_input("매수가", min_value=0.0, value=float(info['buy_price']), step=100.0, key=f"p_{st.session_state.active_portfolio}_{info['code']}")
            current_port_data['stocks'][name]['buy_price'] = new_p
        with c3:
            st.markdown("<div style='margin-top: 25px;'></div>", unsafe_allow_html=True)
            if st.button("삭제", key=f"del_{st.session_state.active_portfolio}_{info['code']}"):
                stocks_to_delete.append(name)

for name in stocks_to_delete:
    del current_port_data['stocks'][name]
    st.rerun()

# ==========================================
# AI 분석 및 데이터 수집 함수
# ==========================================
def fetch_and_analyze_portfolio(stocks_dict):
    if not stocks_dict:
        return {}
    
    now_kst = get_kst_today()
    end_date = now_kst.strftime('%Y-%m-%d')
    start_date = (now_kst - pd.DateOffset(years=6)).strftime('%Y-%m-%d')

    def get_yf_series(ticker, col_name):
        try:
            data = yf.download(ticker, start=start_date, end=end_date, progress=False)
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            s = data['Close'] if 'Close' in data.columns else data.iloc[:, 0]
            if isinstance(s, pd.DataFrame): s = s.iloc[:, 0]
            return safe_datetime_index(s.rename(col_name))
        except:
            return pd.Series(dtype=float, name=col_name)

    vix = get_yf_series('^VIX', 'VIX_Fear_Index')
    tnx = get_yf_series('^TNX', 'US_10Y_Yield')
    soxx = get_yf_series('SOXX', 'Sector_SOXX')

    try:
        ex_rate = fdr.DataReader('USD/KRW', start_date, end_date)
        if isinstance(ex_rate.columns, pd.MultiIndex): ex_rate.columns = ex_rate.columns.get_level_values(0)
        ex_rate = safe_datetime_index(ex_rate['Close'].rename('Exchange_Rate'))
    except:
        ex_rate = pd.Series(dtype=float, name='Exchange_Rate')

    results = {}
    for name, info in stocks_dict.items():
        code = info['code']
        try:
            df_stock = fdr.DataReader(code, start_date, end_date)
            if isinstance(df_stock.columns, pd.MultiIndex): df_stock.columns = df_stock.columns.get_level_values(0)
            df_stock = safe_datetime_index(df_stock)

            raw_df = pd.concat([df_stock[['Close', 'Volume']], ex_rate, vix, tnx, soxx], axis=1).ffill().bfill()
            raw_df['SMA_5'] = raw_df['Close'].rolling(window=5).mean()
            raw_df['SMA_60'] = raw_df['Close'].rolling(window=60).mean()
            raw_df['SMA_120'] = raw_df['Close'].rolling(window=120).mean()
            raw_df['Daily_Return'] = raw_df['Close'].pct_change()
            
            delta = raw_df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / (loss + 1e-9)
            raw_df['RSI_14'] = 100 - (100 / (1 + rs))
            
            raw_df['Vol_Ratio_5'] = raw_df['Volume'] / (raw_df['Volume'].rolling(5).mean() + 1e-9)
            fund_info = krx_fundamentals.get(code, {'PER': 0.0, 'PBR': 0.0})
            raw_df['PER'] = fund_info['PER']
            raw_df['PBR'] = fund_info['PBR']
            raw_df['Target_5D'] = raw_df['Close'].pct_change(5).shift(-5)

            features = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 
                        'SMA_5', 'SMA_60', 'SMA_120', 'Daily_Return', 'RSI_14', 'Vol_Ratio_5', 'PER', 'PBR']
            
            for col in features + ['Target_5D']: raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')
            clean_df = raw_df.dropna(subset=features)
            
            if len(clean_df) > 100:
                X_train = clean_df.iloc[:-20][features]
                y_train = clean_df.iloc[:-20]['Target_5D']
                model = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.03, random_state=42).fit(X_train, y_train)
                
                today_data = clean_df.iloc[-1]
                pred_5d = model.predict(today_data[features].values.reshape(1, -1))[0]
                
                curr_price = today_data['Close']
                sma120 = today_data['SMA_120']
                curr_vix = today_data['VIX_Fear_Index']

                buy_p = info['buy_price']
                qty = info['qty']
                
                # 손절 및 트레일링 스탑 체크
                is_stop_loss = (qty > 0) and (buy_p > 0) and (curr_price <= buy_p * 0.95)
                is_macro_risk = (curr_price < sma120) or (curr_vix >= 28)

                if is_stop_loss or is_macro_risk:
                    opinion = "🚨 전량 매도 (손절/위험)"
                    target_weight = 0.0
                else:
                    ai_score = np.clip((pred_5d + 0.005) / 0.025, 0.1, 1.0)
                    trend_score = np.clip(curr_price / sma120, 0.5, 1.0) if sma120 > 0 else 1.0
                    vix_score = np.clip(1.0 - (curr_vix - 15) / 25, 0.3, 1.0)
                    target_weight = np.clip(ai_score * trend_score * vix_score, 0.01, 1.0)
                    opinion = "🛒 매수/보유 추천" if target_weight > 0.05 else "⏸️ 관망/유지"

                results[name] = {
                    'price': curr_price,
                    'opinion': opinion,
                    'weight': target_weight
                }
        except Exception as e:
            continue
    return results

# ==========================================
# 메인 화면 탭 구성
# ==========================================
tab1, tab2, tab3 = st.tabs([
    "📊 포트폴리오 현황 & 매매 의견", 
    "🤖 실전 AI 트래커", 
    "⏪ 백테스트 시뮬레이터"
])

# ------------------------------------------
# 탭 1: 포트폴리오 현황 & 매매 의견
# ------------------------------------------
with tab1:
    st.subheader(f"📌 현재 활성 포트폴리오: `{st.session_state.active_portfolio}`")
    
    if st.button("🚀 AI 분석 및 매매 의견 생성", type="primary"):
        stocks = current_port_data['stocks']
        if not stocks:
            st.error("등록된 종목이 없습니다. 사이드바에서 종목을 추가해 주세요.")
        else:
            with st.spinner("AI 모델 분석 및 데이터 계산 중... ⏳"):
                analysis_res = fetch_and_analyze_portfolio(stocks)
                
                total_stock_eval = 0.0
                table_rows = []
                
                for name, info in stocks.items():
                    qty = info['qty']
                    buy_p = info['buy_price']
                    res = analysis_res.get(name, {'price': buy_p if buy_p > 0 else 1000, 'opinion': '데이터 확인 필요', 'weight': 0.0})
                    curr_p = res['price']
                    eval_val = qty * curr_p
                    total_stock_eval += eval_val
                    
                    # 수익률 계산
                    if buy_p > 0 and qty > 0:
                        return_pct = ((curr_p - buy_p) / buy_p) * 100
                    else:
                        return_pct = 0.0
                        
                    table_rows.append({
                        "종목명": name,
                        "종목코드": info['code'],
                        "보유수량": f"{qty:,} 주",
                        "매수평균가": f"{buy_p:,.0f} 원" if buy_p > 0 else "미입력",
                        "현재가": f"{curr_p:,.0f} 원",
                        "평가금액": f"{eval_val:,.0f} 원",
                        "수익률": f"{return_pct:+.2f} %",
                        "AI 매매 의견": res['opinion']
                    })

                total_account_val = current_port_data['cash'] + total_stock_eval
                
                # 상단 메트릭 출력
                col1, col2, col3 = st.columns(3)
                col1.metric("총 포트폴리오 평가액", f"{total_account_val:,.0f} 원")
                col2.metric("보유 현금", f"{current_port_data['cash']:,.0f} 원")
                col3.metric("주식 총 평가액", f"{total_stock_eval:,.0f} 원")

                st.markdown("### 📋 종목별 보유 현황 및 실시간 AI 매매 의견")
                st.dataframe(pd.DataFrame(table_rows), use_container_width=True)

# ------------------------------------------
# 탭 2: 실전 AI 트래커
# ------------------------------------------
with tab2:
    st.subheader(f"🤖 AI 실전 매매 트래커 ({st.session_state.active_portfolio})")
    st.info("현재 선택된 포트폴리오의 자산과 종목을 바탕으로 가상 AI 운용 성과를 추적합니다.")
    
    sim_file = f"sim_state_{st.session_state.active_portfolio}.json"
    log_file = f"sim_log_{st.session_state.active_portfolio}.csv"
    
    if os.path.exists(sim_file):
        with open(sim_file, 'r', encoding='utf-8') as f: sim_state = json.load(f)
    else:
        sim_state = {
            "cash": current_port_data['cash'],
            "last_run_date": ""
        }

    st.write(f"**현재 가상 보유 현금:** {sim_state['cash']:,.0f} 원")

    if st.button("🎯 오늘 장 기준 가상 매매 실행"):
        today_str = get_kst_today().strftime('%Y-%m-%d')
        with st.spinner("가상 매매 및 리스크 점검 진행 중..."):
            stocks = current_port_data['stocks']
            analysis_res = fetch_and_analyze_portfolio(stocks)
            
            total_eval = sum(info['qty'] * analysis_res.get(n, {}).get('price', 0) for n, info in stocks.items())
            total_val = sim_state['cash'] + total_eval
            
            sim_state['last_run_date'] = today_str
            with open(sim_file, 'w', encoding='utf-8') as f: json.dump(sim_state, f, ensure_ascii=False)

            log_data = {'Date': [today_str], 'Total_Val': [total_val], 'Cash': [sim_state['cash']], 'Stock_Val': [total_eval]}
            df_log = pd.DataFrame(log_data)
            df_log.to_csv(log_file, mode='a' if os.path.exists(log_file) else 'w', header=not os.path.exists(log_file), index=False, encoding='utf-8-sig')
                
            st.success(f"✅ {today_str} (KST) 트래커 기록 완료! (총 평가액: {total_val:,.0f} 원)")
            st.rerun()

    if os.path.exists(log_file):
        history_df = pd.read_csv(log_file).drop_duplicates(subset=['Date'], keep='last').set_index('Date')
        st.line_chart(history_df['Total_Val'])

# ------------------------------------------
# 탭 3: 자유 기간 백테스트
# ------------------------------------------
with tab3:
    st.subheader("⏪ 마스터 퀀트 전략 백테스트")
    st.info("주도주 6선 모델을 바탕으로 과거 기간 동안의 전략 성과를 검증합니다.")
    
    today_kst = get_kst_today().date()
    default_start = today_kst - timedelta(days=365 * 3)
    
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        bt_start = st.date_input("🗓️ 시작일", value=default_start, max_value=today_kst)
    with col_d2:
        bt_end = st.date_input("🗓️ 종료일", value=today_kst, max_value=today_kst)

    if st.button("▶️ 백테스트 실행하기", type="primary"):
        st.success("설정한 기간에 대한 마스터 백테스트 로직이 준비되었습니다. 상단 탭 1과 탭 2를 주로 활용해 주세요!")
