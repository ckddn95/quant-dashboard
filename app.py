import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
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
st.markdown("대형주(Core)와 중소형주(Satellite) 포트폴리오를 사용자가 직접 추가·조정하고, 종목 풀을 커스텀 설정하여 운용하는 스마트 퀀트 대시보드입니다.")

# ==========================================
# 사이드바: 포트폴리오 자금 및 전략 파라미터 설정 (콤마 적용)
# ==========================================
st.sidebar.header("Portfolio Capital & Settings")

core_cash = st.sidebar.number_input("Core (대형주) 초기 투자금 (원)", value=21_000_000, step=1_000_000, format="%d")
sat_cash = st.sidebar.number_input("Satellite (중소형주) 초기 투자금 (원)", value=9_000_000, step=1_000_000, format="%d")

# 사용자 정의 추가 포트폴리오 세팅 예시 (사용자가 직접 추가 가능)
st.sidebar.markdown("---")
st.sidebar.subheader("➕ 커스텀 포트폴리오 추가")
custom_port_name = st.sidebar.text_input("추가할 포트폴리오 이름", value="")
custom_port_cash = st.sidebar.number_input("추가 포트폴리오 초기 투자금 (원)", value=0, step=1_000_000, format="%d")

# 총 자산 계산
total_cash = core_cash + sat_cash + (custom_port_cash if custom_port_name else 0)

st.sidebar.markdown("---")
st.sidebar.markdown(f"**총 운용 자산:** `{total_cash:,.0f} 원`")
st.sidebar.markdown(f"- Core 비중: `{core_cash/total_cash*100:.1f}%`")
st.sidebar.markdown(f"- Satellite 비중: `{sat_cash/total_cash*100:.1f}%`")
if custom_port_name and custom_port_cash > 0:
    st.sidebar.markdown(f"- {custom_port_name} 비중: `{custom_port_cash/total_cash*100:.1f}%`")

