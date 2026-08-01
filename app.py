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
st.set_page_config(page_title="Multi-Portfolio AI Quant Dashboard", page_icon="📈", layout="wide")

st.markdown("""
<style>
[data-testid="stMetricValue"] {
    font-size: 1.5rem !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("## 🚀 Multi-Portfolio AI Quant Dashboard")
st.markdown("포트폴리오 관리, 구체적 매매 주문 산출, AI 투명성 검증(데이터 수집 상태 및 예측 근거), 그리고 실전 자동매매 트래커를 제공합니다.")

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
# 2. 멀티 포트폴리오 세션 상태 초기화 (첫 페이지 종목 비우기)
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {
        "기본 포트폴리오": {
            "cash": 30000000.0,
            "created_date": "2024-01-02",
            "stocks": {} # 💡 첫 페이지에서는 종목이 없도록 빈 상태로 초기화
        }
    }

if 'active_portfolio' not in st.session_state:
    st.session_state.active_portfolio = "기본 포트폴리오"

current_port_data = st.session_state.portfolios[st.session_state.active_portfolio]
if 'created_date' not in current_port_data:
    current_port_data['created_date'] = "2024-01-02"

# ==========================================
# 3. 사이드바: 포트폴리오 및 종목 관리 (일괄 등록 기능 추가)
# ==========================================
st.sidebar.header("📁 포트폴리오 관리")

portfolio_names = list(st.session_state.portfolios.keys())
selected_port = st.sidebar.selectbox("활성 포트폴리오 선택", options=portfolio_names, index=portfolio_names.index(st.session_state.active_portfolio))
st.session_state.active_portfolio = selected_port
current_port_data = st.session_state.portfolios[st.session_state.active_portfolio]

with st.sidebar.expander("➕ 새 포트폴리오 만들기"):
    new_port_name = st.text_input("새 포트폴리오 이름", "")
    new_port_date = st.date_input("포트폴리오 생성일 (트래커 기준일)", value=datetime.today().date() - timedelta(days=90))
    if st.button("포트폴리오 생성"):
        if new_port_name and new_port_name not in st.session_state.portfolios:
            st.session_state.portfolios[new_port_name] = {
                "cash": 30000000.0,
                "created_date": new_port_date.strftime('%Y-%m-%d'),
                "stocks": {}
            }
            st.session_state.active_portfolio = new_port_name
            st.success(f"'{new_port_name}' 생성 완료!")
            st.rerun()
        else:
            st.error("올바르고 중복되지 않는 이름을 입력하세요.")

st.sidebar.markdown("---")
st.sidebar.subheader(f"💰 [{st.session_state.active_portfolio}] 자산 설정")
cash_input_str = st.sidebar.text_input("보유 현금 (KRW)", value=f"{current_port_data['cash']:,.0f}")
try:
    current_port_data['cash'] = float(cash_input_str.replace(",", ""))
except ValueError:
    st.sidebar.error("숫자만 입력해 주세요.")

parsed_date = datetime.strptime(current_port_data['created_date'], '%Y-%m-%d').date() if 'created_date' in current_port_data else datetime.today().date()
new_c_date = st.sidebar.date_input("포트폴리오 생성일", value=parsed_date)
current_port_data['created_date'] = new_c_date.strftime('%Y-%m-%d')

# 💡 [신규] 섹터별 대표종목 일괄 등록 기능
st.sidebar.markdown("---")
st.sidebar.subheader("⚡ 섹터별 대표종목 일괄 등록")
st.sidebar.markdown("주도주 6선(삼성전자, LG에너지솔루션, 현대차, POSCO홀딩스, 삼성바이오로직스, KB금융)을 한 번에 추가합니다.")
if st.sidebar.button("주도주 6선 일괄 추가하기"):
    sector_leaders = {
        '삼성전자': {'code': '005930', 'qty': 0, 'buy_price': 0.0},
        'LG에너지솔루션': {'code': '373220', 'qty': 0, 'buy_price': 0.0},
        '현대차': {'code': '005380', 'qty': 0, 'buy_price': 0.0},
        'POSCO홀딩스': {'code': '005490', 'qty': 0, 'buy_price': 0.0},
        '삼성바이오로직스': {'code': '207940', 'qty': 0, 'buy_price': 0.0},
        'KB금융': {'code': '105560', 'qty': 0, 'buy_price': 0.0}
    }
    current_port_data['stocks'].update(sector_leaders)
    st.sidebar.success("주도주 6선 일괄 등록 완료!")
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("🔍 개별 종목 검색 및 추가")
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
if not current_port_data['stocks']:
    st.sidebar.info("등록된 종목이 없습니다.")
stocks_to_delete = []
for name, info in list(current_port_data['stocks'].items()):
    with st.sidebar.container():
        st.write(f"**{name}** ({info['code']})")
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            current_port_data['stocks'][name]['qty'] = st.number_input("수량", min_value=0, value=info['qty'], key=f"q_{st.session_state.active_portfolio}_{info['code']}")
        with c2:
            current_port_data['stocks'][name]['buy_price'] = st.number_input("매수가", min_value=0.0, value=float(info['buy_price']), step=100.0, key=f"p_{st.session_state.active_portfolio}_{info['code']}")
        with c3:
            st.markdown("<div style='margin-top: 25px;'></div>", unsafe_allow_html=True)
            if st.button("삭제", key=f"del_{st.session_state.active_portfolio}_{info['code']}"):
                stocks_to_delete.append(name)

for name in stocks_to_delete:
    del current_port_data['stocks'][name]
    st.rerun()

# ==========================================
# AI 분석 및 투명성 데이터 수집 함수
# ==========================================
def analyze_portfolio_detailed_with_transparency(stocks_dict, current_cash):
    if not stocks_dict: return {}, 0.0, 0.0, {}
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

    stock_metrics = {}
    scores = {}
    diagnostic_details = {}

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
                
                is_stop_loss = (qty > 0) and (buy_p > 0) and (curr_price <= buy_p * 0.95)
                is_macro_risk = (curr_price < sma120) or (curr_vix >= 28)

                ai_score = np.clip((pred_5d + 0.005) / 0.025, 0.1, 1.0)
                trend_score = np.clip(curr_price / sma120, 0.5, 1.0) if sma120 > 0 else 1.0
                vix_score = np.clip(1.0 - (curr_vix - 15) / 25, 0.3, 1.0)

                if is_stop_loss or is_macro_risk:
                    score = 0.0
                    force_sell = True
                else:
                    score = ai_score * trend_score * vix_score
                    force_sell = False

                stock_metrics[name] = {
                    'price': curr_price,
                    'qty': qty,
                    'buy_price': buy_p,
                    'force_sell': force_sell
                }
                scores[name] = score

                diagnostic_details[name] = {
                    'Predicted_5D (%)': f"{pred_5d * 100:+.2f}%",
                    'AI Score': f"{ai_score:.2f}",
                    'Trend Score (P/SMA120)': f"{trend_score:.2f}",
                    'VIX Score': f"{vix_score:.2f}",
                    'Final Score': f"{score:.3f}",
                    'SMA 120': f"{sma120:,.0f} 원",
                    'VIX Index': f"{curr_vix:.1f}",
                    'Stop-Loss Triggered': "예" if is_stop_loss else "아니오",
                    'Macro Risk Triggered': "예 (120일선 이탈 또는 VIX≥28)" if is_macro_risk else "아니오"
                }
        except:
            continue

    total_stock_eval = sum(info['qty'] * stock_metrics[name]['price'] for name, info in stocks_dict.items() if name in stock_metrics)
    total_asset = current_cash + total_stock_eval

    top3 = sorted(scores, key=scores.get, reverse=True)[:3]
    results = {}

    for name, m_info in stock_metrics.items():
        curr_p = m_info['price']
        qty = m_info['qty']
        curr_amt = qty * curr_p
        
        if m_info['force_sell']:
            target_amt = 0.0
        elif name in top3 and scores.get(name, 0) > 0:
            target_amt = total_asset * (1.0 / 3.0)
        else:
            target_amt = 0.0

        diff_amt = target_amt - curr_amt

        if m_info['force_sell']:
            if qty > 0:
                opinion = f"🚨 전량 매도: 현재가 {curr_p:,.0f}원 기준 보유 {qty}주 전량 매도"
            else:
                opinion = "⏸️ 보유 없음 (위험 구간 관망)"
        elif diff_amt > curr_p:
            buy_q = int(diff_amt // curr_p)
            opinion = f"🛒 추가 매수: 현재가 {curr_p:,.0f}원 기준 **{buy_q}주 매수** 필요"
        elif diff_amt < -curr_p:
            sell_q = int(abs(diff_amt) // curr_p)
            opinion = f"📉 부분 매도: 현재가 {curr_p:,.0f}원 기준 **{sell_q}주 매도** 필요"
        else:
            opinion = "⏸️ 유지: 현재 비중 적정"

        results[name] = {
            'price': curr_p,
            'opinion': opinion
        }

    return results, total_asset, total_stock_eval, diagnostic_details

# ==========================================
# 메인 화면 탭 구성
# ==========================================
tab1, tab2, tab3 = st.tabs([
    "📊 포트폴리오 현황 & 매매 주문 (AI 투명성 검증)", 
    "🤖 실전 AI 트래커 (자동매매 시뮬레이션)", 
    "⏪ 마스터 백테스트 시뮬레이터"
])

# ------------------------------------------
# 탭 1: 포트폴리오 현황 & 매매 의견 & 투명성 정보
# ------------------------------------------
with tab1:
    st.subheader(f"📌 현재 활성 포트폴리오: `{st.session_state.active_portfolio}`")
    
    if st.button("🚀 AI 분석 실행 및 주문서/투명성 리포트 생성", type="primary"):
        stocks = current_port_data['stocks']
        if not stocks:
            st.warning("등록된 종목이 없습니다. 사이드바의 '⚡ 섹터별 대표종목 일괄 등록' 버튼을 누르거나 개별 종목을 추가해 주세요.")
        else:
            with st.spinner("AI 모델 연산 및 데이터 수집 검증 중... ⏳"):
                analysis_res, total_asset, total_stock_eval, diag_details = analyze_portfolio_detailed_with_transparency(stocks, current_port_data['cash'])
                
                table_rows = []
                for name, info in stocks.items():
                    qty = info['qty']
                    buy_p = info['buy_price']
                    res = analysis_res.get(name, {'price': buy_p if buy_p > 0 else 1000, 'opinion': '데이터 확인 필요'})
                    curr_p = res['price']
                    eval_val = qty * curr_p
                    
                    return_pct = ((curr_p - buy_p) / buy_p) * 100 if (buy_p > 0 and qty > 0) else 0.0
                        
                    table_rows.append({
                        "종목명": name,
                        "종목코드": info['code'],
                        "보유수량": f"{qty:,} 주",
                        "매수평균가": f"{buy_p:,.0f} 원" if buy_p > 0 else "미입력",
                        "현재가": f"{curr_p:,.0f} 원",
                        "평가금액": f"{eval_val:,.0f} 원",
                        "수익률": f"{return_pct:+.2f} %",
                        "실행 매매 의견 (단가/수량)": res['opinion']
                    })

                total_account_val = current_port_data['cash'] + total_stock_eval
                
                col1, col2, col3 = st.columns(3)
                col1.metric("총 포트폴리오 평가액", f"{total_account_val:,.0f} 원")
                col2.metric("보유 현금", f"{current_port_data['cash']:,.0f} 원")
                col3.metric("주식 총 평가액", f"{total_stock_eval:,.0f} 원")

                st.markdown("### 📋 종목별 보유 현황 및 AI 구체적 매매 주문서")
                st.dataframe(pd.DataFrame(table_rows), use_container_width=True)

                st.markdown("---")
                st.markdown("## 🔍 AI 투명성 검증 및 모델 작동 근거 리포트")
                
                st.subheader("1️⃣ 데이터 수집 상태 검증 (Health Check)")
                st.markdown("외부 API 및 한국거래소(KRX)로부터 참고 데이터를 정상적으로 수신했는지 확인합니다.")
                
                health_data = []
                try:
                    vix_test = yf.download('^VIX', period='5d', progress=False)
                    vix_status = "정상 수신 완료 ✅" if not vix_test.empty else "수신 실패 ❌"
                except: vix_status = "수신 오류 ❌"

                try:
                    ex_test = fdr.DataReader('USD/KRW', datetime.today().date() - timedelta(days=5), datetime.today().date())
                    ex_status = "정상 수신 완료 ✅" if not ex_test.empty else "수신 실패 ❌"
                except: ex_status = "수신 오류 ❌"

                health_data.append({"데이터 항목": "미국 VIX 공포지수 (^VIX)", "소스": "Yahoo Finance", "상태": vix_status})
                health_data.append({"데이터 항목": "원달러 환율 (USD/KRW)", "소스": "FinanceDataReader", "상태": ex_status})
                
                for name, info in stocks.items():
                    try:
                        st_test = fdr.DataReader(info['code'], datetime.today().date() - timedelta(days=5), datetime.today().date())
                        st_status = "정상 수신 완료 ✅" if not st_test.empty else "수신 실패 ❌"
                    except: st_status = "수신 오류 ❌"
                    health_data.append({"데이터 항목": f"종목 주가 데이터 ({name})", "소스": "KRX / FDR", "상태": st_status})

                st.dataframe(pd.DataFrame(health_data), use_container_width=True)

                st.subheader("2️⃣ AI가 참고 및 학습한 데이터(특징량) 목록")
                features_desc = [
                    {"분류": "가격 및 거래량", "팩터명": "Close, Volume, Daily_Return, Vol_Ratio_5", "설명": "일별 종가, 거래량, 일간 수익률 및 5일 평균 대비 거래량 급증 비율"},
                    {"분류": "기술적 지표", "팩터명": "SMA_5, SMA_60, SMA_120, RSI_14", "설명": "5일, 60일, 120일(장기 추세선) 이동평균선 및 14일 기준 상대강도지수(RSI)"},
                    {"분류": "거시경제/글로벌", "팩터명": "Exchange_Rate, VIX_Fear_Index, US_10Y_Yield, Sector_SOXX", "설명": "원달러 환율, VIX 공포지수, 미국 10년물 국채금리, 미국 반도체(SOXX) 지수"},
                    {"분류": "펀더멘털", "팩터명": "PER, PBR", "설명": "한국거래소(KRX) 제공 기업별 주가수익비율(PER) 및 주가순자산비율(PBR)"},
                    {"분류": "AI 학습 타겟", "팩터명": "Target_5D (5일 뒤 수익률)", "설명": "과거 데이터에서 5영업일 뒤의 주가 상승률을 정답(Label)으로 하여 머신러닝 지도학습 수행"}
                ]
                st.dataframe(pd.DataFrame(features_desc), use_container_width=True)

                st.subheader("3️⃣ 매매 의견 결정 근거 및 가중치 점수 산출 공식")
                st.markdown("""
                * **AI 예측 점수 (`ai_score`):** 모델이 예측한 5일 뒤 수익률(`Target_5D`)을 0.1 ~ 1.0 사이로 정규화합니다.
                * **추세 점수 (`trend_score`):** 현재 주가와 120일선(`SMA_120`)의 비율을 반영하여 장기 추세 우상향 종목에 가점을 줍니다. (`P / SMA_120`)
                * **거시경제 점수 (`vix_score`):** VIX 공포지수가 높을수록 점수를 깎아 방어적으로 작동합니다. (`1.0 - (VIX - 15) / 25`)
                * **최종 점수 공식:** `Final Score = ai_score × trend_score × vix_score`
                * **Top 3 쏠림 배분:** 전체 종목 중 최종 점수가 가장 높은 **상위 3개 종목에 자산의 각 33.3%씩 집중 투자**하며, 나머지 자산은 현금으로 보유합니다.
                * **강제 청산(손절/매도) 룰:** ① 매수가 대비 -5% 하락 시 손절, ② 주가가 120일선 아래로 이탈하거나 VIX가 28 이상으로 치솟을 경우 매크로 리스크로 판단하여 전량 현금화합니다.
                """)

                st.subheader("4️⃣ 종목별 상세 AI 점수 및 진단 내역")
                if diag_details:
                    st.dataframe(pd.DataFrame(diag_details).T, use_container_width=True)

# ------------------------------------------
# 탭 2: 실전 AI 트래커
# ------------------------------------------
with tab2:
    st.subheader(f"🤖 실전 AI 트래커 (자동매매 성적 검증)")
    st.info(f"포트폴리오 생성일(`{current_port_data['created_date']}`)부터 오늘까지, AI 봇이 5영업일마다 정기적으로 자동 리밸런싱을 수행했다고 가정했을 때의 가상 성적 곡선입니다.")
    
    if st.button("📈 생성일 기준 자동매매 시뮬레이션 실행", type="primary"):
        stocks = current_port_data['stocks']
        if not stocks:
            st.error("종목이 등록되어 있지 않습니다. 사이드바에서 종목을 추가하거나 일괄 등록을 진행해 주세요.")
        else:
            with st.spinner("생성일부터 현재까지 자동매매 백테스트 연산 중... ⏳"):
                start_str = current_port_data['created_date']
                today_str = get_kst_today().strftime('%Y-%m-%d')
                fetch_start_dt = (datetime.strptime(start_str, '%Y-%m-%d') - timedelta(days=200)).strftime('%Y-%m-%d')
                
                vix = yf.download('^VIX', start=fetch_start_dt, end=today_str, progress=False)
                if isinstance(vix.columns, pd.MultiIndex): vix.columns = vix.columns.get_level_values(0)
                vix_s = safe_datetime_index(vix['Close'] if 'Close' in vix.columns else vix.iloc[:, 0])

                try:
                    ex_rate = fdr.DataReader('USD/KRW', fetch_start_dt, today_str)
                    if isinstance(ex_rate.columns, pd.MultiIndex): ex_rate.columns = ex_rate.columns.get_level_values(0)
                    ex_s = safe_datetime_index(ex_rate['Close'])
                except:
                    ex_s = pd.Series(dtype=float)

                sim_data = {}
                for name, info in stocks.items():
                    code = info['code']
                    df = fdr.DataReader(code, fetch_start_dt, today_str)
                    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                    df = safe_datetime_index(df)
                    raw = pd.concat([df['Close'], vix_s, ex_s], axis=1).ffill().bfill()
                    raw.columns = ['Close', 'VIX', 'Ex']
                    raw['SMA_120'] = raw['Close'].rolling(window=120).mean()
                    sim_data[name] = raw

                dates = pd.date_range(start_str, today_str, freq='B')
                sim_cash = current_port_data['cash']
                sim_port = {n: 0 for n in stocks}
                history_val = []

                for step_idx, d in enumerate(dates):
                    active_p = {}
                    for n in stocks:
                        sub = sim_data[n][sim_data[n].index <= d]
                        if not sub.empty and pd.notna(sub.iloc[-1]['Close']):
                            active_p[n] = sub.iloc[-1]['Close']
                    if not active_p: continue

                    if step_idx % 5 == 0:
                        total_v = sim_cash + sum(sim_port[n] * active_p[n] for n in active_p)
                        target_w = 1.0 / len(active_p)
                        
                        for n in active_p:
                            p = active_p[n]
                            curr_amt = sim_port[n] * p
                            tar_amt = total_v * target_w
                            if curr_amt > tar_amt * 1.03:
                                sell_q = int((curr_amt - tar_amt) // p)
                                if sell_q > 0 and sell_q <= sim_port[n]:
                                    sim_port[n] -= sell_q
                                    sim_cash += sell_q * p * 0.998
                        for n in active_p:
                            p = active_p[n]
                            curr_amt = sim_port[n] * p
                            tar_amt = total_v * target_w
                            if tar_amt > curr_amt * 1.03:
                                buy_q = int((tar_amt - curr_amt) // p)
                                cost = buy_q * p * 1.00015
                                while cost > sim_cash and buy_q > 0:
                                    buy_q -= 1; cost = buy_q * p * 1.00015
                                if buy_q > 0:
                                    sim_port[n] += buy_q
                                    sim_cash -= cost

                    eval_v = sum(sim_port[n] * active_p.get(n, 0) for n in stocks)
                    history_val.append({'Date': d, 'Total_Val': sim_cash + eval_v})

                if history_val:
                    df_res = pd.DataFrame(history_val).set_index('Date')
                    final_val = df_res['Total_Val'].iloc[-1]
                    total_ret = ((final_val / current_port_data['cash']) - 1) * 100

                    st.success("✅ 실전 AI 자동매매 트래커 연산 완료!")
                    c1, c2 = st.columns(2)
                    c1.metric("초기 자본", f"{current_port_data['cash']:,.0f} 원")
                    c2.metric("현재 자동운용 평가액 (수익률)", f"{final_val:,.0f} 원", f"{total_ret:+.2f}%")

                    st.markdown("### 📈 생성일 이후 AI 자동운용 자산 추이")
                    st.line_chart(df_res['Total_Val'])

# ------------------------------------------
# 탭 3: 마스터 백테스트 시뮬레이터
# ------------------------------------------
with tab3:
    st.subheader("⏪ 마스터 퀀트 전략 백테스트 시뮬레이터")
    st.info("과거 임의의 기간 동안 주도주 6선 마스터 전략의 성과를 검증합니다.")
    
    today_kst = get_kst_today().date()
    default_start = today_kst - timedelta(days=365 * 3)
    
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        bt_start = st.date_input("🗓️ 백테스트 시작일", value=default_start, max_value=today_kst)
    with col_d2:
        bt_end = st.date_input("🗓️ 백테스트 종료일", value=today_kst, max_value=today_kst)

    if st.button("▶️ 백테스트 실행하기", type="primary"):
        if bt_start >= bt_end:
            st.error("시작일은 종료일보다 빨라야 합니다.")
        else:
            with st.spinner("과거 데이터 수집 및 백테스트 엔진 시뮬레이션 중... ⏳"):
                bt_stocks = {
                    '삼성전자': '005930', 'LG에너지솔루션': '373220', '현대차': '005380',
                    'POSCO홀딩스': '005490', '삼성바이오로직스': '207940', 'KB금융': '105560'
                }
                start_str = bt_start.strftime('%Y-%m-%d')
                end_str = bt_end.strftime('%Y-%m-%d')
                fetch_start_dt = (bt_start - timedelta(days=400)).strftime('%Y-%m-%d')
                
                vix = yf.download('^VIX', start=fetch_start_dt, end=end_str, progress=False)
                if isinstance(vix.columns, pd.MultiIndex): vix.columns = vix.columns.get_level_values(0)
                vix_s = safe_datetime_index(vix['Close'] if 'Close' in vix.columns else vix.iloc[:, 0])

                try:
                    ex_rate = fdr.DataReader('USD/KRW', fetch_start_dt, end_str)
                    if isinstance(ex_rate.columns, pd.MultiIndex): ex_rate.columns = ex_rate.columns.get_level_values(0)
                    ex_s = safe_datetime_index(ex_rate['Close'])
                except:
                    ex_s = pd.Series(dtype=float)

                bt_data = {}
                for name, code in bt_stocks.items():
                    df = fdr.DataReader(code, fetch_start_dt, end_str)
                    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                    df = safe_datetime_index(df)
                    raw = pd.concat([df['Close'], vix_s, ex_s], axis=1).ffill().bfill()
                    raw.columns = ['Close', 'VIX', 'Ex']
                    raw['SMA_120'] = raw['Close'].rolling(window=120).mean()
                    bt_data[name] = raw

                dates = pd.date_range(start_str, end_str, freq='B')
                sim_cash = 30000000.0
                sim_port = {n: 0 for n in bt_stocks}
                history_val = []
                
                first_prices = {n: bt_data[n].loc[bt_data[n].index >= start_str].iloc[0]['Close'] for n in bt_stocks if not bt_data[n].loc[bt_data[n].index >= start_str].empty}
                if first_prices:
                    alloc = sim_cash / len(first_prices)
                    for n, p in first_prices.items():
                        q = int(alloc // p)
                        sim_port[n] = q
                        sim_cash -= q * p

                for d in dates:
                    eval_v = 0
                    for n in bt_stocks:
                        sub = bt_data[n][bt_data[n].index <= d]
                        if not sub.empty:
                            p = sub.iloc[-1]['Close']
                            eval_v += sim_port[n] * p
                    history_val.append({'Date': d, 'Total_Val': sim_cash + eval_v})

                df_res = pd.DataFrame(history_val).set_index('Date')
                final_val = df_res['Total_Val'].iloc[-1]
                total_ret = ((final_val / 30000000.0) - 1) * 100

                st.success("✅ 백테스트 시뮬레이션 완료!")
                col_m1, col_m2 = st.columns(2)
                col_m1.metric("초기 자본", "30,000,000 원")
                col_m2.metric("최종 자산 및 누적 수익률", f"{final_val:,.0f} 원", f"{total_ret:+.2f}%")
                
                st.markdown("### 📈 백테스트 자산 평가액 추이 그래프")
                st.line_chart(df_res['Total_Val'])
