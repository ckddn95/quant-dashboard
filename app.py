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
st.set_page_config(page_title="Premium Quant Dashboard", page_icon="📈", layout="wide")

# --- 🎨 사용자 지정 CSS (폰트 크기 축소 및 버튼 높이 정렬) ---
st.markdown("""
<style>
/* Metric(평가액, 현금 등) 숫자 폰트 크기 축소 */
[data-testid="stMetricValue"] {
    font-size: 1.6rem !important;
}
</style>
""", unsafe_allow_html=True)

# 1. 메인 타이틀 영어로 변경 & 폰트 크기 축소(## H2 사용)
st.markdown("## 🌅 Premium Quant Investment Dashboard")
st.markdown("종목을 자유롭게 추가·제거하고, AI 분석을 통해 내 포트폴리오를 관리하며, 가상의 AI 펀드와 수익률을 비교해보세요.")

# ==========================================
# 🌟 대시보드 운영 원리 및 신뢰도 안내
# ==========================================
with st.expander("💡 이 대시보드는 어떻게 작동하나요? (데이터 출처 및 AI 로직 안내)"):
    st.markdown("""
    **1. 📊 활용되는 기초 데이터 (Data Sources)**
    * **국내 주식 주가:** 한국거래소(KRX) 데이터를 기준으로 수집합니다. (`FinanceDataReader` 활용)
    * **거시경제 지표:** 글로벌 금융 플랫폼 야후 파이낸스(Yahoo Finance)의 핵심 지표를 융합하여 분석합니다.
      - `VIX (공포지수)`: 시장의 불안 심리와 변동성 측정
      - `US 10Y (미국 10년물 국채 금리)`: 글로벌 매크로 자금 흐름 파악
      - `SOXX (반도체 지수)`: 국내 증시에 영향이 큰 기술주 투심 파악
      - `USD/KRW (원/달러 환율)`: 외국인 수급 환경 및 환차손익 분석

    **2. 🤖 AI 분석 및 안전장치 로직 (Core Algorithm)**
    * **AI 예측 모델:** `XGBoost` 머신러닝 알고리즘이 과거 주가 흐름과 거시경제 지표 패턴을 동시에 학습하여 **5거래일 뒤의 주가 방향성**을 예측합니다.
    * **위기 감지 시스템 (폭락 방어):** 
      - 글로벌 VIX(공포지수)가 **28**을 초과하거나, 개별 주가가 **60일 이동평균선(SMA 60)**을 이탈(하회)하면 시장 위기로 간주하여 해당 종목의 목표 비중을 즉시 **0% (전량 매도 및 현금 관망)**로 자동 조정합니다.
    * **정밀 비중 산출:** AI 예측 점수, 이동평균선 추세 점수, VIX 변동성 점수를 종합하여 각 종목에 할당할 최적의 예산 비중을 계산합니다.

    **3. 💰 시뮬레이터 수수료 적용 (Backtest Reality)**
    * 가장 현실적인 가상 투자 결과를 위해 **매수 시 0.015%** (증권사 수수료), **매도 시 0.195%** (증권거래세 0.18% + 수수료 0.015%)의 비용이 탭 2와 탭 3의 시뮬레이션 계산에 100% 차감 반영되어 있습니다.

    > ⚠️ **면책 조항:** 본 대시보드가 제공하는 AI 매매 지시 및 시뮬레이션 수익률은 과거의 데이터 패턴을 기반으로 한 참고 자료일 뿐이며, 미래의 실제 수익을 보장하지 않습니다. 최종 투자 결정과 책임은 투자자 본인에게 있습니다.
    """)

# --- ⏰ 유틸리티 함수 모음 ---
def get_kst_today():
    return datetime.utcnow() + timedelta(hours=9)

def safe_datetime_index(obj):
    """타임존 오류 없이 날짜(인덱스)를 00:00:00으로 통일하는 안전한 함수"""
    obj.index = pd.to_datetime(obj.index, utc=True).tz_localize(None).normalize()
    return obj

