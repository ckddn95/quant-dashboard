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

st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")
# [Rule C] KST 타임존 강제
KST = datetime.timezone(datetime.timedelta(hours=9))

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
    with st.form("login_form"):
        pwd_input = st.text_input("비밀번호를 입력하세요", type="password")
        submitted = st.form_submit_button("로그인")
        
    if submitted:
        try:
            if bcrypt.checkpw(pwd_input.encode('utf-8'), hashed_pw_env.encode('utf-8')):
                st.session_state["password_correct"] = True
                st.rerun()
            else:
                st.error("비밀번호가 일치하지 않습니다.")
        except ValueError:
            st.error("서버 설정 오류: 잘못된 형식의 해시값입니다.")
    return False

if not check_password():
    st.stop()

def mts_metric_html(label, value, delta=None):
    val_color = "white"
    val_str = str(value)
    if not delta: 
        if val_str.startswith('+'):
            val_color = "#FF5050"
        elif val_str.startswith('-') and val_str != '-':
            val_color = "#3b82f6"
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
        if val.startswith('+'):
            return 'color: #FF5050; font-weight: bold;'
        elif val.startswith('-'):
            return 'color: #3b82f6; font-weight: bold;'
    return ''

st.title("Core-Satellite Quant System (MSA)")
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
    SYS_APP_KEY = acc_config["app_key"]
    SYS_APP_SEC = acc_config["app_secret"]
    SYS_CANO = str(acc_config["cano"]).strip()
    
    is_mock_val = acc_config.get("is_mock", True)
    if isinstance(is_mock_val, str):
        SYS_IS_MOCK = is_mock_val.lower() == 'true'
    else:
        SYS_IS_MOCK = bool(is_mock_val)
        
    SYS_ACNT_PRDT = str(acc_config.get("acnt_prdt", "01")).strip()
except KeyError:
    SYS_APP_KEY = None
    SYS_APP_SEC = None
    SYS_CANO = "MOCK_ACCOUNT" 
    SYS_IS_MOCK = True
    SYS_ACNT_PRDT = "01"

ENV_STR = "MOCK" if SYS_IS_MOCK else "REAL"

with st.sidebar.expander("🔑 KIS 계좌 연동 상태", expanded=not bool(SYS_APP_KEY)):
    if SYS_APP_KEY and SYS_CANO != "MOCK_ACCOUNT":
        st.success(f"✅ {STRAT_DISPLAY_MAP[active_strat]} 계좌 연동 완료")
        st.caption(f"계좌번호: {SYS_CANO} ({'모의' if SYS_IS_MOCK else '실전'})")
    else:
        st.error("⚠️ 스트림릿 Secrets에서 계좌 정보를 찾을 수 없습니다. (모의 DB 모드 작동 중)")

st.sidebar.markdown("---")
st.sidebar.header("📱 봇 제어 (DB 연동)")
init_ks = bool(db.get_setting('kill_switch', False))
init_at = bool(db.get_setting('auto_trade_enabled', False))
init_ap = bool(db.get_setting('auto_pilot', False))
kill_switch = st.sidebar.toggle("🚨 긴급 정지 (KILL SWITCH)", value=init_ks)
auto_trade = st.sidebar.toggle("🚀 실전 자동주문 활성화", value=init_at)
auto_pilot = st.sidebar.toggle("🔄 오토파일럿 켜기", value=init_ap)

if kill_switch != init_ks:
    db.set_setting('kill_switch', kill_switch)
if auto_trade != init_at:
    db.set_setting('auto_trade_enabled', auto_trade)
if auto_pilot != init_ap:
    db.set_setting('auto_pilot', auto_pilot)

st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터 (Baseline)")
current_config = quant.get_default_config(active_strat)

