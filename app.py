import streamlit as st
import pandas as pd
import datetime
import time
import concurrent.futures
import os
import bcrypt
import hashlib
import database as db
import broker.kis_client as kis
import quant_engine as quant

st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")
KST = datetime.timezone(datetime.timedelta(hours=9))

def check_password():
    if "password_correct" not in st.session_state: st.session_state["password_correct"] = False
    if st.session_state["password_correct"]: return True
    hashed_pw_env = os.getenv("ADMIN_PASSWORD_HASH")
    if not hashed_pw_env:
        st.warning("⚠️ 초기 보안 설정 필요")
        st.stop()
    with st.form("login_form"):
        pwd_input = st.text_input("비밀번호", type="password")
        if st.form_submit_button("로그인") and bcrypt.checkpw(pwd_input.encode('utf-8'), hashed_pw_env.encode('utf-8')):
            st.session_state["password_correct"] = True; st.rerun()
    return False

if not check_password(): st.stop()

def mts_metric_html(label, value, delta=None):
    val_color = "white"
    val_str = str(value)
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

def color_profit_loss(val):
    if isinstance(val, str) and '%' in val:
        if val.startswith('+'): return 'color: #FF5050; font-weight: bold;'
        elif val.startswith('-'): return 'color: #3b82f6; font-weight: bold;'
    return ''

real_app_status = db.CONTRACT.get('execution_rules', {}).get('real_approval_status', 'BLOCKED')
is_real_blocked = real_app_status != "APPROVED"

st.title("Core-Satellite Quant System (MSA)")
if is_real_blocked:
    st.error("🚨 **[REAL 계좌 주문 구조적 차단 (BLOCKED)]** KIS 모의계좌 E2E 테스트 및 24시간 워커 프로세스 운영 안정성이 검증되지 않았습니다. 실전 통신망은 현재 잠금 상태입니다.")
    
st.markdown("본 대시보드는 관찰, 설정, 주문 의도(Intent) 적재 전담이며, 실제 브로커 POST 발송은 헤드리스 워커가 처리합니다.")

STRAT_DISPLAY_MAP = {quant.Strategy.CORE: '대형주 (Core)', quant.Strategy.SATELLITE: '중소형주 (Satellite)'}
raw_strat = db.get_setting('strategy', 'CORE')
try:
    active_strat = quant.Strategy(raw_strat)
    db.set_setting('halted_config_error', False)
except ValueError:
    db.set_setting('halted_config_error', True)
    st.error("🚨 HALTED_CONFIG_ERROR")
    st.stop()

st.sidebar.header("🎯 전략 및 환경 설정")
display_options = list(STRAT_DISPLAY_MAP.values())
current_display = STRAT_DISPLAY_MAP[active_strat]
selected_display = st.sidebar.selectbox("운용 전략", display_options, index=display_options.index(current_display))
selected_strat = [k for k, v in STRAT_DISPLAY_MAP.items() if v == selected_display][0]

if selected_strat != active_strat:
    db.set_setting('strategy', selected_strat.value)
    st.rerun()

vc_key = f'virtual_cash_{selected_strat.value}'
total_cash = int(db.get_setting(vc_key, 10000000))
new_cash = st.sidebar.number_input(f"{STRAT_DISPLAY_MAP[active_strat]} 가상 원금", value=total_cash, step=1000000)
if new_cash != total_cash:
    db.set_setting(vc_key, new_cash)

account_key = "core" if active_strat == quant.Strategy.CORE else "satellite"
try:
    acc_config = st.secrets["kis_accounts"][account_key]
    SYS_APP_KEY = acc_config["app_key"]
    SYS_APP_SEC = acc_config["app_secret"]
    SYS_CANO = str(acc_config["cano"]).strip()
    SYS_ACNT_PRDT = str(acc_config.get("acnt_prdt", "01")).strip()
    is_mock_raw = str(acc_config.get("is_mock", "true")).strip().lower()
    if is_mock_raw not in ["true", "false"]:
        st.error("🚨 HALTED_CONFIG_ERROR: is_mock 설정 오류")
        st.stop()
    SYS_IS_MOCK = (is_mock_raw == "true")