# ==========================================
# 1. 한국거래소(KRX) 전체 종목 데이터 캐싱
# ==========================================
@st.cache_data
def get_krx_stocks():
    df_krx = fdr.StockListing('KRX')
    return dict(zip(df_krx['Name'], df_krx['Code']))

krx_stocks = get_krx_stocks()

# ==========================================
# 2. 내 포트폴리오 초기 세팅
# ==========================================
if 'portfolio' not in st.session_state:
    st.session_state.portfolio = {
        '삼성전자': {'code': '005930', 'qty': 0},
        'SK하이닉스': {'code': '000660', 'qty': 0},
        'LG에너지솔루션': {'code': '373220', 'qty': 0},
        '포스코퓨처엠': {'code': '003670', 'qty': 0},
        '현대차': {'code': '005380', 'qty': 0},
        '기아': {'code': '000270', 'qty': 0},
        'POSCO홀딩스': {'code': '005490', 'qty': 0},
        '삼성바이오로직스': {'code': '207940', 'qty': 0},
        '셀트리온': {'code': '068270', 'qty': 0},
        'KB금융': {'code': '105560', 'qty': 0},
        '신한지주': {'code': '055550', 'qty': 0}
    }

# ==========================================
# 3. 사이드바 설정 (투자금, 종목 검색, 저장)
# ==========================================
st.sidebar.header("⚙️ 내 포트폴리오 설정")

initial_cash_input = st.sidebar.text_input("현재 보유 현금 (KRW)", value="10,000,000")
try:
    initial_cash = float(initial_cash_input.replace(",", ""))
except ValueError:
    st.sidebar.error("숫자와 쉼표(,)만 입력해 주세요.")
    initial_cash = 10000000.0

st.sidebar.markdown("---")
st.sidebar.subheader("🔍 종목 검색 및 추가")

search_keyword = st.sidebar.text_input("종목명 검색 (예: 삼성, 카카오)", "")
if search_keyword:
    keywords = search_keyword.lower().split()
    filtered_stocks = {name: code for name, code in krx_stocks.items() if all(kw in str(name).lower() for kw in keywords)}
else:
    filtered_stocks = {}

options = ["선택 안 함"] + list(filtered_stocks.keys()) if filtered_stocks else ["검색어를 입력하세요"]
selected_label = st.sidebar.selectbox("조회된 종목 선택", options=options)
new_qty = st.sidebar.number_input("초기 보유 수량 입력", value=0, step=1)

if st.sidebar.button("종목 리스트에 추가"):
    if selected_label not in ["선택 안 함", "검색어를 입력하세요"]:
        new_code = filtered_stocks[selected_label]
        st.session_state.portfolio[selected_label] = {'code': new_code, 'qty': int(new_qty)}
        st.sidebar.success(f"'{selected_label}'이(가) 추가되었습니다!")
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("📋 현재 등록된 종목")

stocks_to_delete = []
for name, info in st.session_state.portfolio.items():
    col1, col2 = st.sidebar.columns([3, 1])
    
    with col1:
        # [신규 기능] 텍스트 대신 수량을 직접 입력받고 즉시 저장하는 위젯
        updated_qty = st.number_input(
            f"{name} ({info['code']})", 
            min_value=0, 
            value=info['qty'], 
            step=1, 
            key=f"qty_input_{info['code']}"
        )
        st.session_state.portfolio[name]['qty'] = updated_qty
        
    with col2:
        # 입력창과 삭제 버튼의 높이 여백 맞추기
        st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
        if st.button("삭제", key=f"del_{info['code']}"):
            stocks_to_delete.append(name)

for name in stocks_to_delete:
    del st.session_state.portfolio[name]
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("💾 설정 저장 및 불러오기")

port_json = json.dumps(st.session_state.portfolio, ensure_ascii=False)
st.sidebar.download_button("📥 내 포트폴리오 저장", data=port_json, file_name="my_quant_portfolio.json", mime="application/json")

