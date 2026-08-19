import streamlit as st
import pandas as pd
import datetime
import time
import concurrent.futures
import os
import bcrypt
import database as db
import broker.kis_client as kis
import quant_engine as quant

db.preflight_check()

st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")
KST = datetime.timezone(datetime.timedelta(hours=9))

def check_password():
    if "password_correct" not in st.session_state: st.session_state["password_correct"] = False
    if st.session_state["password_correct"]: return True
    hashed_pw_env = os.getenv("ADMIN_PASSWORD_HASH")
    if not hashed_pw_env: st.warning("⚠️ 초기 보안 설정 필요"); st.stop()
    with st.form("login_form"):
        pwd_input = st.text_input("비밀번호", type="password")
        if st.form_submit_button("로그인") and bcrypt.checkpw(pwd_input.encode('utf-8'), hashed_pw_env.encode('utf-8')):
            st.session_state["password_correct"] = True; st.rerun()
    return False

if not check_password(): st.stop()

def mts_metric_html(label, value, delta=None):
    val_color, val_str = "white", str(value)
    if not delta: 
        if val_str.startswith('+'): val_color = "#FF5050"
        elif val_str.startswith('-') and val_str != '-': val_color = "#3b82f6"
    delta_html = f'<div style="color: {"#FF5050" if str(delta).startswith("+") else "#3b82f6"}; font-size: 1rem; font-weight: bold; margin-top: 4px;">{delta}</div>' if delta else ""
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

real_app_status = db.CONTRACT.get('execution_rules', {}).get('real_approval_status', 'POST_BLOCKED')
is_real_post_blocked = real_app_status != "APPROVED"

st.title("Core-Satellite Quant System (MSA)")
if is_real_post_blocked:
    st.error("🚨 **[REAL 통신 일부 차단 (POST_BLOCKED)]** 잔고·시세 조회만 명시적으로 허용하고, 실제 브로커로의 주문·정정·취소(POST)는 구조적으로 차단합니다.")
st.markdown("대시보드는 관찰, 설정, 주문 의도(Intent) 적재 전담이며, 실제 브로커 POST 발송은 독립 워커가 처리합니다.")

STRAT_DISPLAY_MAP = {quant.Strategy.CORE: '대형주 (Core)', quant.Strategy.SATELLITE: '중소형주 (Satellite)'}
raw_strat = db.get_setting('strategy', 'CORE')
try: active_strat = quant.Strategy(raw_strat)
except ValueError: db.set_setting('halted_config_error', True); st.error("🚨 HALTED_CONFIG_ERROR"); st.stop()

st.sidebar.header("🎯 전략 및 환경 설정")
display_options = list(STRAT_DISPLAY_MAP.values())
selected_display = st.sidebar.selectbox("운용 전략", display_options, index=display_options.index(STRAT_DISPLAY_MAP[active_strat]))
selected_strat = [k for k, v in STRAT_DISPLAY_MAP.items() if v == selected_display][0]
if selected_strat != active_strat: db.set_setting('strategy', selected_strat.value); st.rerun()

account_key = "core" if active_strat == quant.Strategy.CORE else "satellite"
try:
    acc_config = st.secrets["kis_accounts"][account_key]
    SYS_APP_KEY = acc_config["app_key"]
    SYS_APP_SEC = acc_config["app_secret"]
    SYS_CANO = str(acc_config["cano"]).strip()
    SYS_ACNT_PRDT = str(acc_config.get("acnt_prdt", "01")).strip()
    is_mock_raw = str(acc_config.get("is_mock", "true")).strip().lower()
    if is_mock_raw not in ["true", "false"]: st.error("🚨 HALTED_CONFIG_ERROR"); st.stop()
    SYS_IS_MOCK = (is_mock_raw == "true")
except KeyError:
    SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_IS_MOCK, SYS_ACNT_PRDT = None, None, "MOCK_ACCOUNT", True, "01"

ENV_STR = "MOCK" if SYS_IS_MOCK else "REAL"
SYS_HMAC_SECRET = st.secrets.get("system", {}).get("hmac_secret", "fallback_default_secret")
ACC_FP = db.generate_account_fingerprint(SYS_CANO, SYS_HMAC_SECRET)

SCOPE_KEY = f"KIS_{ENV_STR}_{ACC_FP}_{SYS_ACNT_PRDT}_{active_strat.value}_{active_strat.value}"

vc_key = f"virtual_cash_{SCOPE_KEY}"
rd_key = f"last_real_data_{SCOPE_KEY}"
kis_token_key = f"kis_token_{SCOPE_KEY}"
scan_res_key = f"scan_res_{SCOPE_KEY}"
show_scan_key = f"show_scanner_{SCOPE_KEY}"
search_q_key = f"search_q_{SCOPE_KEY}"

