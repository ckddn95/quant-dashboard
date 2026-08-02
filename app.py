import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import json
import warnings
import datetime
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="📈",
    layout="wide"
)

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장의 **모든 상장 종목(KOSPI/KOSDAQ)**을 검색하여 포트폴리오를 구성하고, **'신규 진입'과 '보유/손절' 진단** 및 **실제 과거 데이터 기반의 가상 매매 시뮬레이션**을 실행하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 1. 한국 시장 전 종목 데이터베이스 로드 (FinanceDataReader)
# ==========================================
@st.cache_data(ttl=86400)
def load_krx_universe():
    try:
        df = fdr.StockListing('KRX')
        df = df.dropna(subset=['Code', 'Name'])
        return df
    except Exception as e:
        st.error("종목 데이터를 불러오는데 실패했습니다.")
        return pd.DataFrame(columns=['Code', 'Name', 'Market'])

# ==========================================
# 실시간 주가 데이터 수집 및 지표 계산 함수
# ==========================================
@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code):
    try:
        for suffix in ['.KS', '.KQ']:
            df = yf.download(f"{ticker_code}{suffix}", period="1y", progress=False)
            if not df.empty and len(df) > 0:
                if isinstance(df.columns, pd.MultiIndex): 
                    df.columns = df.columns.get_level_values(0)
                
                close_prices = df['Close'].dropna()
                if len(close_prices) == 0: continue
                
                current_price = float(close_prices.iloc[-1])
                ma120 = float(close_prices.rolling(window=120).mean().iloc[-1]) if len(close_prices) >= 120 else current_price
                ma20 = float(close_prices.rolling(window=20).mean().iloc[-1]) if len(close_prices) >= 20 else current_price
                recent_high = float(close_prices.tail(120).max())
                drawdown = ((current_price / recent_high) - 1) * 100 if recent_high > 0 else 0.0
                
                return current_price, ma120, ma20, drawdown
    except Exception:
        pass
    return None, None, None, None

# ==========================================
# 2. 데이터 저장 및 불러오기 로직 (JSON)
# ==========================================
def convert_state_to_json():
    save_data = {}
    for p_name, p_data in st.session_state.portfolios.items():
        save_data[p_name] = {
            'strategy': p_data['strategy'],
            'cash': p_data['cash'],
            'stocks': p_data['stocks'].to_dict(orient='records')
        }
    return json.dumps(save_data, ensure_ascii=False, indent=2)

def load_json_to_state(json_file):
    try:
        loaded_data = json.load(json_file)
        new_portfolios = {}
        for p_name, p_data in loaded_data.items():
             new_portfolios[p_name] = {
                 'strategy': p_data['strategy'],
                 'cash': p_data['cash'],
                 'stocks': pd.DataFrame(p_data['stocks'])
             }
        st.session_state.portfolios = new_portfolios
        st.success("데이터 불러오기 성공!")
        st.rerun()
    except Exception as e:
        st.error(f"데이터 불러오기 실패: {e}")

# ==========================================
# 3. 데이터 저장소(Session State) 초기화
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {}

# ==========================================
# 4. 사이드바: 데이터 관리 및 포트폴리오 설정
# ==========================================
st.sidebar.header("💾 Data Management")

json_str = convert_state_to_json()
st.sidebar.download_button(
    label="⬇️ 현재 포트폴리오 백업 저장",
    data=json_str,
    file_name="quant_portfolio_backup.json",
    mime="application/json",
    use_container_width=True
)

uploaded_file = st.sidebar.file_uploader("⬆️ 백업 데이터 불러오기 (JSON 파일)", type=['json'])
if uploaded_file is not None:
    if st.sidebar.button("📂 데이터 복구 실행", use_container_width=True):
        load_json_to_state(uploaded_file)

st.sidebar.markdown("---")
st.sidebar.header("Portfolio Capital & Settings")

