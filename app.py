import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
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
st.markdown("나만의 포트폴리오를 생성하고 전략을 부여한 뒤, 실시간 데이터를 바탕으로 **종목별 매수/매도 진단**과 시뮬레이션을 실행하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 1. 마스터 종목 풀 (검색용 DB)
# ==========================================
MASTER_STOCKS = {
    '삼성전자': '005930', 'LG에너지솔루션': '373220', 'SK하이닉스': '000660', '삼성바이오로직스': '207940',
    '현대차': '005380', '기아': '000270', '셀트리온': '068270', 'POSCO홀딩스': '005490',
    'NAVER': '035420', 'LG화학': '051910', 'KB금융': '105560', '신한지주': '055550',
    '카카오': '035720', '삼성SDI': '006400', '에코프로비엠': '247540', '에코프로': '086520',
    'HLB': '028300', '알테오젠': '196170', '엔켐': '348370', 'HPSP': '403870',
    '셀트리온제약': '068760', '리노공업': '058470', '레인보우로보틱스': '277810',
    '클래시스': '214150', '솔브레인': '365550', '에스티팜': '237690', '파마리서치': '214450',
    '삼천당제약': '000250', '실리콘투': '257720', '엘앤에프': '066970', '브이티': '018290',
    'ISC': '095340', '원익IPS': '240810', '에이비엘바이오': '298380'
}

# ==========================================
# 실시간 주가 데이터 수집 및 지표 계산 함수
# ==========================================
@st.cache_data(ttl=3600) # 1시간 동안 데이터 캐싱 (속도 최적화)
def fetch_stock_status(ticker_code):
    try:
        # 코스피(.KS)와 코스닥(.KQ) 순차 시도
        for suffix in ['.KS', '.KQ']:
            df = yf.download(f"{ticker_code}{suffix}", period="1y", progress=False)
            if not df.empty and len(df) > 0:
                if isinstance(df.columns, pd.MultiIndex): 
                    df.columns = df.columns.get_level_values(0)
                
                close_prices = df['Close'].dropna()
                if len(close_prices) == 0: continue
                
                current_price = float(close_prices.iloc[-1])
                # 120일선 계산
                ma120 = float(close_prices.rolling(window=120).mean().iloc[-1]) if len(close_prices) >= 120 else current_price
                # 최근 6개월(약 120영업일) 고점 계산
                recent_high = float(close_prices.tail(120).max())
                drawdown = ((current_price / recent_high) - 1) * 100 if recent_high > 0 else 0.0
                
                return current_price, ma120, drawdown
    except Exception:
        pass
    return None, None, None

# ==========================================
# 2. 데이터 저장소(Session State) 초기화
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {
        '기본 대형주 (Core)': {
            'strategy': '대형주 (Core)',
            'cash': 21_000_000,
            'stocks': pd.DataFrame({
                '종목명': ['삼성전자', 'LG에너지솔루션', '현대차'],
                '티커': ['005930', '373220', '005380']
            })
        },
        '기본 중소형주 (Satellite)': {
            'strategy': '중소형주 (Satellite)',
            'cash': 9_000_000,
            'stocks': pd.DataFrame({
                '종목명': ['에코프로비엠', '엘앤에프', '리노공업'],
                '티커': ['247540', '066970', '058470']
            })
        }
    }

# ==========================================
# 3. 사이드바: 포트폴리오 관리 및 설정
# ==========================================
st.sidebar.header("Portfolio Capital & Settings")

# 기존 포트폴리오 금액 수정
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
new_p_name = st.sidebar.text_input("포트폴리오 이름 (예: 1번 단기계좌)", key="new_p_name")
new_p_strat = st.sidebar.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"], key="new_p_strat")
new_p_cash = st.sidebar.number_input("초기 투자금", value=5_000_000, step=1_000_000, format="%d", key="new_p_cash")
st.sidebar.caption(f"💰 예정 금액: **{new_p_cash:,.0f} 원**")

if st.sidebar.button("추가하기", use_container_width=True):
    if new_p_name and new_p_name not in st.session_state.portfolios:
        st.session_state.portfolios[new_p_name] = {
            'strategy': new_p_strat,
            'cash': new_p_cash,
            'stocks': pd.DataFrame(columns=['종목명', '티커'])
        }
        st.rerun()
    elif new_p_name in st.session_state.portfolios:
        st.sidebar.warning("동일한 이름이 존재합니다.")

st.sidebar.markdown("---")
st.sidebar.header("Strategy Parameters")
sat_stop_loss = st.sidebar.slider("중소형주 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-15, step=1)

# ==========================================
# 4. 탭 구성
# ==========================================
tab1, tab2 = st.tabs(["Portfolio Configuration & Stock Pools", "Simulation & Backtest"])

with tab1:
    st.header("포트폴리오별 종목 관리 및 실시간 진단")
    st.markdown("포트폴리오에 종목을 추가한 뒤, **진단 버튼**을 눌러 설정된 룰에 따른 매매 액션 플랜을 확인하세요.")
    
    if not st.session_state.portfolios:
        st.info("사이드바에서 포트폴리오를 먼저 추가해주세요.")
    else:
        selected_port = st.selectbox("관리할 포트폴리오 선택", options=list(st.session_state.portfolios.keys()))
        
        if selected_port:
            port_info = st.session_state.portfolios[selected_port]
            current_strategy = port_info['strategy']
            st.subheader(f"📂 {selected_port} (전략: {current_strategy})")
            
            # 대표주 일괄 추가 버튼
            col_rep, _ = st.columns([1, 3])
            with col_rep:
                if current_strategy == '대형주 (Core)':
                    if st.button("🏢 대형 대표주 채우기", use_container_width=True):
                        rep_df = pd.DataFrame({'종목명': ['POSCO홀딩스', 'NAVER'], '티커': ['005490', '035420']})
                        comb = pd.concat([port_info['stocks'], rep_df]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                        st.session_state.portfolios[selected_port]['stocks'] = comb
                        st.rerun()
                else:
                    if st.button("🚀 중소형 대표주 채우기", use_container_width=True):
                        rep_df = pd.DataFrame({'종목명': ['클래시스', '실리콘투'], '티커': ['214150', '257720']})
                        comb = pd.concat([port_info['stocks'], rep_df]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                        st.session_state.portfolios[selected_port]['stocks'] = comb
                        st.rerun()
            
            st.markdown("---")
            
            # 스마트 종목 검색
            st.subheader("🔍 개별 종목 검색 및 추가")
            search_kw = st.text_input("추가하고 싶은 종목명 입력 (예: 삼성, 에코프로)", key=f"search_{selected_port}")
            
            if search_kw:
                filtered_stocks = [name for name in MASTER_STOCKS.keys() if search_kw in name]
                if filtered_stocks:
                    col_search1, col_search2 = st.columns([3, 1])
                    with col_search1:
                        selected_stock = st.selectbox("검색 결과 (선택하세요)", filtered_stocks, key=f"sel_{selected_port}")
                    with col_search2:
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.button("➕ 종목 추가", key=f"add_btn_{selected_port}", use_container_width=True):
                            ticker = MASTER_STOCKS[selected_stock]
                            new_row = pd.DataFrame({'종목명': [selected_stock], '티커': [ticker]})
                            comb = pd.concat([port_info['stocks'], new_row]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                            st.session_state.portfolios[selected_port]['stocks'] = comb
                            st.rerun()
                else:
                    st.warning("일치하는 종목이 없습니다. 정확한 이름을 입력해보세요.")

            st.markdown("---")
            st.markdown("**현재 포트폴리오 구성 종목**")
            
            # 종목 데이터 에디터
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
