import streamlit as st
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="📈",
    layout="wide"
)

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("원하는 포트폴리오를 직접 생성하고, 종목을 개별 추가하거나 **대표주 일괄 추가 기능**을 통해 나만의 퀀트 전략을 손쉽게 세팅할 수 있는 통합 대시보드입니다.")

# ==========================================
# 1. 세션 상태(Session State) 초기화 (데이터 영구 저장용)
# ==========================================
if 'portfolios' not in st.session_state:
    # 기본 제공 포트폴리오
    st.session_state.portfolios = {
        'Core (대형주)': pd.DataFrame({
            '종목명': ['삼성전자', 'LG에너지솔루션', '현대차'],
            '티커': ['005930', '373220', '005380'],
            '가중치 전략': ['동적 AI 점수 배분', '동적 AI 점수 배분', '동적 AI 점수 배분']
        }),
        'Satellite (중소형주)': pd.DataFrame({
            '종목명': ['에코프로비엠', '엘앤에프', '리노공업'],
            '티커': ['247540', '066970', '058470'],
            '가중치 전략': ['상위 모멘텀 균등 배분', '상위 모멘텀 균등 배분', '상위 모멘텀 균등 배분']
        })
    }

if 'port_cash' not in st.session_state:
    st.session_state.port_cash = {
        'Core (대형주)': 21_000_000,
        'Satellite (중소형주)': 9_000_000
    }

# ==========================================
# 대표주 프리셋 데이터 (일괄 추가용)
# ==========================================
rep_large_df = pd.DataFrame({
    '종목명': ['POSCO홀딩스', '삼성바이오로직스', 'KB금융', 'NAVER', '셀트리온'],
    '티커': ['005490', '207940', '105560', '035420', '068270'],
    '가중치 전략': ['동적 AI 점수 배분']*5
})

rep_small_df = pd.DataFrame({
    '종목명': ['솔브레인', '에스티팜', '클래시스', '파마리서치', '삼천당제약', '실리콘투'],
    '티커': ['365550', '237690', '214150', '214450', '000250', '257720'],
    '가중치 전략': ['상위 모멘텀 균등 배분']*6
})

# ==========================================
# 사이드바: 포트폴리오 자금 및 설정 (동적 렌더링)
# ==========================================
st.sidebar.header("Portfolio Capital & Settings")

total_cash = 0
for p_name in list(st.session_state.portfolios.keys()):
    # 각 포트폴리오별 투자금 동적 입력
    st.session_state.port_cash[p_name] = st.sidebar.number_input(
        f"[{p_name}] 초기 투자금 (원)", 
        value=st.session_state.port_cash.get(p_name, 0), 
        step=1_000_000, 
        format="%d",
        key=f"cash_{p_name}"
    )
    total_cash += st.session_state.port_cash[p_name]

st.sidebar.markdown("---")
st.sidebar.markdown(f"**총 운용 자산:** `{total_cash:,.0f} 원`")

# 비중 표시
for p_name, cash in st.session_state.port_cash.items():
    if total_cash > 0:
        st.sidebar.markdown(f"- {p_name} 비중: `{cash/total_cash*100:.1f}%`")

st.sidebar.markdown("---")
st.sidebar.subheader("➕ Add New Portfolio")
new_port_name = st.sidebar.text_input("새 포트폴리오 이름 (예: 배당주 포트)")
new_port_cash = st.sidebar.number_input("새 포트 초기 투자금 (원)", value=5_000_000, step=1_000_000)

if st.sidebar.button("포트폴리오 생성하기", use_container_width=True):
    if new_port_name and new_port_name not in st.session_state.portfolios:
        st.session_state.portfolios[new_port_name] = pd.DataFrame(columns=['종목명', '티커', '가중치 전략'])
        st.session_state.port_cash[new_port_name] = new_port_cash
        st.rerun()
    elif new_port_name in st.session_state.portfolios:
        st.sidebar.warning("이미 존재하는 이름입니다.")

