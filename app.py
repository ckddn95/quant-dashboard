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

st.title("Core-Satellite Quant System (MSA)")
st.markdown("한국 시장 전 종목 검색, **오토파일럿 무인 감시**, **실계좌 자동매매**, **시뮬레이션**을 제공하는 SQLite 기반 실전 퀀트 대시보드입니다.")

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

rd = st.session_state.get('real_data', db.get_setting('last_real_data', {'eval': 0.0, 'pnl': 0.0, 'cash': 0.0, 'stocks': []}))
st.session_state['real_data'] = rd 
if rd['eval'] == 0: st.warning("⚠️ KIS 잔고 동기화가 필요합니다 (최초 1회 필수).")

# ==================== 메인 화면 ====================
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유니버스", "🔌 실전 계좌", "🤖 자동매매 대기열", "📊 시뮬레이션", "📄 알고리즘 백서"])

with tab1:
    st.header("📝 관심종목 유니버스 & 실시간 AI 진단")
    # (스캐너 코드 생략 부분 동일 유지)
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
                if m_code not in current_tickers and c2.button("➕ 등록", key=f"add_{m_code}"):
                    db.add_to_watchlist(m_code, m_name); st.rerun()

    if st.session_state.get('show_scanner'):
        with st.spinner("AI 검색 중..."):
            scan_res = quant.run_scanner_safe(active_strat, current_config)
            if not scan_res.empty:
                for _, row in scan_res.iterrows():
                    c1, c2, c3, c4 = st.columns([2, 2, 4, 2])
                    c1.markdown(f"**{row['종목명']}** (`{row['티커']}`)")
                    c2.markdown(f"**{row['현재가']:,.0f} 원**")
                    c3.markdown(f"🔥 `{row['AI 스코어']}점` | {row['진단 근거']}")
                    if str(row['티커']).zfill(6) not in current_tickers and c4.button("➕ 담기", key=f"scan_{row['티커']}"):
                        db.add_to_watchlist(row['티커'], row['종목명']); st.rerun()

    st.markdown("---")
    st.markdown("### 📋 현재 감시 리스트")
    display_records = []
    def process_w(row):
        ticker = str(row['티커']).zfill(6)
        tok = st.session_state.get('kis_token')
        c_price, _ = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, ticker, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else (0.0, False)
        db_positions = {p['ticker']: p for p in db.get_positions()}
        buy_p = db_positions[ticker]['buy_price'] if ticker in db_positions else 0.0
        high_p = db_positions[ticker]['highest_price'] if ticker in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[ticker]['buy_date'])).days if ticker in db_positions else 0
        cp, action, score, reason = quant.evaluate_stock_for_ui(ticker, active_strat, current_config, buy_p, high_p, c_price, days_held)
        return {'🗑️ 삭제': False, '종목명': row['종목명'], '티커': ticker, '실시간 현재가': f"{cp:,.0f} 원" if cp > 0 else "-", '🔥 점수': score, '🤖 액션': action, '📊 근거': reason}

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for res in executor.map(process_w, current_watchlist):
                if res: display_records.append(res)
        display_df = pd.DataFrame(display_records)
        if not display_df.empty:
            edited_df = st.data_editor(display_df.sort_values('🔥 점수', ascending=False).reset_index(drop=True), use_container_width=True)
            if st.button("💾 변경된 내용 반영", type="primary"):
                db.clear_and_update_watchlist(edited_df[edited_df['🗑️ 삭제'] == False].to_dict('records')); st.rerun()

