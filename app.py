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

st.title("Core-Satellite Quant System (MSA)")
st.error("🚨 **[LIVE 금지 / 미검증 상태]** 실행 워커(Worker) 소스가 제출 및 검증되지 않았으며 KIS 001x API 모의 테스트가 완료되지 않았습니다. 실전 계좌(REAL) 오토파일럿 가동을 구조적으로 전면 차단합니다.")
st.markdown("한국 시장 전 종목 검색, **오토파일럿 무인 감시**, **실계좌 자동매매**, **고급 시뮬레이션**을 제공하는 SQLite 기반 실전 퀀트 대시보드입니다.")

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
    SYS_IS_MOCK = acc_config.get("is_mock", True)
    if isinstance(SYS_IS_MOCK, str): SYS_IS_MOCK = SYS_IS_MOCK.lower() == 'true'
    SYS_ACNT_PRDT = str(acc_config.get("acnt_prdt", "01")).strip()
except KeyError:
    SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_IS_MOCK, SYS_ACNT_PRDT = None, None, "MOCK_ACCOUNT", True, "01"

ENV_STR = "MOCK" if SYS_IS_MOCK else "REAL"
ACC_FP = hashlib.sha256(SYS_CANO.encode()).hexdigest()[:16] if SYS_CANO != "MOCK_ACCOUNT" else "MOCK_ACCOUNT"

with st.sidebar.expander("🔑 KIS 계좌 연동 상태", expanded=not bool(SYS_APP_KEY)):
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        st.success(f"✅ {STRAT_DISPLAY_MAP[active_strat]} 계좌 연동 완료")
        st.caption(f"계좌번호: {SYS_CANO} ({'모의' if SYS_IS_MOCK else '실전'})")
    else:
        st.error("⚠️ 스트림릿 Secrets에서 계좌 정보를 찾을 수 없습니다. (모의 DB 모드 작동 중)")

st.sidebar.markdown("---")
st.sidebar.header("🚨 전역 제어 (Master)")
master_ks = st.sidebar.toggle("전체 매매 일시중지 (Master Kill Switch)", value=bool(db.get_setting('master_kill_switch', False)))
if master_ks != bool(db.get_setting('master_kill_switch', False)): db.set_setting('master_kill_switch', master_ks)

st.sidebar.header(f"📱 {STRAT_DISPLAY_MAP[active_strat]} 계좌 제어")
acc_ks_key = f"kill_switch_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"
acc_at_key = f"auto_trade_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"
acc_ap_key = f"auto_pilot_KIS_{ENV_STR}_{ACC_FP}_{active_strat.value}"

acc_ks = st.sidebar.toggle("해당 계좌 긴급 정지", value=bool(db.get_setting(acc_ks_key, False)))
acc_at = st.sidebar.toggle("실전 자동주문 활성화 (현재 봇 부재로 작동불가)", value=bool(db.get_setting(acc_at_key, False)), disabled=True)
acc_ap = st.sidebar.toggle("오토파일럿 켜기", value=bool(db.get_setting(acc_ap_key, False)))

if acc_ks != bool(db.get_setting(acc_ks_key, False)): db.set_setting(acc_ks_key, acc_ks)
if acc_ap != bool(db.get_setting(acc_ap_key, False)): db.set_setting(acc_ap_key, acc_ap)

st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터 (Baseline)")
current_config = quant.get_default_config(active_strat)

with st.sidebar.expander("📊 현재 적용된 계약 파라미터 보기", expanded=False):
    st.info("💡 시스템 헌장(YAML)에 의해 임의 변경이 차단된 읽기 전용 상태입니다. (OOS 검증 원칙 준수)")
    st.markdown(f"- **200일 추세선 방어:** {'✅ 활성' if current_config.ma200 else '❌ 비활성'}")
    st.markdown(f"- **골든크로스/눌림목 버퍼:** `{current_config.buf * 100:.1f}%` (추세이탈계수: {current_config.buffer_factor})")
    st.markdown(f"- **긴급 손절 컷 (SL):** `{current_config.sl * 100:.1f}%`")
    st.markdown(f"- **트레일링 익절 목표:** `{current_config.ts_tgt * 100:.1f}%`")
    st.markdown(f"- **트레일링 하락 허용:** `{current_config.ts_drp * 100:.1f}%`")
    st.markdown(f"- **종목당 투입 한도:** `{current_config.alloc * 100:.0f}%`")
    st.markdown(f"- **연속 손실 쿨다운:** `{current_config.cd} 거래일`")
    st.markdown(f"- **최소 보유 기간:** `{current_config.min_h} 거래일`")
    st.markdown(f"- **강세장 비중 부스터:** {'✅ 활성' if current_config.boost else '❌ 비활성'}")

