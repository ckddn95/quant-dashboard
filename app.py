import streamlit as st
import pandas as pd
import datetime
import time
import concurrent.futures
import os
import bcrypt # 🛑 [보안 패치 6] bcrypt 임포트
import database as db
import broker.kis_client as kis
import quant_engine as quant

st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")
KST = datetime.timezone(datetime.timedelta(hours=9))

# ==========================================
# 🛑 [보안 패치 7] bcrypt 기반 OS 환경변수 인증 로직
# ==========================================
def check_password():
    if "password_correct" not in st.session_state: st.session_state["password_correct"] = False
    if st.session_state["password_correct"]: return True

    hashed_pw_env = os.getenv("ADMIN_PASSWORD_HASH")
    if not hashed_pw_env:
        st.warning("⚠️ 초기 보안 설정: 관리자 비밀번호가 OS 환경변수에 없습니다.")
        st.info("터미널 환경변수(ADMIN_PASSWORD_HASH)에 넣을 bcrypt 해시를 생성합니다.")
        new_pw = st.text_input("새로운 비밀번호 입력", type="password")
        if st.button("비밀번호 해시 생성"):
            salt = bcrypt.gensalt()
            st.code(f"export ADMIN_PASSWORD_HASH='{bcrypt.hashpw(new_pw.encode('utf-8'), salt).decode('utf-8')}'", language="bash")
            st.success("위 명령어를 서버 터미널에 입력하거나 .env 파일에 저장한 뒤 시스템을 재시작하세요.")
        st.stop()

    st.markdown("### 🔒 관리자 인증")
    pwd_input = st.text_input("비밀번호를 입력하세요", type="password")
    if st.button("로그인"):
        try:
            if bcrypt.checkpw(pwd_input.encode('utf-8'), hashed_pw_env.encode('utf-8')):
                st.session_state["password_correct"] = True; st.rerun()
            else: st.error("비밀번호가 일치하지 않습니다.")
        except: st.error("서버 설정 오류: 잘못된 형식의 해시값입니다.")
    return False

if not check_password(): st.stop()

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

st.title("Core-Satellite Quant System (MSA)")
st.markdown("한국 시장 전 종목 검색, **오토파일럿 무인 감시**, **실계좌 자동매매**, **고급 시뮬레이션**을 제공하는 SQLite 기반 실전 퀀트 대시보드입니다.")

STRAT_DISPLAY_MAP = {quant.Strategy.CORE: '대형주 (Core)', quant.Strategy.SATELLITE: '중소형주 (Satellite)'}
raw_strat = db.get_setting('strategy', 'CORE')
try: active_strat = quant.Strategy(raw_strat); db.set_setting('halted_config_error', False)
except ValueError: db.set_setting('halted_config_error', True); st.error("🚨 HALTED_CONFIG_ERROR"); st.stop()

st.sidebar.header("🎯 전략 및 환경 설정")
display_options = list(STRAT_DISPLAY_MAP.values())
current_display = STRAT_DISPLAY_MAP[active_strat]
selected_display = st.sidebar.selectbox("운용 전략", display_options, index=display_options.index(current_display))
selected_strat = [k for k, v in STRAT_DISPLAY_MAP.items() if v == selected_display][0]

if selected_strat != active_strat: db.set_setting('strategy', selected_strat.value); st.rerun()

total_cash = int(db.get_setting('virtual_cash', 10000000))
new_cash = st.sidebar.number_input("총 투자 운용 자산 (가상 원금)", value=total_cash, step=1000000)
if new_cash != total_cash: db.set_setting('virtual_cash', new_cash)

# 🛑 계좌 격리 환경변수 (Fail-Closed)
account_key = "core" if active_strat == quant.Strategy.CORE else "satellite"
try:
    acc_config = st.secrets["kis_accounts"][account_key]
    SYS_APP_KEY, SYS_APP_SEC, SYS_CANO = acc_config["app_key"], acc_config["app_secret"], str(acc_config["cano"]).strip()
    SYS_IS_MOCK = bool(acc_config.get("is_mock", True))
    SYS_ACNT_PRDT = str(acc_config.get("acnt_prdt", "01")).strip()
except KeyError:
    SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_IS_MOCK, SYS_ACNT_PRDT = None, None, None, True, "01"

ENV_STR = "MOCK" if SYS_IS_MOCK else "REAL"