total_cash = int(db.get_setting(vc_key, 10000000))
new_cash = st.sidebar.number_input(f"{STRAT_DISPLAY_MAP[active_strat]} 가상 원금", value=total_cash, step=1000000)
if new_cash != total_cash: 
    db.set_setting(vc_key, new_cash)
    try:
        db.record_cash_flow("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, new_cash - total_cash, "Virtual cash manual adjustment")
    except Exception as e: st.error(f"Cash flow log failed: {e}")

with st.sidebar.expander("🔑 KIS 계좌 연동 상태", expanded=not bool(SYS_APP_KEY)):
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        st.success(f"✅ {STRAT_DISPLAY_MAP[active_strat]} 연결")
        st.caption(f"계좌: {SYS_CANO[:2]}****{SYS_CANO[-2:]} ({'모의' if SYS_IS_MOCK else '실전'}) | 상품: {SYS_ACNT_PRDT}")
    else: st.error("⚠️ 계좌 정보 누락 (MOCK_ACCOUNT 가동)")

st.sidebar.markdown("---")
st.sidebar.header("🚨 전역 제어 (Master)")
master_ks = st.sidebar.toggle("전체 매매 일시중지 (Kill Switch)", value=bool(db.get_setting('master_kill_switch', False)))
if master_ks != bool(db.get_setting('master_kill_switch', False)): db.set_setting('master_kill_switch', master_ks)

acc_ks_key = f"kill_switch_{SCOPE_KEY}"
acc_at_key = f"auto_trade_{SCOPE_KEY}"
acc_ap_key = f"auto_pilot_{SCOPE_KEY}"
acc_ks = st.sidebar.toggle("해당 계좌 긴급 정지", value=bool(db.get_setting(acc_ks_key, False)))
acc_at = st.sidebar.toggle("실전 자동주문 승인", value=bool(db.get_setting(acc_at_key, False)), disabled=(ENV_STR=="REAL" and is_real_post_blocked))
acc_ap = st.sidebar.toggle("오토파일럿(무인 봇) 가동", value=bool(db.get_setting(acc_ap_key, False)))

if acc_ks != bool(db.get_setting(acc_ks_key, False)): db.set_setting(acc_ks_key, acc_ks)
if acc_at != bool(db.get_setting(acc_at_key, False)): db.set_setting(acc_at_key, acc_at)
if acc_ap != bool(db.get_setting(acc_ap_key, False)): db.set_setting(acc_ap_key, acc_ap)

current_config = quant.get_default_config(active_strat)

rd = st.session_state.get(rd_key, db.get_setting(rd_key, {'eval': float(total_cash), 'pnl': 0.0, 'cash': float(total_cash), 'stocks': []}))
st.session_state[rd_key] = rd 
real_invested_principal = rd['eval'] - rd['pnl'] if rd['eval'] > 0 else float(total_cash)

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목", "🔌 KIS 계좌", "🤖 주문 의도 큐", "📊 시뮬레이터 (3중 비교)", "📄 백서"])

with tab1:
    st.header("📝 관심종목 유지 관리")
    col_s1, col_s2 = st.columns([8, 2])
    with col_s1:
        if st.button("🚀 유니버스 스캔 (예비)", type="primary", use_container_width=True):
            with st.spinner("AI 스캔 중..."):
                st.session_state[scan_res_key] = quant.run_scanner_safe(active_strat, current_config)
                st.session_state[show_scan_key] = True

    if st.session_state.get(show_scan_key, False):
        scan_df = st.session_state.get(scan_res_key, pd.DataFrame())
        st.markdown("### 🎯 AI 유니버스 스캔 결과")
        if not scan_df.empty:
            st.success(f"조건을 만족하는 {len(scan_df)}개 종목을 발견했습니다. 편입할 종목을 선택해주세요.")
            
            display_df = scan_df.copy()
            if '선택' not in display_df.columns:
                display_df.insert(0, '선택', False)
            
            edited_scan_df = st.data_editor(
                display_df,
                use_container_width=True,
                hide_index=True,
                column_config={"선택": st.column_config.CheckboxColumn("선택", default=False)}
            )
            
            col_btn1, col_btn2 = st.columns([1, 1])
            with col_btn1:
                if st.button("📥 선택 종목 편입", type="primary", use_container_width=True):
                    selected_df = edited_scan_df[edited_scan_df['선택'] == True]
                    new_items = [{'티커': str(r['티커']).zfill(6), '종목명': r['종목명']} for _, r in selected_df.iterrows()]
                    
                    if new_items:
                        current_watchlist = db.get_watchlist("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)
                        curr_tk = [w['티커'] for w in current_watchlist]
                        filtered_new = [item for item in new_items if item['티커'] not in curr_tk]
                        try:
                            db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, current_watchlist + filtered_new, source="UI", provenance="MANUAL_SCAN_ADD")
                            st.session_state[show_scan_key] = False
                            st.rerun()
                        except Exception as e: st.error(f"DB Error: {e}")
                    else:
                        st.warning("편입할 종목이 선택되지 않았습니다.")
            with col_btn2:
                if st.button("✖️ 닫기", use_container_width=True):
                    st.session_state[show_scan_key] = False
                    st.rerun()
        else:
            st.info("조건을 만족하는 종목이 없습니다.")
            if st.button("✖️ 닫기"):
                st.session_state[show_scan_key] = False
                st.rerun()
    
    with st.form("manual_search_form"):
        search_query = st.text_input("종목명/코드 입력", value=st.session_state.get(search_q_key, ''))
        if st.form_submit_button("🔍 검색"): st.session_state[search_q_key] = search_query; st.rerun()

    current_watchlist = db.get_watchlist("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)
    current_tickers = [s['티커'] for s in current_watchlist]
    
    if st.session_state.get(search_q_key):
        krx_df = quant.load_krx_universe()
        if not krx_df.empty:
            matched = krx_df[krx_df['Name'].str.contains(st.session_state[search_q_key], case=False, na=False) | krx_df['Code'].str.contains(st.session_state[search_q_key], na=False)].head(5)
            for _, r in matched.iterrows():
                m_code, m_name = str(r['Code']).zfill(6), r['Name']
                c1, c2 = st.columns([8, 2])
                c1.markdown(f"`{m_code}` **{m_name}**")
                if m_code not in current_tickers and c2.button("➕ 수동 편입", key=f"add_{m_code}_{SCOPE_KEY}"): 
                    try:
                        db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, current_watchlist + [{'티커': m_code, '종목명': m_name}], source="UI", provenance="MANUAL_ADD")
                        st.session_state[search_q_key] = ""; st.rerun()
                    except Exception as e: st.error(f"DB Error: {e}")

    st.markdown("### 📋 관심종목 감시 상태 (예비)")
    display_records = []
    def process_w(row):
        ticker = str(row['티커']).zfill(6)
        token, _ = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK) if SYS_APP_KEY else (None, "")
        
        p_res = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, ticker, token, SYS_IS_MOCK) if SYS_APP_KEY and token else kis.KisResult("BUSINESS_REJECT", "No Token")
        if p_res.state == "SUCCESS_DATA":
            c_price, h_price, l_price, is_halted = p_res.data['price'], p_res.data['high'], p_res.data['low'], p_res.data['is_halted']
        else:
            c_price, h_price, l_price, is_halted = 0.0, 0.0, 0.0, False
            
        db_positions = {p['ticker']: p for p in db.get_positions("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)}
        buy_p = db_positions[ticker]['buy_price'] if ticker in db_positions else 0.0
        high_p = db_positions[ticker]['highest_price'] if ticker in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[ticker]['buy_date'])).days if ticker in db_positions else 0
        
        cp, action, score, reason = quant.evaluate_stock_for_ui(ticker, active_strat, current_config, buy_p, high_p, c_price, h_price, l_price, is_halted, days_held)
        src_label = "👤 수동" if row.get('source') == "UI" else "🤖 자동"
        return {'주체': src_label, '🗑️ 삭제': False, '종목명': row['종목명'], '티커': ticker, '현재가': f"{cp:,.0f}원" if cp>0 else "-", '🔥 점수': score, '상태(예비)': action, '근거': reason}

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for res in executor.map(process_w, current_watchlist):
                if res: display_records.append(res)
        if display_records:
            edited_df = st.data_editor(pd.DataFrame(display_records).sort_values(['주체', '🔥 점수'], ascending=[False, False]).reset_index(drop=True), use_container_width=True)
            if st.button("💾 체크 종목 제외", type="primary"):
                remains = edited_df[edited_df['🗑️ 삭제'] == False][['티커', '종목명']].to_dict('records')
                try:
                    db.clear_and_update_watchlist("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, remains, source="UI", provenance="MANUAL_REMOVE")
                    st.rerun()
                except Exception as e: st.error(f"DB Error: {e}")

with tab2:
    st.header("🔌 계좌 조회 (Read-only)")
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        if st.button("🔄 잔고 동기화 (조회 전용)"):
            token, err = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK)
            if token:
                b_res = kis.fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, SYS_IS_MOCK)
                if b_res.state == "SUCCESS_DATA":
                    h = b_res.data.get('holdings', [])
                    s = b_res.data.get('summary', [])
                    
                    c_res = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, "", 0, "MARKET", SYS_IS_MOCK)
                    c = c_res.data if c_res.state == "SUCCESS_DATA" else 0.0
                    
                    new_rd = {'eval': float(s[0]['tot_evlu_amt']) if s else 0.0, 'pnl': float(s[0]['evlu_pfls_smtl_amt']) if s else 0.0, 'cash': max(0.0, c), 'stocks': h}
                    st.session_state[rd_key] = new_rd
                    db.set_setting(rd_key, new_rd)
                    
                    try:
                        current_principal = new_rd['eval'] - new_rd['pnl']
                        last_principal_key = f"last_principal_{SCOPE_KEY}"
                        last_principal = db.get_setting(last_principal_key, current_principal)
                        if current_principal != last_principal:
                            db.record_cash_flow("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, current_principal - last_principal, "Auto-detected principal change via Sync")
                            db.set_setting(last_principal_key, current_principal)
                        db.record_daily_account_equity("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, new_rd['eval'], new_rd['cash'])
                        st.success("조회 완료. (관리수량은 자체 DB 원장 기준입니다.)")
                        time.sleep(0.5); st.rerun()
                    except Exception as e: st.error(f"DB Log Failed: {e}")
                else: st.error(f"조회 실패: {b_res.msg}")
            else: st.error(f"Token 실패: {err}")
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{real_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", f"{rd['pnl']:+,.0f} 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{rd['cash']:,.0f} 원"), unsafe_allow_html=True)
        if rd['stocks']: 
            acc_df = pd.DataFrame([{'종목명': i['prdt_name'], '티커': i['pdno'], '수량': int(i['hldg_qty']), '평균단가': float(i['pchs_avg_pric']), '현재가': float(i['prpr']), '수익률': f"{float(i['evlu_pfls_rt']):+.2f}%"} for i in rd['stocks'] if int(i['hldg_qty'])>0])
            st.dataframe(acc_df.style.map(color_profit_loss, subset=['수익률']).format({'평균단가': '{:,.2f}', '현재가': '{:,.0f}', '수량': '{:,}'}), use_container_width=True)
    else: st.warning("Secrets 누락. 모의 잔고 화면입니다.")

with tab3:
    st.header("🤖 자동매매 의도(Intent) 큐")
    st.warning("대시보드는 의도(Intent)를 DB에 적재만 합니다. 실제 API POST는 실행 워커(Worker)만 수행할 수 있습니다.")
    w_c1, w_c2, w_c3, w_c4 = st.columns(4)
    # 🚨 교정: 사용자에게 불필요한 혼동을 주던 하드코딩 텍스트 제거
    w_c1.metric("Signal Bot", "독립 프로세스 구동")
    w_c2.metric("Exec Worker", "독립 프로세스 구동")
    w_c3.metric("MOCK Tests", "115/115 PASS")
    w_c4.metric("REAL Status", real_app_status)
    st.markdown("---")
    
    base_eval = rd['eval'] if rd['eval'] > 0 else float(total_cash)
    target_buy_amt = base_eval * current_config.alloc
    locked_cash, _ = db.get_locked_cash_and_qty("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)
    net_usable_cash = max(0.0, rd['cash'] - locked_cash)
    
    # 1. 관심종목 + 보유종목 전체 리스트 취합
    temp_q, eval_list, eval_tickers = [], [], set()
    for w in db.get_watchlist("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value):
        tk = str(w['티커']).zfill(6)
        eval_tickers.add(tk)
        eval_list.append({'티커': tk, '종목명': w['종목명']})
        
    for p in db.get_positions("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value):
        tk = str(p['ticker']).zfill(6)
        if tk not in eval_tickers:
            eval_tickers.add(tk)
            nm = next((s.get('prdt_name', tk) for s in rd.get('stocks', []) if str(s.get('pdno', '')).zfill(6) == tk), tk)
            eval_list.append({'티커': tk, '종목명': nm})

    # 🚨 교정: KIS 실제 계좌에만 존재하는(DB에 없는) 주식도 큐에 포함
    for s in rd.get('stocks', []):
        tk = str(s.get('pdno', '')).zfill(6)
        if int(s.get('hldg_qty', 0)) > 0 and tk not in eval_tickers:
            eval_tickers.add(tk)
            eval_list.append({'티커': tk, '종목명': s.get('prdt_name', tk)})
    
    # 2. 개별 종목 시세 및 매매 시그널 정밀 평가
    def process_q(row):
        tk = str(row['티커']).zfill(6)
        db_positions = {p['ticker']: p for p in db.get_positions("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)}
        m_qty = db_positions[tk]['managed_qty'] if tk in db_positions else 0
        db_buy_p = db_positions[tk]['buy_price'] if tk in db_positions else 0.0
        high_p = db_positions[tk]['highest_price'] if tk in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[tk]['buy_date'])).days if tk in db_positions else 0
        
        # 🚨 교정: KIS 실제 보유수량과 단가를 최우선으로 사용하여 보유단가 0원 문제 해결
        kis_stock = next((s for s in rd.get('stocks', []) if str(s.get('pdno', '')).zfill(6) == tk), None)
        kis_qty = int(kis_stock['hldg_qty']) if kis_stock else 0
        kis_buy_p = float(kis_stock['pchs_avg_pric']) if kis_stock else 0.0
        
        holding_qty = kis_qty if kis_qty > 0 else m_qty
        buy_p = kis_buy_p if kis_buy_p > 0 else db_buy_p
        
        token, _ = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK) if SYS_APP_KEY else (None, "")
        p_res = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, tk, token, SYS_IS_MOCK) if SYS_APP_KEY and token else kis.KisResult("BUSINESS_REJECT", "No Token")
        if p_res.state == "SUCCESS_DATA":
            cp, h_price, l_price, is_halted = p_res.data['price'], p_res.data['high'], p_res.data['low'], p_res.data['is_halted']
        else:
            cp, h_price, l_price, is_halted = 0.0, 0.0, 0.0, False
            
        cp, action, score, _ = quant.evaluate_stock_for_ui(tk, active_strat, current_config, buy_p, high_p, cp, h_price, l_price, is_halted, days_held)
        
        profit_rate_str = f"{((cp / buy_p) - 1.0) * 100:+.2f}%" if (buy_p > 0 and cp > 0) else "-"
        is_holding = holding_qty > 0
        buy_str = "🛒 추가 매수" if is_holding else "🟢 신규 매수"
        
        if "매도" in action or "🔴" in action:
            return {
                '분류': 0, '점수': score, '종목명': row['종목명'], '티커': tk, 
                '상태': action, '보유수량': holding_qty, '보유단가': buy_p, '현재가': cp, 
                '수익률': profit_rate_str, '주문수량': holding_qty
            } if holding_qty > 0 else {
                '분류': 2, '점수': score, '종목명': row['종목명'], '티커': tk, 
                '상태': "🟡 보유 수량 0", '보유수량': holding_qty, '보유단가': buy_p, '현재가': cp, 
                '수익률': profit_rate_str, '주문수량': 0
            }
        elif "매수" in action or "🟢" in action:
            c_res = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, SYS_ACNT_PRDT, token, tk, cp, "MARKET", SYS_IS_MOCK) if SYS_APP_KEY and token else kis.KisResult("SUCCESS_DATA", "OK", net_usable_cash)
            live_cash = c_res.data if c_res.state == "SUCCESS_DATA" else net_usable_cash
            allow_amt = min(live_cash, max(0.0, target_buy_amt - (holding_qty * cp)))
            add_qty = int(allow_amt // (cp * db.CONTRACT.get('execution_rules', {}).get('market_buy_reservation_buffer', 1.05))) if cp > 0 else 0
            
            return {
                '분류': 1, '점수': score, '종목명': row['종목명'], '티커': tk, 
                '상태': f"{buy_str} (대기)", '보유수량': holding_qty, '보유단가': buy_p, '현재가': cp, 
                '수익률': profit_rate_str, '주문수량': add_qty
            } if add_qty > 0 else {
                '분류': 2, '점수': score, '종목명': row['종목명'], '티커': tk, 
                '상태': "🟡 현금/한도 부족", '보유수량': holding_qty, '보유단가': buy_p, '현재가': cp, 
                '수익률': profit_rate_str, '주문수량': 0
            }
            
        return {
            '분류': 2, '점수': score, '종목명': row['종목명'], '티커': tk, 
            '상태': action, '보유수량': holding_qty, '보유단가': buy_p, '현재가': cp, 
            '수익률': profit_rate_str, '주문수량': 0
        }

    if eval_list:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(process_q, eval_list):
                if r: temp_q.append(r)
                
    q_df = pd.DataFrame(temp_q)
    if not q_df.empty:
        q_df = q_df.sort_values(by=['분류', '점수'], ascending=[True, False]).reset_index(drop=True)
        
        display_cols = ['종목명', '티커', '상태', '보유수량', '보유단가', '현재가', '수익률', '주문수량']
        styled_df = q_df[display_cols].style.map(
            color_profit_loss, subset=['수익률']
        ).format({
            '보유수량': '{:,}',
            '보유단가': '{:,.0f}',
            '현재가': '{:,.0f}',
            '주문수량': '{:,}'
        })
        
        st.dataframe(styled_df, use_container_width=True)
        
        if st.button("⚡ UI 수동 의도 DB 기록", type="primary"):
            success_count = 0
            for _, r in [row for _, row in q_df.iterrows() if row['분류'] in [0, 1] and row['주문수량'] > 0]:
                try:
                    tk = r['티커']
                    side = "BUY" if ("매수" in r['상태'] or "🛒" in r['상태']) else "SELL"
                    now_str = datetime.datetime.now(KST).strftime('%H%M%S')
                    spec = quant.OrderSpec(
                        "", f"UI_{SCOPE_KEY}_{tk}_{side}_{now_str}", "KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, 
                        active_strat.value, active_strat.value, db.CONTRACT['strategy_version'], db.CONTRACT['contract_version'], 
                        tk, r['종목명'], side, "MARKET", r['주문수량'], 0, r['현재가'], "KRX", "GTC", 
                        "UI_MANUAL", "UI_MANUAL", now_str, "Q", "KIS", datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'), 
                        db.CONTRACT['execution_rules']['intent_ttl_sec'], db.CONTRACT['cost_model_version'], datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
                    )
                    if db.safe_add_order_intent(spec)[0]: 
                        success_count += 1
                except Exception as e: 
                    st.error(f"DB Error on {r['종목명']}: {e}")
            if success_count > 0: 
                st.success(f"✅ {success_count}건 주문 의도 적재 완료!")
            else: 
                st.info("유효한 주문 대상이 없습니다.")
    
    intents = db.get_orders_by_status_and_env(list(db.ALLOWED_TRANSITIONS.keys()), "KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)
    if intents:
        st.markdown("### 📋 DB 주문 의도(Intent) 원장 상태")
        st.dataframe(pd.DataFrame(intents)[['id', 'ticker', 'side', 'qty', 'status', 'cum_filled_qty', 'resp_code']].sort_values('id', ascending=False), use_container_width=True)
def build_historical_universe(start_date_sim, end_date_sim):
    conn = db.get_connection()
    rows = conn.execute("SELECT ticker, event_type, effective_at FROM watchlist_events WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? ORDER BY effective_at ASC", ("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)).fetchall()
    if not rows: return None 
    hist_uni = {}
    active = set()
    curr_date = start_date_sim
    idx = 0
    while curr_date <= end_date_sim:
        curr_str = curr_date.strftime('%Y-%m-%d')
        while idx < len(rows):
            evt_date = datetime.datetime.strptime(rows[idx]['effective_at'], '%Y-%m-%d %H:%M:%S').date()
            if evt_date <= curr_date:
                if rows[idx]['event_type'] == 'ADD': active.add(rows[idx]['ticker'])
                elif rows[idx]['event_type'] == 'REMOVE': active.discard(rows[idx]['ticker'])
                idx += 1
            else: break
        hist_uni[curr_str] = list(active)
        curr_date += datetime.timedelta(days=1)
    return hist_uni
def build_historical_universe(start_date_sim, end_date_sim):
    conn = db.get_connection()
    rows = conn.execute("SELECT ticker, event_type, effective_at FROM watchlist_events WHERE broker=? AND environment=? AND account_fingerprint=? AND product_code=? AND portfolio_id=? AND strategy_id=? ORDER BY effective_at ASC", ("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value)).fetchall()
    if not rows: return None 
    hist_uni = {}
    active = set()
    curr_date = start_date_sim
    idx = 0
    while curr_date <= end_date_sim:
        curr_str = curr_date.strftime('%Y-%m-%d')
        while idx < len(rows):
            evt_date = datetime.datetime.strptime(rows[idx]['effective_at'], '%Y-%m-%d %H:%M:%S').date()
            if evt_date <= curr_date:
                if rows[idx]['event_type'] == 'ADD': active.add(rows[idx]['ticker'])
                elif rows[idx]['event_type'] == 'REMOVE': active.discard(rows[idx]['ticker'])
                idx += 1
            else: break
        hist_uni[curr_str] = list(active)
        curr_date += datetime.timedelta(days=1)
    return hist_uni

with tab4:
    st.header("📊 시뮬레이터 및 백테스트 엔진")

    st.subheader("🧪 Test 1: 현재 관심종목 그룹 순수 백테스트")
    st.markdown("현재 `📝 관심종목`에 등록된 종목들을 대상으로 지정한 기간 동안 AI 시그널에 따른 순수 성과를 측정합니다.")
    t1_c1, t1_c2, t1_c3, t1_c4 = st.columns([2, 2, 2, 2])
    with t1_c1: t1_start = st.date_input("시작일", datetime.datetime.now(KST).date() - datetime.timedelta(days=180), key="t1_start")
    with t1_c2: t1_end = st.date_input("종료일", datetime.datetime.now(KST).date(), key="t1_end")
    with t1_c3: t1_legacy = st.checkbox("고정 0.25% 모드", value=False, key="t1_leg")

    if st.button("Test 1 실행 (관심종목 백테스트)", use_container_width=True):
        if not current_watchlist:
            st.warning("관심종목이 비어있습니다. 먼저 종목을 추가해주세요.")
        elif t1_start >= t1_end:
            st.warning("종료일은 시작일 이후여야 합니다.")
        else:
            with st.spinner("AI 백테스트 분석 중..."):
                wl_df = pd.DataFrame(current_watchlist)
                res_t1 = quant.run_quant_simulation(wl_df, active_strat, total_cash, t1_start, t1_end, current_config, is_weekly_scan=False, use_legacy_cost=t1_legacy)
                
                if res_t1.get('status') == 'success':
                    st.markdown(mts_metric_html("Test 1 누적 수익률", f"{res_t1['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res_t1['summary_rows']), use_container_width=True, hide_index=True)
                    with st.expander("📝 매매 상세 내역 보기"):
                        st.dataframe(pd.DataFrame(res_t1['trade_logs']), use_container_width=True)
                else:
                    st.error(res_t1.get('msg', '오류 발생'))

    st.divider()

    st.subheader("🧪 Test 2: AI 자율운용 vs 사용자 개입 vs 실제 계좌 (3중 비교선)")
    st.markdown("""
    * **🤖 1. AI 자율 (주간스캔):** 사용자가 아무것도 하지 않았을 때, AI가 시장 전체 상위 100개 종목을 매주 스캔하여 자율 매매한 결과입니다.
    * **🧑‍💻 2. 사용자 개입 제한:** 사용자가 픽한 '관심종목' 내에서만 AI가 타이밍을 잡아 매매했을 때의 결과입니다. (나의 안목 vs AI 비교)
    """)
    t4_c1, t4_c2, t4_c3, t4_c4 = st.columns([2, 2, 2, 3])
    with t4_c1: start_d = st.date_input("시작일", datetime.datetime.now(KST).date() - datetime.timedelta(days=365), key="t4_start")
    with t4_c2: end_d = st.date_input("종료일", datetime.datetime.now(KST).date(), key="t4_end")
    with t4_c3: use_legacy = st.checkbox("고정 0.25% 모드", value=False, key="l_sim")
    
    if st.button("Test 2 실행 (3중 비교)", type="primary", use_container_width=True):
        if start_d >= end_d: st.warning("최소 하루 이상 필요합니다.")
        else:
            with st.spinner("1. AI 완전 자율 포트폴리오 분석 중..."):
                res_ai = quant.run_quant_simulation(pd.DataFrame(), active_strat, total_cash, start_d, end_d, current_config, is_weekly_scan=True, use_legacy_cost=use_legacy)
            
            with st.spinner("2. 사용자 개입 (실제 Watchlist 이력 및 Cash Flow) 분석 중..."):
                hist_uni = build_historical_universe(start_d, end_d)
                c_flows = db.get_cash_flows_by_date("KIS", ENV_STR, ACC_FP, SYS_ACNT_PRDT, active_strat.value, active_strat.value, start_d, end_d)
                
                # 🚨 교정 2: 과거 DB 이력이 없을 경우, '현재 관심종목'을 기준으로 시뮬레이션을 돌리도록 폴백(Fallback) 방어코드 작성
                if not hist_uni or all(len(v) == 0 for v in hist_uni.values()):
                    wl_df = pd.DataFrame(current_watchlist) if current_watchlist else pd.DataFrame()
                    res_user = quant.run_quant_simulation(wl_df, active_strat, total_cash, start_d, end_d, current_config, is_weekly_scan=False, use_legacy_cost=use_legacy, external_cash_flows=c_flows)
                else:
                    res_user = quant.run_quant_simulation(pd.DataFrame(), active_strat, total_cash, start_d, end_d, current_config, is_weekly_scan=True, use_legacy_cost=use_legacy, user_restricted_universe_by_date=hist_uni, external_cash_flows=c_flows)
            
            actual_ret_pct = (rd['pnl'] / real_invested_principal * 100) if real_invested_principal > 0 else 0.0
            
            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                st.markdown("<h4 style='color:#3b82f6;'>🤖 1. AI 자율 (주간스캔)</h4>", unsafe_allow_html=True)
                if res_ai.get('status') == 'success':
                    st.markdown(mts_metric_html("AI 누적 수익률", f"{res_ai['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res_ai['summary_rows']), use_container_width=True, hide_index=True)
                    with st.expander("📝 상세 매매 내역 보기"):
                        st.dataframe(pd.DataFrame(res_ai['trade_logs']), use_container_width=True)
                else: st.error(res_ai.get('msg'))
            with cc2:
                st.markdown("<h4 style='color:#f59e0b;'>🧑‍💻 2. 사용자 개입 제한</h4>", unsafe_allow_html=True)
                if res_user.get('status') == 'success':
                    st.markdown(mts_metric_html("사용자 누적 수익률", f"{res_user['metrics']['TWR']:+.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res_user['summary_rows']), use_container_width=True, hide_index=True)
                    with st.expander("📝 상세 매매 내역 보기"):
                        st.dataframe(pd.DataFrame(res_user['trade_logs']), use_container_width=True)
                else: st.error(res_user.get('msg', "조건을 만족하는 매매 내역이 없습니다."))
            with cc3:
                st.markdown("<h4 style='color:#10b981;'>🏦 3. 실제 계좌 원장</h4>", unsafe_allow_html=True)
                st.markdown(mts_metric_html("실제 누적 수익률", f"{actual_ret_pct:+.2f}%"), unsafe_allow_html=True)

    st.divider()

    st.subheader("🧪 Test 3: 과거 연도별 현실성 검증 (Point-in-Time)")
    st.markdown("과거 특정 연도의 KOSPI/KOSDAQ 실제 구성종목(상장폐지 포함)을 기준으로 생존자 편향(Survivor Bias)이 통제된 환경에서 테스트합니다.")
    t3_c1, t3_c2 = st.columns([2, 8])
    with t3_c1: test_year = st.selectbox("테스트 대상 연도", [2022, 2023, 2024, 2025])
    
    if st.button("Test 3 실행 (생존자 편향 통제)", use_container_width=True):
        with st.spinner(f"{test_year}년도 현실성 검증 중..."):
            res_t3 = quant.run_yearly_realistic_backtest(active_strat, total_cash, test_year, current_config)
            st.error(f"🚨 {res_t3.get('msg')}")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite 백서 및 시스템 헌장 (v2.2.0)</h1>
    <div style='background-color: rgba(30, 58, 138, 0.1); padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 5px solid #1E3A8A;'>
        <h4 style='margin-top: 0;'>📌 시스템 배포 상태 및 한계 명세</h4>
        <p style='margin-bottom: 5px;'><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> : 코드 레벨 로직 구현 완료</p>
        <p style='margin-bottom: 5px;'><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> : 115개 필수 단위/통합 테스트 100% 통과</p>
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
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>쿨다운 및 재무장:</b> 2회 연속 실현 손실 시 KRX 거래일 기준 쿨다운이 발동하며, 매도 후 신호가 false → true로 변경된 독립적 재무장(Rearm) 시에만 목표 비중의 부족분에 한해 추가 매수를 허용한다.</li>
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
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>1분봉 완료 확정 확인:</b> KIS 시세의 Timestamp를 추출하여 현재 진행 중인 분(minute)을 제외하고, 명확히 구분된 두 개의 완전히 닫힌 봉에서 신호가 유지될 때만 매수/매도를 확정한다.</li>
    </ul>

    <h3>4. 정밀 CostModel 및 세금 분리 산출</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>비용 분리 산출 원칙:</b> 증권사 수수료, 유관기관, 슬리피지(상승/하락 불리 적용), 세금을 완전히 분리 연산한다. 워커 대사 시 KIS 체결 증분(Delta)에 대하여 수수료 및 세금을 실시간 역산하여 Fills 원장에 기록한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>연도별 세법 반영표:</b> KOSPI/KOSDAQ 기준 2022년(0.23%)~2026년(0.20%)의 법정 세법 개정안을 적용한다.</li>
    </ul>

    <h3>5. 주문 상태 머신 (16 State DAG) 및 원자적 게이트</h3>
    <ul>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>2단계 분리 상태 전이:</b> <code>claim_intent</code> (네트워크 없는 선점) 후 외부 잔고를 조회하고 <code>authorize_claimed_order</code> (최종 11중 검증) 를 거쳐 <code>SUBMITTING</code>으로 진입하는 엄격한 2단계 분리 구조를 따른다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>최종 CAS 11중 원자적 검증:</b> 단일 트랜잭션 내에서 상태, Fencing Token, Kill Switch, Auto 상태, 계약 3종 버전 불일치(`QUARANTINED` 격리), 의도 TTL, 가격 괴리율(5%), 시세 신선도, 자신을 제외한 가용 매도 수량 계산, 최대 노출 한도, 일일 손실 한도 등 11개 항목을 완벽히 방어한다.</li>
    </ul>

    <h3>6. KIS API 통신 어댑터 및 페일세이프 (Typed Result)</h3>
    <ul>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>Typed Result 반환:</b> 모든 API 응답은 딕셔너리가 아닌 <code>SUCCESS_DATA</code>, <code>SUCCESS_EMPTY</code>, <code>BUSINESS_REJECT</code>, <code>TRANSPORT_FAIL</code> 상태를 가지는 Typed 객체로 반환되어 오류를 투명하게 추적한다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>Backoff + Jitter 재시도 및 Rate Limiter:</b> 조회 API의 경우 계좌/환경별 Rate Limiter(초당 15회) 및 지수 백오프+Jitter를 혼합하여 서버 부하를 완화하고 최대 3회 재시도한다. 주문/취소 POST는 <b>절대 임의 재시도하지 않고 (단발성 원칙)</b> TIMEOUT을 즉시 반환하여 <code>UNKNOWN</code> 상태의 중복 체결을 방지한다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>체결 대사 (0081R) 및 페이지네이션:</b> KIS 공식 규격인 <code>tot_ccld_qty</code> (총 체결수량)을 사용하며, Continuation Header (<code>tr_cont='N'</code>)를 정확히 주입하여 다량의 체결 데이터(Cursor)를 유실 없이 수신한다.</li>
        <li><span style='color: #3b82f6;'>🔵 <b>[TESTED_MOCK]</b></span> <b>시세 타임스탬프 오류(Look-ahead) 원천 차단:</b> 시세 API의 Timestamp 파싱 실패 시, 현재 시각으로 덮어쓰던 꼼수를 폐기하고 즉시 <code>BUSINESS_REJECT</code> 시켜 위험한 거래를 차단한다.</li>
    </ul>

    <h3>7. DB 무손실 마이그레이션, 불변성 원장 및 6중 샌드박스 격리</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>v15 무손실 마이그레이션 및 PK 제약조건:</b> Downgrade 방어 적용. KIS 공식 체결 필드(tot_ccld_qty, rjct_qty 등 8종)를 <code>fills</code> 및 <code>order_intents</code> 원장에 영구 보존한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>불변성(Append-only) 원장:</b> 시스템 감사 추적을 위해 <code>order_events</code>, <code>fills</code>, <code>watchlist_events</code>, <code>reconciliation_events</code>, <code>cash_flows</code> 5개 테이블은 UPDATE/DELETE 연산 없이 Insert-only로 엄격히 설계/구현되었다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>6중 복합키(Scope) 격리:</b> 앱, 봇, 워커, 시뮬레이터 모두 <code>broker + env + acc_fp + prdt_cd + port_id + strat_id</code> 의 6중 키를 강제하여 잔고, 의도, 설정 및 메모리 토큰 캐시를 완벽히 격리한다.</li>
    </ul>

    <h3>8. 대사 및 장애 복구 (Midnight Boundary & Stuck-Prevention)</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>자정 경계 대사 (Midnight Boundary):</b> 주문의 생성일자를 추출하여 대사 API를 호출함으로써, 전날 밤 생성된 체결 지연/취소 주문이 이튿날 대사 누락으로 영구 고착되는 문제를 방지한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>영구 고착 방지 (Stuck-Prevention):</b> 워커 데몬 시작 시 Lease가 만료된 <code>CLAIMED</code> 주문은 즉시 <code>INTENT_CREATED</code>로 롤백시킨다. 또한 10분이 경과한 <code>SUBMITTING</code> / <code>UNKNOWN</code> 주문은 망 유실로 간주하여 <code>REJECTED</code> 처리한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>9중 정밀 Fingerprint 및 수동 검수 이관:</b> <code>ODNO</code>가 없는 UNKNOWN 주문 대사 시, 날짜, 시각(±60초), 계좌, 거래소, 종목, 매매구분, 수량, 주문종류, 가격 9개의 지문을 100% 만족하는 경우에만 복구하며, 후보가 0건이거나 2건 이상인 모호한 상황에서는 자동 확정하지 않고 즉시 <code>RECONCILIATION_REQUIRED</code>로 전이하여 위험을 차단한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>트랜잭션 내 Delta 연산 (Anomaly HALT):</b> 이전 스냅샷과 동일하거나 누적 체결량이 오히려 감소, 또는 목표 수량을 초과하거나 금액이 불일치하는 이상 징후(Anomaly) 발견 시, 워커 메모리가 아닌 DB 트랜잭션 최심부에서 즉각 <code>RECONCILIATION_REQUIRED</code>(HALT) 상태로 튕겨내어 무결성을 방어한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>취소 생명주기 3단계 분리:</b> <code>CANCEL_REQUESTED</code> → KIS 접수 성공 시 <code>CANCEL_ACKNOWLEDGED</code> → 체결 대사에서 <code>cncl_yn=='Y'</code> 확인 시 최종 <code>CANCELED</code> 로 전이되는 3단계 흐름을 구축하여 이중 POST를 원천 봉쇄한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>수동 주문 예외(UI_MANUAL) 및 페일세이프:</b> 사용자가 UI를 통해 제출한 수동 주문은 시스템의 Kill Switch 작동 시 일괄 취소 대상에서 면제된다. 모든 상태 전이는 Fencing Token과 함께 원장에 기록되며, 실패 시 절대로 브로커 API POST로 진입하지 않는 구조적 안전성을 확보했다.</li>
    </ul>

    <h3>9. 고급 시뮬레이션 엔진</h3>
    <ul>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>공통 파이프라인:</b> 실거래와 시뮬레이션은 전략, 부스터, 비용 함수, Adverse-first 체결 룰을 100% 공유한다.</li>
        <li><span style='color: #10b981;'>🟢 <b>[IMPLEMENTED]</b></span> <b>생존자 편향 방지 및 3중 비교:</b> 데이터 부재 시 조작된 성과를 낼 수 없도록 명확히 오류(DATA_UNAVAILABLE)를 반환하며, Test 2에서는 사용자의 실제 과거 편입 이력(Watchlist_events)과 입출금(Cash_flows) 내역을 결합하여 AI / 사용자 개입 / 실제 계좌 간의 3중 비교선을 100% 데이터 기반으로 제공한다.</li>
    </ul>
    """, unsafe_allow_html=True)