uploaded_file = st.sidebar.file_uploader("📤 저장한 포트폴리오 불러오기", type=['json'])
if uploaded_file is not None:
    if st.sidebar.button("설정 적용하기"):
        st.session_state.portfolio = json.load(uploaded_file)
        st.sidebar.success("성공적으로 불러왔습니다!")
        st.rerun()

st.sidebar.markdown("---")
rebal_threshold = st.sidebar.slider(
    "리밸런싱 밴드 기준 (%) 💡", 1.0, 10.0, 3.0, 0.5,
    help="현재 비중과 목표 비중의 차이가 이 값(%) 이내면 매매하지 않습니다. 잦은 매매와 수수료 낭비를 막는 안전장치입니다."
) / 100.0


# ==========================================
# 공통 AI 및 데이터 처리 함수
# ==========================================
def fetch_and_predict(codes_list):
    now_kst = get_kst_today()
    end_date = now_kst.strftime('%Y-%m-%d')
    start_date = (now_kst - pd.DateOffset(years=8)).strftime('%Y-%m-%d')

    def get_yf_series(ticker, col_name):
        try:
            data = yf.download(ticker, start=start_date, end=end_date, progress=False)
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            s = data['Close'] if 'Close' in data.columns else data.iloc[:, 0]
            if isinstance(s, pd.DataFrame): s = s.iloc[:, 0]
            s = s.rename(col_name)
            return safe_datetime_index(s)
        except:
            return pd.Series(dtype=float, name=col_name)

    vix = get_yf_series('^VIX', 'VIX_Fear_Index')
    tnx = get_yf_series('^TNX', 'US_10Y_Yield')
    soxx = get_yf_series('SOXX', 'Sector_SOXX')

    ex_rate = fdr.DataReader('USD/KRW', start_date, end_date)
    if isinstance(ex_rate.columns, pd.MultiIndex): ex_rate.columns = ex_rate.columns.get_level_values(0)
    ex_rate = ex_rate['Close'].rename('Exchange_Rate')
    ex_rate = safe_datetime_index(ex_rate)

    results = {}
    for name, code in codes_list.items():
        df_stock = fdr.DataReader(code, start_date, end_date)
        if isinstance(df_stock.columns, pd.MultiIndex): df_stock.columns = df_stock.columns.get_level_values(0)
        df_stock = safe_datetime_index(df_stock)

        raw_df = pd.concat([df_stock[['Close', 'Volume']], ex_rate, vix, tnx, soxx], axis=1).ffill().bfill()
        raw_df['SMA_5'] = raw_df['Close'].rolling(window=5).mean()
        raw_df['SMA_60'] = raw_df['Close'].rolling(window=60).mean()
        raw_df['Daily_Return'] = raw_df['Close'].pct_change()
        raw_df['Target_5D'] = raw_df['Close'].pct_change(5).shift(-5)

        features = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 'SMA_5', 'SMA_60', 'Daily_Return']
        for col in features + ['Target_5D']: raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')
        clean_df = raw_df.dropna(subset=features)
        
        if len(clean_df) > 100:
            X_train = clean_df.iloc[:-20][features]
            y_train_5d = clean_df.iloc[:-20]['Target_5D']
            model_5d = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.03, random_state=42).fit(X_train, y_train_5d)
            
            today_data = clean_df.iloc[-1]
            pred_5d = model_5d.predict(today_data[features].values.reshape(1, -1))[0]
            
            curr_price = today_data['Close']
            sma60 = today_data['SMA_60']
            curr_vix = today_data['VIX_Fear_Index']

            if curr_price < sma60 or curr_vix >= 28:
                target_weight = 0.0
            else:
                ai_score = np.clip((pred_5d + 0.005) / 0.025, 0.1, 1.0)
                trend_score = np.clip(curr_price / sma60, 0.5, 1.0)
                vix_score = np.clip(1.0 - (curr_vix - 15) / 25, 0.3, 1.0)
                target_weight = np.clip(ai_score * trend_score * vix_score, 0.01, 1.0)
            
            results[name] = {'price': curr_price, 'weight': target_weight}
    return results