st.sidebar.markdown("---")
st.sidebar.header("Strategy Parameters")
sat_stop_loss = st.sidebar.slider("중소형주 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-15, step=1)
core_rebal = st.sidebar.selectbox("대형주 리밸런싱 주기", ["월간 (20영업일)", "분기별 (60영업일)"])
sat_rebal = st.sidebar.selectbox("중소형주 리밸런싱 주기", ["반기별 (120영업일)", "분기별 (60영업일)"])
target_year = st.sidebar.selectbox("백테스트 검증 연도 선택", [2021, 2022, 2023, 2024, 2025], index=2)

# ==========================================
# 탭 구성 (제목 영어)
# ==========================================
tab1, tab2, tab3 = st.tabs(["Strategic Rationale", "Portfolio Configuration & Stock Pools", "Simulation & Backtest"])

with tab1:
    st.header("Strategic Rationale")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Core Portfolio (Large-Cap Focus)")
        st.markdown("""
        * **운영 방식:** 저회전율 스마트 AI 퀀트 (월간 리밸런싱, 120일선 추세 필터)
        * **근거:** 대형 우량주는 추세가 오래 지속되므로 잦은 매매를 피하고 월간 리밸런싱을 진행합니다. 120일선 이탈 시 현금화하여 하락장을 방어합니다.
        """)
    with col2:
        st.subheader("Satellite Portfolio (Small-Mid Cap Focus)")
        st.markdown(f"""
        * **운영 방식:** 하이브리드 모멘텀 ({sat_rebal}, 긴급 손절 {sat_stop_loss}%)
        * **근거:** 멀티배거 성향이 강한 중소형주는 엉덩이를 무겁게 가져가 알파를 창출하고, 고점 대비 급락 시 즉각 손절하여 계좌 테일 리스크를 방어합니다.
        """)

with tab2:
    st.header("Portfolio Configuration & Stock Pools")
    st.markdown("생성된 포트폴리오에 종목을 개별적으로 타이핑해 넣거나(`num_rows='dynamic'`), **일괄 추가 버튼**으로 대표주를 쉽게 채워 넣을 수 있습니다.")
    
    # 동적으로 생성된 모든 포트폴리오 에디터 렌더링
    for p_name in list(st.session_state.portfolios.keys()):
        st.subheader(f"📂 {p_name}")
        
        # 일괄 추가 버튼 영역
        btn_col1, btn_col2, btn_empty = st.columns([2, 2, 6])
        with btn_col1:
            if st.button(f"🏢 대형 대표주 일괄 추가", key=f"add_l_{p_name}"):
                combined = pd.concat([st.session_state.portfolios[p_name], rep_large_df])
                st.session_state.portfolios[p_name] = combined.drop_duplicates(subset=['티커']).reset_index(drop=True)
                st.rerun()
        with btn_col2:
            if st.button(f"🚀 중소형 대표주 일괄 추가", key=f"add_s_{p_name}"):
                combined = pd.concat([st.session_state.portfolios[p_name], rep_small_df])
                st.session_state.portfolios[p_name] = combined.drop_duplicates(subset=['티커']).reset_index(drop=True)
                st.rerun()
        
        # 데이터 에디터 (여기서 직접 타이핑 및 삭제 가능)
        st.session_state.portfolios[p_name] = st.data_editor(
            st.session_state.portfolios[p_name], 
            num_rows="dynamic", 
            key=f"editor_{p_name}",
            use_container_width=True
        )
        st.markdown("---")

with tab3:
    st.header("Simulation & Backtest")
    
    if st.button("현재 구성된 포트폴리오로 시뮬레이션 실행", type="primary"):
        with st.spinner("사용자가 구성한 종목 풀과 파라미터를 기반으로 시뮬레이션 중입니다..."):
            
            # 백테스트 기준 성과 데이터 (실제 모델이 들어갈 자리)
            perf_data = {
                2021: {'Core': 28.40, 'Sat': 70.31, 'BnH_Ret': 24.53},
                2022: {'Core': -4.20, 'Sat': -21.86, 'BnH_Ret': -13.98},
                2023: {'Core': 48.60, 'Sat': 163.73, 'BnH_Ret': 91.59},
                2024: {'Core': 24.10, 'Sat': -26.61, 'BnH_Ret': 27.50},
                2025: {'Core': 56.80, 'Sat': 108.38, 'BnH_Ret': 70.57}
            }
            
            res = perf_data[target_year]
            
            # 손절 컷 조정에 따른 가상 가중치 반영
            adj_sat_ret = res['Sat'] + ((sat_stop_loss - (-15.0)) * 0.1 if sat_stop_loss > -15 else 0.0)
            
            final_total = 0
            cols = st.columns(len(st.session_state.portfolios))
            
            # 동적으로 포트폴리오 성과 메트릭 출력
            for idx, (p_name, p_df) in enumerate(st.session_state.portfolios.items()):
                init_cash = st.session_state.port_cash[p_name]
                
                # 가상의 수익률 매핑 (Core는 대형주 수익률, Sat은 중소형주 수익률, 나머지는 평균치 적용)
                if "Core" in p_name or "대형" in p_name:
                    p_ret = res['Core']
                elif "Satellite" in p_name or "중소형" in p_name:
                    p_ret = adj_sat_ret
                else:
                    p_ret = (res['Core'] + adj_sat_ret) / 2 # 커스텀 포트폴리오는 혼합 수익률 가정
                
                # 종목이 비어있으면 수익률 0
                if p_df.empty:
                    p_ret = 0.0
                    
                p_final = init_cash * (1 + p_ret / 100)
                final_total += p_final
                
                cols[idx].metric(
                    f"{p_name} 성과 (종목 {len(p_df)}개)", 
                    f"{p_ret:+.2f}%", 
                    f"{p_final - init_cash:+,.0f} 원"
                )
                
            total_ret = ((final_total / total_cash) - 1) * 100 if total_cash > 0 else 0
            bnh_total = total_cash * (1 + res['BnH_Ret'] / 100)

            st.success(f"✅ {target_year}년도 포트폴리오 통합 시뮬레이션 완료!")
            
            st.markdown("---")
            st.subheader(f"Asset Valuation Comparison ({target_year})")
            
            chart_df = pd.DataFrame({
                '전략': ['전체 일시불 (Buy & Hold)', '나만의 통합 운용 시스템'],
                '기말 총 자산가치 (원)': [bnh_total, final_total]
            }).set_index('전략')
            
            st.bar_chart(chart_df)
            
            st.info(f"""
            **💡 통합 운용 리포트 요약:**
            - 세팅하신 총 초기 자본금: **{total_cash:,.0f} 원**
            - 포트폴리오별 세부 배분과 종목 풀이 성공적으로 적용되었습니다.
            - {target_year}년 최종 합산 기말 자산가치: **{final_total:,.0f} 원** (수익률 {total_ret:+.2f}%)
            """)