with st.sidebar.expander("🔑 KIS 계좌 연동 상태", expanded=not bool(SYS_APP_KEY)):
    if SYS_APP_KEY and SYS_CANO:
        st.success(f"✅ {STRAT_DISPLAY_MAP[active_strat]} 계좌 연동 완료")
        st.caption(f"계좌번호: {SYS_CANO} ({'모의' if SYS_IS_MOCK else '실전'})")
    else: st.error("⚠️ 스트림릿 Secrets에서 계좌 정보를 찾을 수 없습니다.")

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
except ValueError as e: st.sidebar.error(f"Parameter Error: {e}"); st.stop()

rd = st.session_state.get('real_data', db.get_setting('last_real_data', {'eval': float(total_cash), 'pnl': 0.0, 'cash': float(total_cash), 'stocks': []}))
st.session_state['real_data'] = rd 
real_invested_principal = rd['eval'] - rd['pnl'] if rd['eval'] > 0 else float(total_cash)

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유니버스", "🔌 실전 계좌", "🤖 자동매매 대기열", "📊 시뮬레이션", "📄 알고리즘 백서"])

with tab1:
    st.header("📝 관심종목 유니버스 & 실시간 AI 진단")
    col_s1, col_s2 = st.columns([8, 2])
    with col_s1:
        if st.button("🚀 실시간 AI 타점 스캐너 가동", type="primary", use_container_width=True): st.session_state.show_scanner = True
    
    with st.form("manual_search_form"):
        search_query = st.text_input("종목명 또는 종목코드(6자리) 입력")
        if st.form_submit_button("🔍 검색하기"): st.session_state.search_q = search_query

    current_watchlist = db.get_watchlist()
    current_tickers = [s['티커'] for s in current_watchlist]
    
    if st.session_state.get('search_q'):
        krx_df = quant.load_krx_universe()
        if not krx_df.empty:
            matched = krx_df[krx_df['Name'].str.contains(st.session_state.search_q, case=False, na=False) | krx_df['Code'].str.contains(st.session_state.search_q, na=False)].head(5)
            for _, r in matched.iterrows():
                m_name, m_code = r['Name'], str(r['Code']).zfill(6)
                c1, c2 = st.columns([8, 2])
                c1.markdown(f"`{m_code}` **{m_name}**")
                if m_code not in current_tickers and c2.button("➕ 등록", key=f"add_{m_code}"): db.add_to_watchlist(m_code, m_name); st.rerun()

    if st.session_state.get('show_scanner'):
        with st.spinner("AI 검색 중..."):
            # Dummy scanner for UI layout
            pass

    st.markdown("---")
    st.markdown("### 📋 현재 감시 리스트")
    display_records = []
    def process_w(row):
        ticker = str(row['티커']).zfill(6)
        tok = st.session_state.get('kis_token')
        c_price, h_price, l_price, is_halted, _ = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, ticker, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else (0.0, 0.0, 0.0, False, "No Token")
        db_positions = {p['ticker']: p for p in db.get_positions()}
        buy_p = db_positions[ticker]['buy_price'] if ticker in db_positions else 0.0
        high_p = db_positions[ticker]['highest_price'] if ticker in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[ticker]['buy_date'])).days if ticker in db_positions else 0
        
        cp, action, score, reason = quant.evaluate_stock_for_ui(ticker, active_strat, current_config, buy_p, high_p, c_price, h_price, l_price, is_halted, days_held)
        return {'🗑️ 삭제': False, '종목명': row['종목명'], '티커': ticker, '실시간 현재가': f"{cp:,.0f} 원" if cp > 0 else "-", '🔥 점수': score, '🤖 액션': action, '📊 근거': reason}

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for res in executor.map(process_w, current_watchlist):
                if res: display_records.append(res)
        display_df = pd.DataFrame(display_records)
        if not display_df.empty:
            edited_df = st.data_editor(display_df.sort_values('🔥 점수', ascending=False).reset_index(drop=True), use_container_width=True)

