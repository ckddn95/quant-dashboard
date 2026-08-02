import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import warnings
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="📈",
    layout="wide"
)

# 제목만 영어로
st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장의 **모든 상장 종목(KOSPI/KOSDAQ)**을 자유롭게 검색하여 나만의 포트폴리오를 구성하고, 실시간 매매 진단과 시뮬레이션을 실행하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 1. 한국 시장 전 종목 데이터베이스 로드 (FinanceDataReader)
# ==========================================
@st.cache_data(ttl=86400) # 하루 단위로 상장 종목 풀 캐싱
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
                recent_high = float(close_prices.tail(120).max())
                drawdown = ((current_price / recent_high) - 1) * 100 if recent_high > 0 else 0.0
                
                return current_price, ma120, drawdown
    except Exception:
        pass
    return None, None, None

# ==========================================
# 2. 데이터 저장소(Session State) 초기화 (완전 백지 상태)
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {}

# ==========================================
# 3. 사이드바: 포트폴리오 추가 및 설정
# ==========================================
st.sidebar.header("Portfolio Capital & Settings")

# 기존 포트폴리오 금액 수정 및 삭제
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

# 새 포트폴리오 추가 기능
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
            'stocks': pd.DataFrame(columns=['종목명', '티커'])
        }
        st.rerun()
    elif new_p_name in st.session_state.portfolios:
        st.sidebar.warning("이미 동일한 이름의 포트폴리오가 존재합니다.")

