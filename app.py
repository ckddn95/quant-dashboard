import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import altair as alt
import json
import os
import glob
import datetime
import warnings
warnings.filterwarnings('ignore')

# 페이지 설정
st.set_page_config(
    page_title="Core-Satellite Quant System",
    page_icon="🚀",
    layout="wide"
)

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **AI 중소형주 눌림목 스캐너**, **매수 수수료 완벽 연동**, **가짜 반등 필터**, **트레일링 스탑 익절**을 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 0. 로컬 저장소 디렉토리 세팅
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
        return pd.DataFrame(columns=['Code', 'Name', 'Market', 'Marcap', 'Amount'])

# ==========================================
# 거시 지표 수집 함수
# ==========================================
@st.cache_data(ttl=1800)
def fetch_market_data():
    try:
        vix_df = yf.download("^VIX", period="3mo", progress=False)
        if isinstance(vix_df.columns, pd.MultiIndex): vix_df.columns = vix_df.columns.get_level_values(0)
        vix_close = vix_df['Close'].dropna()
        vix_val = float(vix_close.iloc[-1])
        vix_ma3 = float(vix_close.rolling(3).mean().iloc[-1])
        vix_contrarian = (vix_val >= 25.0) and (vix_val < vix_ma3)
        vix_safe = (vix_val < 30.0)
        
        kospi_df = fdr.DataReader('KS11')
        k_close = kospi_df['Close'].tail(61)
        if len(k_close) >= 60:
            kospi_ret_60 = ((float(k_close.iloc[-1]) / float(k_close.iloc[-60])) - 1) * 100
        else:
            kospi_ret_60 = 0.0
            
        return vix_val, vix_contrarian, vix_safe, kospi_ret_60
    except:
        return 20.0, False, True, 0.0

# ==========================================
# 실시간 주가 지표 수집 함수
# ==========================================
@st.cache_data(ttl=3600)
def fetch_stock_status(ticker_code):
    try:
        for suffix in ['.KS', '.KQ']:
            df = yf.download(f"{ticker_code}{suffix}", period="2y", progress=False)
            if not df.empty and len(df) > 0:
                if isinstance(df.columns, pd.MultiIndex): 
                    df.columns = df.columns.get_level_values(0)
                
                close_prices = df['Close'].dropna()
                volumes = df['Volume'].dropna()
                if len(close_prices) == 0: continue
                
                current_price = float(close_prices.iloc[-1])
                ma200 = float(close_prices.rolling(window=200).mean().iloc[-1]) if len(close_prices) >= 200 else current_price
                ma60 = float(close_prices.rolling(window=60).mean().iloc[-1]) if len(close_prices) >= 60 else current_price
                ma60_10d_ago = float(close_prices.rolling(window=60).mean().iloc[-11]) if len(close_prices) >= 70 else ma60
                ma20 = float(close_prices.rolling(window=20).mean().iloc[-1]) if len(close_prices) >= 20 else current_price
                recent_high = float(close_prices.tail(120).max())
                drawdown = ((current_price / recent_high) - 1) * 100 if recent_high > 0 else 0.0
                
                vol_5ma = float(volumes.tail(6).iloc[:-1].mean()) if len(volumes) >= 6 else float(volumes.iloc[-1])
                curr_vol = float(volumes.iloc[-1])
                vol_ratio = (curr_vol / vol_5ma * 100) if vol_5ma > 0 else 100.0
                
                ret_60 = ((current_price / float(close_prices.iloc[-60])) - 1) * 100 if len(close_prices) >= 60 else 0.0
                ret_20 = ((current_price / float(close_prices.iloc[-20])) - 1) * 100 if len(close_prices) >= 20 else 0.0
                
                ma60_slope_positive = (ma60 > ma60_10d_ago) 
                is_above_ma200 = (current_price >= ma200) 
                
                return current_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200
    except Exception:
        pass
    return None, None, None, None, None, None, None, None, False, False

# ==========================================
# [신규] 중소형주 눌림목 스캐너 함수
# ==========================================
@st.cache_data(ttl=3600)
def run_satellite_scanner():
    results = []
    krx = load_krx_universe()
    
    # 1차 필터: KOSDAQ 종목, 시가총액 500억 이상, 거래대금 상위 100개 추출 (속도 및 유동성 확보)
    try:
        kosdaq = krx[(krx['Market'] == 'KOSDAQ') | (krx['Market'] == 'KOSDAQ GLOBAL')]
        if 'Marcap' in kosdaq.columns and 'Amount' in kosdaq.columns:
            kosdaq = kosdaq[kosdaq['Marcap'] >= 50000000000] # 시총 500억 이상
            candidates = kosdaq.sort_values('Amount', ascending=False).head(100) # 거래대금 상위 100
        else:
            candidates = kosdaq.head(100)
    except:
        return []

    # 2차 필터: 야후 파이낸스 개별 종목 데이터 분석 (최근 20일 거래량 폭발 & 20일선 근접도)
    for idx, row in candidates.iterrows():
        code = row['Code']
        name = row['Name']
        try:
            df = yf.download(f"{code}.KQ", period="3mo", progress=False)
            if df.empty: continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            
            df['MA20'] = df['Close'].rolling(20).mean()
            df['Vol_5MA'] = df['Volume'].rolling(5).mean().shift(1)
            df['Vol_Ratio'] = np.where(df['Vol_5MA'] > 0, df['Volume'] / df['Vol_5MA'] * 100, 100.0)
            
            df = df.dropna()
            if len(df) < 20: continue
            
            # 최근 20일 이내에 거래량 200% 이상 폭발한 적이 있는지 (수급 유입)
            recent_20d_vol_max = df['Vol_Ratio'].tail(20).max()
            vol_surged = recent_20d_vol_max >= 200.0
            
            # 현재 주가가 20일선(생명선)의 ±2% 이내로 조정을 받았는지 (눌림목 타점)
            current_close = float(df['Close'].iloc[-1])
            current_ma20 = float(df['MA20'].iloc[-1])
            dist_from_ma20 = ((current_close / current_ma20) - 1) * 100
            is_dip_buying = -2.0 <= dist_from_ma20 <= 2.0
            
            if vol_surged and is_dip_buying:
                results.append({
                    '종목명': name,
                    '티커': code,
                    '현재가': f"{current_close:,.0f} 원",
                    '20일선 이격도': f"{dist_from_ma20:+.2f}%",
                    '최근 최대 거래량 폭발': f"{recent_20d_vol_max:,.0f}%",
                    '진단 근거': "수급 유입 후 20일선 안착 (눌림목)"
                })
        except:
            continue
            
    return pd.DataFrame(results)

# ==========================================
# 2. 데이터 저장소 초기화 및 트래킹
# ==========================================
if 'portfolios' not in st.session_state:
    st.session_state.portfolios = {}
if 'auto_diagnose' not in st.session_state:
    st.session_state.auto_diagnose = False
if 'current_loaded_file' not in st.session_state:
    st.session_state.current_loaded_file = None

# ==========================================
# 3. 사이드바: 포트폴리오 관리 및 퀵 세이브
# ==========================================
st.sidebar.header("📂 내 PC 포트폴리오 불러오기")
saved_files = glob.glob(f"{SAVE_DIR}/*.json")