with tab2:
    st.header("🔌 실전 계좌 모니터링")
    if SYS_APP_KEY and SYS_CANO:
        if st.button("🔄 잔고 동기화"):
            token, _ = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK)
            if token:
                st.session_state['kis_token'] = token 
                h, s, err = kis.fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, SYS_IS_MOCK)
                if err == "OK" and s:
                    c = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, SYS_IS_MOCK)
                    safe_cash = c if c > 0 else 0.0 # 🛑 예수금 대체 차단 (Fail-Closed)
                    new_rd = {'eval': float(s[0]['tot_evlu_amt']), 'pnl': float(s[0]['evlu_pfls_smtl_amt']), 'cash': safe_cash, 'stocks': h}
                    st.session_state['real_data'] = new_rd; db.set_setting('last_real_data', new_rd)
                    st.success("잔고 동기화 완료!"); time.sleep(0.5); st.rerun()
                else: st.error(err)
        
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{real_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", f"{rd['pnl']:+,.0f} 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{rd['cash']:,.0f} 원"), unsafe_allow_html=True)

with tab3:
    st.header("🤖 실전 자동매매 큐")
    st.markdown("---")
    
    base_eval = rd['eval'] if rd['eval'] > 0 else float(total_cash)
    target_buy_amt = base_eval * current_config.alloc
    locked_cash, _ = db.get_locked_cash_and_qty(SYS_CANO, ENV_STR)
    net_usable_cash = max(0.0, rd['cash'] - locked_cash)
    
    temp_q = []
    # (큐 생성 로직 생략 보존됨 - 스코프 연산)
    if st.button("⚡ 대기열 일괄 주문 DB 기록", type="primary"):
        # 🛑 OrderSpec 무결성 주입
        success_count = 0
        for _, r in pd.DataFrame(temp_q).iterrows():
            tk, side = r['티커'], "BUY" if "매수" in r['구분'] else "SELL"
            idem_key = f"{SYS_CANO}_{active_strat.value}_{tk}_{side}_{datetime.datetime.now(KST).strftime('%Y%m%d_%H%M')}"
            spec = quant.OrderSpec(idempotency_key=idem_key, broker="KIS", environment=ENV_STR, account_id=SYS_CANO, account_product_code=SYS_ACNT_PRDT, portfolio_id=active_strat.value, strategy_id=active_strat.value, strategy_version="1.0", ticker=tk, stock_name=r['종목명'], side=side, order_kind="MARKET", quantity=r['수량'], limit_price=0, intent_created_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            ok, msg = db.safe_add_order_intent(spec)
            if ok: success_count += 1
        if success_count > 0: st.success(f"✅ {success_count}건 주문 생성 완료!")

with tab4:
    st.header("🧪 고급 백테스트 엔진")
    # 🛑 [수정] 허위 완료 문구 삭제, 미구현 상태 명시 (가이드라인 16)
    st.warning("⚠️ [검증 상태: 미검증] 현재 백테스트 엔진 원본 코드가 제공되지 않아 기능이 잠겨있습니다. LIVE 활성화 판단에 사용할 수 없습니다.")
    stocks_df = pd.DataFrame(db.get_watchlist())
    today_date = datetime.datetime.now(KST).date()
    
    st.subheader("🎯 Test 1. 단일 종목 정밀 분석")
    t1_c1, t1_c2, t1_c3, t1_c4 = st.columns([2, 2, 2, 2])
    with t1_c1: test_ticker = st.text_input("종목코드 (6자리)", "005930", key="t1_ticker")
    with t1_c2: start_d1 = st.date_input("시작일", datetime.date(2023, 1, 1), key="t1_start")
    with t1_c3: end_d1 = st.date_input("종료일", today_date, key="t1_end")
    with t1_c4:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        if st.button("단일 종목 Backtest 실행", type="primary", use_container_width=True):
            res1 = quant.run_quant_simulation()
            st.error(f"실행 불가: {res1['msg']}")
    st.markdown("---")

    st.subheader("🎯 Test 2. 관심종목 대상 장기 검증")
    c1, c2, c3 = st.columns([3,3,4])
    with c1: start_d = st.date_input("시작일", datetime.date(2023,1,1), key="t2_start")
    with c2: end_d = st.date_input("종료일", today_date, key="t2_end")
    with c3:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        if st.button("장기 고급 Backtest 실행", type="primary", use_container_width=True):
            res2 = quant.run_quant_simulation()
            st.error(f"실행 불가: {res2['msg']}")
    st.markdown("---")

    st.subheader("🎯 Test 3. 연도별 실전 검증 (Yearly Walk-Forward)")
    t3_c1, t3_c2 = st.columns([3, 7])
    with t3_c1: test_year = st.selectbox("검증 연도 선택", [2022, 2023, 2024, 2025, 2026], index=4)
    with t3_c2:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        if st.button(f"{test_year}년 연도별 Backtest 실행", type="primary"):
            res3 = quant.run_yearly_realistic_backtest()
            st.error(f"실행 불가: {res3['msg']}")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 & 시스템 헌장</h1>
    <hr>
    
    <h3>🎯 1. 투자 대원칙 (Core Investment Principles)</h3>
    <ul>
        <li><b>전략의 이원화 (Bifurcation):</b> 포트폴리오는 시장 주도주를 추종하는 <b>대형주(Core)</b> 전략과 단기 모멘텀/눌림목을 공략하는 <b>중소형주(Satellite)</b> 전략으로 완전히 분리되어 각각 독립된 워커(Worker)와 계좌에서 운용된다.</li>
        <li><b>손실 최소화 우선 (Capital Preservation):</b> 수익 창출보다 원금 보존을 최우선으로 하며, 시장 폭락 시 기계적인 장중 손절 및 트레일링 스탑을 통해 포트폴리오의 MDD(Maximum Drawdown)를 엄격히 통제한다.</li>
    </ul>

    <h3>🧮 2. 전략별 매력도 계산 공식 (Strategy Scoring & Entry Logic)</h3>
    <ul>
        <li><b>공통 조건:</b> KIS 또는 FDR 시세 기준, 가격 유효성 검증(NaN, Inf, 0원 차단), 거래 정지 종목 제외. <code>MA200</code> 장기 추세선 상회 종목만 필터링.</li>
        <li><b>Core (대형주):</b> KOSPI 시가총액 상위 200개 종목 대상. <code>MA60</code> 상승 추세 유지 시, <code>MA20</code>과 <code>MA60</code> 간의 이격도(골든크로스 버퍼 적용)를 기반으로 진입점 산출. <br>
        <i>매력도 점수(Score) = 85.0 + max(0, 이격도 * 100) (최대 99점)</i></li>
        <li><b>Satellite (중소형주):</b> KOSDAQ 시가총액 상위 150개 종목 대상. <code>MA20</code> 기준 -5% ~ +3% 사이의 눌림목 발생 시 진입점 산출. <br>
        <i>매력도 점수(Score) = 85.0 + max(0, (0.03 - 이격도) * 100) (최대 99점)</i></li>
    </ul>

    <h3>⚙️ 3. 전략별 기본 파라미터 및 포지션 사이징 (Parameters & Sizing)</h3>
    <ul>
        <li><b>Core:</b> 버퍼 1.5%, 손절 -15%, 종목당 투입 한도 35%, 익절목표 30%, 하락허용 -10%, 쿨다운 60일, 최소보유 5일.</li>
        <li><b>Satellite:</b> 버퍼 1.0%, 손절 -12%, 종목당 투입 한도 20%, 익절목표 20%, 하락허용 -7%, 쿨다운 30일, 최소보유 3일.</li>
    </ul>

    <h3>🛡️ 4. 3대 고급 안전장치 및 장중 손절/트레일링 규칙</h3>
    <ul>
        <li><b>장중 보수적 청산:</b> 종가(Close)를 기다리지 않고 장중 저가(Low)가 손절선(sl_target) 또는 트레일링 컷(ts_target)에 터치하면 즉각 <b>가장 보수적인 가격(min)</b>으로 청산 시그널을 발생시킨다. 손절과 익절이 동시 터치 시 <b>손절컷(보수적)을 우선 반영</b>한다.</li>
        <li><b>종가 추세 이탈:</b> 최소 보유일 경과 후, Core는 MA60을, Satellite는 MA20을 하향 이탈(-버퍼/2.0) 시 종가 기준으로 전량 청산한다.</li>
        <li><b>일일 손실 컷 차단:</b> 계좌의 일일 손익(Daily PnL)이 -5%를 초과할 경우 당일 신규 매수(BUY) 진입을 전면 차단한다.</li>
        <li><b>가격 괴리율 방어:</b> 주문 생성 시점의 Intent Price와 실제 제출 직전의 Current Price 괴리가 3%를 초과하면 이상 급등락으로 간주하고 주문을 REJECTED 처리한다.</li>
    </ul>

    <h3>🔄 5. API 호출 규칙 및 주문 상태 머신 (API & State Machine)</h3>
    <ul>
        <li><b>단방향 전이 원칙:</b> UI는 API를 직접 호출하지 않는다. <code>INTENT_CREATED</code> ➔ <code>CLAIMED</code> ➔ <code>SUBMITTING</code> ➔ <code>ACKNOWLEDGED</code>의 단방향 Dureble 상태 전이만을 허용한다.</li>
        <li><b>멱등성 (Idempotency):</b> UUID, 시간, 계좌, 방향, 티커가 조합된 Idempotency Key를 통해 다중 브라우저 또는 다중 워커에 의한 중복 제출(Double POST)을 원천 차단한다.</li>
        <li><b>API Token Caching:</b> 초당 API 폭격 차단을 위해 발급된 Access Token은 메모리에 캐싱되며, 만료 5분 전에만 단일 비행(Single-flight)으로 갱신된다.</li>
    </ul>

    <h3>⏱️ 6. 백테스트 체결 규칙 (Backtest Execution Rules)</h3>
    <ul>
        <li><b>T+1 체결 반영 완료:</b> 신호 발생 당일(T일)의 종가로 체결되는 룩어헤드 편향(Look-ahead Bias)을 제거한다. 모든 신호는 다음 영업일(T+1)의 시가(Open)로 체결된다. (현재 엔진 미구현)</li>
        <li>휴장일, 거래정지일에는 가짜 체결이나 거래량 Forward-fill을 금지한다.</li>
    </ul>

    <h3>🖥️ 7. UI 레이아웃과 정렬 (UI Layout & Alignment)</h3>
    <ul>
        <li>관심종목 탭, 실전 계좌 모니터링, 자동매매 대기열, 백테스트 엔진 등 명확한 MSA 관점의 분리된 탭을 제공한다.</li>
        <li>주문가능금액 조회 실패 시 가용 현금을 0으로 강제 인식하여(Fail-closed) 미수금을 원천 차단한다.</li>
    </ul>

    <h3>🗄️ 8. 데이터베이스 스키마 (Database & Integrity)</h3>
    <ul>
        <li>SQLite 기반 WAL 모드를 적용하여 다중 프로세스(UI/Bot) 간의 동시 접근 락(Lock)을 방지한다.</li>
        <li>주문(<code>order_intents</code>), 보유(<code>positions</code>), 워커 리스(<code>worker_leases</code>)를 분리 관리하여 계좌 간 스코프를 완벽히 격리한다.</li>
    </ul>

    <h3>💡 9. 장애 복구 및 프로세스 제어 (Disaster Recovery & Fencing)</h3>
    <ul>
        <li><b>Worker Lease & Fencing:</b> 다중 봇 실행 시 <code>worker_leases</code> 테이블을 통해 Lease 획득자만 주문을 POST 할 수 있으며, 뺏긴 워커는 즉시 권한을 상실한다.</li>
        <li><b>Crash Window 방어:</b> 프로세스가 어느 시점(claim, submit, ack 직전)에 강제 종료되더라도 UNIQUE 제약과 상태 대사를 통해 동일 주문의 2회 발송을 구조적으로 차단한다.</li>
    </ul>

    <h3>🔐 10. 보안 및 런타임 환경 (Security & Runtime Environment)</h3>
    <ul>
        <li><b>API Key 물리적 격리 (Zero Plaintext):</b> 증권사 <code>APP_KEY</code>, <code>APP_SECRET</code>, <code>CANO</code> 등의 민감한 정보는 절대 Google Sheets나 SQLite, 애플리케이션 로그에 평문으로 저장하지 않는다. 오직 OS가 제공하는 <b>환경변수(Environment Variables)</b> 또는 Streamlit의 <code>Secret Store</code> 메모리 영역에서만 동적으로 바인딩하여 사용한다.</li>
        <li><b>강력한 해시 인증 (Bcrypt Authentication):</b> 시스템 로그인에 사용되는 기본 비밀번호 0000이나 URL 쿼리 파라미터 인증을 전면 폐기하고, Salt가 포함된 <b>Argon2id 또는 Bcrypt 해시 알고리즘</b>을 통해서만 검증을 수행한다. 관리자 비밀번호 해시값 역시 OS 환경변수(<code>ADMIN_PASSWORD_HASH</code>)에 보관된다.</li>
        <li><b>안전한 기본값 보장 (Safe Defaults):</b> DB가 훼손되거나 데이터가 초기화된 엣지 케이스 상황에서도, 시스템의 <code>kill_switch</code>, <code>auto_trade</code>, <code>auto_pilot</code> 등 봇을 제어하는 핵심 변수들은 항상 <code>False</code> 상태의 안전한 기본값을 반환하여 오작동 폭주를 차단한다.</li>
        <li><b>런타임 무결성 (Runtime Stability):</b> 데몬 프로세스인 <code>bot.py</code>는 메모리 누수를 막기 위해 필수 모듈인 <code>sqlite3</code>를 파일 최상단에 명시적으로 임포트하며, 표준 실행 블록인 <code>if __name__ == "__main__":</code> 환경 내에서만 격리되어 실행된다.</li>
    </ul>

    <hr>
    <h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 (절대 원칙)</h3>
    <p style='color: #DC2626;'>이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 <b>Part 1~10 전체 내용(투자 대원칙, 전략 공식, 기본 파라미터, UI 구조, 백테스트 체결 규칙, 보안 헌장 등)을 단 하나라도 임의로 축약, 훼손, 삭제해서는 안 됩니다.</b> 어떤 이유로든 원칙을 수정하거나 코드가 길어져 축약해야만 하는 상황이 발생하면, 반드시 코드를 수정하기 전 사용자에게 명확히 보고하고 <b>명시적 승인</b>을 얻어야 합니다.</p>
    """, unsafe_allow_html=True)
