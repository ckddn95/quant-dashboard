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
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **시장 공포지수(VIX) 및 수급 모멘텀 기반 상세 AI 진단**과 **전략별 비교 분석(단순보유·적립식·AI설정값) 시뮬레이션**을 제공하는 실전 퀀트 대시보드입니다.")

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
# 시장 공포지수 (VIX) 수집 함수
# ==========================================
@st.cache_data(ttl=1800)
def fetch_market_vix():
    try:
        vix_df = yf.download("^VIX", period="5d", progress=False)
        if not vix_df.empty:
            if isinstance(vix_df.columns, pd.MultiIndex):
                vix_df.columns = vix_df.columns.get_level_values(0)
            vix_val = float(vix_df['Close'].dropna().iloc[-1])
            return vix_val
    except:
        pass
    return 20.0

# ==========================================
# 실시간 주가 및 수급/거래량 지표 수집 함수
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
                volumes = df['Volume'].dropna()
                if len(close_prices) == 0: continue
                
                current_price = float(close_prices.iloc[-1])
                ma120 = float(close_prices.rolling(window=120).mean().iloc[-1]) if len(close_prices) >= 120 else current_price
                ma20 = float(close_prices.rolling(window=20).mean().iloc[-1]) if len(close_prices) >= 20 else current_price
                recent_high = float(close_prices.tail(120).max())
                drawdown = ((current_price / recent_high) - 1) * 100 if recent_high > 0 else 0.0
                
                vol_5ma = float(volumes.tail(6).iloc[:-1].mean()) if len(volumes) >= 6 else float(volumes.iloc[-1])
                curr_vol = float(volumes.iloc[-1])
                vol_ratio = (curr_vol / vol_5ma * 100) if vol_5ma > 0 else 100.0
                
                return current_price, ma120, ma20, drawdown, vol_ratio
    except Exception:
        pass
    return None, None, None, None, None

# ==========================================
# 2. 데이터 저장소(Session State) 초기화
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {}
if 'auto_diagnose' not in st.session_state:
    st.session_state.auto_diagnose = False

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
                st.session_state.auto_diagnose = True
                st.rerun()
        except Exception as e:
            st.sidebar.error(f"불러오기 실패: {e}")

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
                    st.success(f"✅ {file_path} 경로에 저장 완료!")

            st.markdown("---")
            st.subheader("🩺 실시간 매매 액션 플랜 및 종합 AI 판단 근거")
            
            run_btn = st.button("수동으로 진단 실행", type="primary")
            
            if run_btn or st.session_state.auto_diagnose:
                st.session_state.auto_diagnose = False
                
                if edited_df.empty:
                    st.warning("진단할 종목이 없습니다.")
                else:
                    with st.spinner("시장 공포지수(VIX), 수급 모멘텀, 기술적 지표를 종합 분석 중입니다..."):
                        vix_val = fetch_market_vix()
                        
                        if vix_val < 20.0:
                            vix_status = f"VIX {vix_val:.1f}(시장 안정)"
                            vix_safe = True
                        elif vix_val < 30.0:
                            vix_status = f"VIX {vix_val:.1f}(시장 경계/공포)"
                            vix_safe = False
                        else:
                            vix_status = f"VIX {vix_val:.1f}(시장 극심한 공포)"
                            vix_safe = False

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
                            
                            c_price, ma120, ma20, drawdown, vol_ratio = fetch_stock_status(s_ticker)
                            
                            if c_price is None:
                                results.append({'종목명': s_name, '상태': holding_status, '현재가': '데이터 없음', '액션 플랜': '⚠️ 확인 불가', '상세 AI 판단 근거': '-'})
                                continue
                            
                            if vol_ratio >= 150.0:
                                vol_status = f"거래량 {vol_ratio:.0f}%(수급 급증)"
                                vol_strong = True
                            elif vol_ratio >= 80.0:
                                vol_status = f"거래량 {vol_ratio:.0f}%(수급 보통)"
                                vol_strong = False
                            else:
                                vol_status = f"거래량 {vol_ratio:.0f}%(수급 침체)"
                                vol_strong = False

                            if current_strategy == '대형주 (Core)':
                                diff_120 = ((c_price / ma120) - 1) * 100
                                tech_text = f"120일선({ma120:,.0f}원) 이격도 {diff_120:+.2f}%"
                                
                                if is_holding: 
                                    if c_price >= ma120: 
                                        action = "🟢 보유 유지"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 장기 추세가 우상향으로 견고하며 매도 사유 없음."
                                    else: 
                                        action = "🔴 전량 매도 (현금화)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 120일선 하향 이탈. 하락 추세 전환 리스크 방어를 위해 현금화."
                                else: 
                                    if c_price >= ma120 and vix_safe: 
                                        action = "🟢 신규 진입 가능"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 지지선 위 상승 추세 및 거시 공포지수 안정으로 진입 적기."
                                    elif c_price >= ma120 and not vix_safe:
                                        action = "🟡 진입 보류 (시장 리스크)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 종목 추세는 양호하나 시장 공포지수 상승으로 관망 추천."
                                    else: 
                                        action = "🟡 진입 보류 (관망)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 하락 역배열 상태이므로 바닥이 확인될 때까지 대기."
                            else:
                                if is_holding: 
                                    user_ret = ((c_price / buy_price) - 1) * 100
                                    tech_text = f"내 수익률 {user_ret:+.2f}% (손절선 {sat_stop_loss}%)"
                                    
                                    if user_ret <= sat_stop_loss: 
                                        action = "🔴 강제 손절 집행"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 설정된 손절선 도달. 계좌 붕괴 방지를 위해 즉시 매도."
                                    elif user_ret > 0: 
                                        action = "🟢 보유 유지"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 수익권 내 순항 중. 거래량 수급 상태를 관찰하며 홀딩."
                                    else: 
                                        action = "🟡 보유 유지 (주의)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 손실권이나 설정된 강제 손절 컷 라인에는 도달하지 않음."
                                else: 
                                    tech_text = f"20일선({ma20:,.0f}원), 고점낙폭 {drawdown:+.2f}%"
                                    if c_price >= ma20 and drawdown >= -15.0 and (vol_strong or vix_safe): 
                                        action = "🟢 신규 진입 가능"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 단기 모멘텀(20일선 상회) 유지 중이며 수급/시장 안정 확인됨."
                                    else: 
                                        action = "🟡 진입 보류 (관망)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 단기 모멘텀 부진 또는 시장/수급 불안으로 관망 추천."
                                    
                            results.append({
                                '종목명': s_name, '상태': holding_status, '현재가': f"{c_price:,.0f} 원",
                                '액션 플랜': action, '상세 AI 판단 근거': detail
                            })
                        
                        st.table(pd.DataFrame(results))