if saved_files:
    file_names = [os.path.basename(f) for f in saved_files]
    selected_file = st.sidebar.selectbox("저장된 포트폴리오 선택", file_names)
    
    if st.sidebar.button("🚀 포트폴리오 불러오기", use_container_width=True):
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
                st.session_state.current_loaded_file = selected_file
                st.session_state.auto_diagnose = True
                st.rerun()
        except Exception as e:
            st.sidebar.error(f"불러오기 실패: {e}")

st.sidebar.markdown("---")
st.sidebar.header("💾 퀵 세이브 (Quick Save)")
if st.session_state.current_loaded_file:
    st.sidebar.markdown(f"현재 작업 파일: `{st.session_state.current_loaded_file}`")
    if st.sidebar.button("💾 변경사항 즉시 덮어쓰기", type="primary", use_container_width=True):
        save_data = {}
        for p_name, p_data in st.session_state.portfolios.items():
            save_data[p_name] = {
                'strategy': p_data['strategy'], 'cash': p_data['cash'],
                'stocks': p_data['stocks'].to_dict(orient='records')
            }
        file_path = os.path.join(SAVE_DIR, st.session_state.current_loaded_file)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2)
        st.sidebar.success("✅ 파일 덮어쓰기 완료!")
else:
    st.sidebar.caption("포트폴리오를 불러오거나 생성하면 활성화됩니다.")

st.sidebar.markdown("---")
st.sidebar.header("Portfolio Capital & Settings")

for p_name, p_data in list(st.session_state.portfolios.items()):
    strat = p_data['strategy']
    st.sidebar.markdown(f"**[{strat}] {p_name}**")
    
    new_cash = st.sidebar.number_input(
        f"{p_name} 총 투자 운용 자산 (증/감액)", 
        value=int(p_data['cash']), 
        step=1_000_000, 
        format="%d", 
        key=f"cash_input_{p_name}"
    )
    st.session_state.portfolios[p_name]['cash'] = new_cash
    st.sidebar.caption(f"💰 설정 금액: **{new_cash:,.0f} 원**")
    
    if st.sidebar.button(f"🗑️ {p_name} 삭제", key=f"del_{p_name}"):
        del st.session_state.portfolios[p_name]
        st.session_state.current_loaded_file = None
        st.rerun()
    st.sidebar.markdown("---")

st.sidebar.subheader("➕ Add New Portfolio")
new_p_name = st.sidebar.text_input("새 포트폴리오 이름", key="new_p_name")
new_p_strat = st.sidebar.selectbox("전략 (적용될 규칙)", ["대형주 (Core)", "중소형주 (Satellite)"], key="new_p_strat")
new_p_cash = st.sidebar.number_input("초기 총 투자금", value=10_000_000, step=1_000_000, format="%d", key="new_p_cash")

if st.sidebar.button("포트폴리오 생성하기", use_container_width=True):
    if new_p_name and new_p_name not in st.session_state.portfolios:
        st.session_state.portfolios[new_p_name] = {
            'strategy': new_p_strat, 'cash': new_p_cash,
            'stocks': pd.DataFrame(columns=['종목명', '티커', '매수단가', '보유수량'])
        }
        st.session_state.current_loaded_file = f"{new_p_name}.json"
        st.rerun()
    elif new_p_name in st.session_state.portfolios:
        st.sidebar.warning("이미 존재합니다.")

st.sidebar.markdown("---")
st.sidebar.header("⚙️ Advanced Strategy Parameters")

st.sidebar.markdown("**횡보/하락장 방어 필터**")
use_ma200_filter = st.sidebar.checkbox("🛡️ 200일 대장기 추세선 필터 적용", value=True)
cooldown_days = st.sidebar.slider("🔒 연속 2회 손실 시 쿨다운 (일)", min_value=0, max_value=90, value=60, step=15)

st.sidebar.markdown("**기본 리스크 관리**")
whipsaw_buffer = st.sidebar.slider("골든크로스 휩소 방지 버퍼 (%)", min_value=0.0, max_value=5.0, value=1.5, step=0.5)
sat_stop_loss = st.sidebar.slider("중소형주 긴급 손절 컷 (%)", min_value=-25, max_value=-5, value=-12, step=1) # -12% 권장치로 변경
max_alloc_pct = st.sidebar.slider("기본 종목당 투입 한도 (%)", min_value=10, max_value=60, value=20, step=5) # 중소형주 권장 20%
min_hold_days = st.sidebar.slider("최소 보유 기간 (일)", min_value=0, max_value=20, value=5, step=1)

st.sidebar.markdown("**🔥 대세 추세장 셋업**")
ts_target_pct = st.sidebar.slider("트레일링 스탑 목표 수익률 (%)", min_value=10, max_value=100, value=15, step=5) # 중소형주 빠른 익절 권장 15%
ts_drop_pct = st.sidebar.slider("트레일링 스탑 하락 허용 폭 (%)", min_value=-20, max_value=-5, value=-5, step=1)
bull_market_boost = st.sidebar.checkbox("🔥 강세장 자금 풀 부스터", value=True)

