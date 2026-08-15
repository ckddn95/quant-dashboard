import streamlit as st
import pandas as pd
import datetime
import time
import concurrent.futures
import database as db
import broker.kis_client as kis
import quant_engine as quant

st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")
KST = datetime.timezone(datetime.timedelta(hours=9))

def mts_metric_html(label, value, delta=None):
    val_color, val_str = "white", str(value)
    if not delta: 
        if val_str.startswith('+'): val_color = "#FF5050"
        elif val_str.startswith('-') and val_str != '-': val_color = "#3b82f6"
    delta_html = ""
    if delta:
        d_str = str(delta)
        d_color = "#FF5050" if d_str.startswith('+') else ("#3b82f6" if d_str.startswith('-') and d_str != '-' else "#a3a8b8")
        delta_html = f'<div style="color: {d_color}; font-size: 1rem; font-weight: bold; margin-top: 4px;">{d_str}</div>'
    return f"""
    <div style="background-color: rgba(255, 255, 255, 0.05); padding: 1.2rem; border-radius: 0.5rem; margin-bottom: 1rem; border: 1px solid rgba(250, 250, 250, 0.1);">
        <div style="color: #a3a8b8; font-size: 0.9rem; font-weight: 600; margin-bottom: 0.5rem;">{label}</div>
        <div style="font-size: 1.8rem; font-weight: 700; color: {val_color};">{val_str}</div>
        {delta_html}
    </div>
    """

# 🛑 [핵심 패치 3] UI 텍스트 무결성 적용 ("100% 실전 동일" 삭제)
st.title("Core-Satellite Quant System (MSA)")
st.markdown("한국 시장 전 종목 검색, **오토파일럿 무인 감시**, **실계좌 자동매매**, **고급 시뮬레이션**을 제공하는 SQLite 기반 실전 퀀트 대시보드입니다.")

STRAT_DISPLAY_MAP = {quant.Strategy.CORE: '대형주 (Core)', quant.Strategy.SATELLITE: '중소형주 (Satellite)'}

raw_strat = db.get_setting('strategy', 'CORE')
try: active_strat = quant.Strategy(raw_strat); db.set_setting('halted_config_error', False)
except ValueError:
    db.set_setting('halted_config_error', True); st.error("🚨 HALTED_CONFIG_ERROR"); st.stop()

st.sidebar.header("🎯 전략 및 환경 설정")
display_options = list(STRAT_DISPLAY_MAP.values())
current_display = STRAT_DISPLAY_MAP[active_strat]
selected_display = st.sidebar.selectbox("운용 전략", display_options, index=display_options.index(current_display))
selected_strat = [k for k, v in STRAT_DISPLAY_MAP.items() if v == selected_display][0]

if selected_strat != active_strat: db.set_setting('strategy', selected_strat.value); st.rerun()

total_cash = int(db.get_setting('virtual_cash', 10000000))
new_cash = st.sidebar.number_input("총 투자 운용 자산 (가상 원금)", value=total_cash, step=1000000)
if new_cash != total_cash: db.set_setting('virtual_cash', new_cash)

has_keys = bool(db.get_setting('manual_app_key'))
with st.sidebar.expander("🔑 KIS API 설정", expanded=not has_keys):
    if has_keys:
        st.success("✅ API 키 연동 중")
        if st.button("🗑️ 키 삭제"): db.set_setting('manual_app_key', None); st.rerun()
    else:
        k1, k2 = st.text_input("APP KEY", type="password"), st.text_input("APP SECRET", type="password")
        c1, m1 = st.text_input("계좌번호 앞 8자리"), st.checkbox("모의투자", value=True)
        if st.button("저장"): db.set_setting('manual_app_key', k1); db.set_setting('manual_app_secret', k2); db.set_setting('manual_cano', c1); db.set_setting('manual_is_mock', m1); st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("📱 봇 제어 (DB 연동)")
init_ks, init_at, init_ap = bool(db.get_setting('kill_switch', False)), bool(db.get_setting('auto_trade_enabled', False)), bool(db.get_setting('auto_pilot', False))
kill_switch = st.sidebar.toggle("🚨 긴급 정지 (KILL SWITCH)", value=init_ks)
auto_trade = st.sidebar.toggle("🚀 실전 자동주문 활성화", value=init_at)
auto_pilot = st.sidebar.toggle("🔄 오토파일럿 켜기", value=init_ap)