with tab2:
    st.header("Simulation & Backtest")
    st.markdown("진단(Tab 1)과 **100% 동일한 다지표 조건(VIX, 수급, 이평선)**을 과거 데이터에 적용하여 실전 가상 매매를 실행하고, 종목별 상세 통계 및 전략별 비교를 제공합니다.")

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

        if st.button("동기화된 시뮬레이션 및 전략 비교 생성", type="primary", use_container_width=True):
            port_data = st.session_state.portfolios[sim_port]
            stocks = port_data['stocks']
            strat = port_data['strategy']
            init_cash = port_data['cash']

            if stocks.empty:
                st.error("종목이 없습니다.")
            else:
                with st.spinner("과거 데이터 수집 및 3대 전략 비교 백테스트 산출 중..."):
                    fetch_start = start_date - datetime.timedelta(days=200)
                    
                    vix_hist = None
                    try:
                        vix_df = yf.download("^VIX", start=fetch_start, end=end_date, progress=False)
                        if not vix_df.empty:
                            if isinstance(vix_df.columns, pd.MultiIndex):
                                vix_df.columns = vix_df.columns.get_level_values(0)
                            vix_hist = vix_df[['Close']].copy()
                            vix_hist.rename(columns={'Close': 'VIX'}, inplace=True)
                    except:
                        pass
                    
                    all_values = {}
                    bh_values = {}
                    dca_values = {}
                    trade_stats = {}
                    stock_count = len(stocks)
                    cash_per_stock = init_cash / stock_count
                    
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
                        df['Volume'] = df['Volume'].ffill()
                        df['Daily_Ret'] = df['Close'].pct_change()
                        
                        if vix_hist is not None:
                            df = df.join(vix_hist, how='left')
                            df['VIX'] = df['VIX'].ffill().fillna(20.0)
                        else:
                            df['VIX'] = 20.0
                            
                        df['VIX_Safe'] = df['VIX'] < 30.0
                        
                        df['Vol_5MA'] = df['Volume'].rolling(5).mean().shift(1)
                        df['Vol_Ratio'] = np.where(df['Vol_5MA'] > 0, df['Volume'] / df['Vol_5MA'] * 100, 100.0)
                        df['Vol_Strong'] = df['Vol_Ratio'] >= 150.0
                        
                        # 1. AI 설정값 전략 (Core-Satellite Rule)
                        if strat == '대형주 (Core)':
                            df['MA120'] = df['Close'].rolling(120).mean()
                            df['Signal'] = np.where((df['Close'] >= df['MA120']) & df['VIX_Safe'], 1, 
                                           np.where(df['Close'] < df['MA120'], 0, np.nan))
                            df['Signal'] = df['Signal'].ffill().fillna(0)
                        else:
                            df['MA20'] = df['Close'].rolling(20).mean()
                            df['Roll_Max'] = df['Close'].rolling(window=120, min_periods=1).max()
                            df['Drawdown'] = (df['Close'] / df['Roll_Max']) - 1
                            stop_loss_pct = sat_stop_loss / 100.0
                            
                            entry_cond = (df['Close'] >= df['MA20']) & (df['Drawdown'] >= -0.15) & (df['Vol_Strong'] | df['VIX_Safe'])
                            exit_cond = df['Drawdown'] <= stop_loss_pct
                            
                            df['Signal'] = np.where(entry_cond, 1, np.where(exit_cond, 0, np.nan))
                            df['Signal'] = df['Signal'].ffill().fillna(0)

                        # 매매 횟수(진입/청산) 카운팅
                        sim_subset = df.loc[start_date:end_date]
                        buy_cnt = int((sim_subset['Signal'].diff() == 1).sum())
                        sell_cnt = int((sim_subset['Signal'].diff() == -1).sum())
                        
                        df['Strat_Ret'] = df['Signal'].shift(1) * df['Daily_Ret']
                        sim_df = df.loc[start_date:end_date].copy()
                        if sim_df.empty: continue
                            
                        # AI 전략 자산 가치
                        sim_df['Cum_Ret'] = (1 + sim_df['Strat_Ret'].fillna(0)).cumprod()
                        sim_df['Asset_Value'] = sim_df['Cum_Ret'] * cash_per_stock
                        all_values[name] = sim_df['Asset_Value']
                        
                        # 2. 단순보유 (Buy & Hold) 전략 성과 계산
                        sim_df['BH_Cum_Ret'] = (1 + sim_df['Daily_Ret'].fillna(0)).cumprod()
                        bh_values[name] = sim_df['BH_Cum_Ret'] * cash_per_stock

                        # 3. 적립식 매수 (DCA) 전략 성과 계산 (매월 초 일정 금액 분할 적립 가정)
                        # 기간 내 월별 인덱스 추출 후 매월 균등 적립 모사
                        sim_df['Month'] = sim_df.index.to_period('M')
                        monthly_groups = sim_df.groupby('Month')
                        dca_asset_series = pd.Series(index=sim_df.index, dtype=float)
                        
                        # 초기 자금의 20%를 첫 달에 넣고, 나머지를 남은 달 동안 매월 균등 분할 투입
                        n_months = len(monthly_groups)
                        monthly_injection = (init_cash * 0.8) / max(n_months, 1) if n_months > 0 else 0
                        initial_dca_seed = (init_cash * 0.2) / stock_count
                        
                        running_shares = initial_dca_seed / sim_df['Close'].iloc[0]
                        current_cash_pool = 0
                        
                        for m_idx, (m_val, m_data) in enumerate(monthly_groups):
                            # 매월 첫 거래일에 현금 주입 후 주식 매수
                            first_idx = m_data.index[0]
                            if m_idx > 0:
                                cash_addition = monthly_injection / stock_count
                                add_shares = cash_addition / m_data.loc[first_idx, 'Close']
                                running_shares += add_shares
                        
                        # 일별 DCA 평가금액 = 보유 주식 수 누적 * 일별 종가
                        # 정교하게 일별 누적 주식 수를 반영하기 위해 월별 매수분을 누적 계산
                        shares_accumulated = initial_dca_seed / sim_df['Close'].iloc[0]
                        dca_values_list = []
                        current_injected_cash = initial_dca_seed
                        
                        for date_val, row_val in sim_df.iterrows():
                            # 월이 바뀔 때 추가 매수 가정
                            if date_val == sim_df.index[0]:
                                pass
                            elif date_val.day <= 3 and date_val.month != sim_df.index[sim_df.index.get_loc(date_val)-1].month:
                                if n_months > 0:
                                    add_amt = (init_cash * 0.8 / n_months) / stock_count
                                    shares_accumulated += add_amt / row_val['Close']
                                    current_injected_cash += add_amt
                            dca_values_list.append(shares_accumulated * row_val['Close'])
                            
                        dca_values[name] = pd.Series(dca_values_list, index=sim_df.index)

                        # 추정 수수료
                        traded_days = sim_df[sim_df['Signal'].diff() != 0]
                        est_fee = float(traded_days['Asset_Value'].sum() * 0.0025)
                        
                        trade_stats[name] = {
                            'buy': buy_cnt,
                            'sell': sell_cnt,
                            'fee': est_fee
                        }
                        
                    if not all_values:
                        st.warning("유효한 데이터가 없습니다.")
                    else:
                        val_df = pd.DataFrame(all_values).ffill()
                        val_df['Total_Asset'] = val_df.sum(axis=1)
                        
                        bh_df = pd.DataFrame(bh_values).ffill()
                        bh_df['Total_Asset'] = bh_df.sum(axis=1)

                        dca_df = pd.DataFrame(dca_values).ffill()
                        dca_df['Total_Asset'] = dca_df.sum(axis=1)

                        final_asset = val_df['Total_Asset'].iloc[-1]
                        final_port_ret = ((final_asset / init_cash) - 1) * 100

                        final_bh_asset = bh_df['Total_Asset'].iloc[-1]
                        final_bh_ret = ((final_bh_asset / init_cash) - 1) * 100

                        final_dca_asset = dca_df['Total_Asset'].iloc[-1]
                        # DCA는 원금 분할 투입이므로 실질 투입 원금 기준 수익률 산출
                        final_dca_ret = ((final_dca_asset / init_cash) - 1) * 100
                        
                        st.success(f"✅ 백테스트 및 전략 비교 산출 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 초기 자산", f"{init_cash:,.0f} 원")
                        col_r2.metric(f"AI 설정값 최종 기말 자산 (수익률)", f"{final_asset:,.0f} 원", f"{final_port_ret:+.2f}%")
                        
                        st.markdown("---")
                        
                        # ==========================================
                        # 💡 3대 전략 비교 분석 표 (별도 출력)
                        # ==========================================
                        st.subheader("📊 [전략 비교] 단순보유 vs 적립식 매수 vs AI 설정값")
                        st.markdown("동일한 기간 동안 각 투자 방식에 따른 최종 성과 비교 테이블입니다.")
                        
                        comparison_data = [
                            {
                                '전략 구분': '🤖 AI 설정값 전략 (Core-Satellite Rule)',
                                '최종 기말 자산': f"{final_asset:,.0f} 원",
                                '총 수익률': f"{final_port_ret:+.2f}%",
                                '운용 방식 및 특징': '시장 공포지수(VIX), 수급, 이동평균선 조건에 따라 기계적 매수/현금화(리스크 관리형)'
                            },
                            {
                                '전략 구분': '📉 단순보유 (Buy & Hold)',
                                '최종 기말 자산': f"{final_bh_asset:,.0f} 원",
                                '총 수익률': f"{final_bh_ret:+.2f}%",
                                '운용 방식 및 특징': '초기 전액 매수 후 기간 동안 매도 없이 끝까지 홀딩 (변동성 노출형)'
                            },
                            {
                                '전략 구분': '💰 적립식 매수 (DCA - Dollar Cost Averaging)',
                                '최종 기말 자산': f"{final_dca_asset:,.0f} 원",
                                '총 수익률': f"{final_dca_ret:+.2f}%",
                                '운용 방식 및 특징': '초기 시드 분할 후 매월 정기적으로 일정한 금액을 추가 투입하여 매입단가 분산'
                            }
                        ]
                        st.table(pd.DataFrame(comparison_data))

                        st.markdown("---")
                        
                        # 종목별 상세 통계 표 생성
                        st.subheader("📋 종목별 상세 매매 통계 및 성과 분석 (AI 설정값 기준)")
                        summary_rows = []
                        for name in all_values.keys():
                            init_val = cash_per_stock
                            final_val = all_values[name].iloc[-1]
                            profit = final_val - init_val
                            ret = (profit / init_val) * 100
                            weight = (final_val / final_asset) * 100 if final_asset > 0 else 0
                            
                            b_cnt = trade_stats[name]['buy']
                            s_cnt = trade_stats[name]['sell']
                            fee = trade_stats[name]['fee']
                            
                            summary_rows.append({
                                '종목명': name,
                                '초기 배분금': f"{init_val:,.0f} 원",
                                '기말 평가금': f"{final_val:,.0f} 원",
                                '순수익 (원)': f"{profit:+,.0f} 원",
                                '수익률 (%)': f"{ret:+.2f}%",
                                '매매 횟수': f"매수 {b_cnt}회 / 매도 {s_cnt}회",
                                '추정 수수료': f"{fee:,.0f} 원",
                                '기말 비중': f"{weight:.2f}%"
                            })
                        st.table(pd.DataFrame(summary_rows))
                        
                        st.markdown("---")
                        
                        try:
                            eom_val_df = val_df.drop(columns=['Total_Asset']).resample('ME').last()
                        except ValueError:
                            eom_val_df = val_df.drop(columns=['Total_Asset']).resample('M').last()
                        
                        eom_weights = eom_val_df.div(eom_val_df.sum(axis=1), axis=0) * 100
                        eom_weights.index = eom_weights.index.strftime('%Y-%m')
                        
                        st.subheader("📊 월말 기준 각 주식의 보유 비중 추이 (%)")
                        st.area_chart(eom_weights)
                        st.info("💡 위 통계는 진단 탭의 AI 판단 룰(VIX, 수급, 이평선)과 완전히 동일한 조건으로 백테스트 기간 동안 발생한 실제 매매 내역을 집계한 결과입니다.")
