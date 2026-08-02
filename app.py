import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import json
import os
import glob
import datetime
import warnings
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="📈",
    layout="wide"
)

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **로컬 PC 연동 저장/불러오기, 상세 AI 진단, 월말 비중 변화 시뮬레이션**을 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 0. 로컬 저장소 디렉토리 세팅 (PC 내 지정 장소)
# ==========================================
SAVE_DIR = "./saved_portfolios"
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# ==========================================
# 1. 한국 시장 전 종목 데이터베이스 로드 
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
# 실시간 주가 데이터 수집 함수
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
# 2. 데이터 저장소(Session State) 초기화
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {}
if 'auto_diagnose' not in st.session_state:
    st.session_state.auto_diagnose = False # 불러오기 즉시 진단 실행용 플래그

# ==========================================
# 3. 사이드바: 첫 화면 포트폴리오 불러오기 및 설정
# ==========================================
st.sidebar.header("📂 내 PC 포트폴리오 불러오기")
saved_files = glob.glob(f"{SAVE_DIR}/*.json")
if saved_files:
    file_names = [os.path.basename(f) for f in saved_files]
    selected_file = st.sidebar.selectbox("저장된 포트폴리오 선택", file_names)
    
    if st.sidebar.button("🚀 포트폴리오 불러오기 및 실시간 현황 업데이트", use_container_width=True):
        try:
            with open(os.path.join(SAVE_DIR, selected_file), 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                new_portfolios = {}
                for p_name, p_data in loaded_data.items():
                    new_portfolios[p_name] = {
                        'strategy': p_data['strategy'],
                        'cash': p_data['cash'],
                        'stocks': pd.DataFrame(p_data['stocks'])
                    }
                st.session_state.portfolios = new_portfolios
                st.session_state.auto_diagnose = True # 즉시 진단 트리거 온
                st.rerun()
        except Exception as e:
            st.sidebar.error(f"불러오기 실패: {e}")
else:
    st.sidebar.info(f"[{SAVE_DIR}] 폴더에 저장된 파일이 없습니다.")

st.sidebar.markdown("---")
st.sidebar.header("Portfolio Capital & Settings")

for p_name, p_data in list(st.session_state.portfolios.items()):
    strat = p_data['strategy']
    st.sidebar.markdown(f"**[{strat}] {p_name}**")
    
    new_cash = st.sidebar.number_input(
        f"{p_name} 투자금", value=p_data['cash'], step=1_000_000, format="%d", key=f"cash_input_{p_name}", label_visibility="collapsed"
    )
    st.session_state.portfolios[p_name]['cash'] = new_cash
    st.sidebar.caption(f"💰 설정 금액: **{new_cash:,.0f} 원**")
    
    if st.sidebar.button(f"🗑️ {p_name} 삭제", key=f"del_{p_name}"):
        del st.session_state.portfolios[p_name]
        st.rerun()
    st.sidebar.markdown("---")

st.sidebar.subheader("➕ Add New Portfolio")
new_p_name = st.sidebar.text_input("새 포트폴리오 이름", key="new_p_name")
new_p_strat = st.sidebar.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"], key="new_p_strat")
new_p_cash = st.sidebar.number_input("초기 투자금", value=10_000_000, step=1_000_000, format="%d", key="new_p_cash")

if st.sidebar.button("새 포트폴리오 생성", use_container_width=True):
    if new_p_name and new_p_name not in st.session_state.portfolios:
        st.session_state.portfolios[new_p_name] = {
            'strategy': new_p_strat, 'cash': new_p_cash,
            'stocks': pd.DataFrame(columns=['종목명', '티커', '매수단가', '보유수량'])
        }
        st.rerun()
    elif new_p_name in st.session_state.portfolios:
        st.sidebar.warning("이미 존재합니다.")

sat_stop_loss = st.sidebar.slider("중소형주 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-15, step=1)

# ==========================================
# 4. 탭 구성
# ==========================================
tab1, tab2 = st.tabs(["Portfolio Configuration & Stock Pools", "Simulation & Backtest"])

with tab1:
    st.header("포트폴리오 종목 구성 및 실시간 진단")
    
    if not st.session_state.portfolios:
        st.info("👈 좌측 사이드바에서 포트폴리오를 먼저 생성하거나 불러오세요.")
    else:
        selected_port = st.selectbox("관리할 포트폴리오 선택", options=list(st.session_state.portfolios.keys()), key="tab1_port")
        
        if selected_port:
            port_info = st.session_state.portfolios[selected_port]
            current_strategy = port_info['strategy']
            st.subheader(f"📂 {selected_port} (전략: {current_strategy})")
            
            # 종목 검색
            search_kw = st.text_input("추가할 종목명 검색 (예: 솔트룩스)", key=f"search_{selected_port}")
            if search_kw:
                krx_df = load_krx_universe()
                filtered_stocks = krx_df[krx_df['Name'].str.contains(search_kw, case=False, na=False)]
                
                if not filtered_stocks.empty:
                    col_search1, col_search2 = st.columns([3, 1])
                    with col_search1:
                        display_options = [f"{row['Name']} ({row['Code']})" for _, row in filtered_stocks.iterrows()]
                        selected_option = st.selectbox("검색 결과", display_options, key=f"sel_{selected_port}")
                    with col_search2:
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.button("➕ 종목 추가", key=f"add_btn_{selected_port}", use_container_width=True):
                            sel_name = selected_option.split(" (")[0]
                            sel_code = selected_option.split(" (")[1].replace(")", "")
                            
                            new_row = pd.DataFrame({'종목명': [sel_name], '티커': [sel_code], '매수단가': [0], '보유수량': [0]})
                            comb = pd.concat([port_info['stocks'], new_row]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                            st.session_state.portfolios[selected_port]['stocks'] = comb
                            st.rerun()

            st.markdown("---")
            st.markdown("**현재 포트폴리오 구성 종목 (보유 중이라면 '매수단가'와 '보유수량' 입력)**")
            
            edited_df = st.data_editor(
                port_info['stocks'], num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}"
            )
            st.session_state.portfolios[selected_port]['stocks'] = edited_df

            # ==========================================
            # 종목 선택 하단에 위치한 'PC 저장' 기능
            # ==========================================
            st.markdown("---")
            st.subheader("💾 현재 상태 PC에 저장하기")
            st.markdown(f"작업하신 내용을 내 PC의 특정 장소(`{SAVE_DIR}`)에 안전하게 저장합니다.")
            
            col_save1, col_save2 = st.columns([3, 1])
            with col_save1:
                save_filename = st.text_input("저장할 파일명 입력 (확장자 생략)", value=f"포트폴리오_{datetime.date.today().strftime('%Y%m%d')}")
            with col_save2:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("내 PC 지정 장소에 저장하기", type="secondary", use_container_width=True):
                    save_data = {}
                    for p_name, p_data in st.session_state.portfolios.items():
                        save_data[p_name] = {
                            'strategy': p_data['strategy'], 'cash': p_data['cash'],
                            'stocks': p_data['stocks'].to_dict(orient='records')
                        }
                    file_path = os.path.join(SAVE_DIR, f"{save_filename}.json")
                    with open(file_path, "w", encoding="utf-8") as f:
                        json.dump(save_data, f, ensure_ascii=False, indent=2)
                    st.success(f"✅ {file_path} 경로에 저장 완료! (첫 화면에서 바로 불러올 수 있습니다.)")

            st.markdown("---")
            # ==========================================
            # 실시간 포트폴리오 상세 진단 (수동 버튼 또는 자동 실행)
            # ==========================================
            st.subheader("🩺 실시간 매매 액션 플랜 및 상세 판단 근거")
            
            run_btn = st.button("수동으로 진단 실행", type="primary")
            
            # 버튼을 누르거나, 사이드바에서 로드했을 때 자동 실행
            if run_btn or st.session_state.auto_diagnose:
                st.session_state.auto_diagnose = False # 1회 실행 후 초기화
                
                if edited_df.empty:
                    st.warning("진단할 종목이 없습니다.")
                else:
                    with st.spinner("증권사 연동 가상 시뮬레이션 및 AI 진단 근거 산출 중..."):
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
                                results.append({'종목명': s_name, '상태': holding_status, '현재가': '데이터 없음', '상세 판단 근거': '-', '액션 플랜': '⚠️ 확인 불가'})
                                continue
                            
                            # 판단 근거 상세화 로직
                            if current_strategy == '대형주 (Core)':
                                diff_120 = ((c_price / ma120) - 1) * 100
                                detail = f"120일선({ma120:,.0f}원) 대비 이격도 {diff_120:+.2f}%"
                                
                                if is_holding: 
                                    if c_price >= ma120: 
                                        action = "🟢 보유 유지"
                                        detail += " ➔ 장기 상승 추세가 훼손되지 않아 매도 사유 없음."
                                    else: 
                                        action = "🔴 전량 매도 (현금화)"
                                        detail += " ➔ 120일선 하향 이탈 확인. 하락 추세 전환 리스크 방어를 위한 시스템 컷 발생."
                                else: 
                                    if c_price >= ma120: 
                                        action = "🟢 신규 진입 가능"
                                        detail += " ➔ 120일선 위에서 안정적인 우상향 모멘텀 확인."
                                    else: 
                                        action = "🟡 진입 보류 (관망)"
                                        detail += " ➔ 하락 역배열 상태이므로 바닥이 확인될 때까지 대기."
                            else:
                                if is_holding: 
                                    user_ret = ((c_price / buy_price) - 1) * 100
                                    detail = f"나의 수익률 {user_ret:+.2f}% (손절 설정치: {sat_stop_loss}%)"
                                    
                                    if user_ret <= sat_stop_loss: 
                                        action = "🔴 강제 손절 집행"
                                        detail += f" ➔ 수익률이 손절선을 이탈함. 계좌 붕괴 방지를 위해 즉각적인 테일 리스크 차단 필요."
                                    elif user_ret > 0: 
                                        action = "🟢 보유 유지"
                                        detail += " ➔ 수익권 내 순항 중이며, 추세 추종 룰에 따라 이익 극대화 구간."
                                    else: 
                                        action = "🟡 보유 유지 (주의)"
                                        detail += " ➔ 손실권이나 설정된 강제 손절 컷 라인에는 도달하지 않음."
                                else: 
                                    detail = f"20일선({ma20:,.0f}원), 최근고점 낙폭 {drawdown:+.2f}%"
                                    if c_price >= ma20 and drawdown >= -15.0: 
                                        action = "🟢 신규 진입 가능"
                                        detail += " ➔ 단기 모멘텀(20일선)이 살아있고 고점 대비 낙폭이 제한적(우상향 패턴)."
                                    else: 
                                        action = "🟡 진입 보류 (관망)"
                                        detail += " ➔ 단기 모멘텀 붕괴. 멀티배거 폭발력이 상실된 차트로 판단됨."
                                    
                            results.append({
                                '종목명': s_name, '상태': holding_status, '현재가': f"{c_price:,.0f} 원",
                                '액션 플랜': action, '상세 판단 근거': detail
                            })
                        
                        st.table(pd.DataFrame(results))

with tab2:
    st.header("Simulation & Backtest")
    st.markdown("과거 실제 주가 데이터를 기반으로 가상 매매를 실행하고, **월말 기준 각 주식의 보유 평가액 비중 추이**를 차트로 확인합니다.")

    if not st.session_state.portfolios:
        st.warning("포트폴리오가 없습니다.")
    else:
        col_sim1, col_sim2, col_sim3 = st.columns(3)
        with col_sim1:
            sim_port = st.selectbox("시뮬레이션 할 포트폴리오 선택", list(st.session_state.portfolios.keys()), key="sim_port")
        with col_sim2:
            start_date = st.date_input("시작일", datetime.date(2023, 1, 1))
        with col_sim3:
            end_date = st.date_input("종료일", datetime.date.today())

        if st.button("시뮬레이션 및 월말 비중 차트 생성", type="primary", use_container_width=True):
            port_data = st.session_state.portfolios[sim_port]
            stocks = port_data['stocks']
            strat = port_data['strategy']
            init_cash = port_data['cash']

            if stocks.empty:
                st.error("종목이 없습니다.")
            else:
                with st.spinner("과거 데이터 수집 및 월말 비중 산출 중..."):
                    fetch_start = start_date - datetime.timedelta(days=200)
                    all_values = {}
                    
                    stock_count = len(stocks)
                    cash_per_stock = init_cash / stock_count # 초기 동일 비중 투자 가정
                    
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
                        
                        if df is None or df.empty: continue
                            
                        df['Close'] = df['Close'].ffill()
                        df['Daily_Ret'] = df['Close'].pct_change()
                        
                        # 룰 적용
                        if strat == '대형주 (Core)':
                            df['MA120'] = df['Close'].rolling(120).mean()
                            df['Signal'] = np.where(df['Close'] > df['MA120'], 1, 0) 
                        else:
                            df['Roll_Max'] = df['Close'].rolling(window=120, min_periods=1).max()
                            df['Drawdown'] = (df['Close'] / df['Roll_Max']) - 1
                            stop_loss_pct = sat_stop_loss / 100.0
                            df['Signal'] = np.where(df['Drawdown'] > stop_loss_pct, 1, 0)

                        df['Strat_Ret'] = df['Signal'].shift(1) * df['Daily_Ret']
                        sim_df = df.loc[start_date:end_date].copy()
                        if sim_df.empty: continue
                            
                        # 일별 각 종목의 가상 평가액 계산
                        sim_df['Cum_Ret'] = (1 + sim_df['Strat_Ret'].fillna(0)).cumprod()
                        sim_df['Asset_Value'] = sim_df['Cum_Ret'] * cash_per_stock
                        all_values[name] = sim_df['Asset_Value']
                        
                    if not all_values:
                        st.warning("유효한 데이터가 없습니다.")
                    else:
                        # 일별 전체 가상 자산 데이터프레임 병합
                        val_df = pd.DataFrame(all_values).fillna(method='ffill')
                        val_df['Total_Asset'] = val_df.sum(axis=1)
                        
                        final_asset = val_df['Total_Asset'].iloc[-1]
                        final_port_ret = ((final_asset / init_cash) - 1) * 100
                        
                        st.success(f"✅ 시뮬레이션 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 초기 자산", f"{init_cash:,.0f} 원")
                        col_r2.metric(f"최종 기말 자산 (수익률)", f"{final_asset:,.0f} 원", f"{final_port_ret:+.2f}%")
                        
                        st.markdown("---")
                        
                        # 월말 기준 데이터 추출 (Month-End Resampling)
                        # Pandas 2.2+ 호환을 위해 resample('ME') 또는 groupby 사용
                        eom_val_df = val_df.drop(columns=['Total_Asset']).groupby(pd.Grouper(freq='M')).last()
                        
                        # 각 종목별 비중(%) 계산
                        eom_weights = eom_val_df.div(eom_val_df.sum(axis=1), axis=0) * 100
                        # 인덱스를 보기 좋은 연-월 문자열로 변환
                        eom_weights.index = eom_weights.index.strftime('%Y-%m')
                        
                        st.subheader("📊 월말 기준 각 주식의 보유 비중 추이 (%)")
                        st.markdown("가상 매매 결과에 따른 매월 말일 기준 포트폴리오 내 종목별 평가금액 비중 변화입니다.")
                        
                        # 영역 차트 (Area Chart)로 누적 비중 100% 시각화
                        st.area_chart(eom_weights)