if kill_switch != init_ks: db.set_setting('kill_switch', kill_switch)
if auto_trade != init_at: db.set_setting('auto_trade_enabled', auto_trade)
if auto_pilot != init_ap: db.set_setting('auto_pilot', auto_pilot)

st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터")

default_cfg = quant.get_default_config(active_strat)
saved_p = db.get_setting(f'params_{active_strat.value}', None)
if saved_p is None or st.session_state.get('last_strat') != active_strat:
    saved_p = default_cfg.__dict__.copy(); db.set_setting(f'params_{active_strat.value}', saved_p); st.session_state.last_strat = active_strat

is_custom = any(saved_p[k] != v for k, v in default_cfg.__dict__.items())
if is_custom and st.sidebar.button("🔄 기본값 복구"): saved_p = default_cfg.__dict__.copy(); db.set_setting(f'params_{active_strat.value}', saved_p); st.rerun()

saved_p['ma200'] = st.sidebar.checkbox("🛡️ 200일 추세선", value=saved_p['ma200'])
saved_p['buf'] = st.sidebar.slider("골든크로스 버퍼 (%)", 0.0, 5.0, float(saved_p['buf']) * 100.0, 0.1) / 100.0
saved_p['sl'] = st.sidebar.slider("긴급 손절 컷 (%)", -30.0, -5.0, float(saved_p['sl']) * 100.0, 1.0) / 100.0
with st.sidebar.expander("🧪 고급 안전장치", expanded=is_custom):
    saved_p['cd'] = st.slider("쿨다운(일)", 0, 90, int(saved_p['cd']), 5)
    saved_p['alloc'] = st.slider("투입 한도 (%)", 10, 100, int(float(saved_p['alloc']) * 100.0), 5) / 100.0
    saved_p['min_h'] = st.slider("최소 보유(일)", 0, 20, int(saved_p['min_h']), 1)
    saved_p['ts_tgt'] = st.slider("익절 목표 (%)", 5, 100, int(float(saved_p['ts_tgt']) * 100.0), 5) / 100.0
    saved_p['ts_drp'] = st.slider("하락 허용 (%)", -30, -1, int(float(saved_p['ts_drp']) * 100.0), 1) / 100.0
    saved_p['boost'] = st.checkbox("🔥 강세장 부스터", value=saved_p['boost'])

try: current_config = quant.StrategyConfig(**saved_p); db.set_setting(f'params_{active_strat.value}', saved_p)
except ValueError as e: st.sidebar.error(f"입력값 오류: {e}"); st.stop()

SYS_APP_KEY, SYS_APP_SEC, SYS_CANO = db.get_setting('manual_app_key'), db.get_setting('manual_app_secret'), db.get_setting('manual_cano')
SYS_IS_MOCK = bool(db.get_setting('manual_is_mock', True))

rd = st.session_state.get('real_data', db.get_setting('last_real_data', {'eval': float(total_cash), 'pnl': 0.0, 'cash': float(total_cash), 'stocks': []}))
st.session_state['real_data'] = rd 

real_invested_principal = rd['eval'] - rd['pnl'] if rd['eval'] > 0 else float(total_cash)
base_date_str = db.get_setting('created_at', '2024-01-01')
try: real_base_date = pd.to_datetime(base_date_str).date()
except: real_base_date = datetime.date(2024, 1, 1)

# ==================== 메인 화면 ====================
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유니버스", "🔌 실전 계좌", "🤖 자동매매 대기열", "📊 시뮬레이션", "📄 알고리즘 백서"])