with st.sidebar.expander("📊 현재 적용된 계약 파라미터 보기", expanded=False):
    st.info("💡 시스템 헌장(YAML)에 의해 임의 변경이 차단된 읽기 전용 상태입니다. (OOS 검증 원칙 준수)")
    st.markdown(f"- **200일 추세선 방어:** {'✅ 활성' if current_config.ma200 else '❌ 비활성'}")
    st.markdown(f"- **골든크로스/눌림목 버퍼:** `{current_config.buf * 100:.1f}%`")
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

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유니버스", "🔌 실전 계좌", "🤖 자동매매 대기열", "📊 시뮬레이션", "📄 알고리즘 백서"])

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

    current_watchlist = db.get_watchlist("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value)
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
                    db.clear_and_update_watchlist("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value, current_watchlist + [{'티커': m_code, '종목명': m_name}])
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
                        db.clear_and_update_watchlist("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value, current_watchlist + [{'티커': row['티커'], '종목명': row['종목명']}])
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
        db_positions = {p['ticker']: p for p in db.get_positions("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value)}
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
                db.clear_and_update_watchlist("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value, remaining_items)
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
                        db.sync_positions_from_broker("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value, kis_stocks)
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
    c1, c2, c3 = st.columns(3)
    c1.metric("🚨 킬 스위치", "차단됨" if kill_switch else "정상")
    c2.metric("🚀 자동주문", "활성화" if auto_trade else "비활성화")
    c3.metric("💵 가용 현금", f"{rd['cash']:,.0f} 원")
    st.markdown("---")
    
    base_eval = rd['eval'] if rd['eval'] > 0 else float(total_cash)
    target_buy_amt = base_eval * current_config.alloc
    locked_cash, _ = db.get_locked_cash_and_qty("KIS", ENV_STR, SYS_CANO, active_strat.value)
    net_usable_cash = max(0.0, rd['cash'] - locked_cash)
    
    temp_q = []
    eval_list = []
    eval_tickers = set()
    for w in db.get_watchlist("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value):
        tk = str(w['티커']).zfill(6)
        eval_tickers.add(tk)
        eval_list.append({'티커': tk, '종목명': w['종목명']})
        
    for p in db.get_positions("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value):
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
        db_positions = {p['ticker']: p for p in db.get_positions("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value)}
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
        
        if st.button("⚡ 대기열 일괄 주문 DB 기록", type="primary"):
            success_count = 0
            valid_orders = [r for _, r in q_df.iterrows() if r['분류'] in [0, 1] and r['주문수량'] > 0 and "🟡" not in r['상태'] and "👁️" not in r['상태']]
            for r in valid_orders:
                tk = r['티커']
                side = "BUY" if "매수" in r['상태'] else "SELL"
                now_str = datetime.datetime.now(KST).strftime('%Y%m%d_%H%M')
                spec = quant.OrderSpec(correlation_id="", idempotency_key=f"UI_{tk}_{now_str}", broker="KIS", environment=ENV_STR, account_id=SYS_CANO, account_product_code=SYS_ACNT_PRDT, portfolio_id=active_strat.value, strategy_id=active_strat.value, strategy_version="1.0", ticker=tk, stock_name=r['종목명'], side=side, order_kind="MARKET", quantity=r['주문수량'], limit_price=0, intent_created_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                ok, msg = db.safe_add_order_intent(spec)
                if ok:
                    success_count += 1
                else:
                    st.warning(f"⚠️ {r['종목명']} 거절됨: {msg}")
            if success_count > 0:
                st.success(f"✅ {success_count}건 주문 생성 완료!")
            else:
                st.info("실행할 수 있는 유효한 주문 시그널이 없습니다.")
    else:
        st.info("대기 중인 종목이 없습니다.")
    
    st.markdown("### 📊 실시간 체결 대사 현황")
    intents = db.get_orders_by_status_and_env(['INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'REJECTED', 'QUARANTINED'], "KIS", ENV_STR, SYS_CANO, active_strat.value)
    if intents:
        st.dataframe(pd.DataFrame(intents)[['ticker', 'order_type', 'qty', 'status', 'cum_filled_qty', 'resp_code']], use_container_width=True)

with tab4:
    st.header("🧪 고급 백테스트 엔진")
    st.warning("⚠️ [DATA_LIMITED] 현재 시스템은 과거 시가총액/상장폐지 등 Point-in-time 데이터를 제공하지 않아, 생존자 편향(Survivor Bias)이 포함된 근사 시뮬레이션만을 수행합니다. 미래 수익 예측이나 LIVE 활성화의 절대적 기준으로 사용할 수 없습니다.")
    
    today_date = datetime.datetime.now(KST).date()
    stocks_df = pd.DataFrame(db.get_watchlist("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value))
    
    st.subheader("🎯 테스트 1. 관심·보유종목 전략 매매 시뮬레이션")
    st.info("현재 관심종목 및 보유종목 전체를 과거 기간에 소급 적용하여 매매 결과를 회고하는 시나리오입니다.")
    
    combined_tickers = set()
    combined_data = []
    
    for w in db.get_watchlist("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value):
        tk = str(w['티커']).zfill(6)
        if tk not in combined_tickers:
            combined_tickers.add(tk)
            combined_data.append({'티커': tk, '종목명': w['종목명']})
            
    for p in db.get_positions("KIS", ENV_STR, SYS_CANO, active_strat.value, active_strat.value):
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

    t1_c1, t1_c2, t1_c3 = st.columns([4, 3, 3])
    with t1_c1: 
        st.markdown(f"**분석 대상:** 관심 및 보유종목 총 **{len(combined_data)}**개")
        if not target_df.empty:
            st.caption(", ".join([d['종목명'] for d in combined_data][:5]) + ("..." if len(combined_data)>5 else ""))
    with t1_c2: start_d1 = st.date_input("시작일", datetime.date(2023, 1, 1), key="t1_start")
    with t1_c3: end_d1 = st.date_input("종료일", today_date, key="t1_end")
    
    st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
    run_t1 = st.button("테스트 1 실행 (통합 포트폴리오)", type="primary", use_container_width=True)
    
    if run_t1:
        if target_df.empty:
            st.warning("분석할 관심종목이나 보유종목이 없습니다.")
        elif start_d1 >= end_d1:
            st.warning("⚠️ 시뮬레이션 지표를 계산하려면 최소 하루 이상의 기간이 필요합니다. 시작일이 종료일보다 과거여야 합니다.")
        else:
            with st.spinner(f"총 {len(combined_data)}개 종목 대상 시뮬레이션 구동 중... (T+1 체결, 0.25% 비용 가정)"):
                res1 = quant.run_quant_simulation(target_df, active_strat, total_cash, start_d1, end_d1, current_config, is_weekly_scan=False)
                if res1.get('status') == 'success':
                    st.success("테스트 1 시뮬레이션 완료!")
                    r1, r2, r3, r4 = st.columns(4)
                    r1.markdown(mts_metric_html("기말 자산", f"{res1['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                    r2.markdown(mts_metric_html("누적 수익률", f"{res1['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                    r3.markdown(mts_metric_html("연평균 수익률(CAGR)", f"{res1['metrics']['CAGR']*100:+.2f}%"), unsafe_allow_html=True)
                    r4.markdown(mts_metric_html("최대 낙폭(MDD)", f"{res1['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
                    st.dataframe(pd.DataFrame(res1['summary_rows']), use_container_width=True)
                    
                    with st.expander("📝 상세 거래 내역 보기 (클릭하여 펼치기)"):
                        if res1.get('trade_logs'):
                            tl_df = pd.DataFrame(res1['trade_logs'])
                            st.dataframe(tl_df.style.map(color_profit_loss, subset=['수익률']).format({
                                '진입단가': '{:,.0f}', '청산단가': '{:,.0f}', '수량': '{:,}', '손익금': '{:,.0f}'
                            }), use_container_width=True)
                        else:
                            st.info("해당 기간 동안 발생한 거래 내역이 없습니다.")
                else:
                    st.error(f"실행 불가: {res1['msg']}")
    st.markdown("---")

    st.subheader("🎯 테스트 2. AI 가상운용 vs 실제계좌 성과 비교")
    
    t2_end_default = today_date
    one_year_ago = t2_end_default - datetime.timedelta(days=365)
    
    creation_date = db.get_portfolio_creation_date("KIS", ENV_STR, SYS_CANO, active_strat.value)
    
    if creation_date and creation_date > one_year_ago:
        t2_start_default = creation_date
        st.info(f"💡 포트폴리오 개설일({creation_date})이 1년 미만이므로, 과거 1년 전체가 아닌 '개설일부터 오늘까지'의 성과를 1:1로 비교합니다.")
    else:
        t2_start_default = one_year_ago
        st.info("💡 매일(Daily) 종가 기준으로 관심종목을 다시 스캔하여 운용했을 때의 성과와, 현재 사용자의 실제 계좌 성과를 나란히 비교합니다.")
    
    t2_c1, t2_c2, t2_c3 = st.columns([3, 3, 4])
    with t2_c1: start_d2 = st.date_input("시작일", t2_start_default, key="t2_start")
    with t2_c2: end_d2 = st.date_input("종료일", t2_end_default, key="t2_end")
    with t2_c3:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        run_t2 = st.button("테스트 2 실행 (일간 스캔)", type="primary", use_container_width=True)
        
    if run_t2:
        if stocks_df.empty:
            st.error("관심종목 리스트에 종목이 없습니다. 탭 1에서 관심종목을 추가해주세요.")
        elif start_d2 >= end_d2:
            st.warning("⚠️ 시뮬레이션(수익률 계산 등)을 수행하려면 최소 하루 이상의 기간이 필요합니다. (포트폴리오를 오늘 개설하셨다면 내일부터 조회가 가능합니다.)")
        else:
            with st.spinner("일간 유니버스 갱신 및 AI 가상운용 시뮬레이션 구동 중..."):
                res2 = quant.run_quant_simulation(stocks_df, active_strat, real_invested_principal, start_d2, end_d2, current_config, is_weekly_scan=False)
                if res2.get('status') == 'success':
                    st.success("테스트 2 시뮬레이션 완료!")
                    st.markdown("### 🏆 성과 비교: AI 가상운용 vs 실제 계좌 (현재)")
                    actual_ret_pct = (rd['pnl'] / real_invested_principal * 100) if real_invested_principal > 0 else 0.0
                    comp_col1, comp_col2 = st.columns(2)
                    
                    with comp_col1:
                        st.markdown("<h4 style='text-align:center; color:#3b82f6;'>🤖 AI 가상운용</h4>", unsafe_allow_html=True)
                        st.markdown(mts_metric_html("AI 가상 기말 자산", f"{res2['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                        st.markdown(mts_metric_html("AI 가상 누적 수익률", f"{res2['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        st.dataframe(pd.DataFrame(res2['summary_rows']), use_container_width=True)
                        
                        with st.expander("📝 AI 가상운용 상세 거래 내역 보기"):
                            if res2.get('trade_logs'):
                                tl_df = pd.DataFrame(res2['trade_logs'])
                                st.dataframe(tl_df.style.map(color_profit_loss, subset=['수익률']).format({
                                    '진입단가': '{:,.0f}', '청산단가': '{:,.0f}', '수량': '{:,}', '손익금': '{:,.0f}'
                                }), use_container_width=True)
                            else:
                                st.info("해당 기간 동안 발생한 거래 내역이 없습니다.")
                        
                    with comp_col2:
                        st.markdown("<h4 style='text-align:center; color:#10b981;'>🧑‍💻 실제 계좌 (현재 잔고)</h4>", unsafe_allow_html=True)
                        st.markdown(mts_metric_html("실제 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
                        st.markdown(mts_metric_html("실제 누적 수익률", f"{actual_ret_pct:+.2f}%"), unsafe_allow_html=True)
                        st.info("※ 실제 계좌의 성과는 사용자의 외부 입출금 및 수동 거래 내역이 모두 포함된 단순 합산 결과이므로, 전액 현금으로 시작한 AI 가상운용과 100% 동일 선상의 비교는 아님을 유의하시기 바랍니다.")
                else:
                    st.error(f"실행 불가: {res2['msg']}")
    st.markdown("---")

    st.subheader("🎯 테스트 3. 과거연도 자동매매 재현 시뮬레이션")
    st.info("선택한 특정 연도의 전체 기간 동안 AI가 매일(Daily) 단위로 운용했을 때의 결과입니다.")
    t3_c1, t3_c2 = st.columns([3, 7])
    with t3_c1: test_year = st.selectbox("검증 연도 선택", [2022, 2023, 2024, 2025, 2026], index=4)
    with t3_c2:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        run_t3 = st.button(f"테스트 3 실행 ({test_year}년)", type="primary", use_container_width=True)
        
    if run_t3:
        with st.spinner(f"{test_year}년도 시뮬레이션 구동 중..."):
            res3 = quant.run_yearly_realistic_backtest(active_strat, total_cash, test_year, current_config)
            if res3.get('status') == 'success':
                st.success(f"{test_year}년 검증 완료!")
                r1, r2, r3, r4 = st.columns(4)
                r1.markdown(mts_metric_html("기말 자산", f"{res3['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                r2.markdown(mts_metric_html("누적 수익률", f"{res3['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                r3.markdown(mts_metric_html("연평균 수익률(CAGR)", f"{res3['metrics']['CAGR']*100:+.2f}%"), unsafe_allow_html=True)
                r4.markdown(mts_metric_html("최대 낙폭(MDD)", f"{res3['metrics']['MDD']*100:.2f}%"), unsafe_allow_html=True)
                
                with st.expander("📝 상세 거래 내역 보기 (클릭하여 펼치기)"):
                    if res3.get('trade_logs'):
                        tl_df = pd.DataFrame(res3['trade_logs'])
                        st.dataframe(tl_df.style.map(color_profit_loss, subset=['수익률']).format({
                            '진입단가': '{:,.0f}', '청산단가': '{:,.0f}', '수량': '{:,}', '손익금': '{:,.0f}'
                        }), use_container_width=True)
                    else:
                        st.info("해당 기간 동안 발생한 거래 내역이 없습니다.")
            else:
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

    <h3>⚙️ 3. 전략별 기본 파라미터 및 레지스트리 (Parameters & Registry)</h3>
    <ul>
        <li><b>[시스템 규칙] 단일 진실 공급원(SSOT):</b> 시스템의 모든 파라미터(Core/Satellite)와 비용률, 시뮬레이션 주기 등은 오직 <code>system_contract.yaml</code> 파일에서만 관리(하드코딩 배제)되며, UI는 이를 읽기 전용(Read-only)으로 표출만 한다. 변경 시 반드시 버전을 동시 상향해야 한다.</li>
        <li><b>Core:</b> 버퍼 1.5%, 손절 -15%, 투입 한도 35%, 익절목표 30%, 하락허용 -10%, 쿨다운 60 거래일, 최소보유 5 거래일.</li>
        <li><b>Satellite:</b> 버퍼 1.0%, 손절 -12%, 투입 한도 20%, 익절목표 20%, 하락허용 -7%, 쿨다운 30 거래일, 최소보유 3 거래일.</li>
    </ul>

    <h3>🛡️ 4. 3대 고급 안전장치 및 장중 손절/트레일링 규칙</h3>
    <ul>
        <li><b>장중 보수적 청산:</b> 종가(Close)를 기다리지 않고 장중 저가(Low)가 손절선 또는 트레일링 컷에 터치하면 즉각 <b>가장 보수적인 가격(Adverse-first)</b>으로 청산 시그널을 발생시킨다.</li>
        <li><b>원자적 제출 게이트(Atomic Gate):</b> 봇(Worker)은 KIS 실주문 POST를 쏘기 직전, 동일 트랜잭션 내에서 현금 한도, 킬 스위치, 리스 토큰 만료, 가격 괴리, 일일 손실 -5% 초과 여부를 다시 한 번 완벽히 교차 검증하여, 하나라도 실패하면 즉시 CANCELED/RISK_REJECTED 처리한다.</li>
    </ul>

    <h3>🔄 5. API 호출 규칙 및 주문 상태 머신 (API & State Machine)</h3>
    <ul>
        <li><b>[시스템 규칙] 16단계 상태 단방향 전이:</b> 주문은 <code>INTENT_CREATED</code>부터 <code>ACKNOWLEDGED</code>, <code>FILLED</code>, <code>CANCELED</code> 등 계약에 명시된 16개 상태와 엄격한 단방향 전이 룰에 의해서만 움직인다. KIS 접수 ACK를 체결(FILLED)로 오인하지 않으며, UNKNOWN 주문을 맹목적으로 자동 재전송하지 않는다.</li>
        <li><b>[시스템 규칙] 부분 체결 대사:</b> 체결 수량과 손익은 로컬 원장과 KIS 브로커 간의 누적 체결 델타(Delta)만을 산출하여 정확히 1번만 계산(Reconciliation)되며 매매 중지 상태에서도 대사는 지속된다.</li>
        <li><b>[시스템 규칙] API 초당 호출 제한(Rate Limit) 방어 쓰레딩:</b> 증권사 Open API의 초당 호출 제한(Rate Limit)을 준수하고 시스템 밴(Ban)을 막기 위해, 시세 조회 등 대량의 API 호출이 수반되는 로직은 반드시 워커 풀의 동시성 크기(<code>max_workers</code>)를 안전한 수치(예: 10개)로 제한하여 실행한다.</li>
    </ul>

    <h3>⏱️ 6. 시뮬레이션 및 백테스트 실행 규칙 (Simulation Rules)</h3>
    <ul>
        <li><b>[시스템 규칙] 시뮬레이션 정밀도 라벨링:</b> 과거 1분봉 데이터 획득이 불가능한 현 패키지 환경에서는 시뮬레이션 시 종가 데이터를 근사하여 사용하는 <code>DAILY_APPROX</code> 모드가 강제 적용됨을 명시하며, 미래 예측의 절대적 기준이 아님(생존자 편향 내포)을 경고한다.</li>
        <li><b>[시스템 규칙] T+1 체결 및 비용:</b> 모든 신호는 다음 유효 영업일(T+1) 시가(Open)로 체결되며, 왕복/편도 혼선을 막기 위해 매수 시 <code>+0.25%</code>, 매도 시 <code>-0.25%</code>의 명확한 편도 비용 산식을 적용한다.</li>
        <li><b>[시스템 규칙] 일간 스캔 및 미청산 MTM:</b> Test 2/3의 스캔 빈도를 매일(Daily)로 설정하여 1~2일짜리 짧은 계좌도 완벽한 1:1 비교를 수행하며, 시뮬레이션 종료 시점에 팔지 않고 보유 중인 주식(Open Position)도 라운드트립 장부(Ledger)에 현재가 기준으로 명확히 산출하여 누락을 막는다. 최소 2영업일 미만의 불가능한 시뮬레이션 요청은 엔진 레벨에서 즉각 안전하게 거부(Fail-safe)한다.</li>
        <li><b>[시스템 규칙] 잔여 현금 영혼 보내기(Partial Allocation) 원칙:</b> 매수 시그널 발생 시 가용 현금이 목표 투입 금액보다 적더라도, 최소 1주 이상 살 수 있는 현금이 남아있다면 가용 현금을 100% 소진하여 부분 매수(Partial Allocation)를 집행한다.</li>
        <li><b>[시스템 규칙] 호가 단위(Tick Size) 보정 및 1주 미만 절사 원칙:</b> 시뮬레이션 매수 수량 산출 시, 수학적 연산 결과가 소수점으로 나오더라도 반드시 <code>int()</code> 처리하여 절사(내림)하며, 평가 금액 및 체결 가격은 KIS 호가 단위에 부합하는 정수형으로 보정 및 기록한다.</li>
    </ul>

    <h3>🖥️ 7. UI 레이아웃 및 장애 복구 (UI & Recovery)</h3>
    <ul>
        <li><b>[시스템 규칙] KST(한국표준시) 타임존 절대 강제:</b> 클라우드 환경 배포 시 시스템 시간이 UTC로 잡혀 발생할 수 있는 시차 오작동을 막기 위해 모든 시간 연산과 DB 레코딩에는 <code>KST(UTC+9)</code> 타임존을 강제 적용한다.</li>
        <li><b>[시스템 규칙] 스캐너 세션 캐싱 및 상태 보존:</b> AI 타점 스캐너 실행 결과 및 검색 데이터는 Streamlit의 <code>st.session_state</code>에 안전하게 캐싱되어, 화면 새로고침 시 검색 결과 목록이 증발하지 않도록 방어한다.</li>
        <li><b>[시스템 규칙] 다중 연산 스피너(Spinner) 및 중복 클릭 방지:</b> 시뮬레이션이나 API 호출 연산 시 사용자 중복 클릭으로 인한 메모리 충돌을 막기 위해 반드시 UI 스피너를 강제한다.</li>
        <li><b>[시스템 규칙] 세션 만료 및 폼 기반 로그인 (Session Volatility):</b> 로그인 시 '엔터(Enter)' 키 입력을 지원하는 폼 구조를 사용하되, 새로고침(F5) 시 브라우저 세션이 초기화되는 것은 의도된 보안 정책(Secure by default)으로 채택하여 잔고 노출을 방어한다.</li>
        <li><b>[시스템 규칙] Core/Satellite 워커 환경 분리:</b> 두 워커는 완전히 독립된 계좌/포트폴리오 바인딩으로 동작하며, 한 워커의 에러가 다른 계좌의 원장을 오염시키지 않는 구조적 격리(Fencing)를 보장한다.</li>
        <li><b>[시스템 규칙] 도메인 URL 위생화(Sanitization):</b> KIS 통신 도메인은 복사 시 섞여 들어올 수 있는 보이지 않는 제로스페이스 및 특수 문자를 차단하기 위해 철저한 ASCII 클렌징 과정을 거친 후 호출한다.</li>
        <li><b>[시스템 규칙] 무손실 DB 마이그레이션:</b> DB 스키마(v3) 업데이트 시, 기존 데이터를 임시 테이블에 백업 후 <code>INSERT INTO SELECT</code> 방식으로 원자적으로 마이그레이션하여 과거 원장(Ledger)을 영구 보존한다.</li>
    </ul>

    <hr>
    <h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 및 자동 문서화 (절대 원칙)</h3>
    <ul style='color: #DC2626;'>
        <li>이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 <b>Part 1~7 전체 내용(투자 대원칙, 전략 공식, UI 규칙, 방어 로직 등)을 단 하나라도 임의로 축약, 훼손, 삭제해서는 안 됩니다.</b></li>
        <li><b>[자동 문서화 헌장]:</b> 향후 시스템의 로직, 파라미터, UI/UX 규칙을 새롭게 추가하거나 변경할 경우(단순 오타 수정 제외), <b>사용자가 따로 요청하지 않더라도 AI는 반드시 그 변경 사항과 사유를 이 백서(해당하는 Part)에 자동으로 업데이트하여 기록해야 합니다.</b> 이 백서는 시스템의 단일 진실 공급원(Single Source of Truth)으로 취급되어야 합니다.</li>
    </ul>
    """, unsafe_allow_html=True)
