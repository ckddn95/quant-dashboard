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
    # 🛑 [UX 패치] st.form 구조를 도입하여 '엔터(Enter) 키' 로그인 지원
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
st.sidebar.header("⚙️ 전략 파라미터")
current_config = quant.get_default_config(active_strat)

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
                                '체결단가': '{:,.0f}', '수량': '{:,}', '실현손익': '{:,.0f}'
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
                                st.dataframe(pd.DataFrame(res2['trade_logs']).style.map(color_profit_loss, subset=['수익률']).format({
                                    '체결단가': '{:,.0f}', '수량': '{:,}', '실현손익': '{:,.0f}'
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
                            '체결단가': '{:,.0f}', '수량': '{:,}', '실현손익': '{:,.0f}'
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
        <li><b>Core:</b> 버퍼 1.5%, 손절 -15%, 종목당 투입 한도 35%, 익절목표 30%, 하락허용 -10%, 쿨다운 60일, 최소보유 5일.</li>
        <li><b>Satellite:</b> 버퍼 1.0%, 손절 -12%, 종목당 투입 한도 20%, 익절목표 20%, 하락허용 -7%, 쿨다운 30일, 최소보유 3일.</li>
        <li><b>[시스템 규칙] 파라미터 무결성 경계값 보장:</b> <code>NaN</code>, <code>Inf</code>는 시스템 폭주를 유발하므로 엔진단에서 즉시 차단(에러)한다. 손절컷(<code>sl</code>)과 트레일링 하락허용(<code>ts_drp</code>)은 시스템상 <b>반드시 음수(-)</b>로 설정되어야 하며, 투입 한도(<code>alloc</code>)는 0 초과 1.0 이하의 비율, 쿨다운과 최소보유일은 0 이상의 정수만 허용하여 파라미터 조작으로 인한 오작동을 원천 봉쇄한다.</li>
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
        <li><b>멱등성 (Idempotency):</b> UUID, 시간, 계좌, 방향, 티커가 조합된 Idempotency Key를 통해 다중 브라우저 또는 다중 워커에 의한 중복 제출(Double POST)을 원천 차단한다. UI에서 '일괄 주문' 클릭 시에도 관망 상태인 종목은 DB에 삽입되지 않도록 사전 필터링된다.</li>
        <li><b>API Token Caching:</b> 초당 API 폭격 차단을 위해 발급된 Access Token은 메모리에 캐싱되며, 만료 5분 전에만 단일 비행(Single-flight)으로 갱신된다.</li>
    </ul>

    <h3>⏱️ 6. 시뮬레이션 및 백테스트 실행 규칙 (Simulation Execution Rules)</h3>
    <ul>
        <li><b>[시스템 규칙] 데이터 편향 한계 (Data Limitation):</b> 현재 시스템은 과거 시가총액이나 상장폐지 기록 등 Point-in-time 데이터를 제공하지 못하므로, 분석 결과에 필연적으로 '생존자 편향(Survivor Bias)'이 개입된다. 따라서 <b>이 시뮬레이션 결과는 절대적인 미래 예측이나 LIVE 활성화 기준으로 단독 사용될 수 없다.</b></li>
        <li><b>[시스템 규칙] 단일 진실 공급원 (Single Source of Truth):</b> 실시간 UI 스캐너, 실전 자동매매 봇, 백테스트 시뮬레이션 엔진은 모두 완벽히 동일한 '순수 전략 공통 함수(`calc_buy_signal`, `calc_sell_signal`)'를 공유하여, 결과의 정합성을 100% 보장한다.</li>
        <li><b>[시스템 규칙] 무한 출혈(Whipsaw) 방지 로직:</b> 시뮬레이션 및 실거래에서 잦은 휩소로 인한 계좌 녹음을 막기 위해 <b>'연속 손실 쿨다운(Cooldown)'</b> 기능이 필수적으로 적용된다. 한 번 손절된 종목은 설정된 일수만큼 매수 후보에서 배제된다.</li>
        <li><b>[시스템 규칙] 강세장 비중 증액 (Bull-market Boost):</b> 사용자가 '강세장 부스터' 옵션을 켰을 경우, 시뮬레이션 및 실거래 엔진은 코스피 지수가 200일선 위에 위치할 때 매수 비중을 최대 1.5배 증액하여 수익률을 극대화한다.</li>
        <li><b>[시스템 규칙] 평가 단가와 체결 단가의 엄격한 분리 (NAV 보정):</b> 특정 종목이 거래 정지되거나 개별 종목의 휴장으로 일봉 데이터가 누락된 날에도 포트폴리오 자산(NAV)이 0원으로 증발하지 않도록, 가치 평가는 '가장 최근의 유효 종가'를 활용한다. 단, 실제 매매 체결은 데이터가 존재하는 유효 거래일에만 엄격하게 집행하여 가짜 체결을 방지한다.</li>
        <li><b>[시스템 규칙] 시뮬레이션 T+1 체결 반영 완료:</b> 신호 발생 당일(T일)의 종가로 체결되는 룩어헤드 편향(Look-ahead Bias)을 제거한다. 모든 신호는 다음 유효 영업일(T+1)의 시가(Open)로 정확하게 체결된다.</li>
        <li><b>[시스템 규칙] 회계 이중 출금 방어 (No Double-Spend):</b> 매수 시그널 당일(T일)에는 가상 현금(available_cash)만을 차감하여 한도를 체크하고, 실제 계좌 잔고(cash)는 반드시 체결 당일(T+1일)에 단 한 번만 차감되도록 회계 무결성을 유지한다.</li>
        <li><b>[시스템 규칙] 비용 가정 (Cost Assumption):</b> 실제 KIS 브로커 체결 데이터가 없는 시뮬레이션 단계에서는, 매수와 매도 각각 수수료/세금/시장충격을 모두 포함하여 보수적인 <b>All-in 0.25% (왕복 0.50%)</b>의 비용률을 강제 적용한다.</li>
        <li><b>[시스템 규칙] 장중 보수적 손절/익절 (Adverse-first):</b> 과거 일봉 데이터만으로 장중 High/Low 순서를 알 수 없는 경우, 가장 불리한 방향인 <b>손절컷(SL)을 우선 타격</b>한 것으로 가정하여 생존 편향을 억제한다.</li>
        <li><b>[시스템 규칙] 일간 스캔 (Daily Scan) 전면 적용:</b> 짧은 운용 기간(예: 며칠~수 주)을 검증할 때 발생하는 '데이터 부족 및 체결 기회 박탈' 문제를 해결하기 위해, 모든 시뮬레이션(테스트 1, 2, 3)의 신규 진입 종목 스캔 주기를 기존 주 1회에서 <b>매일(Daily) 종가 기준</b>으로 전면 개편하였다. 이를 통해 단기 운용 계좌에서도 AI가 매일의 시장 변화에 즉각 대응하며 정밀한 1:1 성과 비교가 가능하도록 시스템 헌장을 수정한다.</li>
        <li><b>[시스템 규칙] 상세 거래 장부(Ledger) 보존 및 공개:</b> 시뮬레이션의 신뢰성 및 투명성 확보를 위해, 퀀트 엔진은 매수/매도 시그널에 따른 모든 개별 종목의 체결일, 단가, 수량, 실현손익, 매매 사유(손절컷, 익절, 추세이탈 등)를 상세 회계 장부(Trade Logs)로 기록하며, 이를 UI 화면에서 엑셀 표와 같은 형태로 100% 공개해야 한다.</li>
    </ul>

    <h3>🖥️ 7. UI 레이아웃 및 관측 가능성 (UI Observability & Fail-closed)</h3>
    <ul>
        <li><b>[시스템 규칙] 스캐너 세션 캐싱 및 상태 보존:</b> AI 타점 스캐너 실행 결과 및 검색 데이터는 Streamlit의 <code>st.session_state</code>에 안전하게 캐싱되어, 종목 추가(담기) 등의 UI 리렌더링 이벤트 발생 시에도 검색 결과 목록이 증발하지 않고 그대로 유지된다.</li>
        <li><b>[시스템 규칙] 엄격한 불리언(Boolean) 설정 파싱:</b> KIS 계좌 설정값(예: <code>is_mock</code>)은 스트림릿 시크릿에서 문자열(예: <code>"false"</code>)로 잘못 입력되더라도 파이썬에서 참으로 오인하지 않도록 대소문자를 무시한 명시적 형변환(Boolean Parsing)을 거친다.</li>
        <li><b>[시스템 규칙] 도메인 URL 위생화(Sanitization):</b> KIS Open API 등의 통신 도메인은 복사/붙여넣기 시 유입될 수 있는 보이지 않는 제로스페이스 및 비-ASCII 특수 문자를 원천 차단하기 위해 철저한 ASCII 클렌징 및 공백 제거 과정을 거친 후 호출한다.</li>
        <li><b>[시스템 규칙] 테스트 1 포트폴리오 분석:</b> 테스트 1은 기존의 단일 종목 입력 방식을 전면 폐기하고, 시스템 내에 등록된 <b>'현재 관심종목'과 '실제 보유종목' 전체를 하나의 포트폴리오로 취합</b>하여 과거 기간의 성과를 회고하는 UI로 개편되었다. 분석 결과는 화면 전체(Full-width)를 활용하여 사용자 가독성을 극대화한다.</li>
        <li><b>[시스템 규칙] 테스트 2 완벽한 1:1 비교 강제:</b> AI 가상 운용 시뮬레이션(테스트 2) 수행 시, 임의의 가상 원금이 아닌 <b>실제 계좌의 현재 투자 원금(Eval - PnL)</b>을 시뮬레이션의 초기 자금(Init Cash)으로 강제 동기화하여 진정한 의미의 성과 비교(Apples-to-apples)를 제공한다.</li>
        <li><b>[시스템 규칙] 포트폴리오 개설일 강제 동기화 (Test 2):</b> 테스트 2 수행 시, 포트폴리오 개설일(DB상 최초 종목 등록일 또는 주문 발생일)을 동적으로 추적하여 개설된 지 1년 미만인 경우 무의미한 과거 1년 전체를 시뮬레이션하지 않고 '실제 개설일'부터 오늘까지로 시작일을 자동 보정하여 완벽한 비교 신뢰성을 부여한다.</li>
        <li><b>[시스템 규칙] 시뮬레이션 버튼 및 결과 뷰 100% 확장:</b> 모든 테스트(1, 2, 3)의 실행 버튼과 분석 결과는 화면 전체(Full-width) 너비를 100% 활용하여 사용자 가독성과 클릭 편의성을 극대화하며, UI의 통일성을 유지한다.</li>
        <li>관심종목 탭, 실전 계좌 모니터링, 자동매매 대기열, 백테스트 엔진 등 명확한 MSA 관점의 분리된 탭을 제공한다.</li>
        <li><b>[시스템 규칙] 다중 계좌 스위칭:</b> Core와 Satellite 전략은 Streamlit Secrets에 저장된 완전히 다른 계좌 정보를 바라본다. 설정에 <code>is_mock</code> 항목이 누락될 경우, 실전(False)이 아닌 모의투자(True)로 강제 지정되어 사고를 막는다.</li>
        <li><b>[시스템 규칙] 가상원금 기반 과대 매수 차단:</b> 주문 수량 산출 시 <code>max(실제평가금, 가상원금)</code>을 사용하지 않는다. 실제 잔고가 0보다 크면 무조건 실제 평가금만을 베이스로 계산하여 계좌 잔고를 초과하는 주문 생성 자체를 막는다.</li>
        <li><b>[시스템 규칙] Fail-closed (안전 우선 차단):</b> KIS API 장애 등으로 인해 주문가능금액 조회가 일시적으로 실패(0 반환)하더라도, 이를 총 예수금으로 강제 대체하지 않고 가용 현금을 <code>0</code>으로 인식하여 미수금 발생을 방어한다.</li>
        <li><b>[시스템 규칙] 투명한 대기열 및 정렬:</b> 당장 매수/매도 시그널이 발생하지 않더라도 시스템의 판단(현금 부족, 타점 미달 등)을 큐에 모두 표시하여 관측성을 높인다. 큐는 항상 <code>매도(0) ➔ 매수(1) ➔ 관망(2)</code> 순서로 자동 정렬된다.</li>
        <li><b>[시스템 규칙] 대기열 뷰 확장 및 추가 매수 구분:</b> 사용자의 직관적인 판단을 위해 대기열에 종목의 '주문수량'뿐만 아니라 '현재 보유수량'과 '평균단가'를 실시간 대조하여 렌더링한다. 또한 이미 보유 중인 종목에 매수 시그널이 발생할 경우 단순 매수 시그널이 아닌 '추가 매수'로 명확히 구분하여 표기한다.</li>
        <li><b>[시스템 규칙] 예외 처리 충돌 방어:</b> Streamlit의 <code>st.rerun()</code> 동작이 포괄적 <code>except:</code> 구문에 걸려 로직이 멈추는 것을 방지하기 위해, 에러 캐치 시 반드시 <code>ValueError</code> 등 명확한 Exception Class를 지정하여 처리한다.</li>
        <li><b>[UI/UX 포맷팅 절대 규칙]</b> 일반 사용자의 직관성을 위해 성과 지표 용어를 한글로 친절히 순화(예: <code>CAGR ➔ 연평균 수익률(CAGR)</code>, <code>MDD ➔ 최대 낙폭(MDD)</code>)하여 표기한다. 계좌 표의 수익률은 양수일 경우 적색(<code>#FF5050</code>), 음수일 경우 청색(<code>#3b82f6</code>)으로 하드코딩 스타일링한다. AI 스코어, 이격도, 평균단가 등 변동성이 있는 지표는 반드시 소수점 둘째 자리(<code>.2f</code>)로 고정 노출하고, 현재가 및 수량은 정수 콤마 포맷(<code>,.0f</code>)을 적용하여 시각적 혼란을 방지한다.</li>
    </ul>

    <h3>🗄️ 8. 데이터베이스 스키마 (Database & Integrity)</h3>
    <ul>
        <li>SQLite 기반 WAL 모드를 적용하여 다중 프로세스(UI/Bot) 간의 동시 접근 락(Lock)을 방지한다.</li>
        <li>주문(<code>order_intents</code>), 보유(<code>positions</code>), 워커 리스(<code>worker_leases</code>)를 분리 관리하여 계좌 간 스코프를 완벽히 격리한다.</li>
        <li><b>[시스템 규칙] 수동 보유와 자동매매(Managed) 분리:</b> KIS 서버의 전체 잔고를 무조건 자동매매 포지션으로 덮어쓰지 않으며, 봇 스스로 체결한(Fill Delta) 수량만을 <code>managed_qty</code>로 누적하여 장기 투자 수동 보유분의 무단 청산을 방지한다.</li>
    </ul>

    <h3>💡 9. 장애 복구 및 프로세스 제어 (Disaster Recovery & Fencing)</h3>
    <ul>
        <li><b>Worker Lease & Fencing:</b> 다중 봇 실행 시 <code>worker_leases</code> 테이블을 통해 Lease 획득자만 주문을 POST 할 수 있으며, 뺏긴 워커는 즉시 권한을 상실한다.</li>
        <li><b>Crash Window 방어:</b> 프로세스가 어느 시점(claim, submit, ack 직전)에 강제 종료되더라도 UNIQUE 제약과 상태 대사를 통해 동일 주문의 2회 발송을 구조적으로 차단한다.</li>
        <li><b>원자적 안전 게이트(Atomic Safety Gate):</b> 주문 의도가 SUBMITTING 상태로 넘어가기 직전, 단일 트랜잭션 내에서 현금 부족, 킬 스위치 가동 여부, 리스 토큰 만료 여부를 재확인한다.</li>
    </ul>

    <h3>🔐 10. 보안 및 런타임 환경 (Security & Runtime Environment)</h3>
    <ul>
        <li><b>API Key 물리적 격리 (Zero Plaintext):</b> 증권사 <code>APP_KEY</code>, <code>APP_SECRET</code>, <code>CANO</code> 등의 민감한 정보는 절대 Google Sheets나 SQLite, 애플리케이션 로그에 평문으로 저장하지 않는다.</li>
        <li><b>강력한 해시 인증 (Bcrypt Authentication):</b> 시스템 로그인에 사용되는 기본 비밀번호 0000이나 URL 쿼리 파라미터 인증을 전면 폐기하고, Salt가 포함된 <b>Argon2id 또는 Bcrypt 해시 알고리즘</b>을 통해서만 검증을 수행한다. 관리자 비밀번호 해시값 역시 OS 환경변수(<code>ADMIN_PASSWORD_HASH</code>)에 보관된다.</li>
        <li><b>[시스템 규칙] 세션 만료 및 폼 기반 로그인 (Session Volatility):</b> 사용자의 편의를 위해 로그인 시 '엔터(Enter)' 키 입력을 지원하는 폼(Form) 구조를 사용한다. 단, 새로고침(F5) 시에는 브라우저 세션이 초기화되어 로그아웃 처리되는 것을 '의도된 기본 보안 정책(Secure by default)'으로 채택하여, 금융 데이터가 타인에게 노출되는 것을 방지한다.</li>
        <li><b>[시스템 규칙] 패키지 의존성(Dependency) 엄격 관리:</b> 시스템에 새로운 외부 라이브러리(예: <code>PyYAML</code>, <code>bcrypt</code> 등)를 도입하여 코드를 업데이트할 경우, 클라우드 서버(Streamlit Cloud 등) 배포 시 <code>ModuleNotFoundError</code>로 인해 시스템이 즉각 다운되는 치명적 장애를 막기 위해, <b>반드시 <code>requirements.txt</code> 파일에 해당 패키지명을 명시적으로 추가하여 형상 관리를 동기화</b>해야 한다.</li>
    </ul>

    <hr>
    <h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 및 자동 문서화 (절대 원칙)</h3>
    <ul style='color: #DC2626;'>
        <li>이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 <b>Part 1~10 전체 내용(투자 대원칙, 전략 공식, UI 규칙, 방어 로직 등)을 단 하나라도 임의로 축약, 훼손, 삭제해서는 안 됩니다.</b></li>
        <li><b>[자동 문서화 헌장]:</b> 향후 시스템의 로직, 파라미터, UI/UX 규칙을 새롭게 추가하거나 변경할 경우(단순 오타 수정 제외), <b>사용자가 따로 요청하지 않더라도 AI는 반드시 그 변경 사항과 사유를 이 백서(해당하는 Part)에 자동으로 업데이트하여 기록해야 합니다.</b> 이 백서는 시스템의 단일 진실 공급원(Single Source of Truth)으로 취급되어야 합니다.</li>
    </ul>
    """, unsafe_allow_html=True)