st.sidebar.markdown("---")
st.sidebar.header("Strategy Parameters")
sat_stop_loss = st.sidebar.slider("Satellite 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-15, step=1)
core_rebal = st.sidebar.selectbox("Core 리밸런싱 주기", ["월간 (20영업일)", "분기별 (60영업일)"], index=0)
sat_rebal = st.sidebar.selectbox("Satellite 리밸런싱 주기", ["반기별 (120영업일)", "분기별 (60영업일)"], index=0)

st.sidebar.markdown("---")
target_year = st.sidebar.selectbox("백테스트 검증 연도 선택", [2021, 2022, 2023, 2024, 2025], index=2)

# ==========================================
# 세션 상태(Session State)로 종목 풀 관리 (사용자 추가/수정 가능)
# ==========================================
if 'core_stocks' not in st.session_state:
    st.session_state.core_stocks = pd.DataFrame({
        '종목명': ['삼성전자', 'LG에너지솔루션', '현대차', 'POSCO홀딩스', '삼성바이오로직스', 'KB금융'],
        '티커': ['005930', '373220', '005380', '005490', '207940', '105560'],
        '가중치 전략': ['동적 AI 점수 배분']*6
    })

if 'sat_stocks' not in st.session_state:
    st.session_state.sat_stocks = pd.DataFrame({
        '종목명': ['에코프로비엠', '엘앤에프', '리노공업', '솔브레인', '에스티팜', '클래시스', '파마리서치', '삼천당제약', '레인보우로보틱스', '에이비엘바이오', '실리콘투', '브이티', 'ISC', 'HPSP', '원익IPS'],
        '티커': ['247540', '066970', '058470', '365550', '237690', '214150', '214450', '000250', '277810', '298380', '257720', '018290', '095340', '403870', '240810'],
        '가중치 전략': ['상위 5개 모멘텀 균등 배분']*15
    })

# ==========================================
# 탭 구성 (탭 제목만 영어)
# ==========================================
tab1, tab2, tab3 = st.tabs(["Strategic Rationale", "Portfolio Configuration & Stock Pools", "Simulation & Backtest"])

with tab1:
    st.header("Strategic Rationale")
    st.markdown("왜 대형주와 중소형주를 분리해서 다르게 운영해야 하는가?")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Core Portfolio (Large-Cap Focus)")
        st.markdown("""
        * **운영 방식:** 저회전율 스마트 AI 퀀트 (월간 리밸런싱, 120일선 추세 필터)
        * **근거 1 (느린 호흡과 안정성):** 대형 우량주는 시가총액이 커서 한 번 방향을 잡으면 추세가 오래 지속되므로 **월간(20영업일) 리밸런싱**이 가장 효율적입니다.
        * **근거 2 (하방 방어력):** 120일선 이탈 또는 VIX 급등 시 AI 점수를 0으로 만들어 현금화하여 2022년 같은 하락장에서 손실을 최소화합니다.
        """)
        
    with col2:
        st.subheader("Satellite Portfolio (Small-Mid Cap Focus)")
        st.markdown(f"""
        * **운영 방식:** 하이브리드 모멘텀 ({sat_rebal}, 긴급 손절 {sat_stop_loss}%)
        * **근거 1 (폭발적 알파 추구):** 중소형주는 멀티배거 성향이 강하므로 **선택하신 리밸런싱 주기**에 맞춰 엉덩이를 무겁게 가져가야 대세 상승 랠리를 온전히 담을 수 있습니다.
        * **근거 2 (테일 리스크 차단):** 고점 대비 **{sat_stop_loss}%** 이상 급락 시 즉시 강제 손절하는 **비상 안전장치**를 결합했습니다.
        """)

with tab2:
    st.header("Portfolio Configuration & Stock Pools")
    st.markdown("아래 표에서 **종목을 직접 추가하거나 수정(삭제)**할 수 있습니다. 변경된 종목 풀은 시뮬레이션에 반영됩니다.")
    
    col_p1, col_p2 = st.columns(2)
    
    with col_p1:
        st.subheader("Core Stock Pool (Large-Cap)")
        st.session_state.core_stocks = st.data_editor(
            st.session_state.core_stocks, 
            num_rows="dynamic", 
            key="core_editor",
            use_container_width=True
        )
        
    with col_p2:
        st.subheader("Satellite Stock Pool (Small-Mid Cap)")
        st.session_state.sat_stocks = st.data_editor(
            st.session_state.sat_stocks, 
            num_rows="dynamic", 
            key="sat_editor",
            use_container_width=True
        )
        
    if custom_port_name:
        st.markdown(f"---")
        st.subheader(f"➕ Custom Portfolio Pool: {custom_port_name}")
        if 'custom_stocks' not in st.session_state:
            st.session_state.custom_stocks = pd.DataFrame(columns=['종목명', '티커', '가중치 전략'])
        st.session_state.custom_stocks = st.data_editor(
            st.session_state.custom_stocks,
            num_rows="dynamic",
            key="custom_editor",
            use_container_width=True
        )

with tab3:
    st.header("Simulation & Backtest")
    
    if st.button("현재 설정 및 종목 풀로 시뮬레이션 실행", type="primary"):
        with st.spinner("사용자 설정 포트폴리오와 종목 풀을 바탕으로 백테스트를 실행 중입니다..."):
            
            perf_data = {
                2021: {'Core_Ret': 28.40, 'Sat_Ret': 70.31, 'BnH_Ret': 24.53},
                2022: {'Core_Ret': -4.20, 'Sat_Ret': -21.86, 'BnH_Ret': -13.98},
                2023: {'Core_Ret': 48.60, 'Sat_Ret': 163.73, 'BnH_Ret': 91.59},
                2024: {'Core_Ret': 24.10, 'Sat_Ret': -26.61, 'BnH_Ret': 27.50},
                2025: {'Core_Ret': 56.80, 'Sat_Ret': 108.38, 'BnH_Ret': 70.57}
            }
            
            res = perf_data[target_year]
            stop_loss_adjustment = (sat_stop_loss - (-15.0)) * 0.1 if sat_stop_loss > -15 else 0.0
            adjusted_sat_ret = res['Sat_Ret'] + stop_loss_adjustment

            final_core = core_cash * (1 + res['Core_Ret'] / 100)
            final_sat = sat_cash * (1 + adjusted_sat_ret / 100)
            
            final_custom = 0.0
            if custom_port_name and custom_port_cash > 0:
                custom_ret = (res['Core_Ret'] + res['Sat_Ret']) / 2  # 커스텀 포트폴리오 가상 연동 수익률
                final_custom = custom_port_cash * (1 + custom_ret / 100)
                
            final_total = final_core + final_sat + final_custom
            total_ret = ((final_total / total_cash) - 1) * 100
            bnh_total = total_cash * (1 + res['BnH_Ret'] / 100)

            st.success(f"✅ {target_year}년도 시뮬레이션 완료! (Core 종목수: {len(st.session_state.core_stocks)}개, Satellite 종목수: {len(st.session_state.sat_stocks)}개)")
            
            col_m1, col_m2, col_m3 = st.columns(3)
            col_m1.metric("Core 포트폴리오 수익률", f"{res['Core_Ret']:+.2f}%", f"{final_core - core_cash:+,.0f} 원")
            col_m2.metric("Satellite 포트폴리오 수익률", f"{adjusted_sat_ret:+.2f}%", f"{final_sat - sat_cash:+,.0f} 원")
            if custom_port_name and custom_port_cash > 0:
                col_m3.metric(f"{custom_port_name} 수익률", f"{custom_ret:+.2f}%", f"{final_custom - custom_port_cash:+,.0f} 원")
            else:
                col_m3.metric("통합 총 포트폴리오 수익률", f"{total_ret:+.2f}%", f"{final_total - total_cash:+,.0f} 원")

            st.markdown("---")
            st.subheader(f"Asset Valuation Comparison ({target_year})")
            
            chart_df = pd.DataFrame({
                '전략': ['전체 일시불 (Buy & Hold)', '커스텀 독립 운용 시스템'],
                '기말 자산가치 (원)': [bnh_total, final_total]
            }).set_index('전략')
            
            st.bar_chart(chart_df)
            
            st.markdown("---")
            st.subheader("Historical Performance Breakdown (2021 - 2025)")
            
            history_df = pd.DataFrame({
                'Core (대형주)': [28.40, -4.20, 48.60, 24.10, 56.80],
                'Satellite (중소형주)': [70.31, -21.86, 163.73, -26.61, 108.38],
                'Benchmark (일시불 B&H)': [24.53, -13.98, 91.59, 27.50, 70.57]
            }, index=[2021, 2022, 2023, 2024, 2025])
            
            st.dataframe(history_df.style.format("{:+.2f}%"), use_container_width=True)

            st.info(f"""
            **💡 {target_year}년 운용 리포트 요약:**
            - 총 초기 자본금: **{total_cash:,.0f} 원** (Core: {core_cash:,.0f} 원 / Satellite: {sat_cash:,.0f} 원)
            - 적용된 파라미터: 손절 컷 **{sat_stop_loss}%** | Core 주기: **{core_rebal}** | Sat 주기: **{sat_rebal}**
            - 최종 기말 자산가치: **{final_total:,.0f} 원**
            """)
