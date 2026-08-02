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
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **스마트 차등 가중치 배분(모멘텀 알파 틸트)**, **현금 예비율 관리**, **시장 벤치마크(KOSPI) 비교 시뮬레이션**을 제공하는 실전 퀀트 대시보드입니다.")

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
# 3. 사이드바: 포트폴리오 관리 및 설정
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

if st.sidebar.button("포트폴리오 생성하기", use_container_width=True):
    if new_p_name and new_p_name not in st.session_state.portfolios:
        st.session_state.portfolios[new_p_name] = {
            'strategy': new_p_strat, 'cash': new_p_cash,
            'stocks': pd.DataFrame(columns=['종목명', '티커', '매수단가', '보유수량'])
        }
        st.rerun()
    elif new_p_name in st.session_state.portfolios:
        st.sidebar.warning("이미 존재합니다.")

st.sidebar.markdown("---")
st.sidebar.header("Strategy Parameters")
sat_stop_loss = st.sidebar.slider("중소형주 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-15, step=1)
max_alloc_pct = st.sidebar.slider("종목당 최대 투입 비중 한도 (%)", min_value=10, max_value=60, value=35, step=5, 
                                  help="조건이 매우 좋을 때 한 종목에 실어줄 수 있는 최대 자산 비율입니다.")

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
            total_cash = port_info['cash']
            st.subheader(f"📂 {selected_port} (전략: {current_strategy} | 총 자산 풀: {total_cash:,.0f}원)")
            
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
            st.subheader("🩺 실시간 매매 액션 플랜 및 스마트 가중치 진단")
            
            run_btn = st.button("수동으로 진단 실행", type="primary")
            
            if run_btn or st.session_state.auto_diagnose:
                st.session_state.auto_diagnose = False
                
                if edited_df.empty:
                    st.warning("진단할 종목이 없습니다.")
                else:
                    with st.spinner("시장 공포지수(VIX), 수급 모멘텀, 기술적 지표 및 스마트 가중치 산출 중..."):
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

                        stock_data_cache = {}
                        buy_scores = {}
                        
                        for idx, row in edited_df.iterrows():
                            s_ticker = row['티커']
                            s_name = row['종목명']
                            buy_price = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                            quantity = pd.to_numeric(row.get('보유수량', 0), errors='coerce')
                            if pd.isna(buy_price): buy_price = 0
                            if pd.isna(quantity): quantity = 0
                            is_holding = (quantity > 0) and (buy_price > 0)
                            
                            c_price, ma120, ma20, drawdown, vol_ratio = fetch_stock_status(s_ticker)
                            if c_price is None: continue
                            
                            stock_data_cache[s_name] = {
                                'price': c_price, 'ma120': ma120, 'ma20': ma20, 
                                'drawdown': drawdown, 'vol_ratio': vol_ratio, 'is_holding': is_holding
                            }
                            
                            vol_strong = vol_ratio >= 150.0
                            
                            # 신호 만족 여부 및 조건별 알파 스코어 부여 (조건이 좋을수록 점수 높음)
                            if current_strategy == '대형주 (Core)':
                                if not is_holding and c_price >= ma120 and vix_safe:
                                    score = 1.0
                                    if vol_strong: score += 0.5  # 수급 폭발 시 가산점
                                    if c_price > ma120 * 1.05: score += 0.5 # 이격도 우수 시 가산점
                                    buy_scores[s_name] = score
                            else:
                                if not is_holding and c_price >= ma20 and drawdown >= -15.0 and (vol_strong or vix_safe):
                                    score = 1.0
                                    if vol_strong: score += 0.5
                                    if drawdown >= -5.0: score += 0.5 # 고점 근접 시 가산점
                                    buy_scores[s_name] = score

                        # 점수 기반 동적 가중치(비중) 계산
                        total_score = sum(buy_scores.values()) if buy_scores else 1.0
                        max_alloc_ratio = max_alloc_pct / 100.0

                        current_stock_eval = 0
                        for idx, row in edited_df.iterrows():
                            s_name = row['종목명']
                            qty = pd.to_numeric(row.get('보유수량', 0), errors='coerce')
                            if pd.isna(qty): qty = 0
                            if qty > 0 and s_name in stock_data_cache:
                                current_stock_eval += qty * stock_data_cache[s_name]['price']
                                
                        current_cash = max(total_cash - current_stock_eval, 0)

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
                            
                            if s_name not in stock_data_cache:
                                results.append({'종목명': s_name, '상태': holding_status, '현재가': '데이터 없음', '액션 플랜': '⚠️ 확인 불가', '상세 AI 판단 근거': '-'})
                                continue
                                
                            data = stock_data_cache[s_name]
                            c_price = data['price']
                            ma120 = data['ma120']
                            ma20 = data['ma20']
                            drawdown = data['drawdown']
                            vol_ratio = data['vol_ratio']
                            vol_strong = vol_ratio >= 150.0
                            
                            vol_status = f"거래량 {vol_ratio:.0f}%(수급 급증)" if vol_strong else (f"거래량 {vol_ratio:.0f}%(수급 보통)" if vol_ratio >= 80 else f"거래량 {vol_ratio:.0f}%(수급 침체)")

                            if current_strategy == '대형주 (Core)':
                                diff_120 = ((c_price / ma120) - 1) * 100
                                tech_text = f"120일선({ma120:,.0f}원) 이격도 {diff_120:+.2f}%"
                                
                                if is_holding: 
                                    if c_price >= ma120: 
                                        action = "🟢 보유 유지"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 장기 추세 우상향 유지."
                                    else: 
                                        action = "🔴 전량 매도 (현금화)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 120일선 이탈로 리스크 관리."
                                else: 
                                    if c_price >= ma120 and vix_safe: 
                                        # 알파 점수 비중에 따른 차등 예산 배분 (단, 최대 한도 이내)
                                        stock_weight = (buy_scores[s_name] / total_score) if total_score > 0 else (1 / max(len(buy_scores), 1))
                                        target_amt = min(total_cash * stock_weight, total_cash * max_alloc_ratio, current_cash)
                                        rec_shares = int(target_amt // c_price) if c_price > 0 else 0
                                        
                                        if rec_shares > 0:
                                            action = f"🟢 신규 진입 (추천: {rec_shares}주 / 약 {rec_shares*c_price:,.0f}원)"
                                            detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 조건 우수 (알파 가중치 반영 적극 매수)."
                                        else:
                                            action = "🟡 진입 보류 (현금 부족)"
                                            detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 조건은 좋으나 가용 현금 부족."
                                    else: 
                                        action = "🟡 진입 보류 (관망)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 역배열 또는 시장 공포."
                            else:
                                if is_holding: 
                                    user_ret = ((c_price / buy_price) - 1) * 100
                                    tech_text = f"수익률 {user_ret:+.2f}%"
                                    if user_ret <= sat_stop_loss: 
                                        action = "🔴 강제 손절 집행"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 손절선 이탈."
                                    else: 
                                        action = "🟢 보유 유지"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 홀딩 구간."
                                else: 
                                    tech_text = f"20일선({ma20:,.0f}원), 낙폭 {drawdown:+.2f}%"
                                    if c_price >= ma20 and drawdown >= -15.0 and (vol_strong or vix_safe): 
                                        stock_weight = (buy_scores[s_name] / total_score) if total_score > 0 else (1 / max(len(buy_scores), 1))
                                        target_amt = min(total_cash * stock_weight, total_cash * max_alloc_ratio, current_cash)
                                        rec_shares = int(target_amt // c_price) if c_price > 0 else 0
                                        
                                        if rec_shares > 0:
                                            action = f"🟢 신규 진입 (추천: {rec_shares}주 / 약 {rec_shares*c_price:,.0f}원)"
                                            detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 모멘텀 우수 (알파 가중치 반영 적극 매수)."
                                        else:
                                            action = "🟡 진입 보류 (현금 부족)"
                                            detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 조건은 좋으나 가용 현금 부족."
                                    else: 
                                        action = "🟡 진입 보류 (관망)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{vol_status}]\n➔ 모멘텀 부진."
                                    
                            results.append({
                                '종목명': s_name, '상태': holding_status, '현재가': f"{c_price:,.0f} 원",
                                '액션 플랜': action, '상세 AI 판단 근거': detail
                            })
                        
                        st.info(f"📊 **[스마트 가중치 배분 및 현금 현황]**\n\n"
                                f"• **가용 현금 잔고:** `{current_cash:,.0f} 원` (보유 주식 평가액: `{current_stock_eval:,.0f} 원`)\n"
                                f"• **운용 특징:** 매수 조건이 뛰어난 종목(수급 폭발 등)에는 **더 높은 알파 가중치**를 부여하여 자금을 집중 배분하며, 현금 예비율을 항상 유지합니다.")
                        st.table(pd.DataFrame(results))

with tab2:
    st.header("Simulation & Backtest")
    st.markdown("과거 실제 주가 데이터를 기반으로 **스마트 가중치 배분 룰 및 시장 벤치마크(KOSPI)** 비교 백테스트를 실행합니다.")

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

        if st.button("스마트 가중치 시뮬레이션 및 벤치마크 비교 생성", type="primary", use_container_width=True):
            port_data = st.session_state.portfolios[sim_port]
            stocks = port_data['stocks']
            strat = port_data['strategy']
            init_cash = port_data['cash']

            if stocks.empty:
                st.error("종목이 없습니다.")
            else:
                with st.spinner("과거 데이터 및 KOSPI 지수 수집 및 스마트 백테스트 산출 중..."):
                    fetch_start = start_date - datetime.timedelta(days=200)
                    
                    kospi_ret = 0.0
                    final_kospi_asset = init_cash
                    try:
                        kospi_df = yf.download("^KS11", start=start_date, end=end_date, progress=False)
                        if not kospi_df.empty:
                            if isinstance(kospi_df.columns, pd.MultiIndex):
                                kospi_df.columns = kospi_df.columns.get_level_values(0)
                            kospi_close = kospi_df['Close'].dropna()
                            if len(kospi_close) > 1:
                                k_start = float(kospi_close.iloc[0])
                                k_end = float(kospi_close.iloc[-1])
                                kospi_ret = ((k_end / k_start) - 1) * 100
                                final_kospi_asset = init_cash * (1 + kospi_ret / 100)
                    except:
                        pass

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
                    
                    stock_dfs = {}
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
                        
                        if strat == '대형주 (Core)':
                            df['MA120'] = df['Close'].rolling(120).mean()
                            df['Signal'] = np.where((df['Close'] >= df['MA120']) & df['VIX_Safe'], 1, 
                                           np.where(df['Close'] < df['MA120'], 0, np.nan))
                            df['Signal'] = df['Signal'].ffill().fillna(0)
                            # 모멘텀 스코어 컬럼 생성
                            df['Score'] = np.where((df['Close'] >= df['MA120']) & df['VIX_Safe'], 1.0 + np.where(df['Vol_Strong'], 0.5, 0.0), 0.0)
                        else:
                            df['MA20'] = df['Close'].rolling(20).mean()
                            df['Roll_Max'] = df['Close'].rolling(window=120, min_periods=1).max()
                            df['Drawdown'] = (df['Close'] / df['Roll_Max']) - 1
                            stop_loss_pct = sat_stop_loss / 100.0
                            
                            entry_cond = (df['Close'] >= df['MA20']) & (df['Drawdown'] >= -0.15) & (df['Vol_Strong'] | df['VIX_Safe'])
                            exit_cond = df['Drawdown'] <= stop_loss_pct
                            
                            df['Signal'] = np.where(entry_cond, 1, np.where(exit_cond, 0, np.nan))
                            df['Signal'] = df['Signal'].ffill().fillna(0)
                            df['Score'] = np.where(entry_cond, 1.0 + np.where(df['Vol_Strong'], 0.5, 0.0), 0.0)
                            
                        stock_dfs[name] = df.loc[start_date:end_date].copy()
                        
                    if not stock_dfs:
                        st.warning("유효한 데이터가 없습니다.")
                    else:
                        common_index = list(stock_dfs.values())[0].index
                        for name in stock_dfs:
                            common_index = common_index.intersection(stock_dfs[name].index)
                            
                        portfolio_history = []
                        trade_stats = {name: {'buy': 0, 'sell': 0, 'fee': 0.0, 'realized_pnl': 0.0} for name in stock_dfs}
                        
                        dates = common_index
                        shares = {name: 0.0 for name in stock_dfs}
                        cash = init_cash
                        avg_buy_price = {name: 0.0 for name in stock_dfs}
                        realized_pnl = {name: 0.0 for name in stock_dfs}
                        
                        max_alloc_ratio = max_alloc_pct / 100.0
                        
                        for i, date_val in enumerate(dates):
                            if i == 0:
                                portfolio_history.append(init_cash)
                                continue
                                
                            prev_date = dates[i-1]
                            
                            active_stocks = []
                            scores = {}
                            for name, df in stock_dfs.items():
                                sig = df.loc[date_val, 'Signal']
                                if sig == 1:
                                    active_stocks.append(name)
                                    scores[name] = df.loc[date_val, 'Score']
                                    
                            for name, df in stock_dfs.items():
                                curr_sig = df.loc[date_val, 'Signal']
                                prev_sig = df.loc[prev_date, 'Signal']
                                if curr_sig == 1 and prev_sig == 0:
                                    trade_stats[name]['buy'] += 1
                                elif curr_sig == 0 and prev_sig == 1:
                                    trade_stats[name]['sell'] += 1
                                    
                            stock_eval_total = sum(shares[name] * stock_dfs[name].loc[date_val, 'Close'] for name in stock_dfs)
                            total_asset = cash + stock_eval_total
                            
                            n_active = len(active_stocks)
                            if n_active > 0:
                                total_score = sum(scores.values()) if sum(scores.values()) > 0 else n_active
                                for name in stock_dfs:
                                    c_price = stock_dfs[name].loc[date_val, 'Close']
                                    current_val = shares[name] * c_price
                                    
                                    if name in active_stocks:
                                        # 알파 점수 비중에 따른 차등 목표 배분액 산정
                                        weight = scores.get(name, 1.0) / total_score
                                        target_alloc = min(total_asset * weight, total_asset * max_alloc_ratio)
                                        diff_val = target_alloc - current_val
                                        
                                        if diff_val > 0: # 매수
                                            cost = diff_val
                                            fee = cost * 0.0025
                                            if cash >= (cost + fee):
                                                cash -= (cost + fee)
                                                added_shares = cost / c_price
                                                if shares[name] > 0:
                                                    avg_buy_price[name] = ((shares[name] * avg_buy_price[name]) + cost) / (shares[name] + added_shares)
                                                else:
                                                    avg_buy_price[name] = c_price
                                                shares[name] += added_shares
                                                trade_stats[name]['fee'] += fee
                                            else:
                                                cost = max(cash - (cash * 0.0025), 0)
                                                if cost > 0:
                                                    fee = cost * 0.0025
                                                    cash -= (cost + fee)
                                                    added_shares = cost / c_price
                                                    if shares[name] > 0:
                                                        avg_buy_price[name] = ((shares[name] * avg_buy_price[name]) + cost) / (shares[name] + added_shares)
                                                    else:
                                                        avg_buy_price[name] = c_price
                                                    shares[name] += added_shares
                                                    trade_stats[name]['fee'] += fee
                                        elif diff_val < 0: # 일부 매도
                                            proceeds = abs(diff_val)
                                            fee = proceeds * 0.0025
                                            sold_shares = proceeds / c_price
                                            pnl = sold_shares * (c_price - avg_buy_price[name]) - fee
                                            realized_pnl[name] += pnl
                                            cash += (proceeds - fee)
                                            shares[name] -= sold_shares
                                            trade_stats[name]['fee'] += fee
                                    else:
                                        if shares[name] > 0: # 전량 매도
                                            proceeds = shares[name] * c_price
                                            fee = proceeds * 0.0025
                                            pnl = shares[name] * (c_price - avg_buy_price[name]) - fee
                                            realized_pnl[name] += pnl
                                            cash += (proceeds - fee)
                                            trade_stats[name]['fee'] += fee
                                            shares[name] = 0.0
                                            avg_buy_price[name] = 0.0
                            else:
                                for name in stock_dfs:
                                    if shares[name] > 0:
                                        c_price = stock_dfs[name].loc[date_val, 'Close']
                                        proceeds = shares[name] * c_price
                                        fee = proceeds * 0.0025
                                        pnl = shares[name] * (c_price - avg_buy_price[name]) - fee
                                        realized_pnl[name] += pnl
                                        cash += (proceeds - fee)
                                        trade_stats[name]['fee'] += fee
                                        shares[name] = 0.0
                                        avg_buy_price[name] = 0.0
                                        
                            final_eval = sum(shares[name] * stock_dfs[name].loc[date_val, 'Close'] for name in stock_dfs)
                            portfolio_history.append(max(cash + final_eval, 0))
                            
                        ai_portfolio_series = pd.Series(portfolio_history, index=common_index)
                        
                        bh_values = {}
                        dca_values = {}
                        cash_per_stock_init = init_cash / len(stock_dfs)
                        
                        for name, df in stock_dfs.items():
                            sim_df = df.copy()
                            bh_values[name] = (sim_df['Close'] / sim_df['Close'].iloc[0]) * cash_per_stock_init
                            
                            n_months = len(sim_df.groupby(sim_df.index.to_period('M')))
                            initial_seed = (init_cash * 0.2) / len(stock_dfs)
                            shares_acc = initial_seed / sim_df['Close'].iloc[0]
                            dca_list = []
                            for date_val, row_val in sim_df.iterrows():
                                if date_val != sim_df.index[0] and date_val.day <= 3 and date_val.month != sim_df.index[sim_df.index.get_loc(date_val)-1].month:
                                    if n_months > 0:
                                        add_amt = (init_cash * 0.8 / n_months) / len(stock_dfs)
                                        shares_acc += add_amt / row_val['Close']
                                    dca_list.append(shares_acc * row_val['Close'])
                                else:
                                    dca_list.append(shares_acc * row_val['Close'])
                            dca_values[name] = pd.Series(dca_list, index=sim_df.index)
                            
                        bh_df = pd.DataFrame(bh_values).sum(axis=1)
                        dca_df = pd.DataFrame(dca_values).sum(axis=1)
                        
                        final_asset = ai_portfolio_series.iloc[-1]
                        final_port_ret = ((final_asset / init_cash) - 1) * 100
                        
                        final_bh_asset = bh_df.iloc[-1]
                        final_bh_ret = ((final_bh_asset / init_cash) - 1) * 100
                        
                        final_dca_asset = dca_df.iloc[-1]
                        final_dca_ret = ((final_dca_asset / init_cash) - 1) * 100
                        
                        st.success(f"✅ 스마트 가중치 및 벤치마크 비교 백테스트 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 초기 자산", f"{init_cash:,.0f} 원")
                        col_r2.metric(f"AI 동적배분 최종 기말 자산 (수익률)", f"{final_asset:,.0f} 원", f"{final_port_ret:+.2f}%")
                        
                        st.markdown("---")
                        
                        st.subheader("📊 [전략 비교] KOSPI 지수 vs 단순보유 vs 적립식 매수 vs AI 스마트 가중치 전략")
                        comparison_data = [
                            {
                                '전략 구분': '🤖 AI 스마트 가중치 전략 (Alpha-Tilt Rule)',
                                '최종 기말 자산': f"{final_asset:,.0f} 원",
                                '총 수익률': f"{final_port_ret:+.2f}%",
                                '운용 방식 및 특징': '수급 및 모멘텀 조건이 우수한 종목에 비중을 차등 집중 배분, 신호 소멸 시 100% 현금 방어'
                            },
                            {
                                '전략 구분': '📈 시장 벤치마크 (KOSPI 지수 ^KS11)',
                                '최종 기말 자산': f"{final_kospi_asset:,.0f} 원",
                                '총 수익률': f"{kospi_ret:+.2f}%",
                                '운용 방식': '한국 종합주가지수(KOSPI) 시장 수익률 추종 (패시브 투자 기준)'
                            },
                            {
                                '전략 구분': '📉 단순보유 (Buy & Hold)',
                                '최종 기말 자산': f"{final_bh_asset:,.0f} 원",
                                '총 수익률': f"{final_bh_ret:+.2f}%",
                                '운용 방식': '동일 종목 풀 초기 전액 동일 비중 매수 후 홀딩'
                            },
                            {
                                '전략 구분': '💰 적립식 매수 (DCA)',
                                '최종 기말 자산': f"{final_dca_asset:,.0f} 원",
                                '총 수익률': f"{final_dca_ret:+.2f}%",
                                '운용 방식': '동일 종목 풀 시드 분할 후 매월 정기 추가 투입'
                            }
                        ]
                        st.table(pd.DataFrame(comparison_data))

                        st.markdown("---")
                        
                        st.subheader("📋 종목별 상세 매매 통계 및 성과 분석")
                        summary_rows = []
                        for name in stock_dfs:
                            final_c_price = stock_dfs[name].iloc[-1]['Close']
                            holding_val = shares[name] * final_c_price
                            
                            unrealized_pnl = shares[name] * (final_c_price - avg_buy_price[name]) if shares[name] > 0 else 0.0
                            total_profit = realized_pnl[name] + unrealized_pnl
                            
                            init_val = init_cash / len(stock_dfs)
                            ret = (total_profit / init_val) * 100 if init_val > 0 else 0.0
                            weight = (holding_val / final_asset) * 100 if final_asset > 0 else 0.0
                            
                            b_cnt = trade_stats[name]['buy']
                            s_cnt = trade_stats[name]['sell']
                            fee = trade_stats[name]['fee']
                            
                            summary_rows.append({
                                '종목명': name,
                                '최종 보유 주수': f"{shares[name]:.2f} 주",
                                '기말 평가금': f"{holding_val:,.0f} 원",
                                '총 순수익 (원)': f"{total_profit:+,.0f} 원",
                                '수익률 (%)': f"{ret:+.2f}%",
                                '매매 횟수': f"매수 {b_cnt}회 / 매도 {s_cnt}회",
                                '총 발생 수수료': f"{fee:,.0f} 원",
                                '기말 포트폴리오 비중': f"{weight:.2f}%"
                            })
                        st.table(pd.DataFrame(summary_rows))
                        
                        st.markdown("---")
                        
                        val_df_chart = pd.DataFrame({name: (stock_dfs[name]['Close'] * shares[name]) for name in stock_dfs})
                        try:
                            eom_val_df = val_df_chart.resample('ME').last()
                        except ValueError:
                            eom_val_df = val_df_chart.resample('M').last()
                            
                        eom_weights = eom_val_df.div(eom_val_df.sum(axis=1), axis=0) * 100
                        eom_weights = eom_weights.fillna(0)
                        eom_weights.index = eom_weights.index.strftime('%Y-%m')
                        
                        st.subheader("📊 월말 기준 각 주식의 보유 비중 추이 (%)")
                        st.area_chart(eom_weights)
                        st.info("💡 위 비중 추이는 AI 진단 룰에 따라 조건이 우수한 종목에 가중치가 차등 부여되어 자금이 역동적으로 배분된 결과입니다.")