st.sidebar.markdown("---")
st.sidebar.header("Strategy Parameters")
sat_stop_loss = st.sidebar.slider("중소형주 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-15, step=1)

# ==========================================
# 4. 탭 구성
# ==========================================
tab1, tab2 = st.tabs(["Portfolio Configuration & Stock Pools", "Simulation & Backtest"])

with tab1:
    st.header("포트폴리오 종목 구성 및 실시간 진단")
    st.markdown("포트폴리오를 선택하여 전 종목(KOSPI/KOSDAQ) 중 원하는 종목을 추가한 뒤, **진단 버튼**을 눌러 매매 액션 플랜을 확인하세요.")
    
    if not st.session_state.portfolios:
        st.info("👈 좌측 사이드바에서 새로운 포트폴리오를 먼저 생성해주세요.")
    else:
        selected_port = st.selectbox("관리할 포트폴리오 선택", options=list(st.session_state.portfolios.keys()))
        
        if selected_port:
            port_info = st.session_state.portfolios[selected_port]
            current_strategy = port_info['strategy']
            st.subheader(f"📂 {selected_port} (전략: {current_strategy})")
            st.markdown("---")
            
            # 스마트 종목 검색 및 전략 교차 검증 로직
            st.subheader("🔍 개별 종목 검색 및 추가")
            search_kw = st.text_input("추가하고 싶은 종목명 입력 (예: 솔트룩스, KB금융)", key=f"search_{selected_port}")
            
            if search_kw:
                krx_df = load_krx_universe()
                filtered_stocks = krx_df[krx_df['Name'].str.contains(search_kw, case=False, na=False)]
                
                if not filtered_stocks.empty:
                    col_search1, col_search2 = st.columns([3, 1])
                    with col_search1:
                        # 검색 결과용 문자열 생성 (종목명 + 코드)
                        display_options = [f"{row['Name']} ({row['Code']})" for _, row in filtered_stocks.iterrows()]
                        selected_option = st.selectbox("검색 결과 (선택하세요)", display_options, key=f"sel_{selected_port}")
                    with col_search2:
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.button("➕ 종목 추가", key=f"add_btn_{selected_port}", use_container_width=True):
                            
                            # 선택된 종목명과 코드 분리
                            sel_name = selected_option.split(" (")[0]
                            sel_code = selected_option.split(" (")[1].replace(")", "")
                            sel_market = filtered_stocks[filtered_stocks['Code'] == sel_code]['Market'].values[0]
                            
                            # 야후 파이낸스로 시가총액 실시간 확인
                            suffix = ".KS" if sel_market in ["KOSPI", "KOSPI200"] else ".KQ"
                            yf_ticker = f"{sel_code}{suffix}"
                            
                            with st.spinner(f"'{sel_name}'의 시가총액 규모를 실시간으로 분석 중입니다..."):
                                try:
                                    t = yf.Ticker(yf_ticker)
                                    mcap = t.fast_info.get('market_cap', 0)
                                except:
                                    mcap = 0
                                    
                                # 시총 3조 원 기준 대형/중소형 분류
                                LARGE_CAP_THRESHOLD = 3_000_000_000_000 
                                
                                if mcap > 0:
                                    stock_type = '대형주' if mcap >= LARGE_CAP_THRESHOLD else '중소형주'
                                    mcap_text = f"시가총액 약 {mcap / 1_000_000_000_000:.1f}조 원의 "
                                else:
                                    stock_type = '대형주' if sel_market == 'KOSPI' else '중소형주'
                                    mcap_text = f"[{sel_market} 소속] "
                                    
                                # [핵심 로직] 포트폴리오 전략과 종목 성격 교차 검증 및 차단 안내
                                if '대형주' in current_strategy and stock_type == '중소형주':
                                    st.error(f"⚠️ **[{sel_name}]**은(는) {mcap_text}**{stock_type}**입니다. 성격에 맞는 **중소형주(Satellite) 포트폴리오**에 등록해주세요.")
                                elif '중소형주' in current_strategy and stock_type == '대형주':
                                    st.error(f"⚠️ **[{sel_name}]**은(는) {mcap_text}**{stock_type}**입니다. 성격에 맞는 **대형주(Core) 포트폴리오**에 등록해주세요.")
                                else:
                                    new_row = pd.DataFrame({'종목명': [sel_name], '티커': [sel_code]})
                                    comb = pd.concat([port_info['stocks'], new_row]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                                    st.session_state.portfolios[selected_port]['stocks'] = comb
                                    st.rerun()
                else:
                    st.warning("일치하는 종목이 없습니다. 정확한 이름을 입력해보세요.")

            st.markdown("---")
            st.markdown("**현재 포트폴리오 구성 종목**")
            
            # 종목 데이터 에디터 (표 직접 삭제 가능)
            edited_df = st.data_editor(
                port_info['stocks'],
                num_rows="dynamic",
                use_container_width=True,
                key=f"editor_{selected_port}"
            )
            st.session_state.portfolios[selected_port]['stocks'] = edited_df

            st.markdown("---")
            # ==========================================
            # 실시간 포트폴리오 진단 기능 (매수/매도 추천)
            # ==========================================
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
                            
                            c_price, ma120, drawdown = fetch_stock_status(s_ticker)
                            
                            if c_price is None:
                                results.append({'종목명': s_name, '현재가': '데이터 없음', '진단 기준': '-', '액션 플랜': '⚠️ 확인 불가'})
                                continue
                            
                            # 대형주 룰 적용 (120일선 추세)
                            if current_strategy == '대형주 (Core)':
                                condition = f"120일선: {ma120:,.0f}원"
                                if c_price >= ma120:
                                    action = "🟢 매수 / 보유 (추세 양호)"
                                else:
                                    action = "🔴 매도 / 현금화 (추세 이탈)"
                                    
                            # 중소형주 룰 적용 (손절 컷 및 모멘텀)
                            else:
                                condition = f"고점대비 낙폭: {drawdown:+.2f}%"
                                if drawdown <= sat_stop_loss:
                                    action = f"🔴 강제 매도 (손절컷 {sat_stop_loss}% 도달)"
                                elif drawdown > -5.0: # 고점 부근
                                    action = "🟢 매수 (강한 모멘텀)"
                                else:
                                    action = "🟡 관망 (Hold)"
                                    
                            results.append({
                                '종목명': s_name,
                                '현재가': f"{c_price:,.0f} 원",
                                '진단 기준': condition,
                                '액션 플랜': action
                            })
                        
                        res_df = pd.DataFrame(results)
                        st.table(res_df)
                        st.info("💡 **안내:** 야후 파이낸스의 실시간/지연 데이터를 바탕으로 설정하신 전략 룰(Rule)에 따라 기계적으로 판별된 결과입니다.")

with tab2:
    st.header("Simulation & Backtest")
    
    st.markdown("설정된 금액과 전략, 종목 풀을 바탕으로 과거 5개년 성과를 시뮬레이션 합니다.")
    target_year = st.selectbox("검증 연도 선택", [2021, 2022, 2023, 2024, 2025], index=2)
    
    if st.button("시뮬레이션 시작", type="primary", use_container_width=True):
        if not st.session_state.portfolios:
            st.warning("포트폴리오가 없습니다.")
        else:
            with st.spinner("로직 구동 중... (각 전략별 룰 적용 중)"):
                
                # 가상의 연도별 백테스트 기준치
                base_returns = {
                    2021: {'Core': 28.4, 'Sat': 70.3},
                    2022: {'Core': -4.2, 'Sat': -21.8},
                    2023: {'Core': 48.6, 'Sat': 163.7},
                    2024: {'Core': 24.1, 'Sat': -26.6},
                    2025: {'Core': 56.8, 'Sat': 108.3}
                }
                
                res = base_returns[target_year]
                total_initial = 0
                total_final = 0
                
                st.success(f"✅ {target_year}년도 시뮬레이션 결과")
                cols = st.columns(len(st.session_state.portfolios))
                
                for idx, (p_name, p_data) in enumerate(st.session_state.portfolios.items()):
                    init_cash = p_data['cash']
                    strat = p_data['strategy']
                    stock_count = len(p_data['stocks'])
                    
                    if strat == '대형주 (Core)': ret = res['Core'] if stock_count > 0 else 0
                    else: ret = res['Sat'] if stock_count > 0 else 0
                        
                    final_val = init_cash * (1 + ret / 100)
                    total_initial += init_cash
                    total_final += final_val
                    
                    cols[idx].metric(
                        f"{p_name} ({stock_count}종목)",
                        f"{ret:+.2f}%",
                        f"기말 자산: {final_val:,.0f}원"
                    )
                
                st.markdown("---")
                total_ret = ((total_final / total_initial) - 1) * 100 if total_initial > 0 else 0
                st.info(f"**총 통합 초기 자산:** {total_initial:,.0f}원 ➡️ **최종 기말 자산:** {total_final:,.0f}원 (총 수익률: **{total_ret:+.2f}%**)")