for p_name, p_data in list(st.session_state.portfolios.items()):
    strat = p_data['strategy']
    st.sidebar.markdown(f"**[{strat}] {p_name}**")
    
    new_cash = st.sidebar.number_input(
        f"{p_name} 투자금",
        value=p_data['cash'],
        step=1_000_000,
        format="%d",
        key=f"cash_input_{p_name}",
        label_visibility="collapsed"
    )
    st.session_state.portfolios[p_name]['cash'] = new_cash
    st.sidebar.caption(f"💰 설정 금액: **{new_cash:,.0f} 원**")
    
    if st.sidebar.button(f"🗑️ {p_name} 삭제", key=f"del_{p_name}"):
        del st.session_state.portfolios[p_name]
        st.rerun()
    st.sidebar.markdown("---")

st.sidebar.subheader("➕ Add New Portfolio")
new_p_name = st.sidebar.text_input("새 포트폴리오 이름 (예: 단기 모멘텀)", key="new_p_name")
new_p_strat = st.sidebar.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"], key="new_p_strat")
new_p_cash = st.sidebar.number_input("초기 투자금", value=10_000_000, step=1_000_000, format="%d", key="new_p_cash")
st.sidebar.caption(f"💰 예정 금액: **{new_p_cash:,.0f} 원**")

if st.sidebar.button("포트폴리오 생성하기", use_container_width=True):
    if new_p_name and new_p_name not in st.session_state.portfolios:
        st.session_state.portfolios[new_p_name] = {
            'strategy': new_p_strat,
            'cash': new_p_cash,
            'stocks': pd.DataFrame(columns=['종목명', '티커', '매수단가', '보유수량'])
        }
        st.rerun()
    elif new_p_name in st.session_state.portfolios:
        st.sidebar.warning("이미 동일한 이름의 포트폴리오가 존재합니다.")