# ==========================================
# 메인 화면 탭 구성
# ==========================================
tab1, tab2, tab3 = st.tabs([
    "📊 내 포트폴리오 지시서", 
    "🤖 실전 AI 트래커 (일간)", 
    "⏪ 자유 기간 백테스트"
])

ai_target_stocks = {
    '삼성전자': '005930', 'SK하이닉스': '000660', 'LG에너지솔루션': '373220', '포스코퓨처엠': '003670',
    '현대차': '005380', '기아': '000270', 'POSCO홀딩스': '005490', '삼성바이오로직스': '207940',
    '셀트리온': '068270', 'KB금융': '105560', '신한지주': '055550'
}
BUY_FEE, SELL_FEE = 0.00015, 0.00195

# ------------------------------------------
# 탭 1: 내 실제 포트폴리오 관리
# ------------------------------------------
with tab1:
    if st.button("🚀 내 계좌 최신 분석 및 매매 지시서 생성", type="primary"):
        if not st.session_state.portfolio:
            st.error("등록된 종목이 없습니다. 사이드바에서 종목을 추가해 주세요.")
        else:
            with st.spinner("한국(KST) 시간 기준 데이터 수집 및 AI 모델 분석 중... ⏳"):
                codes_list = {k: v['code'] for k, v in st.session_state.portfolio.items()}
                ai_results = fetch_and_predict(codes_list)
                
                total_stock_eval = 0.0
                signals = {}
                for name, res in ai_results.items():
                    qty = st.session_state.portfolio[name]['qty']
                    eval_val = qty * res['price']
                    total_stock_eval += eval_val
                    signals[name] = {'price': res['price'], 'eval': eval_val, 'weight': res['weight'], 'qty': qty}

                total_account_val = initial_cash + total_stock_eval
                max_slot_ratio = 1.0 / len(st.session_state.portfolio)
                slot_budget = total_account_val * max_slot_ratio

                st.success(f"✨ 분석 완료! (기준: {get_kst_today().strftime('%Y-%m-%d %H:%M')} KST)")
                col1, col2, col3 = st.columns(3)
                col1.metric("총 계좌 평가액", f"{total_account_val:,.0f} 원")
                col2.metric("보유 현금", f"{initial_cash:,.0f} 원")
                col3.metric("주식 평가액", f"{total_stock_eval:,.0f} 원")

                st.markdown("### 📋 종목별 정밀 리밸런싱 가이드")
                
                display_data = []
                total_buy_cost = 0
                total_sell_cash = 0

                for name, info in signals.items():
                    p, eval_val, t_weight, qty = info['price'], info['eval'], info['weight'], info['qty']
                    target_amount = slot_budget * t_weight
                    current_weight_ratio = eval_val / total_account_val if total_account_val > 0 else 0
                    target_weight_ratio = target_amount / total_account_val
                    diff_ratio = target_weight_ratio - current_weight_ratio
                    diff_amount = target_amount - eval_val

                    if abs(diff_ratio) < rebal_threshold:
                        action = "⏸️ 유지 (밴드 내)"
                    elif diff_amount > 0:
                        buy_qty = int(diff_amount // p)
                        if buy_qty > 0:
                            action = f"🛒 매수 추천 (+{buy_qty}주)"
                            total_buy_cost += buy_qty * p * (1 + BUY_FEE)
                        else:
                            action = "⏸️ 관망"
                    else:
                        sell_qty = int(round(abs(diff_amount) / p))
                        if sell_qty > 0:
                            action = f"📉 매도 추천 (-{sell_qty}주)"
                            total_sell_cash += sell_qty * p * (1 - SELL_FEE)
                        else:
                            action = "⏸️ 관망"

                    display_data.append({
                        "종목명": name, "현재가": f"{p:,.0f} 원", "보유수량": f"{qty}주", 
                        "평가금액": f"{eval_val:,.0f} 원", "목표비중": f"{t_weight*100:.1f}%", 
                        "괴리율": f"{diff_ratio*100:+.1f}%", "매매 지시": action
                    })

                st.dataframe(pd.DataFrame(display_data), use_container_width=True)

                st.info(f"💡 **오늘의 리밸런싱 예상 요약** \n\n"
                        f"- 🛒 지시서대로 매수 시 필요한 총비용: 약 **{total_buy_cost:,.0f} 원** (수수료 포함)\n"
                        f"- 📉 지시서대로 매도 시 확보되는 현금: 약 **{total_sell_cash:,.0f} 원** (세금/수수료 제함)")

# ------------------------------------------
# 탭 2: 실시간 AI 트래커
# ------------------------------------------
with tab2:
    st.subheader("🤖 AI 실전 매매 트래커 (초기자본: 1,000만 원)")
    st.info("이 탭은 매일 버튼을 눌러 AI 가상 펀드의 실력을 추적하는 공간입니다. (한국 시간 기준)")
    
    sim_file = "ai_sim_state.json"
    log_file = "ai_sim_log.csv"
    
    if os.path.exists(sim_file):
        with open(sim_file, 'r', encoding='utf-8') as f: sim_state = json.load(f)
    else:
        sim_state = {"cash": 10000000.0, "portfolio": {name: 0 for name in ai_target_stocks.keys()}, "last_run_date": ""}

    st.write(f"**현재 가상 보유 현금:** {sim_state['cash']:,.0f} 원")

    if st.button("🎯 오늘 장 기준 매매 실행 (하루 1회)"):
        today_str = get_kst_today().strftime('%Y-%m-%d')
        with st.spinner("가상 매매 진행 중..."):
            ai_res = fetch_and_predict(ai_target_stocks)
            total_fund_val = sim_state['cash'] + sum(sim_state['portfolio'][n] * r['price'] for n, r in ai_res.items())
            max_weight = 1.0 / len(ai_target_stocks)
            trade_logs = []
            
            for name, res in ai_res.items():
                p = res['price']
                curr_qty = sim_state['portfolio'][name]
                tar_amt = total_fund_val * max_weight * res['weight']
                if curr_qty * p > tar_amt:
                    sell_qty = int(round((curr_qty * p - tar_amt) / p))
                    if 0 < sell_qty <= curr_qty:
                        sim_state['portfolio'][name] -= sell_qty
                        sim_state['cash'] += (sell_qty * p) * (1 - SELL_FEE)
                        trade_logs.append(f"📉 {name} {sell_qty}주 매도")
            
            for name, res in ai_res.items():
                p = res['price']
                tar_amt = total_fund_val * max_weight * res['weight']
                if sim_state['portfolio'][name] * p < tar_amt:
                    buy_qty = int((tar_amt - sim_state['portfolio'][name] * p) // p)
                    cost = (buy_qty * p) * (1 + BUY_FEE)
                    while cost > sim_state['cash'] and buy_qty > 0:
                        buy_qty -= 1
                        cost = (buy_qty * p) * (1 + BUY_FEE)
                    if buy_qty > 0:
                        sim_state['portfolio'][name] += buy_qty
                        sim_state['cash'] -= cost
                        trade_logs.append(f"🛒 {name} {buy_qty}주 매수")
            
            final_eval = sum(sim_state['portfolio'][n] * r['price'] for n, r in ai_res.items())
            final_total = sim_state['cash'] + final_eval
            
            sim_state['last_run_date'] = today_str
            with open(sim_file, 'w', encoding='utf-8') as f: json.dump(sim_state, f, ensure_ascii=False)

            log_data = {'Date': [today_str], 'Total_Val': [final_total], 'Cash': [sim_state['cash']], 'Stock_Val': [final_eval]}
            df_log = pd.DataFrame(log_data)
            df_log.to_csv(log_file, mode='a' if os.path.exists(log_file) else 'w', header=not os.path.exists(log_file), index=False, encoding='utf-8-sig')
                
            st.success(f"✅ {today_str} (KST) 매매 완료! (총 평가액: {final_total:,.0f} 원)")
            for log in trade_logs: st.write(log)
            st.rerun()

    if os.path.exists(log_file):
        history_df = pd.read_csv(log_file).drop_duplicates(subset=['Date'], keep='last').set_index('Date')
        st.line_chart(history_df['Total_Val'])

# ------------------------------------------
# 탭 3: 자유 기간 백테스트 (최대 3년)
# ------------------------------------------
with tab3:
    st.subheader("⏪ 퀀트 전략 백테스트 (최대 3년 지정 가능)")
    st.warning("⚠️ 웹 환경 보호(서버 과부하 방지)를 위해 **월간(20영업일) 리밸런싱** 기준으로 진행됩니다.")
    
    today_kst = get_kst_today().date()
    default_start = today_kst - timedelta(days=365 * 3)
    
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        bt_start = st.date_input("🗓️ 백테스트 시작일", value=default_start, max_value=today_kst)
    with col_d2:
        bt_end = st.date_input("🗓️ 백테스트 종료일", value=today_kst, max_value=today_kst)

    duration_days = (bt_end - bt_start).days
    is_valid_duration = 0 < duration_days <= (365 * 3 + 5) 

    if not is_valid_duration:
        st.error("🚨 백테스트 기간은 최소 1일에서 최대 3년(약 1095일) 이내로 설정해 주세요.")
        
    elif st.button("▶️ 선택한 기간 백테스트 실행하기", type="primary"):
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        try:
            status_text.text("1/3. 주가 및 거시경제 데이터를 불러오는 중... (잠시만 기다려주세요)")
            
            fetch_start = (bt_start - timedelta(days=365 * 5)).strftime('%Y-%m-%d')
            bt_end_str = bt_end.strftime('%Y-%m-%d')

            def get_hist_series(ticker, col_name):
                data = yf.download(ticker, start=fetch_start, end=bt_end_str, progress=False)
                if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
                s = data['Close'] if 'Close' in data.columns else data.iloc[:, 0]
                s = s.rename(col_name)
                return safe_datetime_index(s)

            h_vix = get_hist_series('^VIX', 'VIX_Fear_Index')
            h_tnx = get_hist_series('^TNX', 'US_10Y_Yield')
            h_soxx = get_hist_series('SOXX', 'Sector_SOXX')
            
            h_ex = fdr.DataReader('USD/KRW', fetch_start, bt_end_str)
            if isinstance(h_ex.columns, pd.MultiIndex): h_ex.columns = h_ex.columns.get_level_values(0)
            h_ex = h_ex['Close'].rename('Exchange_Rate')
            h_ex = safe_datetime_index(h_ex)

            stock_data = {}
            for name, code in ai_target_stocks.items():
                df = fdr.DataReader(code, fetch_start, bt_end_str)
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                df = safe_datetime_index(df)
                stock_data[name] = df[['Close', 'Volume']]

            valid_dates = pd.date_range(bt_start.strftime('%Y-%m-%d'), bt_end_str, freq='B')
            bt_portfolio = {name: 0 for name in ai_target_stocks.keys()}
            bt_cash = 10000000.0
            bt_history = []

            step_size = 20
            total_steps = len(range(0, len(valid_dates), step_size))
            
            for step_idx, i in enumerate(range(0, len(valid_dates), step_size)):
                current_date = valid_dates[i]
                status_text.text(f"2/3. AI 펀드 운용 중... ({current_date.strftime('%Y-%m')} 진행 중)")
                progress_bar.progress((step_idx + 1) / total_steps)
                
                signals = {}
                current_eval = 0
                
                for name, df in stock_data.items():
                    past_df = df[df.index <= current_date]
                    if len(past_df) < 100: continue
                        
                    raw_df = pd.concat([past_df, h_ex[h_ex.index <= current_date], h_vix[h_vix.index <= current_date], 
                                        h_tnx[h_tnx.index <= current_date], h_soxx[h_soxx.index <= current_date]], axis=1).ffill().bfill()
                    
                    raw_df['SMA_5'] = raw_df['Close'].rolling(window=5).mean()
                    raw_df['SMA_60'] = raw_df['Close'].rolling(window=60).mean()
                    raw_df['Daily_Return'] = raw_df['Close'].pct_change()
                    raw_df['Target_5D'] = raw_df['Close'].pct_change(5).shift(-5)
                    
                    features = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 'SMA_5', 'SMA_60', 'Daily_Return']
                    clean_df = raw_df.dropna(subset=features + ['Target_5D'])
                    
                    if len(clean_df) > 100:
                        X_train = clean_df.iloc[:-20][features]
                        y_train_5d = clean_df.iloc[:-20]['Target_5D']
                        model = XGBRegressor(n_estimators=50, max_depth=3, learning_rate=0.05, random_state=42).fit(X_train, y_train_5d)
                        
                        today_data = raw_df.iloc[-1]
                        if pd.isna(today_data['Close']): continue
                        
                        pred_5d = model.predict(today_data[features].values.reshape(1, -1))[0]
                        p, sma60, v = today_data['Close'], today_data['SMA_60'], today_data['VIX_Fear_Index']
                        
                        if p < sma60 or v >= 28: weight = 0.0
                        else:
                            ai_s, tr_s, vx_s = np.clip((pred_5d + 0.005)/0.025, 0.1, 1), np.clip(p/sma60, 0.5, 1), np.clip(1-(v-15)/25, 0.3, 1)
                            weight = np.clip(ai_s * tr_s * vx_s, 0.01, 1.0)
                            
                        signals[name] = {'price': p, 'weight': weight}
                        current_eval += bt_portfolio[name] * p

                if not signals: continue
                total_val = bt_cash + current_eval
                max_w = 1.0 / len(ai_target_stocks)
                
                for name, res in signals.items():
                    p, tar = res['price'], total_val * max_w * res['weight']
                    curr = bt_portfolio[name] * p
                    if curr > tar:
                        sell_qty = int(round((curr - tar) / p))
                        if 0 < sell_qty <= bt_portfolio[name]:
                            bt_portfolio[name] -= sell_qty
                            bt_cash += (sell_qty * p) * (1 - SELL_FEE)
                            
                for name, res in signals.items():
                    p, tar = res['price'], total_val * max_w * res['weight']
                    curr = bt_portfolio[name] * p
                    if curr < tar:
                        buy_qty = int((tar - curr) // p)
                        cost = (buy_qty * p) * (1 + BUY_FEE)
                        while cost > bt_cash and buy_qty > 0:
                            buy_qty -= 1
                            cost = (buy_qty * p) * (1 + BUY_FEE)
                        if buy_qty > 0:
                            bt_portfolio[name] += buy_qty
                            bt_cash -= cost

                final_eval = sum(bt_portfolio[n] * signals[n]['price'] for n in signals if n in bt_portfolio)
                bt_history.append({'Date': current_date.strftime('%Y-%m-%d'), 'Total_Val': bt_cash + final_eval})

            status_text.text("3/3. 차트 생성 중...")
            df_bt = pd.DataFrame(bt_history).set_index('Date')
            
            st.success(f"🎉 설정하신 기간({bt_start} ~ {bt_end}) 시뮬레이션 완료!")
            
            final_val = df_bt['Total_Val'].iloc[-1]
            years = duration_days / 365.25 
            cagr = ((final_val / 10000000.0) ** (1 / years) - 1) * 100 if years > 0 else 0
            tot_ret = (final_val / 10000000.0 - 1) * 100
            
            c1, c2, c3 = st.columns(3)
            c1.metric("초기 자본", "10,000,000 원")
            c2.metric("최종 자산", f"{final_val:,.0f} 원")
            c3.metric(f"누적 수익률 ({years:.1f}년)", f"{tot_ret:+.2f} %", f"연평균 {cagr:+.2f}%")
            
            st.line_chart(df_bt['Total_Val'])
            
        except Exception as e:
            st.error(f"오류가 발생했습니다: {e}")