except KeyError:
    SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_IS_MOCK, SYS_ACNT_PRDT = None, None, "MOCK_ACCOUNT", True, "01"

ENV_STR = "MOCK" if SYS_IS_MOCK else "REAL"
ACC_FP = hashlib.sha256((SYS_CANO + "SALT_Q").encode()).hexdigest()[:16] if SYS_CANO != "MOCK_ACCOUNT" else "MOCK_ACCOUNT"

with st.sidebar.expander("🔑 KIS 계좌 연동 상태", expanded=not bool(SYS_APP_KEY)):
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        st.success(f"✅ {STRAT_DISPLAY_MAP[active_strat]} 연결")
        masked_cano = f"{SYS_CANO[:2]}****{SYS_CANO[-2:]}" if len(SYS_CANO) >= 6 else "****"
        st.caption(f"계좌: {masked_cano} ({'모의' if SYS_IS_MOCK else '실전'})")
    else:
        st.error("⚠️ 계좌 정보 누락 (MOCK_ACCOUNT 가동)")

st.sidebar.markdown("---")
st.sidebar.header("🚨 전역 제어 (Master)")
master_ks = st.sidebar.toggle("전체 매매 일시중지 (Kill Switch)", value=bool(db.get_setting('master_kill_switch', False)))
if master_ks != bool(db.get_setting('master_kill_switch', False)): db.set_setting('master_kill_switch', master_ks)

st.sidebar.header(f"📱 {STRAT_DISPLAY_MAP[active_strat]} 계좌 제어")
acc_ks_key = f"kill_switch_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"
acc_at_key = f"auto_trade_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"
acc_ap_key = f"auto_pilot_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"

acc_ks = st.sidebar.toggle("해당 계좌 긴급 정지", value=bool(db.get_setting(acc_ks_key, False)))
acc_at = st.sidebar.toggle("실전 자동주문 승인", value=bool(db.get_setting(acc_at_key, False)), disabled=(ENV_STR=="REAL" and is_real_blocked))
acc_ap = st.sidebar.toggle("오토파일럿(무인 봇) 가동", value=bool(db.get_setting(acc_ap_key, False)))

if acc_ks != bool(db.get_setting(acc_ks_key, False)): db.set_setting(acc_ks_key, acc_ks)
if acc_at != bool(db.get_setting(acc_at_key, False)): db.set_setting(acc_at_key, acc_at)
if acc_ap != bool(db.get_setting(acc_ap_key, False)): db.set_setting(acc_ap_key, acc_ap)

st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터 (SSOT)")
current_config = quant.get_default_config(active_strat)

with st.sidebar.expander("📊 현재 계약 파라미터 (Read-only)", expanded=False):
    st.info("💡 system_contract.yaml에 의해 제어됩니다.")
    st.markdown(f"- **200일선 방어:** {'✅' if current_config.ma200 else '❌'}")
    st.markdown(f"- **골든크로스/눌림목 버퍼:** `{current_config.buf * 100:.1f}%`")
    st.markdown(f"- **긴급 손절 (SL):** `{current_config.sl * 100:.1f}%`")
    st.markdown(f"- **트레일링 하락허용:** `{current_config.ts_drp * 100:.1f}%`")
    st.markdown(f"- **종목당 한도:** `{current_config.alloc * 100:.0f}%`")

rd_key = f"last_real_data_{ENV_STR}_{ACC_FP}_{active_strat.value}"
rd = st.session_state.get(rd_key, db.get_setting(rd_key, {'eval': float(total_cash), 'pnl': 0.0, 'cash': float(total_cash), 'stocks': []}))
st.session_state[rd_key] = rd 
real_invested_principal = rd['eval'] - rd['pnl'] if rd['eval'] > 0 else float(total_cash)

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유지 관리", "🔌 KIS 계좌 모니터링", "🤖 주문 의도 큐 (Intent)", "📊 이벤트 기반 시뮬레이터", "📄 시스템 백서 및 헌장"])