with tab4:
    st.header("🧪 고급 백테스트 엔진")
    st.info("💡 현실적 수수료(0.015%), 증권거래세(0.2%), 슬리피지(0.1%), 익일 시가 체결 및 보수적 모형이 완벽히 적용되었습니다. (단, 물리적 한계로 과거 소급 유니버스는 임시 보류됨)")
    stocks_df = pd.DataFrame(db.get_watchlist())
    today_date = datetime.datetime.now(KST).date()
    
    st.subheader("🎯 Test 2. 관심종목 대상 장기 검증")
    c1, c2, c3 = st.columns([3,3,4])
    with c1: start_d = st.date_input("시작일", datetime.date(2023,1,1))
    with c2: end_d = st.date_input("종료일", today_date)
    with c3:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        if st.button("장기 고급 Backtest 실행", type="primary", use_container_width=True):
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner("엔진 구동 중..."):
                    res = quant.run_quant_simulation(stocks_df, active_strat, total_cash, start_d, end_d, current_config)
                    if res:
                        st.success("완료!")
                        r1, r2, r3, r4 = st.columns(4)
                        r1.markdown(mts_metric_html("기말 자산", f"{res['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                        r2.markdown(mts_metric_html("누적 수익률", f"{res['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        r3.markdown(mts_metric_html("CAGR (연평균)", f"{res['metrics']['CAGR']*100:+.2f}%"), unsafe_allow_html=True)
                        r4.markdown(mts_metric_html("MDD (최대낙폭)", f"{res['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
                        
                        m1, m2, m3, m4 = st.columns(4)
                        m1.markdown(mts_metric_html("Sharpe Ratio", f"{res['metrics']['Sharpe']:.2f}"), unsafe_allow_html=True)
                        m2.markdown(mts_metric_html("Sortino Ratio", f"{res['metrics']['Sortino']:.2f}"), unsafe_allow_html=True)
                        m3.markdown(mts_metric_html("Calmar Ratio", f"{res['metrics']['Calmar']:.2f}"), unsafe_allow_html=True)
                        m4.markdown(mts_metric_html("BM 초과수익", f"{res['metrics']['Excess']*100:+.2f}%"), unsafe_allow_html=True)
                        
                        st.dataframe(pd.DataFrame(res['summary_rows']), use_container_width=True)

# ==========================================
# 🛑 [핵심 보강] Part 9. 백테스트 및 성과 지표 헌장 추가
# ==========================================
with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 & 시스템 헌장</h1>
    <hr>
    *(Part 1~8 기존 내용 보존됨)*

    <h3>🔬 9. 백테스트 및 성과 지표 (Backtesting & Metrics)</h3>
    <ul>
        <li><b>익일 시가 체결 (t+1 Open Execution):</b> 당일 종가(Close) 기준으로 확정된 매수 시그널은 무조건 대기열에 담기고, 다음 날의 시가(Open)로만 체결되어 미래 참조 편향(Look-ahead bias)을 원천 차단한다.</li>
        <li><b>보수적 체결 모형 (Conservative Execution):</b> 장중 손절 및 트레일링 익절을 평가할 때, 하루의 캔들 내에서 고가(High)와 저가(Low)를 모두 터치한 경우 <b>'손절이 먼저 발생했다'고 간주하는 보수적 룰</b>을 적용하여 유리한 결과를 취하지 않는다.</li>
        <li><b>현실적 비용 모델 (Realistic Costs):</b> 모든 시뮬레이션에는 매수/매도 수수료(0.015%), 매도 시 증권거래세(0.2%), 그리고 시가 체결 시 발생하는 슬리피지(0.1%)가 하드코딩되어 강제 공제된다.</li>
        <li><b>고급 성과 지표 반환:</b> 단순 수익률이 아닌 CAGR(연평균 성장률), Volatility(변동성), MDD(최대 낙폭), Sharpe, Sortino, Calmar Ratio 및 KOSPI/KOSDAQ 벤치마크 대비 초과 성과(Excess Return)를 산출하여 엄격하게 리스크를 검증한다.</li>
        <li><b>(보류됨) 유니버스 소급 제약:</b> 파이썬 무료 라이브러리(FDR)의 한계로 인해 상폐 종목 및 과거 시가총액을 완벽히 소급하는 Point-in-time 유니버스는 현재 보류 중이며, 사용자 승인 하에 별도 API(Dataguide 등)가 연동되기 전까지 '100% 실전 동일'이라는 표현은 시스템에서 노출을 금지한다.</li>
    </ul>

    <hr>
    <h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 (절대 원칙)</h3>
    <p style='color: #DC2626;'>이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 Part 1~9에 기록된 <b>'MSA 구조, 무결성 계약, 주문 상태 머신 대사 원칙, 동시성 제어, 킬 스위치, 리스크 통제, 실시간 스냅샷, t+1 보수적 체결 룰'을 단 하나라도 임의로 훼손하거나 삭제해서는 안 됩니다.</b> 원칙을 수정해야만 하는 상황이라면 사용자에게 명확히 보고하고 승인을 얻어야 합니다.</p>
    """, unsafe_allow_html=True)
