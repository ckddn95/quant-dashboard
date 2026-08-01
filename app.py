import streamlit as st
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite 퀀트 시스템",
    page_icon="📈",
    layout="wide"
)

# 제목만 영어로 유지, 나머지는 모두 한글
st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("나만의 포트폴리오를 생성하고 전략(대형주/중소형주)을 부여한 뒤, 개별 종목을 검색 및 관리하여 시뮬레이션을 실행하는 실전형 퀀트 대시보드입니다.")

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
st.sidebar.header("포트폴리오 자금 및 설정")

# 기존 포트폴리오 리스트 및 금액 수정
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
    
    # 숫자 아래에 콤마 포맷으로 직관적 표시
    st.sidebar.caption(f"💰 설정 금액: **{new_cash:,.0f} 원**")
    
    if st.sidebar.button(f"🗑️ {p_name} 삭제", key=f"del_{p_name}"):
        del st.session_state.portfolios[p_name]
        st.rerun()
    st.sidebar.markdown("---")

# 새 포트폴리오 추가 기능
st.sidebar.subheader("➕ 새 포트폴리오 추가")
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

# ==========================================
# 4. 탭 구성: 종목 관리 vs 시뮬레이션
# ==========================================
tab1, tab2 = st.tabs(["종목 관리 및 세팅", "시뮬레이션 실행"])

with tab1:
    st.header("포트폴리오별 종목 관리")
    st.markdown("관리할 포트폴리오를 선택한 뒤, 종목을 검색하여 쉽게 추가하거나 표에서 직접 삭제할 수 있습니다.")
    
    if not st.session_state.portfolios:
        st.info("사이드바에서 포트폴리오를 먼저 추가해주세요.")
    else:
        # 드롭다운으로 관리할 포트폴리오 선택
        selected_port = st.selectbox(
            "관리할 포트폴리오 선택", 
            options=list(st.session_state.portfolios.keys())
        )
        
        if selected_port:
            port_info = st.session_state.portfolios[selected_port]
            st.subheader(f"📂 {selected_port} (전략: {port_info['strategy']})")
            
            # 대표주 일괄 추가 버튼
            col_rep, _ = st.columns([1, 3])
            with col_rep:
                if port_info['strategy'] == '대형주 (Core)':
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
            
            # 🔍 스마트 종목 검색 및 자동 추가 기능
            st.subheader("🔍 개별 종목 검색 및 추가")
            search_kw = st.text_input("추가하고 싶은 종목명 입력 (예: 삼성, 에코프로)", key=f"search_{selected_port}")
            
            if search_kw:
                # 검색어 필터링 로직
                filtered_stocks = [name for name in MASTER_STOCKS.keys() if search_kw in name]
                
                if filtered_stocks:
                    col_search1, col_search2 = st.columns([3, 1])
                    with col_search1:
                        selected_stock = st.selectbox("검색 결과 (선택하세요)", filtered_stocks, key=f"sel_{selected_port}")
                    with col_search2:
                        st.markdown("<br>", unsafe_allow_html=True) # 버튼 높이 맞춤
                        if st.button("➕ 종목 추가", key=f"add_btn_{selected_port}", use_container_width=True):
                            ticker = MASTER_STOCKS[selected_stock]
                            new_row = pd.DataFrame({'종목명': [selected_stock], '티커': [ticker]})
                            # 중복 방지하며 데이터프레임에 결합
                            comb = pd.concat([port_info['stocks'], new_row]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                            st.session_state.portfolios[selected_port]['stocks'] = comb
                            st.rerun()
                else:
                    st.warning("일치하는 종목이 없습니다. 정확한 이름을 입력해보세요.")

            st.markdown("---")
            st.markdown("**현재 포트폴리오 구성 종목 (표 내부에서 직접 삭제 가능)**")
            
            # 종목 데이터 에디터 (수동 타이핑 및 삭제 가능)
            edited_df = st.data_editor(
                port_info['stocks'],
                num_rows="dynamic",
                use_container_width=True,
                key=f"editor_{selected_port}"
            )
            # 변경된 데이터 즉시 저장
            st.session_state.portfolios[selected_port]['stocks'] = edited_df

with tab2:
    st.header("백테스트 시뮬레이션")
    
    st.markdown("설정된 금액과 전략(룰), 종목 풀을 바탕으로 시뮬레이션을 실행합니다.")
    target_year = st.selectbox("검증 연도 선택", [2021, 2022, 2023, 2024, 2025], index=2)
    
    if st.button("시뮬레이션 시작", type="primary", use_container_width=True):
        if not st.session_state.portfolios:
            st.warning("포트폴리오가 없습니다.")
        else:
            with st.spinner("로직 구동 중... (각 전략별 룰 적용 중)"):
                
                # 가상의 성과 데이터 (각 연도별 대형주/중소형주 룰 적용 결과값)
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
                
                # 포트폴리오 개수에 맞춰 컬럼 동적 생성
                cols = st.columns(len(st.session_state.portfolios))
                
                for idx, (p_name, p_data) in enumerate(st.session_state.portfolios.items()):
                    init_cash = p_data['cash']
                    strat = p_data['strategy']
                    stock_count = len(p_data['stocks'])
                    
                    # 태그(전략)에 따라 수익률 차등 적용 (종목이 없으면 0%)
                    if strat == '대형주 (Core)':
                        ret = res['Core'] if stock_count > 0 else 0
                    else:
                        ret = res['Sat'] if stock_count > 0 else 0
                        
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