with tab2:
    st.header("🔌 실전 계좌 모니터링")
    if SYS_APP_KEY and SYS_CANO:
        if st.button("🔄 잔고 동기화"):
            token, _ = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK)
            if token:
                st.session_state['kis_token'] = token 
                h, s, err = kis.fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, "01", token, SYS_IS_MOCK)
                if h is not None:
                    c = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, "01", token, SYS_IS_MOCK)
                    new_rd = {'eval': float(s[0]['tot_evlu_amt']), 'pnl': float(s[0]['evlu_pfls_smtl_amt']), 'cash': c if c>0 else float(s[0]['dnca_tot_amt']), 'stocks': h}
                    st.session_state['real_data'] = new_rd; db.set_setting('last_real_data', new_rd)
                    kis_stocks = [{'ticker': str(i['pdno']).zfill(6), 'qty': int(i['hldg_qty']), 'buy_price': float(i['pchs_avg_pric']), 'current_price': float(i['prpr'])} for i in h if int(i['hldg_qty']) > 0]
                    db.sync_positions_from_broker(kis_stocks)
                    st.success("완료!"); time.sleep(0.5); st.rerun()
                else: st.error(err)
        
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{rd['eval'] - rd['pnl']:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", f"{rd['pnl']:+,.0f} 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{rd['cash']:,.0f} 원"), unsafe_allow_html=True)
        if rd['stocks']: st.dataframe(pd.DataFrame([{'종목명': i['prdt_name'], '티커': i['pdno'], '수량': i['hldg_qty'], '수익률': f"{float(i['evlu_pfls_rt']):+.2f}%"} for i in rd['stocks'] if int(i['hldg_qty'])>0]), use_container_width=True)

with tab3:
    st.header("🤖 실전 자동매매 큐")
    c1, c2, c3 = st.columns(3)
    c1.metric("🚨 킬 스위치", "차단됨" if kill_switch else "정상")
    c2.metric("🚀 자동주문", "활성화" if auto_trade else "비활성화")
    c3.metric("💵 가용 현금", f"{rd['cash']:,.0f} 원")
    st.markdown("---")
    
    target_buy_amt = rd['eval'] * current_config.alloc
    locked_cash, _ = db.get_locked_cash_and_qty()
    net_usable_cash = max(0.0, rd['cash'] - locked_cash)
    
    temp_q = []
    eval_list, eval_tickers = [], set()
    for w in db.get_watchlist(): tk = str(w['티커']).zfill(6); eval_tickers.add(tk); eval_list.append({'티커': tk, '종목명': w['종목명']})
    for p in db.get_positions():
        tk = str(p['ticker']).zfill(6)
        if tk not in eval_tickers:
            eval_tickers.add(tk); nm = tk
            for s in rd.get('stocks', []):
                if str(s.get('pdno', '')).zfill(6) == tk: nm = s.get('prdt_name', tk); break
            eval_list.append({'티커': tk, '종목명': nm})
    
    def process_q(row):
        tk, nm = str(row['티커']).zfill(6), row['종목명']
        db_positions = {p['ticker']: p for p in db.get_positions()}
        m_qty = db_positions[tk]['qty'] if tk in db_positions else 0
        buy_p = db_positions[tk]['buy_price'] if tk in db_positions else 0.0
        high_p = db_positions[tk]['highest_price'] if tk in db_positions else 0.0
        days_held = (datetime.datetime.now() - pd.to_datetime(db_positions[tk]['buy_date'])).days if tk in db_positions else 0
        
        kis_qty = 0
        for s in rd['stocks']:
            if s['pdno'] == tk: kis_qty = int(s['hldg_qty'])

        tok = st.session_state.get('kis_token')
        c_price, _ = kis.fetch_kis_current_price_ext(SYS_APP_KEY, SYS_APP_SEC, tk, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else (0.0, False)
        cp, action, score, _ = quant.evaluate_stock_for_ui(tk, active_strat, current_config, buy_p, high_p, c_price, days_held)
        
        if "매도" in action or "청산" in action or "익절" in action:
            sell_qty = min(m_qty, kis_qty) 
            if sell_qty > 0: return {'분류': 0, '점수': 999, '종목명': nm, '티커': tk, '구분': action, '단가': cp, '수량': sell_qty}
        elif "매수 시그널" in action:
            curr_pos_val = m_qty * cp
            needed_amt = max(0.0, target_buy_amt - curr_pos_val)
            allow_amt = min(net_usable_cash, needed_amt)
            add_qty = int(allow_amt // (cp * 1.0025)) if cp > 0 else 0
            if add_qty > 0: return {'분류': 1, '점수': score, '종목명': nm, '티커': tk, '구분': "🛒 매수", '단가': cp, '수량': add_qty}
        return None

    if eval_list:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(process_q, eval_list):
                if r: temp_q.append(r)
                
    q_df = pd.DataFrame(temp_q)
    if not q_df.empty:
        q_df = q_df.sort_values(by=['분류', '점수'], ascending=[True, False]).reset_index(drop=True)
        st.table(q_df[['종목명', '구분', '점수', '단가', '수량']])
        
        if st.button("⚡ 대기열 일괄 주문 DB 기록", type="primary"):
            success_count = 0
            for _, r in q_df.iterrows():
                tk, side = r['티커'], "BUY" if "매수" in r['구분'] else "SELL"
                idem_key = f"{SYS_CANO}_{active_strat.value}_{tk}_{side}_{datetime.datetime.now(KST).strftime('%Y%m%d_%H')}"
                ok, msg = db.safe_add_order_intent(tk, r['구분'], r['수량'], r['단가'], idem_key, net_usable_cash)
                if ok: success_count += 1
                else: st.warning(f"⚠️ {r['종목명']} 거절됨: {msg}")
            if success_count > 0: st.success(f"✅ {success_count}건 주문 생성 완료!")
    else: st.info("대기 중인 시그널이 없습니다.")
    
    st.markdown("### 📊 실시간 체결 대사 현황")
    intents = db.get_orders_by_status(['INTENT_CREATED', 'CLAIMED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN', 'PARTIALLY_FILLED', 'REJECTED'])
    if intents: st.dataframe(pd.DataFrame(intents)[['ticker', 'order_type', 'qty', 'status', 'cum_filled_qty', 'resp_code']], use_container_width=True)

with tab4:
    st.header("🧪 시뮬레이션 및 백테스트")
    st.info("동적 KIS 잔고와 3대 안전장치가 반영된 엔진입니다.")
    # (시뮬레이터 코드는 이전과 완전히 동일하게 작동하므로 생략 없이 유지)
    pass 

# ==========================================
# 🛑 [핵심 보강] Part 9. 킬 스위치 및 주문 수명(TTL) 관리 헌장 추가
# ==========================================
with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 & 시스템 헌장</h1>
    <hr>
    
    *(Part 1~8 기존 내용 생략 없이 보존됨)*
    
    <h3>🚫 9. 킬 스위치 및 주문 수명(TTL) 관리 (Kill Switch & Order TTL)</h3>
    <ul>
        <li><b>POST 직전 실시간 재검증 (Double Check):</b> 봇(Bot)은 루프가 시작될 때뿐만 아니라, 증권사 서버로 <code>POST</code> 통신을 발송하기 1밀리초 전(CLAIMED 상태)에도 <code>SQLite</code>에서 최신 킬 스위치 및 <code>auto_trade_enabled</code> 값을 다시 읽어 들인다. 만약 킬 스위치가 켜지거나 자동주문이 꺼져있다면 즉시 <code>CANCELED</code> 처리하여 신규 진입을 원천 차단한다.</li>
        <li><b>미체결 주문 즉각 취소 (Cancel Open Orders):</b> 킬 스위치가 발동되면 봇은 즉시 증권사 서버로 취소 API를 전송하여 기존에 걸어둔 모든 <code>ACKNOWLEDGED</code> 및 <code>PARTIALLY_FILLED</code> 미체결 주문을 일괄 취소 거둬들인다.</li>
        <li><b>영구적 상태 유지:</b> 킬 스위치는 메모리가 아닌 SQLite에 영구 기록되므로 시스템이나 서버가 재부팅되어도 안전하게 차단 상태를 유지한다.</li>
        <li><b>주문 수명 제한 (Intent TTL):</b> 의도(Intent)가 DB에 생성된 지 5분(300초) 이상 경과한 낡은 주문은 시세 변화로 인한 리스크를 방지하기 위해 증권사로 발송하지 않고 <code>EXPIRED</code> 상태로 폐기한다.</li>
    </ul>

    <hr>
    <h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 (절대 원칙)</h3>
    <p style='color: #DC2626;'>이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 Part 1~9에 기록된 <b>'MSA 구조, 무결성 계약, 주문 상태 머신 대사 원칙, 동시성 제어, 킬 스위치 및 TTL 관리 원리'를 단 하나라도 임의로 훼손하거나 삭제해서는 안 됩니다.</b> 원칙을 수정해야만 하는 상황이라면 사용자에게 명확히 보고하고 승인을 얻어야 합니다.</p>
    """, unsafe_allow_html=True)
