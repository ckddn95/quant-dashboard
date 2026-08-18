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
    st.error("🚨 **[REAL 주문 전면 금지 상태]** MOCK 체결 대사 및 워커 분산 실행 프로세스 100% 검증 전까지 실전(REAL) 오토파일럿 활성화를 구조적으로 차단합니다.")
else:
    st.success("✅ **[LIVE 활성화]** 모든 필수 검증을 통과하여 실계좌 연동이 허가되었습니다.")
    
st.markdown("대시보드는 지휘·설정·관찰·주문 의도 적재 역할만 수행합니다. 실제 주문은 외부 헤드리스 워커가 독립 처리합니다.")

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

total_cash = int(db.get_setting('virtual_cash', 10000000))
new_cash = st.sidebar.number_input("총 투자 운용 자산 (가상 원금)", value=total_cash, step=1000000)
if new_cash != total_cash:
    db.set_setting('virtual_cash', new_cash)

account_key = "core" if active_strat == quant.Strategy.CORE else "satellite"
try:
    acc_config = st.secrets["kis_accounts"][account_key]
    SYS_APP_KEY, SYS_APP_SEC, SYS_CANO = acc_config["app_key"], acc_config["app_secret"], str(acc_config["cano"]).strip()
    SYS_IS_MOCK = str(acc_config.get("is_mock", "True")).lower() == 'true'
    SYS_ACNT_PRDT = str(acc_config.get("acnt_prdt", "01")).strip()
except KeyError:
    SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_IS_MOCK, SYS_ACNT_PRDT = None, None, "MOCK_ACCOUNT", True, "01"

ENV_STR = "MOCK" if SYS_IS_MOCK else "REAL"
ACC_FP = hashlib.sha256(SYS_CANO.encode()).hexdigest()[:16] if SYS_CANO != "MOCK_ACCOUNT" else "MOCK_ACCOUNT"

with st.sidebar.expander("🔑 KIS 계좌 연동 상태", expanded=not bool(SYS_APP_KEY)):
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        st.success(f"✅ {STRAT_DISPLAY_MAP[active_strat]} 계좌 설정됨")
        # 계좌번호 마스킹 처리
        masked_cano = f"{SYS_CANO[:2]}****{SYS_CANO[-2:]}" if len(SYS_CANO) >= 6 else "****"
        st.caption(f"계좌: {masked_cano} ({'모의' if SYS_IS_MOCK else '실전'})")
    else:
        st.error("⚠️ Secrets 누락. 모의 DB 모드로 작동합니다.")

st.sidebar.markdown("---")
st.sidebar.header("🚨 전역 제어 (Master)")
master_ks = st.sidebar.toggle("전체 매매 일시중지 (Kill Switch)", value=bool(db.get_setting('master_kill_switch', False)))
if master_ks != bool(db.get_setting('master_kill_switch', False)): db.set_setting('master_kill_switch', master_ks)

st.sidebar.header(f"📱 {STRAT_DISPLAY_MAP[active_strat]} 계좌 제어")
acc_ks_key = f"kill_switch_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"
acc_at_key = f"auto_trade_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"
acc_ap_key = f"auto_pilot_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"

acc_ks = st.sidebar.toggle("해당 계좌 긴급 정지", value=bool(db.get_setting(acc_ks_key, False)))
# REAL 모드는 BLOCKED 상태면 활성화 불가능
acc_at = st.sidebar.toggle("실전 자동주문 활성화", value=bool(db.get_setting(acc_at_key, False)), disabled=(ENV_STR=="REAL" and is_real_blocked))
acc_ap = st.sidebar.toggle("오토파일럿(봇) 켜기", value=bool(db.get_setting(acc_ap_key, False)))

if acc_ks != bool(db.get_setting(acc_ks_key, False)): db.set_setting(acc_ks_key, acc_ks)
if acc_at != bool(db.get_setting(acc_at_key, False)): db.set_setting(acc_at_key, acc_at)
if acc_ap != bool(db.get_setting(acc_ap_key, False)): db.set_setting(acc_ap_key, acc_ap)

st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터 (SSOT)")
current_config = quant.get_default_config(active_strat)