# ==========================================
# 4. 탭 구성
# ==========================================
tab1, tab2, tab3 = st.tabs(["Portfolio Configuration & Stock Pools", "Simulation & Backtest", "AI Strategy & Rules Report 📄"])

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
            
            # [신규 추가] 중소형주 전략일 때만 스캐너 기능 노출
            if current_strategy == '중소형주 (Satellite)':
                st.markdown("---")
                st.markdown("### 🔍 AI 중소형주 눌림목 스캐너 (발굴)")
                st.caption("코스닥 거래대금 상위 100종목 중, 수급이 폭발한 후 20일선 부근으로 예쁘게 조정을 받은(Dip-Buying) 타점을 실시간으로 찾아냅니다.")
                if st.button("🚀 오늘 진입 가능한 중소형주 탐색하기", type="primary"):
                    with st.spinner("코스닥 유동성 100개 종목의 거래량 및 20일선 이격도 정밀 분석 중... (약 10초 소요)"):
                        scan_result = run_satellite_scanner()
                        if not scan_result.empty:
                            st.success(f"✅ AI가 오늘 진입하기 좋은 눌림목 종목 {len(scan_result)}개를 발굴했습니다! (아래 표의 종목명을 복사하여 추가하세요)")
                            st.table(scan_result)
                        else:
                            st.warning("⚠️ 현재 조건(수급 폭발 후 20일선 눌림목)을 완벽히 만족하는 중소형 주도주가 없습니다. 시장이 과열되거나 침체된 상태입니다.")
            
            st.markdown("---")
            st.markdown("### 📝 포트폴리오 종목 관리 (추가 / 삭제)")
            col_manage1, col_manage2 = st.columns(2)
            
            with col_manage1:
                st.markdown("**[➕ 종목 추가]**")
                search_kw = st.text_input("추가할 종목명 검색", key=f"search_{selected_port}")
                if search_kw:
                    krx_df = load_krx_universe()
                    filtered_stocks = krx_df[krx_df['Name'].str.contains(search_kw, case=False, na=False)]
                    if not filtered_stocks.empty:
                        display_options = [f"{row['Name']} ({row['Code']})" for _, row in filtered_stocks.iterrows()]
                        selected_option = st.selectbox("검색 결과", display_options, key=f"sel_{selected_port}")
                        if st.button("종목 추가하기", key=f"add_btn_{selected_port}", use_container_width=True):
                            sel_name = selected_option.split(" (")[0]
                            sel_code = selected_option.split(" (")[1].replace(")", "")
                            
                            new_row = pd.DataFrame({'종목명': [sel_name], '티커': [sel_code], '매수단가': [0], '보유수량': [0]})
                            comb = pd.concat([port_info['stocks'], new_row]).drop_duplicates(subset=['티커']).reset_index(drop=True)
                            st.session_state.portfolios[selected_port]['stocks'] = comb
                            st.rerun()

            with col_manage2:
                st.markdown("**[🗑️ 종목 삭제]**")
                if not port_info['stocks'].empty:
                    del_options = port_info['stocks']['종목명'].tolist()
                    del_selected = st.selectbox("삭제할 종목 선택", del_options, key=f"del_sel_{selected_port}")
                    if st.button("선택 종목 삭제하기", key=f"del_btn_{selected_port}", use_container_width=True):
                        port_info['stocks'] = port_info['stocks'][port_info['stocks']['종목명'] != del_selected].reset_index(drop=True)
                        st.session_state.portfolios[selected_port]['stocks'] = port_info['stocks']
                        st.rerun()
                else:
                    st.caption("현재 등록된 종목이 없습니다.")

            st.markdown("---")
            st.markdown("**현재 포트폴리오 보유 내역 (보유 중이라면 '매수단가'와 '보유수량' 입력)**")
            
            edited_df = st.data_editor(
                port_info['stocks'], num_rows="dynamic", use_container_width=True, key=f"editor_{selected_port}"
            )
            st.session_state.portfolios[selected_port]['stocks'] = edited_df

            st.markdown("---")
            st.subheader("💾 본문에서 다른 이름으로 새로 저장하기 (Save As)")
            
            col_save1, col_save2 = st.columns([3, 1])
            with col_save1:
                default_save_name = st.session_state.current_loaded_file.replace('.json', '') if st.session_state.current_loaded_file else f"포트폴리오_{datetime.date.today().strftime('%Y%m%d')}"
                save_filename = st.text_input("저장할 파일명 (사이드바의 '퀵 세이브'를 이용하면 더 편리합니다)", value=default_save_name)
            with col_save2:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("새 이름으로 저장하기", type="secondary", use_container_width=True):
                    save_data = {}
                    for p_name, p_data in st.session_state.portfolios.items():
                        save_data[p_name] = {
                            'strategy': p_data['strategy'], 'cash': p_data['cash'],
                            'stocks': p_data['stocks'].to_dict(orient='records')
                        }
                    file_path = os.path.join(SAVE_DIR, f"{save_filename}.json")
                    with open(file_path, "w", encoding="utf-8") as f:
                        json.dump(save_data, f, ensure_ascii=False, indent=2)
                    st.session_state.current_loaded_file = f"{save_filename}.json"
                    st.success(f"✅ {file_path} 경로에 새롭게 저장 완료!")

            st.markdown("---")
            st.subheader("🩺 실시간 매매 액션 플랜 및 증/감액 동적 리밸런싱 진단")
            
            run_btn = st.button("수동으로 진단 실행", type="secondary")
            
            if run_btn or st.session_state.auto_diagnose:
                st.session_state.auto_diagnose = False
                
                if edited_df.empty:
                    st.warning("진단할 종목이 없습니다.")
                else:
                    with st.spinner("거시 지표 및 개별 종목 필터, 자본 증감액 룰 분석 중..."):
                        vix_val, vix_contrarian, vix_safe, kospi_ret_60 = fetch_market_data()
                        
                        vix_text = f"VIX {vix_val:.1f}"
                        if vix_contrarian: vix_status = f"{vix_text}(🔥극단적 공포 V자 반등 포착)"
                        elif vix_safe: vix_status = f"{vix_text}(시장 안정)"
                        else: vix_status = f"{vix_text}(시장 경계/공포)"

                        stock_data_cache = {}
                        buy_scores = {}
                        buf = whipsaw_buffer / 100.0
                        
                        for idx, row in edited_df.iterrows():
                            s_ticker = row['티커']
                            s_name = row['종목명']
                            buy_price = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                            quantity = pd.to_numeric(row.get('보유수량', 0), errors='coerce')
                            if pd.isna(buy_price): buy_price = 0
                            if pd.isna(quantity): quantity = 0
                            is_holding = (quantity > 0) and (buy_price > 0)
                            
                            c_price, ma200, ma60, ma20, drawdown, vol_ratio, ret_60, ret_20, ma60_slope_positive, is_above_ma200 = fetch_stock_status(s_ticker)
                            if c_price is None: continue
                            
                            stock_data_cache[s_name] = {
                                'price': c_price, 'ma200': ma200, 'ma60': ma60, 'ma20': ma20, 
                                'drawdown': drawdown, 'vol_ratio': vol_ratio, 'ret_60': ret_60, 'ret_20': ret_20,
                                'ma60_slope': ma60_slope_positive, 'is_above_ma200': is_above_ma200, 'is_holding': is_holding,
                                'qty': quantity
                            }
                            
                            vol_strong = vol_ratio >= 150.0
                            rs_strong = ret_60 > kospi_ret_60
                            ma200_pass = (not use_ma200_filter) or is_above_ma200
                            
                            if current_strategy == '대형주 (Core)':
                                if ma200_pass and (((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) and vix_safe) or vix_contrarian):
                                    score = 1.0
                                    if vol_strong: score += 0.5
                                    if rs_strong: score += 0.5
                                    if vix_contrarian: score += 1.0
                                    buy_scores[s_name] = score
                            else:
                                # 중소형주: 20일선 눌림목 조건으로 진입 스코어 계산
                                dist_ma20 = ((c_price / ma20) - 1) * 100
                                is_dip = -2.0 <= dist_ma20 <= 2.0
                                if ma200_pass and (is_dip or vix_contrarian) and drawdown >= -20.0:
                                    score = 1.0
                                    if vol_strong: score += 1.0 # 중소형주는 수급 비중 높임
                                    if rs_strong: score += 0.5
                                    if vix_contrarian: score += 1.0
                                    buy_scores[s_name] = score

                        total_score = sum(buy_scores.values()) if buy_scores else 1.0
                        
                        current_max_alloc_ratio = max_alloc_pct / 100.0
                        if bull_market_boost and kospi_ret_60 > 0:
                            current_max_alloc_ratio = min(current_max_alloc_ratio * 1.5, 1.0)

                        current_stock_eval = sum((data['qty'] * data['price']) for data in stock_data_cache.values() if data['is_holding'])
                        
                        available_cash = total_cash - current_stock_eval
                        force_sell_plans = {}
                        
                        if available_cash < 0:
                            deficit = abs(available_cash)
                            held_stocks_info = [
                                {'name': k, 'eval_amt': v['qty'] * v['price'], 'price': v['price'], 'score': buy_scores.get(k, 0.0), 'ret_20': v['ret_20']}
                                for k, v in stock_data_cache.items() if v['is_holding']
                            ]
                            held_stocks_info.sort(key=lambda x: (x['score'], x['ret_20']))
                            
                            for stock in held_stocks_info:
                                if deficit <= 0: break
                                s_name = stock['name']
                                eval_amt = stock['eval_amt']
                                price = stock['price']
                                
                                if eval_amt <= deficit:
                                    force_sell_plans[s_name] = "🔴 전량 매도 (설정 투자금 감액에 따른 우선 청산)"
                                    deficit -= eval_amt
                                else:
                                    sell_qty = int(np.ceil(deficit / price))
                                    force_sell_plans[s_name] = f"🟡 부분 매도 (설정 투자금 감액 맞춤: {sell_qty}주 매도)"
                                    deficit = 0

                        current_cash = max(available_cash, 0)

                        results = []
                        for idx, row in edited_df.iterrows():
                            s_name = row['종목명']
                            
                            if s_name not in stock_data_cache:
                                results.append({'종목명': s_name, '상태': '알수없음', '현재가': '-', '액션 플랜': '⚠️ 확인 불가', '상세 AI 판단 근거': '-'})
                                continue
                                
                            data = stock_data_cache[s_name]
                            c_price = data['price']
                            is_holding = data['is_holding']
                            holding_status = "보유중" if is_holding else "신규/관심"
                            
                            vol_strong = data['vol_ratio'] >= 150.0
                            rs_strong = data['ret_60'] > kospi_ret_60
                            vol_status = f"거래량 {data['vol_ratio']:.0f}%(수급 급증)" if vol_strong else (f"거래량 {data['vol_ratio']:.0f}%(수급 보통)" if data['vol_ratio'] >= 80 else f"거래량 {data['vol_ratio']:.0f}%(수급 침체)")
                            rs_status = f"상대강도 우위" if rs_strong else "상대강도 열위"
                            ma200_status = "200일선 상회" if data['is_above_ma200'] else "200일선 하회"

                            ma20, ma60, ret_20, ma60_slope_positive = data['ma20'], data['ma60'], data['ret_20'], data['ma60_slope']
                            diff_ma = ((ma20 / ma60) - 1) * 100
                            dist_ma20 = ((c_price / ma20) - 1) * 100
                            
                            if current_strategy == '대형주 (Core)':
                                tech_text = f"20/60선 이격 {diff_ma:+.2f}%, 20일 모멘텀 {ret_20:+.2f}%"
                            else:
                                tech_text = f"20일선 이격 {dist_ma20:+.2f}% (눌림목 타점)"
                                
                            slope_status = "60일선 우상향" if ma60_slope_positive else "60일선 횡보/우하향"

                            stock_weight = (buy_scores.get(s_name, 1.0) / total_score) if total_score > 0 else (1 / max(len(buy_scores), 1))
                            target_amt = min(total_cash * stock_weight, total_cash * current_max_alloc_ratio)
                            current_val = data['qty'] * c_price
                            diff_amt = target_amt - current_val

                            if is_holding and s_name in force_sell_plans:
                                action = force_sell_plans[s_name]
                                detail = f"[{tech_text}] | [{rs_status}]\n➔ ⚠️ 총 투자 운용 자산이 감액 설정됨에 따라, AI 모멘텀이 가장 낮은 본 종목을 우선 매도하여 현금 비중을 맞춥니다."
                            
                            elif current_strategy == '대형주 (Core)':
                                if is_holding: 
                                    if ma20 >= ma60 * (1 - buf/2): 
                                        add_cond = ((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) and vix_safe) or vix_contrarian
                                        if diff_amt > 0 and current_cash >= c_price and add_cond:
                                            add_shares = int(min(diff_amt, current_cash) // c_price)
                                            if add_shares > 0:
                                                action = f"🟢 추가 매수 (비중 확대: {add_shares}주 추가)"
                                                detail = f"[{tech_text}] | [{rs_status}]\n➔ 자본 증액 또는 모멘텀 우위로 비중을 리밸런싱(확대)합니다."
                                            else:
                                                action = "🟢 보유 유지"
                                                detail = f"[{tech_text}] | [{vix_status}]\n➔ 정배열 추세 지속 중."
                                        elif diff_amt < -c_price: 
                                            reduce_shares = int(abs(diff_amt) // c_price)
                                            action = f"🟡 부분 매도 (비중 조절: {reduce_shares}주 매도)"
                                            detail = f"[{tech_text}]\n➔ 목표 비중 초과로 부분 익절 리밸런싱을 진행합니다."
                                        else:
                                            action = "🟢 보유 유지"
                                            detail = f"[{tech_text}] | [{vix_status}] | [{rs_status}]\n➔ 정배열 추세 지속 중."
                                    else: 
                                        action = "🔴 전량 매도 (현금화)"
                                        detail = f"[{tech_text}] | [{vix_status}] | [{rs_status}]\n➔ 데드크로스 발생으로 즉각 현금화."
                                else: 
                                    if (use_ma200_filter and not data['is_above_ma200']):
                                        action = "🟡 진입 보류 (200일선 하회)"
                                        detail = f"[{tech_text}] | [{ma200_status}]\n➔ 장기 추세선(200일선) 아래에 위치하여 진입 금지(하락장 방어)."
                                    elif ((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0) and vix_safe) or vix_contrarian: 
                                        rec_shares = int(min(target_amt, current_cash) // c_price) if c_price > 0 else 0
                                        if rec_shares > 0:
                                            action = f"🟢 신규 진입 (추천: {rec_shares}주 / 약 {rec_shares*c_price:,.0f}원)"
                                            reason = "V자 반등 바닥잡기" if vix_contrarian else f"{whipsaw_buffer}% 버퍼 및 200일선 통과"
                                            detail = f"[{tech_text}] | [{ma200_status}] | [{vix_status}] | [{vol_status}]\n➔ {reason} 검증 완료."
                                        else:
                                            action = "🟡 진입 보류 (현금 부족)"
                                            detail = f"[{tech_text}]\n➔ 가용 현금 부족."
                                    else: 
                                        action = "🟡 진입 보류 (관망)"
                                        detail = f"[{tech_text}] | [{ma200_status}]\n➔ 가짜 반등(휩소) 구간으로 진입 보류."
                            else: # 중소형주 (Satellite)
                                if is_holding: 
                                    buy_price = pd.to_numeric(row.get('매수단가', 0), errors='coerce')
                                    user_ret = ((c_price / buy_price) - 1) * 100 if buy_price > 0 else 0
                                    tech_text_sat = f"수익률 {user_ret:+.2f}%"
                                    if user_ret <= sat_stop_loss: 
                                        action = "🔴 강제 손절 집행"
                                        detail = f"[{tech_text_sat}]\n➔ 긴급 손절선({sat_stop_loss}%) 이탈로 하드 컷."
                                    elif ma20 >= ma60 * (1 - buf/2):
                                        is_dip = -2.0 <= dist_ma20 <= 2.0
                                        add_cond = (is_dip or vix_contrarian) and data['drawdown'] >= -20.0
                                        if diff_amt > 0 and current_cash >= c_price and add_cond:
                                            add_shares = int(min(diff_amt, current_cash) // c_price)
                                            if add_shares > 0:
                                                action = f"🟢 추가 매수 (비중 확대: {add_shares}주 추가)"
                                                detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 자본 증액 반영 추가 매수."
                                            else:
                                                action = "🟢 보유 유지"
                                                detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 정배열 추세 홀딩."
                                        elif diff_amt < -c_price:
                                            reduce_shares = int(abs(diff_amt) // c_price)
                                            action = f"🟡 부분 매도 (비중 조절: {reduce_shares}주 매도)"
                                            detail = f"[{tech_text_sat}]\n➔ 목표 비중 조절."
                                        else:
                                            action = "🟢 보유 유지"
                                            detail = f"[{tech_text_sat}] | [{rs_status}]\n➔ 정배열 추세 홀딩 구간."
                                    else:
                                        action = "🔴 전량 매도"
                                        detail = f"[{tech_text_sat}]\n➔ 20일선 데드크로스로 완전 이탈."
                                else: 
                                    if (use_ma200_filter and not data['is_above_ma200']):
                                        action = "🟡 진입 보류 (200일선 하회)"
                                        detail = f"[{tech_text}] | [{ma200_status}]\n➔ 장기 추세선 아래 진입 금지."
                                    else:
                                        is_dip = -2.0 <= dist_ma20 <= 2.0
                                        if (is_dip or vix_contrarian) and data['drawdown'] >= -20.0: 
                                            rec_shares = int(min(target_amt, current_cash) // c_price) if c_price > 0 else 0
                                            if rec_shares > 0:
                                                action = f"🟢 신규 진입 (추천: {rec_shares}주 / 약 {rec_shares*c_price:,.0f}원)"
                                                detail = f"[{tech_text}] | [{ma200_status}] | [{vix_status}]\n➔ 20일선 눌림목 타점 정확히 진입."
                                            else:
                                                action = "🟡 진입 보류 (현금 부족)"
                                                detail = f"[{tech_text}]\n➔ 현금 부족."
                                        else: 
                                            action = "🟡 진입 보류 (타점 대기)"
                                            detail = f"[{tech_text}] | [{ma200_status}]\n➔ 20일선 눌림목(±2%) 도달 시까지 매수 대기."
                                    
                            results.append({
                                '종목명': s_name, '상태': holding_status, '현재가': f"{c_price:,.0f} 원",
                                '액션 플랜': action, '상세 AI 판단 근거': detail
                            })
                        
                        warning_msg = "⚠️ **[투자금 감액 감지]** 감액된 투자금에 맞춰 최약체 종목 우선 청산 액션 플랜이 발동되었습니다." if available_cash < 0 else ""
                        boost_msg = f" (🔥강세장 부스터 작동 중: 한도 최대 {current_max_alloc_ratio*100:.0f}%)" if (bull_market_boost and kospi_ret_60 > 0) else ""
                        st.info(f"📊 **[스마트 자금 리밸런싱 및 현금 현황]**\n\n"
                                f"• **총 운용 설정 자산:** `{total_cash:,.0f} 원`\n"
                                f"• **가용 현금 잔고:** `{current_cash:,.0f} 원` (보유 주식 평가액: `{current_stock_eval:,.0f} 원`)\n"
                                f"• **운용 특징:** 투자금을 증액하면 추가 매수를 지시하고, 감액 시 최약체부터 우선 청산하는 산식이 100% 반영되었습니다.\n"
                                f"{warning_msg}{boost_msg}")
                        st.table(pd.DataFrame(results))

with tab2:
    st.header("Simulation & Backtest")
    st.markdown("과거 주가 데이터를 바탕으로 **매매 수수료 누락 산식이 완벽히 보정된 초과수익 엔진**을 검증합니다.")

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

        if st.button("고수익 모멘텀 시뮬레이션 및 벤치마크 비교 생성", type="primary", use_container_width=True):
            port_data = st.session_state.portfolios[sim_port]
            stocks = port_data['stocks']
            strat = port_data['strategy']
            init_cash = port_data['cash']

            if stocks.empty:
                st.error("종목이 없습니다.")
            else:
                with st.spinner("산식 보정된 KOSPI 벤치마크 및 동적 백테스트 구동 중..."):
                    fetch_start = start_date - datetime.timedelta(days=300)
                    
                    market_df = pd.DataFrame()
                    kospi_ret_val = 0.0
                    final_kospi_asset = init_cash
                    
                    try:
                        kospi_df = fdr.DataReader('KS11', fetch_start, end_date)
                        if not kospi_df.empty:
                            if kospi_df.index.tz is not None:
                                kospi_df.index = kospi_df.index.tz_localize(None)
                            kospi_df['Kospi_Ret_60'] = kospi_df['Close'] / kospi_df['Close'].shift(60) - 1
                            kospi_df['Kospi_MA60'] = kospi_df['Close'].rolling(60).mean()
                            market_df['Kospi_Ret_60'] = kospi_df['Kospi_Ret_60']
                            market_df['Kospi_Bull'] = kospi_df['Close'] > kospi_df['Kospi_MA60']
                            
                            start_dt = pd.to_datetime(start_date)
                            sim_kospi = kospi_df[kospi_df.index >= start_dt]['Close'].dropna()
                            if len(sim_kospi) > 1:
                                k_start = float(sim_kospi.iloc[0])
                                k_end = float(sim_kospi.iloc[-1])
                                kospi_ret_val = ((k_end / k_start) - 1) * 100
                                final_kospi_asset = init_cash * (1 + kospi_ret_val / 100)
                    except: pass

                    try:
                        vix_df = yf.download("^VIX", start=fetch_start, end=end_date, progress=False)
                        if not vix_df.empty:
                            if isinstance(vix_df.columns, pd.MultiIndex): vix_df.columns = vix_df.columns.get_level_values(0)
                            vix_df['VIX_MA3'] = vix_df['Close'].rolling(3).mean()
                            market_df['VIX_Contrarian'] = (vix_df['Close'] >= 25.0) & (vix_df['Close'] < vix_df['VIX_MA3'])
                            market_df['VIX_Safe'] = vix_df['Close'] < 30.0
                    except: pass
                    
                    market_df = market_df.ffill().fillna(0)
                    
                    stock_dfs = {}
                    buf = whipsaw_buffer / 100.0
                    start_dt = pd.to_datetime(start_date)
                    
                    for idx, row in stocks.iterrows():
                        ticker = row['티커']
                        name = row['종목명']
                        
                        df = None
                        for suf in ['.KS', '.KQ']:
                            temp_df = yf.download(f"{ticker}{suf}", start=fetch_start, end=end_date, progress=False)
                            if not temp_df.empty:
                                if isinstance(temp_df.columns, pd.MultiIndex): temp_df.columns = temp_df.columns.get_level_values(0)
                                df = temp_df
                                break
                        
                        if df is None or df.empty: continue
                            
                        df['Close'] = df['Close'].ffill()
                        df['Volume'] = df['Volume'].ffill()
                        df['Daily_Ret'] = df['Close'].pct_change()
                        
                        df['MA200'] = df['Close'].rolling(200).mean()
                        df['Is_Above_MA200'] = df['Close'] >= df['MA200']
                        
                        df['MA60'] = df['Close'].rolling(60).mean()
                        df['MA60_Slope'] = df['MA60'] > df['MA60'].shift(10)
                        df['MA20'] = df['Close'].rolling(20).mean()
                        df['Ret_60'] = df['Close'] / df['Close'].shift(60) - 1
                        df['Ret_20'] = df['Close'] / df['Close'].shift(20) - 1
                        
                        df['Vol_5MA'] = df['Volume'].rolling(5).mean().shift(1)
                        df['Vol_Ratio'] = np.where(df['Vol_5MA'] > 0, df['Volume'] / df['Vol_5MA'] * 100, 100.0)
                        df['Vol_Strong'] = df['Vol_Ratio'] >= 150.0
                        
                        if df.index.tz is not None:
                            df.index = df.index.tz_localize(None)
                            
                        df = df.join(market_df, how='left')
                        df['VIX_Safe'] = df['VIX_Safe'].fillna(True)
                        df['VIX_Contrarian'] = df['VIX_Contrarian'].fillna(False)
                        df['Kospi_Ret_60'] = df['Kospi_Ret_60'].fillna(0.0)
                        df['Kospi_Bull'] = df['Kospi_Bull'].fillna(False)
                        
                        ma200_cond = df['Is_Above_MA200'] if use_ma200_filter else True
                        
                        if strat == '대형주 (Core)':
                            entry_cond = (ma200_cond & (df['MA20'] >= df['MA60'] * (1 + buf)) & df['MA60_Slope'] & (df['Ret_20'] > 0) & df['VIX_Safe']) | df['VIX_Contrarian']
                            exit_cond = (df['MA20'] < df['MA60'] * (1 - buf/2)) & (~df['VIX_Contrarian'])
                        else:
                            df['Roll_Max'] = df['Close'].rolling(window=120, min_periods=1).max()
                            df['Drawdown'] = (df['Close'] / df['Roll_Max']) - 1
                            stop_loss_pct = sat_stop_loss / 100.0
                            
                            dist_ma20 = ((df['Close'] / df['MA20']) - 1) * 100
                            is_dip = (dist_ma20 >= -2.0) & (dist_ma20 <= 2.0)
                            
                            entry_cond = (ma200_cond & (is_dip | df['VIX_Contrarian'])) & (df['Drawdown'] >= -0.20)
                            exit_cond = (df['Drawdown'] <= stop_loss_pct) | ((df['MA20'] < df['MA60'] * (1 - buf/2)) & (~df['VIX_Contrarian']))
                        
                        df['Signal'] = np.where(entry_cond, 1, np.where(exit_cond, 0, np.nan))
                        df['Signal'] = df['Signal'].ffill().fillna(0)
                        
                        rs_condition = df['Ret_60'] > df['Kospi_Ret_60']
                        df['Score'] = np.where(entry_cond, 
                                               1.0 + np.where(df['Vol_Strong'], 1.0 if strat != '대형주 (Core)' else 0.5, 0.0) + 
                                               np.where(rs_condition, 0.5, 0.0) + 
                                               np.where(df['VIX_Contrarian'], 1.0, 0.0), 
                                               0.0)
                        
                        stock_dfs[name] = df[df.index >= start_dt].copy()
                        
                    if not stock_dfs:
                        st.warning("유효한 데이터가 없습니다.")
                    else:
                        common_index = list(stock_dfs.values())[0].index
                        for name in stock_dfs:
                            common_index = common_index.intersection(stock_dfs[name].index)
                            
                        portfolio_history = []
                        history_records = [] 
                        
                        trade_stats = {name: {'buy': 0, 'sell': 0, 'fee': 0.0, 'realized_pnl': 0.0} for name in stock_dfs}
                        
                        dates = common_index
                        shares = {name: 0.0 for name in stock_dfs}
                        hold_days = {name: 0 for name in stock_dfs}
                        max_invested = {name: 0.0 for name in stock_dfs}
                        peak_price_since_buy = {name: 0.0 for name in stock_dfs}
                        
                        consecutive_losses = {name: 0 for name in stock_dfs}
                        cooldown_until = {name: pd.Timestamp.min for name in stock_dfs}
                        
                        cash = init_cash
                        avg_buy_price = {name: 0.0 for name in stock_dfs}
                        realized_pnl = {name: 0.0 for name in stock_dfs}
                        
                        base_alloc_ratio = max_alloc_pct / 100.0
                        ts_target = ts_target_pct / 100.0
                        ts_drop = ts_drop_pct / 100.0
                        
                        for i, date_val in enumerate(dates):
                            if i == 0:
                                portfolio_history.append(init_cash)
                                record = {'Date': date_val, '현금(Cash)': init_cash}
                                for name in stock_dfs: record[name] = 0.0
                                history_records.append(record)
                                continue
                                
                            prev_date = dates[i-1]
                            
                            current_max_alloc_ratio = base_alloc_ratio
                            market_bull = False
                            for df in stock_dfs.values():
                                market_bull = df.loc[date_val, 'Kospi_Bull']
                                break
                            
                            if bull_market_boost and market_bull:
                                current_max_alloc_ratio = min(base_alloc_ratio * 1.5, 1.0)
                            
                            for name in stock_dfs:
                                if shares[name] > 0: hold_days[name] += 1
                                else: hold_days[name] = 0
                            
                            active_stocks = []
                            scores = {}
                            for name, df in stock_dfs.items():
                                sig = df.loc[date_val, 'Signal']
                                c_price = df.loc[date_val, 'Close']
                                
                                if shares[name] == 0 and date_val < cooldown_until[name]:
                                    sig = 0.0
                                
                                trailing_stop_exit = False
                                if shares[name] > 0 and avg_buy_price[name] > 0:
                                    peak_price_since_buy[name] = max(peak_price_since_buy[name], c_price)
                                    curr_ret = (c_price / avg_buy_price[name]) - 1
                                    drop_from_peak = (c_price / peak_price_since_buy[name]) - 1
                                    
                                    if curr_ret >= ts_target and drop_from_peak <= ts_drop:
                                        trailing_stop_exit = True
                                
                                force_exit = False
                                if strat != '대형주 (Core)' and df.loc[date_val, 'Drawdown'] <= (sat_stop_loss / 100.0):
                                    force_exit = True
                                    
                                if trailing_stop_exit or force_exit:
                                    sig = 0.0
                                elif shares[name] > 0 and hold_days[name] < min_hold_days:
                                    sig = 1.0
                                    
                                if sig == 1:
                                    active_stocks.append(name)
                                    scores[name] = df.loc[date_val, 'Score'] if df.loc[date_val, 'Score'] > 0 else 1.0
                                else:
                                    peak_price_since_buy[name] = 0.0
                                    
                            for name, df in stock_dfs.items():
                                curr_sig = 1 if name in active_stocks else 0
                                prev_sig = 1 if shares[name] > 0 else 0
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
                                        weight = scores.get(name, 1.0) / total_score
                                        target_alloc = min(total_asset * weight, total_asset * current_max_alloc_ratio)
                                        diff_val = target_alloc - current_val
                                        
                                        if diff_val > 0: 
                                            cost = diff_val
                                            fee = cost * 0.0025
                                            if cash >= (cost + fee):
                                                cash -= (cost + fee)
                                                added_shares = cost / c_price
                                                if shares[name] > 0:
                                                    avg_buy_price[name] = ((shares[name] * avg_buy_price[name]) + cost) / (shares[name] + added_shares)
                                                else:
                                                    avg_buy_price[name] = c_price
                                                    peak_price_since_buy[name] = c_price
                                                shares[name] += added_shares
                                                trade_stats[name]['fee'] += fee
                                                realized_pnl[name] -= fee 
                                                max_invested[name] = max(max_invested[name], shares[name] * c_price)
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
                                                        peak_price_since_buy[name] = c_price
                                                    shares[name] += added_shares
                                                    trade_stats[name]['fee'] += fee
                                                    realized_pnl[name] -= fee
                                                    max_invested[name] = max(max_invested[name], shares[name] * c_price)
                                        elif diff_val < 0: 
                                            proceeds = abs(diff_val)
                                            fee = proceeds * 0.0025
                                            sold_shares = proceeds / c_price
                                            pnl = sold_shares * (c_price - avg_buy_price[name]) - fee
                                            realized_pnl[name] += pnl
                                            cash += (proceeds - fee)
                                            shares[name] -= sold_shares
                                            trade_stats[name]['fee'] += fee
                                    else:
                                        if shares[name] > 0: 
                                            proceeds = shares[name] * c_price
                                            fee = proceeds * 0.0025
                                            pnl = shares[name] * (c_price - avg_buy_price[name]) - fee
                                            
                                            if pnl < 0:
                                                consecutive_losses[name] += 1
                                                if consecutive_losses[name] >= 2 and cooldown_days > 0:
                                                    cooldown_until[name] = date_val + pd.Timedelta(days=cooldown_days)
                                            else:
                                                consecutive_losses[name] = 0
                                                
                                            realized_pnl[name] += pnl
                                            cash += (proceeds - fee)
                                            trade_stats[name]['fee'] += fee
                                            shares[name] = 0.0
                                            avg_buy_price[name] = 0.0
                                            peak_price_since_buy[name] = 0.0
                            else:
                                for name in stock_dfs:
                                    if shares[name] > 0:
                                        c_price = stock_dfs[name].loc[date_val, 'Close']
                                        proceeds = shares[name] * c_price
                                        fee = proceeds * 0.0025
                                        pnl = shares[name] * (c_price - avg_buy_price[name]) - fee
                                        
                                        if pnl < 0:
                                            consecutive_losses[name] += 1
                                            if consecutive_losses[name] >= 2 and cooldown_days > 0:
                                                cooldown_until[name] = date_val + pd.Timedelta(days=cooldown_days)
                                        else:
                                            consecutive_losses[name] = 0
                                            
                                        realized_pnl[name] += pnl
                                        cash += (proceeds - fee)
                                        trade_stats[name]['fee'] += fee
                                        shares[name] = 0.0
                                        avg_buy_price[name] = 0.0
                                        peak_price_since_buy[name] = 0.0
                                        
                            final_eval = sum(shares[name] * stock_dfs[name].loc[date_val, 'Close'] for name in stock_dfs)
                            portfolio_history.append(max(cash + final_eval, 0))
                            
                            record = {'Date': date_val, '현금(Cash)': max(cash, 0)}
                            for name in stock_dfs:
                                record[name] = shares[name] * stock_dfs[name].loc[date_val, 'Close']
                            history_records.append(record)
                            
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
                        
                        st.success(f"✅ 매수 수수료 오류 산식 완벽 교정 및 백테스트 완료!")
                        col_r1, col_r2 = st.columns(2)
                        col_r1.metric(f"총 초기 자산", f"{init_cash:,.0f} 원")
                        col_r2.metric(f"AI 초과수익 전략 최종 기말 자산 (수익률)", f"{final_asset:,.0f} 원", f"{final_port_ret:+.2f}%")
                        
                        st.markdown("---")
                        
                        st.subheader("📊 [전략 비교] KOSPI 지수 vs 단순보유 vs 적립식 매수 vs AI 초과수익 추구 전략")
                        
                        comparison_data = [
                            {
                                '전략 구분': '🚀 AI 초과수익 전략 (200D Filter + Cooldown)',
                                '최종 기말 자산': f"{final_asset:,.0f} 원",
                                '총 수익률': f"{final_port_ret:+.2f}%",
                                '운용 방식 및 특징': f'200일선 아래 장기 하락 종목 매수 금지 및 연속 2회 손실 종목 {cooldown_days}일 매수 동결. 버퍼({whipsaw_buffer}%) 통과 상승 종목에 집중 배분하여 박스권 수수료 낭비 원천 차단.'
                            },
                            {
                                '전략 구분': '📈 시장 벤치마크 (KOSPI 지수 ^KS11)',
                                '최종 기말 자산': f"{final_kospi_asset:,.0f} 원",
                                '총 수익률': f"{kospi_ret_val:+.2f}%",
                                '운용 방식 및 특징': '한국 종합주가지수(KOSPI) 시장 수익률 추종 (버그 수정 완료)'
                            },
                            {
                                '전략 구분': '📉 단순보유 (Buy & Hold)',
                                '최종 기말 자산': f"{final_bh_asset:,.0f} 원",
                                '총 수익률': f"{final_bh_ret:+.2f}%",
                                '운용 방식 및 특징': '동일 종목 풀 초기 전액 동일 비중 매수 후 매도 없이 홀딩 (변동성 그대로 노출)'
                            },
                            {
                                '전략 구분': '💰 적립식 매수 (DCA)',
                                '최종 기말 자산': f"{final_dca_asset:,.0f} 원",
                                '총 수익률': f"{final_dca_ret:+.2f}%",
                                '운용 방식 및 특징': '동일 종목 풀 시드 분할 후 매월 정기 추가 투입으로 매입단가 분산'
                            }
                        ]
                        st.table(pd.DataFrame(comparison_data))

                        st.markdown("---")
                        
                        st.subheader("📋 종목별 상세 매매 통계 및 성과 분석")
                        summary_rows = []
                        for name in stock_dfs:
                            final_c_price = stock_dfs[name].loc[dates[-1], 'Close']
                            holding_val = shares[name] * final_c_price
                            
                            unrealized_pnl = shares[name] * (final_c_price - avg_buy_price[name]) if shares[name] > 0 else 0.0
                            total_profit = realized_pnl[name] + unrealized_pnl
                            
                            invested_base = max_invested[name] if max_invested[name] > 0 else (init_cash / len(stock_dfs))
                            ret = (total_profit / invested_base) * 100
                            ret = max(ret, -100.0) 
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
                        
                        history_df = pd.DataFrame(history_records).set_index('Date')
                        try:
                            eom_val_df = history_df.resample('ME').last()
                        except ValueError:
                            eom_val_df = history_df.resample('M').last()
                            
                        eom_weights = eom_val_df.div(eom_val_df.sum(axis=1), axis=0) * 100
                        eom_weights = eom_weights.fillna(0)
                        eom_weights.index = eom_weights.index.strftime('%Y-%m')
                        
                        stock_cols = sorted([c for c in eom_weights.columns if c != '현금(Cash)'])
                        cols_ordered = stock_cols + ['현금(Cash)'] 
                        eom_weights = eom_weights[cols_ordered]
                        
                        eom_weights_reset = eom_weights.reset_index().melt('Date', var_name='Asset', value_name='Weight')
                        
                        order_map = {name: i for i, name in enumerate(cols_ordered)}
                        eom_weights_reset['Order'] = eom_weights_reset['Asset'].map(order_map)
                        
                        base_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
                        color_range = [base_colors[i % len(base_colors)] for i in range(len(stock_cols))] + ['#000000']
                        
                        chart = alt.Chart(eom_weights_reset).mark_bar().encode(
                            x=alt.X('Date:O', title='', axis=alt.Axis(labelAngle=-45)),
                            y=alt.Y('Weight:Q', title='비중 (%)', stack='zero'),
                            color=alt.Color('Asset:N', scale=alt.Scale(domain=cols_ordered, range=color_range), title='자산 구분'),
                            order=alt.Order('Order:Q', sort='ascending'),
                            tooltip=['Date', 'Asset', alt.Tooltip('Weight:Q', format='.2f', title='비중(%)')]
                        ).properties(height=450)
                        
                        st.subheader("📊 월말 기준 포트폴리오 비중 추이 (현금 포함, 누적 막대)")
                        st.altair_chart(chart, use_container_width=True)
                        st.info("💡 위 차트는 **현금(블랙)을 항상 최상단에 고정**하여 현재 시장에 노출된 총 주식 비중을 직관적으로 보여주며, 각 종목의 층(Layer)이 항상 일정하여 비중 증감을 쉽게 파악할 수 있습니다.")

with tab3:
    st.header("📄 AI 퀀트 투자 전략 및 운용 알고리즘 백서")
    
    st.markdown("""
    본 대시보드에 탑재된 AI 퀀트 엔진은 단순한 기술적 지표의 조합을 넘어, **시장 거시 지표(Macro), 개별 종목 모멘텀(Micro), 그리고 강력한 리스크 관리(Risk Management)**가 통합된 기관급(Institutional-grade) 다이내믹 자산 배분 알고리즘을 사용합니다.
    """)
    
    st.markdown("---")
    
    st.subheader("1. 핵심 운용 철학 (Core Philosophy)")
    st.markdown("""
    * **추세 추종과 손익 비대칭성 (Let Winners Run, Cut Losses Short):** 확실한 상승 추세에 올라타 이익을 길게 가져가고, 횡보 및 하락장에서는 휩소(잦은 매매)를 차단하여 수수료와 원금을 철저히 방어합니다.
    * **동적 자본 관리 (Dynamic Capital Allocation):** 투자금의 증액/감액을 실시간으로 반영하며, 증액 시 즉시 비중 추가 확대 매수를 지시하고, 감액 시 최약체(모멘텀 하위) 종목부터 기계적으로 청산합니다.
    * **공포 탐욕 지수 역발상 (Contrarian):** 대다수 투자자가 시장을 떠나는 극단적 공포(VIX 폭등 후 꺾임) 시점을 수리적으로 포착하여 V자 반등을 낚아챕니다.
    """)

    st.markdown("---")
    
    st.subheader("2. 실시간 데이터 수집 및 판단 지표 (Data Pipeline)")
    
    col1, col2 = st.columns(2)
    with col1:
        st.info("**📈 거시 지표 (Macro Indicators)**")
        st.markdown(f"""
        * **KOSPI 시장 강도:** KOSPI 지수의 최근 60일 수익률을 계산하여 강세장 여부를 판별하고, 개별 종목의 상대강도(RS)를 비교하는 벤치마크로 사용합니다.
        * **시장 공포지수 (VIX):** 
            * `안정/경계 구간`: VIX < 30 (정상적인 매매 작동)
            * `극단적 공포 (VIX Contrarian)`: VIX $\ge$ 25 이상 고점 도달 후 3일 이동평균선을 하향 이탈할 때 (공포 절정 후 반등 시그널)
        """)
    with col2:
        st.success("**🔎 개별 종목 지표 (Micro Indicators)**")
        st.markdown(f"""
        * **20일/60일/200일 이동평균선:** 단기, 중기, 장기 추세를 정의합니다. 특히 200일선은 대장기 추세를 필터링하는 핵심 방어선입니다.
        * **60일선 기울기 (Slope):** 10일 전의 60일선 값과 비교하여 추세가 위를 향하고 있는지 수치화합니다.
        * **거래량 폭발 (Volume Surge):** 당일 거래량이 최근 5일 평균 거래량 대비 150% 이상 급증했는지 파악하여 세력(수급) 개입을 추적합니다.
        """)

    st.markdown("---")
    
    st.subheader("3. 진입 및 매수 알고리즘 (Entry Rules)")
    st.markdown("대형주(Core)와 중소형주(Satellite)는 시장 특성에 맞춰 완전히 분리된 진입 로직을 사용합니다.")
    
    st.markdown(f"""
    **[대형주 (Core) 전략 4대 필터]**
    1. **대장기 하락장 차단:** 주가가 200일선 위에 위치해야 함.
    2. **가짜 반등 차단:** 단기 20일선이 중기 60일선을 단순 교차를 넘어 **버퍼({whipsaw_buffer}%) 이상 확실하게 돌파**해야 함.
    3. **진짜 추세 검증:** 60일선 우상향(Slope > 0) 및 최근 20일 모멘텀 양수(> 0).
    4. **거시적 승인:** VIX가 30 미만이거나 VIX 역발상 바닥 시그널 발생.
    
    **[중소형주 (Satellite) 전략 전용 필터]**
    * 코스닥의 잦은 휩소를 방지하기 위해 돌파 매매 대신 **주도주 눌림목(Dip-Buying)** 타점을 공략합니다.
    * 수급이 폭발한 주도주가 조정을 받아 **20일선 기준 ±2% 이내로 근접했을 때만** 진입을 승인하여 승률을 극대화합니다.
    """)
    
    st.markdown("---")
    
    st.subheader("4. 자산 배분 및 자본금 증감액 룰 (Capital & Weight Allocation)")
    
    st.markdown(f"""
    * **기본 배분 및 부스터:** 한 종목 최대 투입 비중은 **{max_alloc_pct}%** 로 제한되나, KOSPI 지수가 60일선 위에 있는 대세 상승장에서는 남는 현금을 없애기 위해 최대 투입 한도를 **1.5배** 강제로 상향합니다.
    * **알파 점수 부여 (Score-Tilt):** 수급 폭발(+0.5), 시장 주도주(+0.5), VIX 바닥잡기(+1.0) 조건 만족 시 가점를 부여하여 주도주에 자금을 싹쓸이(Overweight) 합니다.
    * **자본 증액 리밸런싱 (Capital Inflow):** 사이드바의 설정 운용 자금이 늘어나면, **이미 보유 중인 주도주라도 새로 늘어난 한도(Target Weight)만큼 정확히 계산하여 추가 매수를 지시**합니다.
    * **자본 감액 최약체 청산 (Deficit Liquidation):** 운용 자본이 현재 주식 평가액보다 낮게 감액 설정될 경우, 부족한 현금을 마련하기 위해 **'AI 스코어 하위 ➔ 20일 모멘텀 하위'** 순서로 가장 부진한 종목부터 기계적으로 부분/전량 매도 지시를 내립니다.
    """)

    st.markdown("---")
    
    st.subheader("5. 리스크 관리 및 청산 알고리즘 (Exit Rules)")
    
    st.warning("**🔻 매도 및 방어 규정**")
    st.markdown(f"""
    * **추세 이탈 (데드크로스):** 20일선이 60일선을 하향 이탈하되, 잦은 손절을 막기 위해 버퍼의 절반({whipsaw_buffer/2}%) 이상 뚫고 내려갈 때 전량 매도합니다.
    * **트레일링 스탑 익절 (Trailing Stop):** 수익이 **{ts_target_pct}%** 에 도달한 이후부터 룰이 켜집니다. 이후 최고점 대비 **{abs(ts_drop_pct)}%** 이상 하락하면 더 기다리지 않고 즉시 수익을 확정 짓습니다.
    * **연속 손실 쿨다운 (Cooldown):** 횡보 박스권에 갇혀 연속으로 2회 손실을 발생시킨 종목은 **{cooldown_days}일 동안 강제로 매수를 금지(격리)** 시켜 수수료 누수를 막습니다.
    * **최소 보유 기간:** 한 번 매수하면 잔파도에 털리지 않도록 최소 **{min_hold_days}일** 간은 강제로 홀딩합니다. (단, 하드 손절컷 {sat_stop_loss}% 도달 시 예외적 즉각 탈출)
    """)