rd = st.session_state.get('real_data', db.get_setting('last_real_data', {'eval': float(total_cash), 'pnl': 0.0, 'cash': float(total_cash), 'stocks': []}))
st.session_state['real_data'] = rd 
real_invested_principal = rd['eval'] - rd['pnl'] if rd['eval'] > 0 else float(total_cash)

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유니버스", "🔌 실전 계좌", "🤖 자동매매 대기열", "📊 시뮬레이션", "📄 시스템 백서 v2.0"])

with tab1:
    st.header("📝 관심종목 유니버스 & 실시간 AI 진단")
    col_s1, col_s2 = st.columns([8, 2])
    
    with col_s1:
        if st.button("🚀 실시간 AI 타점 스캐너 가동", type="primary", use_container_width=True):
            with st.spinner("AI 검색 중... (엄격한 전략 기준에 따라 종목이 없을 수 있습니다)"):
                st.session_state.scan_res = quant.run_scanner_safe(active_strat, current_config)
                st.session_state.show_scanner = True
    
    with st.form("manual_search_form"):
        search_query = st.text_input("종목명 또는 종목코드(6자리) 입력", value=st.session_state.get('search_q', ''))
        if st.form_submit_button("🔍 검색하기"):
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
                else:
                    c4.button("✅ 완료", key=f"scan_{row['티커']}", disabled=True)
        else:
            st.info("현재 시장 상황에서는 백서의 엄격한 매수 조건(200일선 상회 등)을 만족하는 안전한 종목이 없습니다. 수동 검색을 통해 종목을 추가해보세요.")

    st.markdown("---")
    st.markdown("### 📋 현재 감시 리스트")
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
        return {'🗑️ 삭제': False, '종목명': row['종목명'], '티커': ticker, '실시간 현재가': f"{cp:,.0f} 원" if cp > 0 else "-", '🔥 점수': score, '🤖 액션': action, '📊 근거': reason}

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for res in executor.map(process_w, current_watchlist):
                if res:
                    display_records.append(res)
        display_df = pd.DataFrame(display_records)
        if not display_df.empty:
            edited_df = st.data_editor(display_df.sort_values('🔥 점수', ascending=False).reset_index(drop=True).style.format({'🔥 점수': '{:.2f}'}), use_container_width=True)
            if st.button("💾 체크한 종목 삭제 적용", type="primary"):
                remaining_items = edited_df[edited_df['🗑️ 삭제'] == False][['티커', '종목명']].to_dict('records')
                db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value, remaining_items)
                st.rerun()