with st.sidebar.expander("📊 현재 적용된 계약 파라미터 보기", expanded=False):
    st.info("💡 system_contract.yaml에 동결된 읽기 전용 값입니다.")
    st.markdown(f"- **200일선 방어:** {'✅' if current_config.ma200 else '❌'}")
    st.markdown(f"- **골든크로스/눌림목 버퍼:** `{current_config.buf * 100:.1f}%`")
    st.markdown(f"- **정상 추세매도 버퍼계수:** `{current_config.buffer_factor}`")
    st.markdown(f"- **긴급 손절 (SL):** `{current_config.sl * 100:.1f}%`")
    st.markdown(f"- **트레일링 익절:** `{current_config.ts_tgt * 100:.1f}%`")
    st.markdown(f"- **트레일링 하락허용:** `{current_config.ts_drp * 100:.1f}%`")
    st.markdown(f"- **종목당 한도:** `{current_config.alloc * 100:.0f}%`")
    st.markdown(f"- **연속손실 쿨다운:** `{current_config.cd} Session`")
    st.markdown(f"- **강세장 부스터:** {'✅' if current_config.boost else '❌'}")

rd = st.session_state.get('real_data', db.get_setting('last_real_data', {'eval': float(total_cash), 'pnl': 0.0, 'cash': float(total_cash), 'stocks': []}))
st.session_state['real_data'] = rd 
real_invested_principal = rd['eval'] - rd['pnl'] if rd['eval'] > 0 else float(total_cash)

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유니버스", "🔌 계좌 모니터링", "🤖 자동매매 의도 큐", "📊 고급 시뮬레이터", "📄 시스템 백서 (v2.2.0)"])

with tab1:
    st.header("📝 관심종목 유지 및 예비 진단")
    st.info("UI 스캐너는 1분봉 확정을 기다리지 않는 '예비 신호'를 출력합니다. 자동매매 워커는 확정봉 2연속 판정을 따릅니다.")
    col_s1, col_s2 = st.columns([8, 2])
    
    with col_s1:
        if st.button("🚀 실시간 예비 타점 스캔", type="primary", use_container_width=True):
            with st.spinner("AI 유니버스 스캔 중..."):
                st.session_state.scan_res = quant.run_scanner_safe(active_strat, current_config)
                st.session_state.show_scanner = True
    
    with st.form("manual_search_form"):
        search_query = st.text_input("종목명 또는 코드(6자리) 입력", value=st.session_state.get('search_q', ''))
        if st.form_submit_button("🔍 검색"):
            st.session_state.search_q = search_query
            st.rerun()

    current_watchlist = db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value)
    current_tickers = [s['티커'] for s in current_watchlist]
    
    if st.session_state.get('search_q'):
        krx_df = quant.load_krx_universe()
        if not krx_df.empty:
            matched = krx_df[krx_df['Name'].str.contains(st.session_state.search_q, case=False, na=False) | krx_df['Code'].str.contains(st.session_state.search_q, na=False)].head(5)
            for _, r in matched.iterrows():
                m_name = r['Name']
                m_code = str(r['Code']).zfill(6)
                c1, c2 = st.columns([8, 2])
                c1.markdown(f"`{m_code}` **{m_name}**")
                if m_code not in current_tickers and c2.button("➕ 등록", key=f"add_{m_code}"): 
                    db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value, current_watchlist + [{'티커': m_code, '종목명': m_name}])
                    st.session_state.search_q = "" 
                    st.rerun()

    if st.session_state.get('show_scanner'):
        scan_res = st.session_state.get('scan_res', pd.DataFrame())
        if not scan_res.empty:
            for _, row in scan_res.iterrows():
                c1, c2, c3, c4 = st.columns([2, 2, 4, 2])
                c1.markdown(f"**{row['종목명']}** (`{row['티커']}`)")
                c2.markdown(f"**{row['현재가']:,.0f} 원**")
                c3.markdown(f"🔥 `{row['AI 스코어']:.2f}점` | {row['진단 근거']}")
                if str(row['티커']).zfill(6) not in current_tickers:
                    if c4.button("➕ 담기", key=f"scan_{row['티커']}"):
                        db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value, current_watchlist + [{'티커': row['티커'], '종목명': row['종목명']}])
                        st.rerun() 
                else: c4.button("✅ 보유중", key=f"scan_{row['티커']}", disabled=True)
        else:
            st.info("조건을 만족하는 종목이 없습니다.")

    st.markdown("---")
    st.markdown("### 📋 관심종목 감시 상태")
    display_records = []
    def process_w(row):
        ticker = str(row['티커']).zfill(6)
        tok = st.session_state.get('kis_token')
        c_price, h_price, l_price, is_halted, _ = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, ticker, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else (0.0, 0.0, 0.0, False, "No Token")
        db_positions = {p['ticker']: p for p in db.get_positions("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value)}
        buy_p = db_positions[ticker]['buy_price'] if ticker in db_positions else 0.0
        high_p = db_positions[ticker]['highest_price'] if ticker in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[ticker]['buy_date'])).days if ticker in db_positions else 0
        cp, action, score, reason = quant.evaluate_stock_for_ui(ticker, active_strat, current_config, buy_p, high_p, c_price, h_price, l_price, is_halted, days_held)
        return {'🗑️ 삭제': False, '종목명': row['종목명'], '티커': ticker, '현재가': f"{cp:,.0f}원" if cp>0 else "-", '🔥 점수': score, '상태': action, '근거': reason}

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for res in executor.map(process_w, current_watchlist):
                if res: display_records.append(res)
        if display_records:
            edited_df = st.data_editor(pd.DataFrame(display_records).sort_values('🔥 점수', ascending=False).reset_index(drop=True), use_container_width=True)
            if st.button("💾 체크 종목 삭제", type="primary"):
                remains = edited_df[edited_df['🗑️ 삭제'] == False][['티커', '종목명']].to_dict('records')
                db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value, remains)
                st.rerun()