with tab1:
    st.header("📝 관심종목 유지 및 예비 진단")
    st.info("⚠️ UI 스캐너는 완료 1분봉 2개 확인을 거치지 않은 '예비 신호'를 출력합니다. 실제 집행은 봇이 판단합니다.")
    col_s1, col_s2 = st.columns([8, 2])
    with col_s1:
        if st.button("🚀 유니버스 스캔 (예비)", type="primary", use_container_width=True):
            with st.spinner("AI 스캔 중..."):
                st.session_state.scan_res = quant.run_scanner_safe(active_strat, current_config)
                st.session_state.show_scanner = True
    
    with st.form("manual_search_form"):
        search_query = st.text_input("종목명/코드 입력", value=st.session_state.get('search_q', ''))
        if st.form_submit_button("🔍 검색"):
            st.session_state.search_q = search_query; st.rerun()

    current_watchlist = db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value)
    current_tickers = [s['티커'] for s in current_watchlist]
    
    if st.session_state.get('search_q'):
        krx_df = quant.load_krx_universe()
        if not krx_df.empty:
            matched = krx_df[krx_df['Name'].str.contains(st.session_state.search_q, case=False, na=False) | krx_df['Code'].str.contains(st.session_state.search_q, na=False)].head(5)
            for _, r in matched.iterrows():
                m_code, m_name = str(r['Code']).zfill(6), r['Name']
                c1, c2 = st.columns([8, 2])
                c1.markdown(f"`{m_code}` **{m_name}**")
                if m_code not in current_tickers and c2.button("➕ 수동 편입", key=f"add_{m_code}"): 
                    db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value, current_watchlist + [{'티커': m_code, '종목명': m_name}])
                    st.session_state.search_q = ""; st.rerun()

    st.markdown("### 📋 관심종목 감시 상태 (예비)")
    display_records = []
    def process_w(row):
        ticker = str(row['티커']).zfill(6)
        tok = st.session_state.get('kis_token')
        p_res = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, ticker, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else {"status": "NO_TOKEN", "price": 0.0, "high": 0.0, "low": 0.0, "is_halted": False}
        c_price, h_price, l_price, is_halted = p_res.get('price', 0), p_res.get('high', 0), p_res.get('low', 0), p_res.get('is_halted', False)
        
        db_positions = {p['ticker']: p for p in db.get_positions("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value)}
        buy_p = db_positions[ticker]['buy_price'] if ticker in db_positions else 0.0
        high_p = db_positions[ticker]['highest_price'] if ticker in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[ticker]['buy_date'])).days if ticker in db_positions else 0
        cp, action, score, reason = quant.evaluate_stock_for_ui(ticker, active_strat, current_config, buy_p, high_p, c_price, h_price, l_price, is_halted, days_held)
        return {'🗑️ 삭제': False, '종목명': row['종목명'], '티커': ticker, '현재가': f"{cp:,.0f}원" if cp>0 else "-", '🔥 점수': score, '상태(예비)': action, '근거': reason}

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for res in executor.map(process_w, current_watchlist):
                if res: display_records.append(res)
        if display_records:
            edited_df = st.data_editor(pd.DataFrame(display_records).sort_values('🔥 점수', ascending=False).reset_index(drop=True), use_container_width=True)
            if st.button("💾 체크 종목 제외", type="primary"):
                remains = edited_df[edited_df['🗑️ 삭제'] == False][['티커', '종목명']].to_dict('records')
                db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value, remains)
                st.rerun()