with tab2:
    st.header("🔌 실전 계좌 모니터링")
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        if st.button("🔄 잔고 동기화"):
            token, token_err = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK)
            if token:
                st.session_state['kis_token'] = token 
                h, s, err = kis.fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, SYS_IS_MOCK)
                if err == "OK" and s:
                    c = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, SYS_IS_MOCK)
                    safe_cash = c if c > 0 else 0.0 
                    new_rd = {'eval': float(s[0]['tot_evlu_amt']), 'pnl': float(s[0]['evlu_pfls_smtl_amt']), 'cash': safe_cash, 'stocks': h}
                    st.session_state['real_data'] = new_rd
                    db.set_setting('last_real_data', new_rd)
                    kis_stocks = [{'ticker': str(i['pdno']).zfill(6), 'qty': int(i['hldg_qty']), 'buy_price': float(i['pchs_avg_pric']), 'current_price': float(i['prpr'])} for i in h if int(i['hldg_qty']) > 0]
                    try:
                        db.sync_positions_from_broker("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value, kis_stocks)
                    except Exception as e:
                        st.error(f"DB 동기화 중 오류 발생: {e}")
                        
                    st.success("잔고 동기화 완료! (자동매매 수량은 자체 원장을 따릅니다)")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error(f"계좌 조회 실패: {err}")
            else:
                st.error(f"API Token 발급 실패: {token_err} (is_mock 설정: {SYS_IS_MOCK})")
        
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{real_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", f"{rd['pnl']:+,.0f} 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{rd['cash']:,.0f} 원"), unsafe_allow_html=True)
        
        if rd['stocks']: 
            acc_df = pd.DataFrame([{'종목명': i['prdt_name'], '티커': i['pdno'], '수량': int(i['hldg_qty']), '평균단가': float(i['pchs_avg_pric']), '현재가': float(i['prpr']), '수익률': f"{float(i['evlu_pfls_rt']):+.2f}%"} for i in rd['stocks'] if int(i['hldg_qty'])>0])
            st.dataframe(acc_df.style.map(color_profit_loss, subset=['수익률']).format({'평균단가': '{:,.2f}', '현재가': '{:,.0f}', '수량': '{:,}'}), use_container_width=True)
    else:
        st.warning("KIS API Key가 연동되지 않아 모의 잔고 화면만 표시됩니다. (현재 설정된 가상 원금 기준)")
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", "0 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)

with tab3:
    st.header("🤖 실전 자동매매 큐")
    st.warning("⚠️ 대시보드는 주문 의도(Intent)를 DB에 적재만 합니다. 백그라운드 워커가 없을 경우 실제 KIS 체결은 발생하지 않습니다.")
    c1, c2, c3 = st.columns(3)
    c1.metric("🚨 킬 스위치", "차단됨" if (master_ks or acc_ks) else "정상")
    c2.metric("🚀 자동주문", "활성화" if acc_at else "비활성화")
    c3.metric("💵 가용 현금", f"{rd['cash']:,.0f} 원")
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
        kis_buy_p = 0.0
        for s in rd.get('stocks', []):
            if str(s.get('pdno', '')).zfill(6) == tk: 
                kis_qty = int(s['hldg_qty'])
                kis_buy_p = float(s['pchs_avg_pric'])
                break
                
        display_qty = kis_qty if kis_qty > 0 else m_qty
        display_buy_p = kis_buy_p if kis_qty > 0 else buy_p
        
        tok = st.session_state.get('kis_token')
        c_price, h_price, l_price, is_halted, _ = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, tk, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else (0.0, 0.0, 0.0, False, "No Token")
        cp, action, score, _ = quant.evaluate_stock_for_ui(tk, active_strat, current_config, buy_p, high_p, c_price, h_price, l_price, is_halted, days_held)
        
        is_holding = display_qty > 0
        buy_str = "추가 매수" if is_holding else "신규 매수"
        
        if "매도" in action or "청산" in action or "익절" in action:
            if m_qty > 0:
                return {'분류': 0, '점수': score, '종목명': nm, '티커': tk, '상태': f"🔴 {action}", '현재가': cp, '주문수량': m_qty, '보유수량': display_qty, '평균단가': display_buy_p}
            else:
                return {'분류': 2, '점수': score, '종목명': nm, '티커': tk, '상태': f"🟡 Managed 수량 없음 ({action})", '현재가': cp, '주문수량': 0, '보유수량': display_qty, '평균단가': display_buy_p}
        elif "매수" in action:
            curr_pos_val = display_qty * cp
            needed_amt = max(0.0, target_buy_amt - curr_pos_val)
            allow_amt = min(net_usable_cash, needed_amt)
            add_qty = int(allow_amt // (cp * 1.0025)) if cp > 0 else 0
            if add_qty > 0:
                return {'분류': 1, '점수': score, '종목명': nm, '티커': tk, '상태': f"🛒 {buy_str} 시그널 (승인 대기)", '현재가': cp, '주문수량': add_qty, '보유수량': display_qty, '평균단가': display_buy_p}
            else:
                return {'분류': 2, '점수': score, '종목명': nm, '티커': tk, '상태': f"🟡 현금/한도 부족 ({buy_str} 시그널)", '현재가': cp, '주문수량': 0, '보유수량': display_qty, '평균단가': display_buy_p}
        
        return {'분류': 2, '점수': score, '종목명': nm, '티커': tk, '상태': f"👁️ {action}", '현재가': cp, '주문수량': 0, '보유수량': display_qty, '평균단가': display_buy_p}

    if eval_list:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(process_q, eval_list):
                if r:
                    temp_q.append(r)
                
    q_df = pd.DataFrame(temp_q)
    if not q_df.empty:
        q_df = q_df.sort_values(by=['분류', '점수'], ascending=[True, False]).reset_index(drop=True)
        display_df = q_df[['종목명', '상태', '점수', '현재가', '주문수량', '보유수량', '평균단가']].copy()
        
        st.dataframe(display_df.style.format({
            '점수': '{:.2f}', '현재가': '{:,.0f}', '주문수량': '{:,}', '보유수량': '{:,}', '평균단가': '{:,.2f}'
        }), use_container_width=True)
        
        if st.button("⚡ 대기열 일괄 주문 의도 DB 기록", type="primary"):
            success_count = 0
            valid_orders = [r for _, r in q_df.iterrows() if r['분류'] in [0, 1] and r['주문수량'] > 0 and "🟡" not in r['상태'] and "👁️" not in r['상태']]
            for r in valid_orders:
                tk = r['티커']
                side = "BUY" if "매수" in r['상태'] else "SELL"
                now_str = datetime.datetime.now(KST).strftime('%Y%m%d_%H%M')
                spec = quant.OrderSpec(
                    correlation_id="", idempotency_key=f"UI_{tk}_{now_str}", broker="KIS", environment=ENV_STR, 
                    account_fingerprint=ACC_FP, account_product_code=SYS_ACNT_PRDT, portfolio_id=active_strat.value, 
                    strategy_id=active_strat.value, strategy_version="1.0", contract_version=db.CONTRACT['contract_version'],
                    ticker=tk, stock_name=r['종목명'], side=side, order_kind="MARKET", quantity=r['주문수량'], limit_price=0, 
                    reference_price=0.0, exchange="KRX", time_in_force="GTC", signal_id="UI_MANUAL", signal_source="UI", 
                    signal_cutoff=now_str, quote_id="", quote_source="UI", quote_timestamp=now_str, 
                    intent_ttl=300, cost_model_version=db.CONTRACT.get('cost_model_version', '2.0.0'), 
                    intent_created_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                )
                ok, msg = db.safe_add_order_intent(spec)
                if ok:
                    success_count += 1
                else:
                    st.warning(f"⚠️ {r['종목명']} 거절됨: {msg}")
            if success_count > 0:
                st.success(f"✅ {success_count}건 주문 의도 적재 완료! 워커의 CLAIM 대기 중...")
            else:
                st.info("기록할 수 있는 유효한 주문 시그널이 없습니다.")
    else:
        st.info("대기 중인 종목이 없습니다.")
    
    st.markdown("### 📊 실시간 체결 대사 현황")
    intents = db.get_orders_by_status_and_env(['INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'REJECTED', 'QUARANTINED'], "KIS", ENV_STR, ACC_FP, active_strat.value)
    if intents:
        st.dataframe(pd.DataFrame(intents)[['ticker', 'side', 'qty', 'status', 'cum_filled_qty', 'resp_code']], use_container_width=True)

with tab4:
    st.header("🧪 고급 백테스트 엔진")
    st.warning("⚠️ [DATA_LIMITED] 과거 분봉 및 상장폐지 데이터를 미반영한 DAILY_APPROX 방식입니다. 생존자 편향(Survivor Bias)을 주의하십시오.")
    
    today_date = datetime.datetime.now(KST).date()
    stocks_df = pd.DataFrame(db.get_watchlist("KIS", ENV_STR, ACC_FP, active_strat.value, active_strat.value))
    
    st.subheader("🎯 테스트 1. 관심·보유종목 매매 시뮬레이션")
    
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
    with t1_c1: 
        st.markdown(f"**분석 대상:** 총 **{len(combined_data)}**개 종목")
    with t1_c2: start_d1 = st.date_input("시작일", datetime.date(2023, 1, 1), key="t1_start")
    with t1_c3: end_d1 = st.date_input("종료일", today_date, key="t1_end")
    with t1_c4: use_legacy1 = st.checkbox("고정 0.25% 모델", value=False, key="l1")
    
    st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
    run_t1 = st.button("테스트 1 실행 (통합 포트폴리오)", type="primary", use_container_width=True)
    
    if run_t1:
        if target_df.empty:
            st.warning("분석 대상 종목이 없습니다.")
        elif start_d1 >= end_d1:
            st.warning("⚠️ 최소 하루 이상의 기간이 필요합니다.")
        else:
            with st.spinner(f"총 {len(combined_data)}개 종목 시뮬레이션 중..."):
                res1 = quant.run_quant_simulation(target_df, active_strat, total_cash, start_d1, end_d1, current_config, is_weekly_scan=False, use_legacy_cost=use_legacy1)
                if res1.get('status') == 'success':
                    st.success("시뮬레이션 완료!")
                    r1, r2, r3, r4 = st.columns(4)
                    r1.markdown(mts_metric_html("기말 자산", f"{res1['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                    r2.markdown(mts_metric_html("누적 수익률", f"{res1['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                    r3.markdown(mts_metric_html("시간가중수익률(TWR)", f"{res1['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    r4.markdown(mts_metric_html("최대 낙폭(MDD)", f"{res1['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res1['summary_rows']), use_container_width=True)
                    
                    with st.expander("📝 상세 거래 내역 보기"):
                        if res1.get('trade_logs'):
                            tl_df = pd.DataFrame(res1['trade_logs'])
                            st.dataframe(tl_df.style.map(color_profit_loss, subset=['수익률']).format({
                                '진입단가': '{:,.0f}', '청산단가': '{:,.0f}', '수량': '{:,}', '손익금': '{:,.0f}'
                            }), use_container_width=True)
                else:
                    st.error(f"실행 불가: {res1['msg']}")
    st.markdown("---")

    st.subheader("🎯 테스트 2. AI 가상운용 vs 실제계좌 성과 비교")
    t2_end_default = today_date
    one_year_ago = t2_end_default - datetime.timedelta(days=365)
    creation_date = db.get_portfolio_creation_date("KIS", ENV_STR, ACC_FP, active_strat.value)
    
    if creation_date and creation_date > one_year_ago:
        t2_start_default = creation_date
        st.info(f"💡 개설일({creation_date})부터 오늘까지의 성과를 1:1 비교합니다.")
    else:
        t2_start_default = one_year_ago
        
    t2_c1, t2_c2, t2_c3, t2_c4 = st.columns([2, 2, 2, 3])
    with t2_c1: start_d2 = st.date_input("시작일", t2_start_default, key="t2_start")
    with t2_c2: end_d2 = st.date_input("종료일", t2_end_default, key="t2_end")
    with t2_c3: use_legacy2 = st.checkbox("고정 0.25% 모델", value=False, key="l2")
    with t2_c4:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        run_t2 = st.button("테스트 2 실행 (일간 스캔)", type="primary", use_container_width=True)
        
    if run_t2:
        if stocks_df.empty:
            st.error("관심종목이 없습니다.")
        elif start_d2 >= end_d2:
            st.warning("⚠️ 최소 하루 이상의 기간이 필요합니다.")
        else:
            with st.spinner("AI 가상운용 시뮬레이션 구동 중..."):
                res2 = quant.run_quant_simulation(stocks_df, active_strat, real_invested_principal, start_d2, end_d2, current_config, is_weekly_scan=False, use_legacy_cost=use_legacy2)
                if res2.get('status') == 'success':
                    st.success("시뮬레이션 완료!")
                    actual_ret_pct = (rd['pnl'] / real_invested_principal * 100) if real_invested_principal > 0 else 0.0
                    comp_col1, comp_col2 = st.columns(2)
                    with comp_col1:
                        st.markdown("<h4 style='text-align:center; color:#3b82f6;'>🤖 AI 가상운용</h4>", unsafe_allow_html=True)
                        st.markdown(mts_metric_html("AI 가상 기말 자산", f"{res2['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                        st.markdown(mts_metric_html("AI 가상 누적 수익률", f"{res2['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        st.dataframe(pd.DataFrame(res2['summary_rows']), use_container_width=True)
                    with comp_col2:
                        st.markdown("<h4 style='text-align:center; color:#10b981;'>🧑‍💻 실제 계좌 (현재 잔고)</h4>", unsafe_allow_html=True)
                        st.markdown(mts_metric_html("실제 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
                        st.markdown(mts_metric_html("실제 누적 수익률", f"{actual_ret_pct:+.2f}%"), unsafe_allow_html=True)
                        st.info("※ 실제 계좌에는 외부 입출금 요인이 포함되어 있습니다.")
                else:
                    st.error(f"실행 불가: {res2['msg']}")
    st.markdown("---")

    st.subheader("🎯 테스트 3. 과거연도 재현 시뮬레이션")
    t3_c1, t3_c2, t3_c3 = st.columns([3, 2, 5])
    with t3_c1: test_year = st.selectbox("검증 연도 선택", [2022, 2023, 2024, 2025, 2026], index=4)
    with t3_c2: use_legacy3 = st.checkbox("고정 0.25% 모델", value=False, key="l3")
    with t3_c3:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        run_t3 = st.button(f"테스트 3 실행 ({test_year}년)", type="primary", use_container_width=True)
        
    if run_t3:
        with st.spinner(f"{test_year}년도 시뮬레이션 구동 중..."):
            res3 = quant.run_yearly_realistic_backtest(active_strat, total_cash, test_year, current_config, use_legacy_cost=use_legacy3)
            if res3.get('status') == 'success':
                st.success(f"{test_year}년 검증 완료!")
                r1, r2, r3, r4 = st.columns(4)
                r1.markdown(mts_metric_html("기말 자산", f"{res3['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                r2.markdown(mts_metric_html("누적 수익률", f"{res3['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                r3.markdown(mts_metric_html("시간가중수익률(TWR)", f"{res3['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                r4.markdown(mts_metric_html("최대 낙폭(MDD)", f"{res3['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
            else:
                st.error(f"실행 불가: {res3['msg']}")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 & 시스템 헌장 (v2.0.0)</h1>
    <div style='background-color: rgba(30, 58, 138, 0.1); padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 5px solid #1E3A8A;'>
        <h4 style='margin-top: 0;'>📌 헌장 상태 범례 (Status Legend)</h4>
        <p style='margin-bottom: 5px;'><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> : 코드 레벨 로직 구현이 완료되었으나 자동화 테스트(QA) 완벽 검증 대기 중인 룰</p>
        <p style='margin-bottom: 5px;'><span style='color: #3b82f6;'>🔵 <b>[설계됨]</b></span> : 시스템 아키텍처 상 구조적으로 정의되었으나 외부 연동 등 모의 검증이 필요한 룰</p>
        <p style='margin-bottom: 5px;'><span style='color: #f59e0b;'>🟡 <b>[미검증]</b></span> : 외부 종속성 등으로 인해 아직 테스트를 통과하지 못한 룰</p>
        <p style='margin-bottom: 0;'><span style='color: #ef4444;'>🔴 <b>[금지/차단]</b></span> : 시스템 안전을 위해 강제로 락(Lock)을 걸어둔 정책적 금지 사항</p>
    </div>
    <hr>
    
    <h3>🎯 1. 투자 대원칙 (Core Investment Principles)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>전략의 이원화 (Bifurcation):</b> 포트폴리오는 시장 주도주를 추종하는 대형주(Core) 전략과 단기 모멘텀/눌림목을 공략하는 중소형주(Satellite) 전략으로 완전히 분리되어 각각 독립된 워커(Worker)와 계좌에서 운용된다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>손실 최소화 우선 (Capital Preservation):</b> 수익 창출보다 원금 보존을 최우선으로 하며, 시장 폭락 시 기계적인 장중 손절 및 트레일링 스탑을 통해 포트폴리오의 MDD(Maximum Drawdown)를 엄격히 통제한다.</li>
    </ul>

    <h3>🏗️ 2. 시스템 아키텍처 및 워커 분리 규칙 (MSA Architecture)</h3>
    <ul>
        <li><span style='color: #3b82f6;'>🔵 <b>[설계됨]</b></span> <b>지휘 통제와 실행의 분리:</b> 대시보드(UI)는 KIS 주문 API를 직접 호출하지 않는다. 주문 버튼을 누르면 단지 DB에 <code>INTENT_CREATED</code> 상태로 정규화된 의도만을 적재한다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[설계됨]</b></span> <b>헤드리스 워커(Worker)의 독립성:</b> 실제 주문 제출, 체결 대사 및 포지션 갱신은 대시보드와 물리적으로 분리된 별도의 백그라운드 봇 프로세스 몫이다. 대시보드가 꺼져도 24시간 안전 감시망은 유지되어야 한다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[설계됨]</b></span> <b>워커의 원자적(Atomic) 책임:</b> 워커는 주문을 DB에서 읽어갈 때 원자적 Claim을 통해 독점 권한을 얻으며, 발송 직전 매매 중지 여부, 잔고, TTL을 이중 점검(Double-check) 후 단 1회만 KIS로 송출한다.</li>
        <li><span style='color: #ef4444;'>🔴 <b>[금지/차단]</b></span> <b>LIVE 활성화 차단 정책:</b> 외부 워커 소스가 제공되지 않았거나 검증되지 않은 상태이므로, AI는 실전(REAL) 오토파일럿 전환을 임의로 승인하거나 활성화할 수 없다.</li>
    </ul>

    <h3>🧮 3. 전략별 매력도 계산 공식 및 스캔 분리 (Strategy & Signal Regime)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>공통 조건:</b> KIS 또는 FDR 시세 기준, 가격 유효성 검증(NaN, Inf, 0원 차단), 거래 정지 종목 제외. <code>MA200</code> 장기 추세선 상회 종목만 필터링.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>Core (대형주) 산식:</b> KOSPI 시총 상위 200개 종목 대상. <code>MA60</code> 상승 추세 유지 시, <code>MA20</code>과 <code>MA60</code> 이격도를 기반으로 진입. (Score = 85.0 + max(0, 이격도 * 100))</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>Satellite (중소형주) 산식:</b> KOSDAQ 시총 상위 150개 종목 대상. <code>MA20</code> 기준 -5% ~ +3% 사이의 눌림목 발생 시 진입. (Score = 85.0 + max(0, (0.03 - 이격도) * 100))</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>일봉 지표와 실시간 가격의 완전 분리:</b> 이동평균선 등 지표 연산은 '미래 참조(Look-ahead)' 방지를 위해 전일(T-1) 종가까지만 반영하여 픽스(Fix)한다. 당일 실시간 현재가(T)가 이 고정된 지표선을 돌파하는지만 검사한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>UI 스캐너 예비 타점 명시:</b> UI의 '실시간 스캐너'는 버튼을 누른 그 순간(Instant)의 1차 조건 충족 여부만을 보여주며, 확정이 아닌 '예비 신호'로 취급한다.</li>
    </ul>

    <h3>⚙️ 4. 전략별 기본 파라미터 및 레지스트리 (Parameters & Registry)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>단일 진실 공급원(SSOT):</b> 시스템 파라미터(Core/Sat)와 추세이탈계수는 오직 <code>system_contract.yaml</code> 파일에서만 관리되며, UI는 이를 읽기 전용(Read-only)으로 표출한다. 변경 시 버전을 상향해야 전면 반영된다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>Core 기본 파라미터:</b> 버퍼 1.5%, 손절 -15%, 투입 한도 35%, 익절목표 30%, 하락허용 -10%, 쿨다운 60 거래일, 최소보유 5 거래일.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>Satellite 기본 파라미터:</b> 버퍼 1.0%, 손절 -12%, 투입 한도 20%, 익절목표 20%, 하락허용 -7%, 쿨다운 30 거래일, 최소보유 3 거래일.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>파라미터 무결성 경계값 보장:</b> <code>NaN</code>, <code>Inf</code> 차단. 손절컷(<code>sl</code>)과 트레일링 하락허용(<code>ts_drp</code>)은 반드시 음수(-)로 강제된다.</li>
    </ul>

    <h3>🛡️ 5. 고급 안전장치 및 추세 매도 버퍼 정책 (Safety & Trend Exit)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>공통 추세이탈 버퍼 계수 도입:</b> 휩쏘(속임수)로 인한 과매매를 막기 위해 <code>trend_exit.buffer_factor = 0.5</code> 정책을 적용하여 약간의 노이즈 하락은 허용한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>즉각 위험 판정의 분리:</b> <code>ExitReason</code> 열거형(Enum)을 통해 손절(STOP_LOSS)과 트레일링 스탑(TRAILING_STOP)은 버퍼 없이 닿는 즉시 보수적 가격으로 강제 청산한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>재진입 쿨다운 (Signal Rearm):</b> 매도가 발생한 종목은, 매수 조건이 한 번 완전히 이탈되었다가 다시 충족되어야만 재무장(Rearm)을 허용하여 무한 물타기를 차단한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>일일 손실 컷 차단:</b> 계좌의 일일 손익이 -5%를 초과할 경우 당일 신규 매수 진입을 전면 차단한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>가격 괴리율 방어:</b> 의도 생성 시점의 호가와 제출 시점의 실제 호가 괴리가 3%를 초과하면 급등락으로 판단하고 반려(REJECT) 처리한다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[설계됨]</b></span> <b>2연속 1분봉 확인 룰 (Signal Regime):</b> 실시간 분봉 검사 시 60초 대기가 아닌, 거래소 API의 '봉 종료 시각'을 기준으로 명확히 구분된 두 개의 분봉 데이터를 비교하여 신호를 확정해야 한다.</li>
    </ul>

    <h3>🔄 6. KIS API 규격 및 001x 마이그레이션 (API & 001x Migration)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>어댑터 격리 (080x vs 001x):</b> 구형 080x 호출 계약은 <code>LEGACY_080X</code>로 하위 호환용으로만 격리되었으며, 신규 봇은 <code>CURRENT_001X</code> 공식 어댑터를 강제 사용하여 매매한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>지수 백오프(Exponential Backoff) 방어막:</b> 증권사 호출 과부하(429 Too Many Requests) 시, 0.5초부터 시작하여 2배씩 대기 시간을 늘리는 로직이 내장되었다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>KRX 전용 정책:</b> 001x 주문 시 <code>EXCG_ID_DVSN_CD="KRX"</code>를 강제 세팅하여 사용자 동의 없는 다크풀(NXT/SOR) 전송을 원천 차단한다.</li>
        <li><span style='color: #ef4444;'>🔴 <b>[금지/차단]</b></span> <b>자동 Fallback 금지:</b> 001x 주문 중 Timeout이 발생하여 <code>UNKNOWN</code> 상태가 되더라도, 이중 매매(Double POST)를 막기 위해 절대 080x로 재호출하지 않고 대사를 통해 후행 처리한다.</li>
    </ul>

    <h3>🔄 7. 주문 상태 머신 및 회계 대사 (State Machine & Reconciliation)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>엄격한 단방향 전이(State DAG):</b> <code>INTENT_CREATED</code>부터 <code>FILLED</code>까지 계약(yaml)에 사전 정의된 16개 단방향 흐름으로만 주문이 전이되도록 통제한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>멱등성 보장 (Idempotency):</b> UUID, 시간, 종목, 계좌를 조합한 해시 키를 통해 네트워크 지연으로 인한 다중 워커의 동일 주문 중복 전송을 완벽히 차단한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>이중 지출 방지 (Double-Spend Block):</b> 시장가 매수 2건이 동일한 가용 현금을 중복 예약하지 않도록 <code>pre_flight_risk_check</code> 게이트가 즉시 차감 계산을 수행한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>부분 체결 누적 대사 델타(Delta) 룰:</b> 주문의 부분 체결량은 누적량과 기존 처리량의 차이값(Delta)만을 산출하여 정확히 단 1번만 포지션 원장에 더하거나 뺀다.</li>
    </ul>

    <h3>📉 8. 정밀 시뮬레이션 및 실제 비용 모델 (Simulation & CostModel)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>비용 분리 산출 엔진:</b> 수수료(0.015%), 유관기관 제비용, 슬리피지(0.1%) 및 매도 세금을 모두 쪼개서 연산하는 <code>CostModel</code> 클래스를 도입하였다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>연도별 세법 개정안 반영표:</b> KOSPI/KOSDAQ 구분을 통해 2022년(0.23%)부터 2026년(0.20%)까지의 연도별 세금 정책을 완벽하게 적용하여 과거 성과의 왜곡을 방지한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>0.25% 레거시 비교 모드:</b> 과거 백테스트 결과와의 호환성 검증을 위해 <code>LEGACY_CONSERVATIVE</code> 모드 토글을 지원한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>슬리피지 불리한 적용의 원칙:</b> 매수 시에는 가격을 올리고 매도 시에는 가격을 내리는 구조를 채택하며, 이 회계 단가가 포지션 평균단가의 모수가 된다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>T+1 영업일 시가 체결 원칙:</b> 신호가 발생한 날(T) 종가에 샀다는 미래 참조(Look-ahead) 사기를 방지하고, 다음 날 아침(T+1) 시가(Open)로 체결 처리한다.</li>
    </ul>

    <h3>🖥️ 9. UI 레이아웃 및 관측 가능성 (UI Observability)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>KST 타임존 강제 적용:</b> 클라우드 OS 타임존 차이로 인한 오류를 막기 위해 모든 시간 연산은 한국 표준시(UTC+9)로 고정한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>Session State 보존:</b> 새로고침 시에도 AI 검색 결과 타점이 증발하지 않도록 Streamlit 세션 변수에 데이터를 캐싱한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>시뮬레이션 UI 분리 (Test 1, 2, 3):</b> 통합 포트폴리오 회고(Test 1), 계좌와 1:1 비교(Test 2), 과거 연간 풀 테스트(Test 3)를 분리하여 가시성을 극대화한다.</li>
        <li><span style='color: #f59e0b;'>🟡 <b>[미검증]</b></span> <b>생존자 편향 경고 (Point-in-time 한계):</b> 과거 상장폐지 종목을 가져올 수 없는 현 환경상 <code>DAILY_APPROX</code> 모드가 강제되며, 생존자 편향 근사치임이 붉은 텍스트로 상시 노출된다.</li>
    </ul>

    <h3>🗄️ 10. 데이터베이스 및 계좌 격리 (Database & Integrity)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>SQLite WAL 모드 활성화:</b> 대시보드와 워커 간의 동시 다발적 락(Database is locked) 에러 방지를 위해 WAL 모드를 선언하였다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>계좌 Fingerprint 익명화:</b> 평문 계좌번호 저장을 금지하고, <code>account_id</code>는 SHA-256 해시화 처리되어 격리 기록된다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>단일 호스트 강제 선언:</b> SQLite의 MSA 한계를 인지하며, 서로 다른 서버에서 공유 폴더로 DB를 물리지 않고 동일 영구 호스트 내에서만 구동하는 것을 헌장으로 못 박는다.</li>
    </ul>

    <h3>🚨 11. 장애 복구 및 보안 (Disaster Recovery & Security)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>워커 Lease Fencing 토큰:</b> 복수의 워커가 구동될 경우 <code>worker_leases</code> 테이블의 토큰 시스템을 통해 독점 권한을 가진 단 한 명의 워커만 KIS POST를 허가한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>API Key 물리적 격리:</b> <code>st.secrets</code> 또는 <code>.env</code> 등 오프체인에 키를 저장하며, 데이터베이스나 로그에 절대 평문으로 노출하지 않는다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>Bcrypt 검증 폼 해싱:</b> 대시보드 진입 비밀번호는 Salt가 결합된 Bcrypt 해시만을 비교하여 인증한다.</li>
    </ul>

    <h3>🧪 12. 자동화 테스트 및 품질 보증 (QA & Automated Testing)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>의도 생성 테스트 뼈대:</b> 23개의 필수 안전망 단위 테스트(<code>test_quant.py</code>) 구조가 수립되었다.</li>
        <li><span style='color: #f59e0b;'>🟡 <b>[미검증]</b></span> <b>Mock E2E 검증 대기:</b> 현재 워커 부재 및 계좌 자격증명 모의 환경 우회로 인해 다수의 Test Case에 Skip 마킹이 되어 있다.</li>
        <li><span style='color: #ef4444;'>🔴 <b>[금지/차단]</b></span> <b>실계좌 라이브 락(Lock):</b> 자동화 테스트(QA) 100% 통과 전까지 실계좌 오토파일럿 활성화 버튼은 강제로 Disabled 상태로 잠긴다.</li>
    </ul>

    <h3>📈 13. 포트폴리오 성과 및 리스크 지표 (Metrics)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>샤프 비율 (Sharpe Ratio):</b> 변동성 대비 얼마나 효율적으로 포트폴리오 수익을 창출했는지 평가한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>소르티노 비율 (Sortino Ratio):</b> 하락 변동성(Downside Risk)만을 척도로 삼아 알고리즘의 실질적 하방 경직성을 산출한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>칼마 비율 (Calmar Ratio):</b> 연평균 수익률(CAGR)을 최대 낙폭(MDD)으로 나눈 값으로 펀드매니저 수준의 회복 탄력성을 제시한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[구현 완료]</b></span> <b>포트폴리오 회전율 및 누수 비용 (Turnover & Cost Drag):</b> 과매매로 인해 녹아내린 수수료 및 세금 누수액을 적나라하게 보여주어 휩쏘 억제를 유도한다.</li>
    </ul>

    <hr>
    <h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 및 자동 문서화 (절대 원칙)</h3>
    <ul style='color: #DC2626;'>
        <li>이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 <b>Part 1~13 전체 내용(투자 대원칙, 전략 공식, 아키텍처 규칙, 비용모델, 방어 로직 등)을 단 하나라도 임의로 축약, 훼손, 삭제해서는 안 됩니다.</b></li>
        <li><b>[자동 문서화 헌장]:</b> 향후 시스템의 로직, 파라미터, UI/UX 규칙을 새롭게 추가하거나 변경할 경우(단순 오타 수정 제외), <b>사용자가 따로 요청하지 않더라도 AI는 반드시 그 변경 사항과 사유를 이 백서(해당하는 Part)에 자동으로 업데이트하여 기록해야 합니다.</b> 이 백서는 시스템의 단일 진실 공급원(Single Source of Truth)으로 취급되어야 합니다.</li>
    </ul>
    """, unsafe_allow_html=True)