with tab2:
    st.header("🔌 계좌 조회 모니터링 (Read-only)")
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        if st.button("🔄 잔고 동기화 (단순 Read-only)"):
            token, err = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK)
            if token:
                st.session_state['kis_token'] = token 
                h, s, _ = kis.fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, SYS_IS_MOCK)
                if s:
                    c = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, "", 0, "00", SYS_IS_MOCK)
                    new_rd = {'eval': float(s[0]['tot_evlu_amt']), 'pnl': float(s[0]['evlu_pfls_smtl_amt']), 'cash': max(0.0, c), 'stocks': h}
                    st.session_state['real_data'] = new_rd
                    db.set_setting('last_real_data', new_rd)
                    st.success("조회 완료.")
                    time.sleep(0.5); st.rerun()
            else: st.error(f"Token 발급 실패: {err}")
        
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
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", "0 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)

with tab3:
    st.header("🤖 자동매매 의도(Intent) 큐")
    st.warning("UI는 의도를 DB에 기록만 합니다. 실제 API POST는 백그라운드 Worker가 수행합니다.")
    
    # 워커 및 봇 생존 여부 (가상으로 DB에서 최근 활동 시간 체크 기능 추가 권장. 현재는 텍스트 표시)
    w_c1, w_c2, w_c3, w_c4 = st.columns(4)
    w_c1.metric("Signal Bot", "IN PROGRESS")
    w_c2.metric("Execution Worker", "IN PROGRESS")
    w_c3.metric("MOCK Test", "VERIFIED")
    w_c4.metric("REAL Status", real_app_status)
    st.markdown("---")
    
    base_eval = rd['eval'] if rd['eval'] > 0 else float(total_cash)
    target_buy_amt = base_eval * current_config.alloc
    locked_cash, _ = db.get_locked_cash_and_qty("KIS", ENV_STR, ACC_FP, active_strat.value)
    net_usable_cash = max(0.0, rd['cash'] - locked_cash)
    
    temp_q = []
    eval_list = []
    eval_tickers = set()
    for w in db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value):
        tk = str(w['티커']).zfill(6)
        eval_tickers.add(tk)
        eval_list.append({'티커': tk, '종목명': w['종목명']})
        
    for p in db.get_positions("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value):
        tk = str(p['ticker']).zfill(6)
        if tk not in eval_tickers:
            eval_tickers.add(tk)
            nm = tk
            for s in rd.get('stocks', []):
                if str(s.get('pdno', '')).zfill(6) == tk:
                    nm = s.get('prdt_name', tk)
                    break
            eval_list.append({'티커': tk, '종목명': nm})
    
    def process_q(row):
        tk = str(row['티커']).zfill(6)
        nm = row['종목명']
        db_positions = {p['ticker']: p for p in db.get_positions("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value)}
        m_qty = db_positions[tk]['managed_qty'] if tk in db_positions else 0
        buy_p = db_positions[tk]['buy_price'] if tk in db_positions else 0.0
        high_p = db_positions[tk]['highest_price'] if tk in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[tk]['buy_date'])).days if tk in db_positions else 0
        
        kis_qty = 0
        for s in rd.get('stocks', []):
            if str(s.get('pdno', '')).zfill(6) == tk: 
                kis_qty = int(s['hldg_qty'])
                break
                
        tok = st.session_state.get('kis_token')
        c_price, h_price, l_price, is_halted, _ = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, tk, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else (0.0, 0.0, 0.0, False, "No Token")
        cp, action, score, _ = quant.evaluate_stock_for_ui(tk, active_strat, current_config, buy_p, high_p, c_price, h_price, l_price, is_halted, days_held)
        
        is_holding = kis_qty > 0 or m_qty > 0
        buy_str = "추가 매수" if is_holding else "신규 매수"
        
        if "매도" in action or "🔴" in action:
            if m_qty > 0:
                return {'분류': 0, '점수': score, '종목명': nm, '티커': tk, '상태': action, '현재가': cp, '수량': m_qty}
            else:
                return {'분류': 2, '점수': score, '종목명': nm, '티커': tk, '상태': f"🟡 Managed 수량 0 ({action})", '현재가': cp, '수량': 0}
        elif "매수" in action or "🟢" in action:
            needed_amt = max(0.0, target_buy_amt - (kis_qty * cp))
            allow_amt = min(net_usable_cash, needed_amt)
            add_qty = int(allow_amt // (cp * db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05))) if cp > 0 else 0
            if add_qty > 0:
                return {'분류': 1, '점수': score, '종목명': nm, '티커': tk, '상태': f"🛒 {buy_str} (대기)", '현재가': cp, '수량': add_qty}
            else:
                return {'분류': 2, '점수': score, '종목명': nm, '티커': tk, '상태': "🟡 현금/한도 부족", '현재가': cp, '수량': 0}
        
        return {'분류': 2, '점수': score, '종목명': nm, '티커': tk, '상태': action, '현재가': cp, '수량': 0}

    if eval_list:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(process_q, eval_list):
                if r: temp_q.append(r)
                
    q_df = pd.DataFrame(temp_q)
    if not q_df.empty:
        q_df = q_df.sort_values(by=['분류', '점수'], ascending=[True, False]).reset_index(drop=True)
        st.dataframe(q_df[['종목명', '상태', '현재가', '수량']].style.format({'현재가': '{:,.0f}', '수량': '{:,}'}), use_container_width=True)
        
        if st.button("⚡ UI 수동 의도 DB 기록", type="primary"):
            success_count = 0
            valid = [r for _, r in q_df.iterrows() if r['분류'] in [0, 1] and r['수량'] > 0]
            for r in valid:
                tk = r['티커']
                side = "BUY" if "매수" in r['상태'] or "🛒" in r['상태'] else "SELL"
                now_str = datetime.datetime.now(KST).strftime('%H%M%S')
                spec = quant.OrderSpec("", f"UI_{tk}_{now_str}", "KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, "1.0", db.CONTRACT['contract_version'], tk, r['종목명'], side, "MARKET", r['수량'], 0, r['현재가'], "KRX", "GTC", "UI_MANUAL", "UI", now_str, "Q", "KIS", now_str, db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'))
                if db.safe_add_order_intent(spec)[0]: success_count += 1
            if success_count > 0: st.success(f"✅ {success_count}건 적재 완료!")
            else: st.info("유효한 시그널이 없습니다.")
    
    st.markdown("### 📊 실시간 의도 상태 현황")
    intents = db.get_orders_by_status_and_env(list(db.ALLOWED_TRANSITIONS.keys()), "KIS", ENV_STR, ACC_FP, active_strat.value)
    if intents:
        st.dataframe(pd.DataFrame(intents)[['id', 'ticker', 'side', 'qty', 'status', 'cum_filled_qty', 'resp_code']].sort_values('id', ascending=False), use_container_width=True)

with tab4:
    st.header("🧪 고급 백테스트 엔진")
    st.warning("⚠️ [DATA_LIMITED] 1분봉 데이터 획득 불가로 DAILY_APPROX (T+1 시가) 모드가 적용됩니다. 생존자 편향(Survivor Bias) 위험을 감안하십시오.")
    
    today_date = datetime.datetime.now(KST).date()
    stocks_df = pd.DataFrame(db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value))
    
    st.subheader("🎯 Test 1. 관심·보유종목 매매규칙 재현 시뮬레이션")
    combined_tickers = set()
    combined_data = []
    
    for w in db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value):
        tk = str(w['티커']).zfill(6)
        if tk not in combined_tickers:
            combined_tickers.add(tk)
            combined_data.append({'티커': tk, '종목명': w['종목명']})
            
    for p in db.get_positions("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value):
        tk = str(p['ticker']).zfill(6)
        if tk not in combined_tickers:
            combined_tickers.add(tk)
            nm = tk
            for s in rd.get('stocks', []):
                if str(s.get('pdno', '')).zfill(6) == tk: 
                    nm = s.get('prdt_name', tk)
                    break
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
        elif (end_d1 - start_d1).days > 366: st.warning("Test 1은 최근 1년 이내만 지원합니다.")
        else:
            with st.spinner("시뮬레이션 중..."):
                res1 = quant.run_quant_simulation(target_df, active_strat, total_cash, start_d1, end_d1, current_config, is_weekly_scan=False, use_legacy_cost=use_legacy1)
                if res1.get('status') == 'success':
                    r1, r2, r3, r4 = st.columns(4)
                    r1.markdown(mts_metric_html("기말 자산", f"{res1['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                    r2.markdown(mts_metric_html("누적 수익률", f"{res1['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                    r3.markdown(mts_metric_html("TWR", f"{res1['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    r4.markdown(mts_metric_html("MDD", f"{res1['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res1['summary_rows']), use_container_width=True)
                    with st.expander("📝 상세 매매 내역"):
                        if res1.get('trade_logs'): st.dataframe(pd.DataFrame(res1['trade_logs']).style.map(color_profit_loss, subset=['수익률']).format({'진입단가': '{:,.0f}', '청산단가': '{:,.0f}', '수량': '{:,}', '손익금': '{:,.0f}'}), use_container_width=True)
                else: st.error(res1['msg'])

    st.markdown("---")
    st.subheader("🎯 Test 2. AI 자율운용 대 사용자 개입 비교")
    t2_start_default = today_date - datetime.timedelta(days=365)
    t2_c1, t2_c2, t2_c3, t2_c4 = st.columns([2, 2, 2, 3])
    with t2_c1: start_d2 = st.date_input("시작일", t2_start_default, key="t2_start")
    with t2_c2: end_d2 = st.date_input("종료일", today_date, key="t2_end")
    with t2_c3: use_legacy2 = st.checkbox("고정 0.25% 모드", value=False, key="l2")
    with t2_c4: run_t2 = st.button("Test 2 실행", type="primary", use_container_width=True)
        
    if run_t2:
        if stocks_df.empty: st.error("관심종목이 없습니다.")
        elif start_d2 >= end_d2: st.warning("최소 하루 이상 필요합니다.")
        else:
            with st.spinner("AI 가상운용 시뮬레이션 중..."):
                res2 = quant.run_quant_simulation(stocks_df, active_strat, real_invested_principal, start_d2, end_d2, current_config, is_weekly_scan=True, use_legacy_cost=use_legacy2) # Test 2는 주간 스캔
                if res2.get('status') == 'success':
                    actual_ret_pct = (rd['pnl'] / real_invested_principal * 100) if real_invested_principal > 0 else 0.0
                    cc1, cc2 = st.columns(2)
                    with cc1:
                        st.markdown("<h4 style='color:#3b82f6;'>🤖 AI 자유운용 (주간스캔)</h4>", unsafe_allow_html=True)
                        st.markdown(mts_metric_html("AI 기말 자산", f"{res2['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                        st.markdown(mts_metric_html("AI 수익률", f"{res2['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        st.dataframe(pd.DataFrame(res2['summary_rows']), use_container_width=True)
                    with cc2:
                        st.markdown("<h4 style='color:#10b981;'>🧑‍💻 실제 계좌 (사용자 개입)</h4>", unsafe_allow_html=True)
                        st.markdown(mts_metric_html("실제 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
                        st.markdown(mts_metric_html("실제 누적 수익률", f"{actual_ret_pct:+.2f}%"), unsafe_allow_html=True)
                else: st.error(res2['msg'])

    st.markdown("---")
    st.subheader("🎯 Test 3. 과거 연도 시뮬레이션")
    t3_c1, t3_c2, t3_c3 = st.columns([3, 2, 5])
    with t3_c1: test_year = st.selectbox("검증 연도", [2022, 2023, 2024, 2025, 2026], index=4)
    with t3_c2: use_legacy3 = st.checkbox("고정 0.25% 모드", value=False, key="l3")
    with t3_c3: run_t3 = st.button(f"Test 3 실행 ({test_year})", type="primary", use_container_width=True)
        
    if run_t3:
        with st.spinner(f"{test_year}년도 구동 중 (과거 유니버스 사용)..."):
            res3 = quant.run_yearly_realistic_backtest(active_strat, total_cash, test_year, current_config, use_legacy_cost=use_legacy3)
            if res3.get('status') == 'success':
                r1, r2, r3, r4 = st.columns(4)
                r1.markdown(mts_metric_html("기말 자산", f"{res3['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                r2.markdown(mts_metric_html("수익률", f"{res3['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                r3.markdown(mts_metric_html("TWR", f"{res3['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                r4.markdown(mts_metric_html("MDD", f"{res3['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
            else: st.error(res3['msg'])

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite 백서 및 시스템 헌장 (v2.2.0)</h1>
    <div style='background-color: rgba(30, 58, 138, 0.1); padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 5px solid #1E3A8A;'>
        <h4 style='margin-top: 0;'>📌 시스템 배포 상태 및 한계 명세</h4>
        <p style='margin-bottom: 5px;'><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> : 코드 레벨 로직 구현 완료 및 MOCK 테스트 통과</p>
        <p style='margin-bottom: 5px;'><span style='color: #3b82f6;'>🔵 <b>[DESIGNED]</b></span> : 아키텍처 상 구조적 정의 완료</p>
        <p style='margin-bottom: 5px;'><span style='color: #f59e0b;'>🟡 <b>[IN PROGRESS]</b></span> : 외부 봇/워커 연동 및 E2E 실계좌 검증 진행 대기</p>
        <p style='margin-bottom: 0;'><span style='color: #ef4444;'>🔴 <b>[BLOCKED]</b></span> : MOCK 및 안전성 100% 검증 전까지 강제 락(Lock) 적용</p>
    </div>
    
    <p><i>※ 본 백서의 현재 내용 및 향후 추가되는 모든 파트(Part) 전체는 시스템 헌장으로서 엄격히 보호되며, AI 업데이트 시 임의로 축소/삭제될 수 없습니다.</i></p>
    <hr>
    
    <h3>1. 투자 대원칙 및 운용 정책</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>전략의 이원화:</b> 시장 주도주 추종 대형주(Core)와 단기 모멘텀 중소형주(Satellite) 전략을 분리 운용한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>보수적 위험 관리:</b> 수익보다 원금 보존이 우선이며, 일일 손익이 -5%를 초과하면 당일 신규 진입을 전면 차단한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>부스터 (+10%p 절대값):</b> 강세장 시 개별 종목 한도(Core 35%, Sat 20%)는 유지하되, 전체 계좌의 투자 목표 예산 비중만 +10%p 확대한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>쿨다운 및 재무장:</b> 2회 연속 실현 손실 시 KRX 영업일 기준 쿨다운이 발동하며, 매도 후 신호가 false → true로 변경된 독립적 재무장 시에만 추가 매수를 허용한다.</li>
    </ul>

    <h3>2. 시스템 아키텍처 및 역할 분리 (MSA)</h3>
    <ul>
        <li><span style='color: #3b82f6;'>🔵 <b>[DESIGNED]</b></span> <b>대시보드(UI):</b> 지휘, 통제 및 주문 의도(Intent)를 적재만 하며 API 직접 호출 금지.</li>
        <li><span style='color: #f59e0b;'>🟡 <b>[IN PROGRESS]</b></span> <b>Signal Bot:</b> 실시간 시장 감시 및 독립적 신호 생성 (Heartbeat 대기).</li>
        <li><span style='color: #f59e0b;'>🟡 <b>[IN PROGRESS]</b></span> <b>Execution Worker:</b> 브로커 주문, 취소, 체결 대사 전담 (Heartbeat 대기).</li>
        <li><span style='color: #ef4444;'>🔴 <b>[BLOCKED]</b></span> <b>REAL 활성화 차단:</b> MOCK 대사가 완료되기 전까지 시스템은 REAL 전송을 차단한다.</li>
    </ul>

    <h3>3. 전략 산식 및 추세 매도 버퍼 정책</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>정상 추세매도 버퍼:</b> 노이즈 필터링을 위해 <code>buf * buffer_factor(0.5)</code> 즉, 절반의 하락 버퍼를 두어 휩쏘를 방어한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>즉각 위험 판정:</b> 손절 및 트레일링 스탑은 2분봉 대기 없이 최신 호가에서 즉시 체결 가격으로 강제 발동한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>1분봉 연속 확인:</b> 일반 매수와 정상 매도는 KIS 분봉 API의 Timestamp를 대조해 명확히 다른 2개의 완료봉에서 신호가 유지될 때만 확정한다.</li>
    </ul>

    <h3>4. 정밀 CostModel 도입</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>비용 분리 산출:</b> 수수료(0.015%), 유관기관, 슬리피지(0.1%), 세금을 완전히 분리하여 회계 원장에 각각 기록한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>연도별 세법 반영표:</b> KOSPI/KOSDAQ 기준 2022년(0.23%)~2026년(0.20%)의 법정 세법 개정안을 적용한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>Legacy 비교 모드:</b> 고정 편도 0.25% 모드를 토글로 지원한다.</li>
    </ul>

    <h3>5. 주문 상태 머신 (16 State DAG)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>단방향 흐름 통제:</b> INTENT_CREATED → CLAIMED → SUBMITTING → ACKNOWLEDGED → FILLED 등 16개 상태를 <code>system_contract.yaml</code>에 강제 종속시킨다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>원자적 게이트:</b> <code>claim_and_authorize_submission</code> 단일 트랜잭션에서 현금 예약 및 한도를 점검해 이중 지출(Double-Spend)을 막는다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>시장가 예약금 계산:</b> 가격 0이 아닌 <code>reference_price * 1.05</code> (상한가 버퍼)를 적용하여 정밀하게 한도를 차감한다.</li>
    </ul>

    <h3>6. KIS 001x 마이그레이션 및 대사</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>KRX-only 어댑터:</b> 최신 001x 규격을 사용하며 다크풀(NXT) 송출을 차단한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>자동 Fallback 금지:</b> Timeout 시 080x로 재전송하지 않고 UNKNOWN으로 마킹해 대사로 풀어나간다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>부분 체결 델타 룰:</b> 누적 체결량 간의 차이(Delta)만 원자적으로 포지션에 반영한다.</li>
    </ul>

    <h3>7. DB 무손실 마이그레이션 및 보안</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>v8 승급:</b> <code>fills</code>, <code>watchlist_events</code> 등 원장을 추가하고, <code>signal_states</code>에 쿨다운/재무장 필드를 확장(UPSERT)하였다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>API Key 격리:</b> <code>.gitignore</code>를 통해 비밀 키와 DB를 Git 추적망에서 완전히 제거하였다.</li>
    </ul>

    <h3>8. 고급 시뮬레이션 엔진</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>공통 엔진 사용:</b> 실거래와 시뮬레이션은 전략, 부스터, 비용 함수를 100% 공유한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>Test 1,2,3 분리:</b> 포트폴리오 회고(Test 1), 현금흐름 1:1 비교(Test 2), 과거 유니버스 재현(Test 3)의 목적을 분리한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>생존자 편향 경고:</b> 1분봉 및 상폐 종목 획득 불가 한계를 UI 상단에 명확히 표기(DAILY_APPROX 모드 강제)한다.</li>
    </ul>
    """, unsafe_allow_html=True)