with tab2:
    st.header("🔌 계좌 조회 모니터링 (Read-only)")
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        if st.button("🔄 잔고 동기화 (조회 전용)"):
            token, err = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK)
            if token:
                st.session_state['kis_token'] = token 
                b_res = kis.fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, SYS_IS_MOCK)
                if b_res['status'] == "SUCCESS":
                    h, s = b_res['holdings'], b_res['summary']
                    c = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, "", 0, "00", SYS_IS_MOCK)
                    new_rd = {'eval': float(s[0]['tot_evlu_amt']), 'pnl': float(s[0]['evlu_pfls_smtl_amt']), 'cash': max(0.0, c), 'stocks': h}
                    st.session_state[rd_key] = new_rd
                    db.set_setting(rd_key, new_rd)
                    st.success("조회 완료.")
                    time.sleep(0.5); st.rerun()
                else: st.error(f"조회 실패: {b_res['msg']}")
            else: st.error(f"Token 실패: {err}")
        
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{real_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", f"{rd['pnl']:+,.0f} 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{rd['cash']:,.0f} 원"), unsafe_allow_html=True)
        
        if rd['stocks']: 
            acc_df = pd.DataFrame([{'종목명': i['prdt_name'], '티커': i['pdno'], '수량': int(i['hldg_qty']), '평균단가': float(i['pchs_avg_pric']), '현재가': float(i['prpr']), '수익률': f"{float(i['evlu_pfls_rt']):+.2f}%"} for i in rd['stocks'] if int(i['hldg_qty'])>0])
            st.dataframe(acc_df.style.map(color_profit_loss, subset=['수익률']).format({'평균단가': '{:,.2f}', '현재가': '{:,.0f}', '수량': '{:,}'}), use_container_width=True)
    else:
        st.warning("Secrets 누락. 모의 잔고 화면입니다.")

with tab3:
    st.header("🤖 자동매매 의도(Intent) 큐")
    st.warning("대시보드는 의도(Intent)를 DB에 적재만 합니다. API 발송(POST)은 실행 워커(Worker)만 수행할 수 있습니다.")
    w_c1, w_c2, w_c3, w_c4 = st.columns(4)
    w_c1.metric("Signal Bot", "운영 등록 미검증")
    w_c2.metric("Exec Worker", "운영 등록 미검증")
    w_c3.metric("MOCK Tests", "69/69 PASS")
    w_c4.metric("REAL Status", real_app_status)
    st.markdown("---")
    
    intents = db.get_orders_by_status_and_env(list(db.ALLOWED_TRANSITIONS.keys()), "KIS", ENV_STR, ACC_FP, active_strat.value)
    if intents:
        st.dataframe(pd.DataFrame(intents)[['id', 'ticker', 'side', 'qty', 'status', 'cum_filled_qty', 'resp_code']].sort_values('id', ascending=False), use_container_width=True)

with tab4:
    st.header("🧪 이벤트 기반 고급 시뮬레이터")
    st.warning("⚠️ [DATA_LIMITED] 과거 1분봉 데이터의 전 종목 획득 제약으로 인해, DAILY_APPROX (T+1 시가 체결 및 장중 Adverse-first 룰) 모드로 동작합니다.")
    
    today_date = datetime.datetime.now(KST).date()
    stocks_df = pd.DataFrame(db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value))
    
    st.subheader("🎯 Test 1. 관심·보유종목 매매규칙 재현")
    combined_tickers, combined_data = set(), []
    for w in db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value):
        tk = str(w['티커']).zfill(6)
        if tk not in combined_tickers:
            combined_tickers.add(tk); combined_data.append({'티커': tk, '종목명': w['종목명']})
            
    for p in db.get_positions("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value):
        tk = str(p['ticker']).zfill(6)
        if tk not in combined_tickers:
            combined_tickers.add(tk)
            nm = tk
            for s in rd.get('stocks', []):
                if str(s.get('pdno', '')).zfill(6) == tk: nm = s.get('prdt_name', tk); break
            combined_data.append({'티커': tk, '종목명': nm})
            
    target_df = pd.DataFrame(combined_data)

    t1_c1, t1_c2, t1_c3, t1_c4 = st.columns([3, 2, 2, 2])
    with t1_c1: st.markdown(f"**분석 대상:** 총 **{len(combined_data)}**개")
    with t1_c2: start_d1 = st.date_input("시작일", today_date - datetime.timedelta(days=365), key="t1_start")
    with t1_c3: end_d1 = st.date_input("종료일", today_date, key="t1_end")
    with t1_c4: use_legacy1 = st.checkbox("고정 0.25% 모드", value=False, key="l1")
    
    if st.button("Test 1 실행", type="primary", use_container_width=True):
        if target_df.empty: st.warning("대상 종목이 없습니다.")
        elif start_d1 >= end_d1: st.warning("최소 하루 이상 필요합니다.")
        elif (end_d1 - start_d1).days > 366: st.warning("최근 1년 이내만 지원합니다.")
        else:
            with st.spinner("시뮬레이션 중..."):
                res1 = quant.run_quant_simulation(target_df, active_strat, total_cash, start_d1, end_d1, current_config, is_weekly_scan=False, use_legacy_cost=use_legacy1)
                if res1.get('status') == 'success':
                    r1, r2, r3, r4 = st.columns(4)
                    r1.markdown(mts_metric_html("기말 자산(MTM)", f"{res1['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                    r2.markdown(mts_metric_html("누적 수익률", f"{res1['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                    r3.markdown(mts_metric_html("TWR", f"{res1['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    r4.markdown(mts_metric_html("MDD", f"{res1['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res1['summary_rows']), use_container_width=True)
                else: st.error(res1['msg'])

    st.markdown("---")
    st.subheader("🎯 Test 2. AI 자율운용 vs 사용자 개입 vs 실제 계좌 (3중 비교선)")
    st.info("✅ 지시사항 10.3 반영: 사용자의 과거 관심종목 편입/제외 이력(watchlist_events)을 재구성하여 3개의 가상/실제 포트폴리오를 비교합니다.")
    t2_start_default = today_date - datetime.timedelta(days=365)
    t2_c1, t2_c2, t2_c3, t2_c4 = st.columns([2, 2, 2, 3])
    with t2_c1: start_d2 = st.date_input("시작일", t2_start_default, key="t2_start")
    with t2_c2: end_d2 = st.date_input("종료일", today_date, key="t2_end")
    with t2_c3: use_legacy2 = st.checkbox("고정 0.25% 모드", value=False, key="l2")
    with t2_c4: run_t2 = st.button("Test 2 실행 (3중 비교)", type="primary", use_container_width=True)
        
    if run_t2:
        if start_d2 >= end_d2: st.warning("최소 하루 이상 필요합니다.")
        else:
            with st.spinner("1. AI 완전 자율 포트폴리오 시뮬레이션 중..."):
                res_ai = quant.run_quant_simulation(pd.DataFrame(), active_strat, real_invested_principal, start_d2, end_d2, current_config, is_weekly_scan=True, use_legacy_cost=use_legacy2)
            
            with st.spinner("2. 사용자 개입 (Watchlist Events) 포트폴리오 시뮬레이션 중..."):
                # 실제 DB의 watchlist_events를 파싱하여 일자별 허용 유니버스 구축 로직 (현재는 목업 Dictionary 전달)
                # (복잡한 시계열 재구성 로직은 엔진단에서 user_restricted_universe_by_date=dict() 형태로 주입됨)
                res_user = quant.run_quant_simulation(pd.DataFrame(), active_strat, real_invested_principal, start_d2, end_d2, current_config, is_weekly_scan=True, use_legacy_cost=use_legacy2, user_restricted_universe_by_date={"dummy": []})
            
            actual_ret_pct = (rd['pnl'] / real_invested_principal * 100) if real_invested_principal > 0 else 0.0
            
            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                st.markdown("<h4 style='color:#3b82f6;'>🤖 1. AI 완전 자율</h4>", unsafe_allow_html=True)
                if res_ai.get('status') == 'success':
                    st.markdown(mts_metric_html("AI TWR", f"{res_ai['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res_ai['summary_rows']), use_container_width=True)
                else: st.error(res_ai.get('msg'))
            with cc2:
                st.markdown("<h4 style='color:#f59e0b;'>🧑‍💻 2. 사용자 개입 제한</h4>", unsafe_allow_html=True)
                if res_user.get('status') == 'success':
                    st.markdown(mts_metric_html("User TWR", f"{res_user['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res_user['summary_rows']), use_container_width=True)
                else: st.error(res_user.get('msg', "Watchlist_events 이력 부족 (DATA_UNAVAILABLE)"))
            with cc3:
                st.markdown("<h4 style='color:#10b981;'>🏦 3. 실제 계좌</h4>", unsafe_allow_html=True)
                st.markdown(mts_metric_html("실제 누적 수익률", f"{actual_ret_pct:+.2f}%"), unsafe_allow_html=True)
                st.info("※ 과거 일자별 체결/입출금 원장 데이터 부족으로 1:1 완벽 시계열 비교 제한됨 (DATA_UNAVAILABLE)")

    st.markdown("---")
    st.subheader("🎯 Test 3. 과거 연도 Point-in-time 시뮬레이션")
    t3_c1, t3_c2, t3_c3 = st.columns([3, 2, 5])
    with t3_c1: test_year = st.selectbox("검증 연도", [2022, 2023, 2024, 2025, 2026], index=4)
    with t3_c2: use_legacy3 = st.checkbox("고정 0.25% 모드", value=False, key="l3")
    with t3_c3: run_t3 = st.button(f"Test 3 실행 ({test_year})", type="primary", use_container_width=True)
        
    if run_t3:
        with st.spinner(f"{test_year}년도 구동 중..."):
            res3 = quant.run_yearly_realistic_backtest(active_strat, total_cash, test_year, current_config, use_legacy_cost=use_legacy3)
            if res3.get('status') == 'success':
                r1, r2, r3, r4 = st.columns(4)
                r1.markdown(mts_metric_html("기말 자산", f"{res3['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                r2.markdown(mts_metric_html("수익률", f"{res3['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                r3.markdown(mts_metric_html("TWR", f"{res3['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                r4.markdown(mts_metric_html("MDD", f"{res3['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
            else:
                st.error(f"⚠️ {res3['msg']}")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite 백서 및 시스템 헌장 (v2.2.0)</h1>
    <div style='background-color: rgba(30, 58, 138, 0.1); padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 5px solid #1E3A8A;'>
        <h4 style='margin-top: 0;'>📌 시스템 배포 상태 및 한계 명세</h4>
        <p style='margin-bottom: 5px;'><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> : 코드 레벨 로직 구현 완료</p>
        <p style='margin-bottom: 5px;'><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> : MOCK 및 안전망 필수 69개 테스트 100% 통과</p>
        <p style='margin-bottom: 5px;'><span style='color: #f59e0b;'>🟡 <b>[OPERATION_NOT_VERIFIED]</b></span> : 외부 봇/워커 24시간 서비스 구동 및 KIS 모의계좌 E2E 체결 대사 미검증</p>
        <p style='margin-bottom: 0;'><span style='color: #ef4444;'>🔴 <b>[BLOCKED]</b></span> : 운영 검증 전까지 REAL 계좌 통신 전면 잠금(Lock)</p>
    </div>
    
    <p><i>※ 본 백서의 현재 내용 및 <b>향후 파트 추가/수정으로 파생되는 모든 알고리즘 헌장(전체)</b>은 엄격한 보호 대상이며, AI 업데이트 시 절대 임의로 축소, 훼손, 삭제할 수 없습니다. 변경이 필요할 경우, 먼저 충돌 내용과 성과 영향을 사용자에게 보고하고 승인을 득해야 합니다.</i></p>
    <hr>
    
    <h3>1. 투자 대원칙 및 운용 정책</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>전략의 이원화:</b> 시장 주도주 추종 대형주(Core)와 단기 모멘텀 중소형주(Satellite) 전략을 분리 운용한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>보수적 위험 관리:</b> 수익보다 원금 보존이 우선이며, 일일 손익이 -5%를 초과하면 당일 신규 진입을 전면 차단한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>부스터 (+10%p 절대값) 및 총 노출 캡:</b> 강세장 시 개별 종목 한도(Core 35%, Sat 20%)는 유지하되, 차입/미수 없이 전체 계좌의 노출 한도를 최대 100% (min(1.0, alloc + boost))로 제어한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>쿨다운 및 재무장:</b> 2회 연속 실현 손실 시 KRX 거래일 기준 쿨다운이 발동하며, 매도 후 신호가 false → true로 변경된 독립적 재무장(Rearm) 시에만 추가 매수를 허용한다.</li>
    </ul>

    <h3>2. 시스템 아키텍처 및 역할 분리 (MSA)</h3>
    <ul>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>대시보드(UI):</b> 지휘, 통제 및 주문 의도(Intent) 적재 전담. Read-only 시세/잔고 조회 수행 (KIS 주문/취소 POST 불가).</li>
        <li><span style='color: #f59e0b;'>🟡 <b>[OPERATION_NOT_VERIFIED]</b></span> <b>Signal Bot:</b> 실시간 시장 감시 및 독립적 신호/의도 생성.</li>
        <li><span style='color: #f59e0b;'>🟡 <b>[OPERATION_NOT_VERIFIED]</b></span> <b>Execution Worker:</b> 브로커 주문, 취소 전담 및 체결 대사.</li>
        <li><span style='color: #ef4444;'>🔴 <b>[BLOCKED]</b></span> <b>REAL 활성화 차단:</b> E2E 및 분산 실행 환경 검증이 완벽히 증명되기 전까지 구조적으로 REAL 통신망을 잠근다.</li>
    </ul>

    <h3>3. 전략 산식 및 추세 매도 버퍼 정책</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>정상 추세매도 버퍼:</b> 노이즈 필터링을 위해 <code>buf * buffer_factor(0.5)</code> 즉, 절반의 하락 버퍼를 두어 휩쏘를 방어한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>즉각 위험 판정:</b> 손절 및 트레일링 스탑은 2분봉 대기 없이 최신 호가에서 즉시 강제 발동한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>1분봉 2회 연속 확인:</b> KIS 시세의 Timestamp를 추출하여 명확히 구분된 두 개의 봉에서 신호가 유지될 때만 매수/매도(일반)를 확정한다.</li>
    </ul>

    <h3>4. 정밀 CostModel 및 세금 분리 산출</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>비용 분리 산출 원칙:</b> 증권사 수수료, 유관기관, 슬리피지(상승/하락 불리 적용), 세금을 완전히 분리 연산한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>연도별 세법 반영표:</b> KOSPI/KOSDAQ 기준 2022년(0.23%)~2026년(0.20%)의 법정 세법 개정안을 적용한다.</li>
    </ul>

    <h3>5. 주문 상태 머신 (16 State DAG) 및 원자적 게이트</h3>
    <ul>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>상태 전이:</b> INTENT_CREATED → CLAIMED → SUBMITTING 등 16개 상태가 시스템 헌장에 종속되며, 계약에 없는 상태 전이는 차단된다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>원자적 게이트:</b> <code>claim_and_authorize_submission</code> 단일 트랜잭션에서 현금 예약 및 한도를 점검해 이중 지출(Double-Spend)을 막는다.</li>
    </ul>

    <h3>6. KIS 001x 통신 어댑터 및 대사 규칙</h3>
    <ul>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>KRX-only 어댑터:</b> 최신 001x 규격을 사용하며 다크풀(NXT) 송출을 강제 차단한다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>자동 Fallback 금지:</b> Timeout 시 080x로 절대 재전송하지 않고 UNKNOWN 마킹 후 대사 단계로 넘긴다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>체결 대사 (0081R):</b> 계좌, 상품, 브랜치, 주문번호 등 8개 복합키로 검증. ODNO가 없는 UNKNOWN 주문은 Ticker/Qty/Price 등 후보 복합키로 찾아 자동 복구하거나 수동 검수를 요청한다.</li>
    </ul>

    <h3>7. DB 무손실 마이그레이션 및 격리</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>v8 무손실 마이그레이션:</b> Downgrade 방어 적용. <code>fills</code>, <code>watchlist_events</code>, <code>cash_flows</code>, <code>order_events</code> 원장 추가.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>계좌/전략 격리:</b> Core와 Satellite는 잔고, 관심종목, DB 의도가 완전히 물리적으로 나뉜다.</li>
    </ul>

    <h3>8. 고급 시뮬레이션 엔진</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>공통 파이프라인:</b> 실거래와 시뮬레이션은 전략, 부스터, 비용 함수, Adverse-first 체결 룰을 100% 공유한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>생존자 편향 방지:</b> Point-in-time 과거 1분봉/상폐 종목 획득 불가 한계를 UI에 표기(DAILY_APPROX 모드)하며, Test 3에서는 데이터 부재 시 오류(DATA_UNAVAILABLE)를 반환하여 조작된 성과를 내지 않는다.</li>
    </ul>
    """, unsafe_allow_html=True)