st.sidebar.markdown("---")
st.sidebar.header("Strategy Parameters")
sat_stop_loss = st.sidebar.slider("중소형주 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-15, step=1)

# ==========================================
# 5. 탭 구성
# ==========================================
tab1, tab2 = st.tabs(["Portfolio Configuration & Stock Pools", "Simulation & Backtest"])

with tab1:
    st.header("포트폴리오 종목 구성 및 실시간 진단")
    st.markdown("종목을 추가한 뒤, 실제 보유 중이라면 **'매수단가'**와 **'보유수량'**을 입력하세요. 진단 시 신규 진입 여부와 손절 여부를 명확히 구분해 줍니다.")
    
    if not st.session_state.portfolios:
        st.info("👈 좌측 사이드바에서 새로운 포트폴리오를 먼저 생성해주세요.")
    else:
        selected_port = st.selectbox("관리할 포트폴리오 선택", options=list(st.session_state.portfolios.keys()), key="tab1_port")
        
        if selected_port:
            port_info = st.session_state.portfolios[selected_port]
            current_strategy = port_info['strategy']
            st.subheader(f"📂 {selected_port} (전략: {current_strategy})")
            st.markdown("---")
            
            st.subheader("🔍 개별 종목 검색 및 추가")
            search_kw = st.text_input("추가하고 싶은 종목명 입력 (예: 솔트룩스, KB금융)", key=f"search_{selected_port}")
            
            if search_kw:
                krx_df = load_krx_universe()
                filtered_stocks = krx_df[krx_df['Name'].str.contains(search_kw, case=False, na=False)]
                
                if not filtered_stocks.empty:
                    col_search1, col_search2 = st.columns([3, 1])
                    with col_search1:
                        display_options = [f"{row['Name']} ({row['Code']})" for _, row in filtered_stocks.iterrows()]
                        selected_option = st.selectbox("검색 결과 (선택하세요)", display_options, key=f"sel_{selected_port}")
                    with col_search2:
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.button("➕ 종목 추가", key=f"add_btn_{selected_port}", use_container_width=True):
                            
                            sel_name = selected_option.split(" (")[0]
                            sel_code = selected_option.split(" (")[1].replace(")", "")
                            sel_market = filtered_stocks[filtered_stocks['Code'] == sel_code]['Market'].values[0]
                            
                            suffix = ".KS" if sel_market in ["KOSPI", "KOSPI200"] else ".KQ"
                            yf_ticker = f"{sel_code}{suffix}"
                            
                            with st.spinner(f"'{sel_name}'의 시가총액 규모를 실시간으로 분석 중입니다..."):
                                try:
                                    t = yf.Ticker(yf_ticker)
                                    mcap = t.fast_info.get('market_cap', 0)
                                except:
                                    mcap = 0
                                    
                                LARGE_CAP_THRESHOLD = 3_000_000_000_000 
                                
                                if mcap > 0:
                                    stock_type = '대형주' if mcap >= LARGE_CAP_THRESHOLD else '중소형주'
                                    mcap_text = f"시가총액 약 {mcap / 1_000_000_000_000:.1f}조 원의 "
                                else:
                                    stock_type = '대형주' if sel_market == 'KOSPI' else '중소형주'
                                    mcap_text = f"[{sel_market} 소속] "
                                    
                                if '대형주' in current_strategy and stock_type == '중소형주':
                                    st.error(f"⚠️ **[{sel_name}]**은(는) {mcap_text}**{stock_type}**입니다. 성격에 맞는 **중소형주(Satellite) 포트폴리오**에 등록해주세요.")
                                elif '중소형주' in current_strategy and stock_type == '대형주':
                                    st.error(f"⚠️ **[{sel_name}]**은(는) {mcap_text}**{stock_type}**입니다. 성격에 맞는 **대형주(Core) 포트폴리오**에 등록해주세요.")
                                else:
                                    new_row = pd.DataFrame({'종목명': [sel_name], '티커': [sel_code], '매수단가': [0], '보유수량': [0]})
                                    comb = pd.concat([port_info['stocks'], new_row]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                                    st.session_state.portfolios[selected_port]['stocks'] = comb
                                    st.rerun()
                else:
                    st.warning("일치하는 종목이 없습니다. 정확한 이름을 입력해보세요.")

            st.markdown("---")
            st.markdown("**현재 포트폴리오 구성 종목 (보유 중이라면 '매수단가'와 '보유수량'을 표에 직접 입력하세요)**")
            
            edited_df = st.data_editor(
                port_info['stocks'],
                num_rows="dynamic",
                use_container_width=True,
                key=f"editor_{selected_port}"
            )
            st.session_state.portfolios[selected_port]['stocks'] = edited_df

            st.markdown("---")
            st.subheader("🩺 실시간 매매 액션 플랜 진단")
            
            if st.button("현재 포트폴리오 진단 실행", type="primary"):
                if edited_df.empty:
                    st.warning("진단할 종목이 없습니다. 먼저 종목을 추가해주세요.")
                else:
                    with st.spinner("야후 파이낸스에서 실시간 데이터를 분석 중입니다..."):
                        results = []
                        for idx, row in edited_df.iterrows():
                            s_name = row['종목명']
                            s_ticker = row['티커']
                            
                            buy_price = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                            quantity = pd.to_numeric(row.get('보유수량', 0), errors='coerce')
                            if pd.isna(buy_price): buy_price = 0
                            if pd.isna(quantity): quantity = 0
                            
                            is_holding = (quantity > 0) and (buy_price > 0)
                            holding_status = "보유중" if is_holding else "신규/관심"
                            
                            c_price, ma120, ma20, drawdown = fetch_stock_status(s_ticker)
                            
                            if c_price is None:
                                results.append({'종목명': s_name, '상태': holding_status, '현재가': '데이터 없음', '진단 기준': '-', '액션 플랜': '⚠️ 확인 불가'})
                                continue
                            
                            if current_strategy == '대형주 (Core)':
                                condition = f"120일선: {ma120:,.0f}원"
                                if is_holding: 
                                    if c_price >= ma120: action = "🟢 보유 유지 (추세 양호)"
                                    else: action = "🔴 매도 / 현금화 (120일선 이탈)"
                                else: 
                                    if c_price >= ma120: action = "🟢 신규 진입 가능 (추세 양호)"
                                    else: action = "🟡 진입 보류 / 관망 (하락 추세)"
                            else:
                                if is_holding: 
                                    user_ret = ((c_price / buy_price) - 1) * 100
                                    condition = f"내 수익률: {user_ret:+.2f}%"
                                    if user_ret <= sat_stop_loss: action = f"🔴 강제 매도 (설정 손절컷 {sat_stop_loss}% 도달)"
                                    elif user_ret > 0: action = "🟢 보유 유지 (수익권 순항)"
                                    else: action = "🟡 보유 유지 (손실권, 관망)"
                                else: 
                                    condition = f"20일선: {ma20:,.0f}원 / 낙폭: {drawdown:+.2f}%"
                                    if c_price >= ma20 and drawdown >= -15.0: action = "🟢 신규 진입 가능 (단기 모멘텀 양호)"
                                    else: action = "🟡 진입 보류 / 관망 (모멘텀 부족 또는 하락 추세)"
                                    
                            results.append({
                                '종목명': s_name,
                                '상태': holding_status,
                                '현재가': f"{c_price:,.0f} 원",
                                '진단 기준': condition,
                                '액션 플랜': action
                            })
                        
                        st.table(pd.DataFrame(results))
                        st.info("💡 **안내:** 사이드바 상단의 **'현재 포트폴리오 백업 저장'**을 누르면 세팅이 PC에 저장됩니다.")


with tab2:
    st.header("Simulation & Backtest")
    st.markdown("선택한 포트폴리오와 기간에 대해 **실제 과거 주가 데이터**를 바탕으로 가상 매매 시뮬레이션을 진행하고 상세한 근거를 확인합니다.")

    if not st.session_state.portfolios:
        st.warning("포트폴리오가 없습니다. 먼저 포트폴리오와 종목을 추가해주세요.")
    else:
        col_sim1, col_sim2, col_sim3 = st.columns(3)
        with col_sim1:
            sim_port = st.selectbox("시뮬레이션 할 포트폴리오 선택", list(st.session_state.portfolios.keys()), key="sim_port")
        with col_sim2:
            start_date = st.date_input("시작일", datetime.date(2023, 1, 1))
        with col_sim3:
            end_date = st.date_input("종료일", datetime.date.today())

        if st.button("가상 매매 시뮬레이션 시작", type="primary", use_container_width=True):
            port_data = st.session_state.portfolios[sim_port]
            stocks = port_data['stocks']
            strat = port_data['strategy']
            init_cash = port_data['cash']

            if stocks.empty:
                st.error("해당 포트폴리오에 등록된 종목이 없습니다. 종목을 먼저 추가해주세요.")
            else:
                with st.spinner("야후 파이낸스에서 과거 주가 데이터를 수집하고 가상 매매 룰을 적용 중입니다..."):
                    fetch_start = start_date - datetime.timedelta(days=200) # 120일선 계산을 위한 여유 기간 확보
                    all_rets = []
                    trade_logs = []
                    
                    for idx, row in stocks.iterrows():
                        ticker = row['티커']
                        name = row['종목명']
                        
                        df = None
                        for suf in ['.KS', '.KQ']:
                            temp_df = yf.download(f"{ticker}{suf}", start=fetch_start, end=end_date, progress=False)
                            if not temp_df.empty:
                                if isinstance(temp_df.columns, pd.MultiIndex):
                                    temp_df.columns = temp_df.columns.get_level_values(0)
                                df = temp_df
                                break
                        
                        if df is None or df.empty:
                            trade_logs.append({'종목명': name, '가상 누적 수익률': 'N/A', '매매 횟수': '-', '상세 근거 (적용 룰)': '데이터 수집 불가'})
                            continue
                            
                        df['Close'] = df['Close'].ffill()
                        df['Daily_Ret'] = df['Close'].pct_change()
                        
                        # 룰(Rule)에 따른 기계적 매매 시그널 계산
                        if strat == '대형주 (Core)':
                            df['MA120'] = df['Close'].rolling(120).mean()
                            df['Signal'] = np.where(df['Close'] > df['MA120'], 1, 0) # 120일선 위면 매수/보유, 아래면 현금화
                            rationale = "120일 이동평균선 상회 시 보유, 하회 시 전량 현금화"
                        else:
                            df['Roll_Max'] = df['Close'].rolling(window=120, min_periods=1).max()
                            df['Drawdown'] = (df['Close'] / df['Roll_Max']) - 1
                            stop_loss_pct = sat_stop_loss / 100.0
                            df['Signal'] = np.where(df['Drawdown'] > stop_loss_pct, 1, 0) # 손절컷 도달 시 매도(0), 아닐시 보유(1)
                            rationale = f"120일 내 고점 대비 {sat_stop_loss}% 하락 시 즉시 현금화 (강제 손절)"

                        # 익일 시초가 기준 매매 가정 (Shift 1)
                        df['Strat_Ret'] = df['Signal'].shift(1) * df['Daily_Ret']
                        
                        sim_df = df.loc[start_date:end_date].copy()
                        if sim_df.empty: continue
                            
                        # 개별 종목 누적 수익률
                        sim_df['Cum_Ret'] = (1 + sim_df['Strat_Ret'].fillna(0)).cumprod()
                        final_ret = (sim_df['Cum_Ret'].iloc[-1] - 1) * 100
                        
                        # 시그널 변동을 추적하여 매수/매도 횟수 카운팅
                        signal_diff = sim_df['Signal'].diff().fillna(0)
                        buy_count = (signal_diff == 1).sum()
                        sell_count = (signal_diff == -1).sum()
                        
                        all_rets.append(sim_df['Strat_Ret'].rename(name))
                        
                        trade_logs.append({
                            '종목명': name,
                            '가상 누적 수익률': f"{final_ret:+.2f}%",
                            '매매 횟수': f"진입(매수) {buy_count}회 / 청산(매도) {sell_count}회",
                            '상세 근거 (적용 룰)': rationale
                        })
                        
                    if not all_rets:
                        st.warning("선택하신 기간의 유효한 주가 데이터가 없습니다.")
                    else:
                        # 포트폴리오 통합 성과 (동일비중 가정)
                        port_ret_df = pd.concat(all_rets, axis=1).fillna(0)
                        port_ret_df['Portfolio_Ret'] = port_ret_df.mean(axis=1)
                        port_ret_df['Cum_Portfolio'] = (1 + port_ret_df['Portfolio_Ret']).cumprod()
                        
                        final_port_ret = (port_ret_df['Cum_Portfolio'].iloc[-1] - 1) * 100
                        final_asset = init_cash * (1 + final_port_ret / 100)
                        
                        st.success(f"✅ {start_date} ~ {end_date} 기간 시뮬레이션 완료!")
                        
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 통합 초기 자산", f"{init_cash:,.0f} 원")
                        col_r2.metric(f"최종 기말 자산 (수익률)", f"{final_asset:,.0f} 원", f"{final_port_ret:+.2f}%")
                        
                        st.markdown("---")
                        st.subheader("📈 포트폴리오 자산 추이 차트 (단위: 원)")
                        chart_data = port_ret_df[['Cum_Portfolio']] * init_cash
                        chart_data.columns = [f"{sim_port} 자산 흐름"]
                        st.line_chart(chart_data)
                        
                        st.markdown("---")
                        st.subheader("📋 종목별 가상 매매 결과 및 상세 근거")
                        st.table(pd.DataFrame(trade_logs))
                        st.info("💡 위 매매 횟수는 지정된 기간 동안 설정된 전략(Rule)에 의해 시스템이 기계적으로 매수와 매도를 반복한 횟수를 의미합니다.")
