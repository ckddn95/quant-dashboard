import streamlit as st
import pandas as pd
import datetime
import time
import concurrent.futures
import database as db
import broker.kis_client as kis
import quant_engine as quant

# ==========================================
# 0. 페이지 설정 및 초기화
# ==========================================
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

# ==========================================
# 1. 사이드바 UI (SQLite 연동)
# ==========================================
st.sidebar.header("🎯 전략 및 환경 설정")
active_strat = db.get_setting('strategy', '대형주 (Core)')
active_strat = st.sidebar.selectbox("운용 전략", ['대형주 (Core)', '중소형주 (Satellite)'], index=0 if active_strat=='대형주 (Core)' else 1)
db.set_setting('strategy', active_strat)

total_cash = int(db.get_setting('virtual_cash', 10000000))
new_cash = st.sidebar.number_input("총 투자 운용 자산 (가상 원금)", value=total_cash, step=1000000)
if new_cash != total_cash: db.set_setting('virtual_cash', new_cash)

has_keys = bool(db.get_setting('manual_app_key'))
with st.sidebar.expander("🔑 KIS API 설정", expanded=not has_keys):
    if has_keys:
        st.success("✅ API 키 연동 중")
        if st.button("🗑️ 키 삭제"): 
            db.set_setting('manual_app_key', None)
            st.rerun()
    else:
        k1 = st.text_input("APP KEY", type="password")
        k2 = st.text_input("APP SECRET", type="password")
        c1 = st.text_input("계좌번호 앞 8자리")
        m1 = st.checkbox("모의투자", value=True)
        if st.button("저장"):
            db.set_setting('manual_app_key', k1); db.set_setting('manual_app_secret', k2)
            db.set_setting('manual_cano', c1); db.set_setting('manual_is_mock', m1)
            st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("📱 봇 제어 (DB 연동)")
st.sidebar.info("UI 변경 사항은 즉시 SQLite DB에 기록되어 백그라운드 봇에 반영됩니다.")

init_ks = bool(db.get_setting('kill_switch', False))
init_at = bool(db.get_setting('auto_trade_enabled', False))
init_ap = bool(db.get_setting('auto_pilot', False))

kill_switch = st.sidebar.toggle("🚨 긴급 정지 (KILL SWITCH)", value=init_ks)
auto_trade = st.sidebar.toggle("🚀 실전 자동주문 활성화", value=init_at)
auto_pilot = st.sidebar.toggle("🔄 오토파일럿 켜기", value=init_ap)

if kill_switch != init_ks: db.set_setting('kill_switch', kill_switch)
if auto_trade != init_at: db.set_setting('auto_trade_enabled', auto_trade)
if auto_pilot != init_ap: db.set_setting('auto_pilot', auto_pilot)
if kill_switch: st.sidebar.error("⚠️ 킬 스위치 작동 중!")

st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터")
default_params = {
    '대형주 (Core)': {'ma200': True, 'buf': 1.5, 'sl': -15, 'alloc': 35, 'ts_tgt': 30, 'ts_drp': -10, 'cd': 60, 'min_h': 5, 'boost': True},
    '중소형주 (Satellite)': {'ma200': True, 'buf': 1.0, 'sl': -12, 'alloc': 20, 'ts_tgt': 20, 'ts_drp': -7, 'cd': 30, 'min_h': 3, 'boost': True}
}
curr_def = default_params[active_strat]

if 'params' not in st.session_state or st.session_state.get('last_strat') != active_strat:
    st.session_state.params = db.get_setting(f'params_{active_strat}', curr_def.copy())
    st.session_state.last_strat = active_strat

is_custom = any(st.session_state.params[k] != v for k, v in curr_def.items())
if is_custom:
    st.sidebar.error("⚠️ 사용자 맞춤 파라미터 적용 중")
    if st.sidebar.button("🔄 기본값 복구"):
        st.session_state.params = curr_def.copy()
        db.set_setting(f'params_{active_strat}', st.session_state.params)
        st.rerun()
else: st.sidebar.success("✅ 기본 권장 파라미터 적용 중")

p = st.session_state.params
p['ma200'] = st.sidebar.checkbox("🛡️ 200일 추세선", value=p['ma200'])
p['buf'] = st.sidebar.slider("골든크로스 버퍼 (%)", 0.0, 5.0, float(p['buf']), 0.1)
p['sl'] = st.sidebar.slider("긴급 손절 컷 (%)", -30, -5, int(p['sl']), 1)
with st.sidebar.expander("🧪 시뮬레이션 및 고급 안전장치", expanded=is_custom):
    p['cd'] = st.slider("쿨다운(일)", 0, 90, int(p['cd']), 5)
    p['alloc'] = st.slider("투입 한도 (%)", 10, 100, int(p['alloc']), 5)
    p['min_h'] = st.slider("최소 보유(일)", 0, 20, int(p['min_h']), 1)
    p['ts_tgt'] = st.slider("익절 목표 (%)", 5, 100, int(p['ts_tgt']), 5)
    p['ts_drp'] = st.slider("하락 허용 (%)", -30, -1, int(p['ts_drp']), 1)
    p['boost'] = st.checkbox("🔥 강세장 부스터", value=p['boost'])

db.set_setting(f'params_{active_strat}', p)

use_ma200_filter, whipsaw_buffer, sat_stop_loss = p['ma200'], p['buf'] / 100.0, p['sl'] / 100.0
cooldown_days, max_alloc_pct, min_hold_days = p['cd'], float(p['alloc']), p['min_h']
ts_target_pct, ts_drop_pct, bull_market_boost = p['ts_tgt'] / 100.0, p['ts_drp'] / 100.0, p['boost']

SYS_APP_KEY = db.get_setting('manual_app_key')
SYS_APP_SEC = db.get_setting('manual_app_secret')
SYS_CANO = db.get_setting('manual_cano')
SYS_IS_MOCK = bool(db.get_setting('manual_is_mock', True))

rd = st.session_state.get('real_data', {'eval': float(total_cash), 'pnl': 0.0, 'cash': float(total_cash), 'stocks': []})
real_invested_principal = rd['eval'] - rd['pnl'] if rd['eval'] > 0 else float(total_cash)
base_date_str = db.get_setting('created_at', '2024-01-01')
try: real_base_date = pd.to_datetime(base_date_str).date()
except: real_base_date = datetime.date(2024, 1, 1)

# ==================== 메인 화면 ====================
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
                if m_code not in current_tickers and c2.button("➕ 등록", key=f"add_{m_code}"):
                    db.add_to_watchlist(m_code, m_name)
                    st.rerun()

    if st.session_state.get('show_scanner'):
        with st.spinner("AI 검색 중..."):
            scan_res = quant.run_scanner_safe(active_strat, use_ma200_filter, whipsaw_buffer, min_hold_days)
            if not scan_res.empty:
                st.markdown("### 💡 AI 스캐너 포착 종목")
                for _, row in scan_res.iterrows():
                    c1, c2, c3, c4 = st.columns([2, 2, 4, 2])
                    c1.markdown(f"**{row['종목명']}** (`{row['티커']}`)")
                    c2.markdown(f"**{row['현재가']:,.0f} 원**")
                    c3.markdown(f"🔥 `{row['AI 스코어']}점` | {row['진단 근거']}")
                    if str(row['티커']).zfill(6) not in current_tickers and c4.button("➕ 담기", key=f"scan_{row['티커']}"):
                        db.add_to_watchlist(row['티커'], row['종목명'])
                        st.rerun()
            else: st.info("조건에 맞는 종목이 없습니다.")

    st.markdown("---")
    st.markdown("### 📋 현재 감시 리스트")
    display_records = []
    
    # 🛑 [에러 방어 완벽 패치 1] 모듈 명 및 세션 토큰 매핑 완료
    def process_w(row):
        ticker = str(row['티커']).zfill(6)
        tok = st.session_state.get('kis_token')
        c_price = kis.fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SEC, ticker, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else 0.0
        cp, action, score, reason = quant.evaluate_stock_for_ui(ticker, active_strat, 0.0, 0.0, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, min_hold_days, c_price)
        return {'🗑️ 삭제': False, '종목명': row['종목명'], '티커': ticker, '실시간 현재가': f"{cp:,.0f} 원" if cp > 0 else "-", '🔥 매력도 점수': score, '🤖 AI 액션 플랜': action, '📊 근거': reason}

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for res in executor.map(process_w, current_watchlist):
                if res: display_records.append(res)
        
        display_df = pd.DataFrame(display_records)
        if not display_df.empty:
            display_df = display_df.sort_values('🔥 매력도 점수', ascending=False).reset_index(drop=True)
            edited_df = st.data_editor(display_df, use_container_width=True)
            if st.button("💾 변경된 내용 반영 (삭제 적용)", type="primary"):
                keep_df = edited_df[edited_df['🗑️ 삭제'] == False]
                db.clear_and_update_watchlist(keep_df.to_dict('records'))
                st.success("업데이트 완료!")
                time.sleep(0.5); st.rerun()
    else:
        st.info("현재 등록된 관심종목이 없습니다. 스캐너를 돌리거나 직접 검색하여 추가해주세요.")

with tab2:
    st.header("🔌 실전 계좌 모니터링")
    if SYS_APP_KEY and SYS_CANO:
        if st.button("🔄 잔고 동기화"):
            token, _ = kis.get_kis_access_token(SYS_APP_KEY, SYS_APP_SEC, SYS_IS_MOCK)
            if token:
                st.session_state['kis_token'] = token # 🛑 [패치 2] 발급받은 토큰을 세션에 저장
                h, s, err = kis.fetch_kis_account_balance(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, "01", token, SYS_IS_MOCK)
                if h is not None:
                    c = kis.fetch_kis_orderable_cash(SYS_APP_KEY, SYS_APP_SEC, SYS_CANO, "01", token, SYS_IS_MOCK)
                    st.session_state['real_data'] = {'eval': float(s[0]['tot_evlu_amt']), 'pnl': float(s[0]['evlu_pfls_smtl_amt']), 'cash': c if c>0 else float(s[0]['dnca_tot_amt']), 'stocks': h}
                    st.success("완료!")
                    time.sleep(0.5)
                    st.rerun()
                else: st.error(err)
        
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(mts_metric_html("💰 총 평가 금액", f"{rd['eval']:,.0f} 원"), unsafe_allow_html=True)
        c2.markdown(mts_metric_html("📥 투자 원금", f"{real_invested_principal:,.0f} 원"), unsafe_allow_html=True)
        c3.markdown(mts_metric_html("📈 누적 수익금", f"{rd['pnl']:+,.0f} 원"), unsafe_allow_html=True)
        c4.markdown(mts_metric_html("💵 주문가능 원화", f"{rd['cash']:,.0f} 원"), unsafe_allow_html=True)
        if rd['stocks']:
            df = pd.DataFrame([{'종목명': i['prdt_name'], '티커': i['pdno'], '보유수량': i['hldg_qty'], '수익률': f"{float(i['evlu_pfls_rt']):+.2f}%"} for i in rd['stocks'] if int(i['hldg_qty'])>0])
            st.dataframe(df, use_container_width=True)
    else: st.info("API 키를 설정해주세요.")

with tab3:
    st.header("🤖 실전 자동매매 큐 (UI -> DB 의도 생성)")
    c1, c2, c3 = st.columns(3)
    c1.metric("🚨 킬 스위치", "차단됨" if kill_switch else "정상")
    c2.metric("🚀 자동주문", "활성화" if auto_trade else "비활성화")
    c3.metric("💵 주문가능 원화", f"{rd['cash']:,.0f} 원")
    st.markdown("---")
    
    target_buy_amt = max(rd['eval'], float(total_cash)) * (max_alloc_pct / 100.0)
    temp_q = []
    
    # 🛑 [패치 3] 자동매매 큐에서도 변수명 충돌 완벽 방어
    def process_q(row):
        tk, nm = str(row['티커']).zfill(6), row['종목명']
        qty, buy_p = 0, 0.0
        for s in rd['stocks']:
            if s['pdno'] == tk:
                qty, buy_p = int(s['hldg_qty']), float(s['pchs_avg_pric'])
        
        tok = st.session_state.get('kis_token')
        c_price = kis.fetch_kis_current_price(SYS_APP_KEY, SYS_APP_SEC, tk, tok, SYS_IS_MOCK) if SYS_APP_KEY and tok else 0.0
        cp, action, score, _ = quant.evaluate_stock_for_ui(tk, active_strat, buy_p, buy_p, use_ma200_filter, whipsaw_buffer, ts_target_pct, ts_drop_pct, sat_stop_loss, min_hold_days, c_price)
        
        if "매도" in action or "청산" in action or "익절" in action:
            if qty > 0: return {'분류': 0, '점수': 999, '종목명': nm, '티커': tk, '구분': action, '단가': cp, '수량': qty}
        elif "매수 시그널" in action:
            add_amt = max(0.0, target_buy_amt - (qty * cp))
            add_qty = int(add_amt // (cp * 1.0025)) if cp > 0 else 0
            if add_qty > 0: return {'분류': 1, '점수': score, '종목명': nm, '티커': tk, '구분': "🛒 신규/추가 매수", '단가': cp, '수량': add_qty}
        return None

    if current_watchlist:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for r in ex.map(process_q, current_watchlist):
                if r: temp_q.append(r)
                
    q_df = pd.DataFrame(temp_q)
    if not q_df.empty:
        q_df = q_df.sort_values(by=['분류', '점수'], ascending=[True, False]).reset_index(drop=True)
        st.table(q_df[['종목명', '구분', '점수', '단가', '수량']])
        if st.button("⚡ 대기열 일괄 주문 DB 기록", type="primary"):
            for _, r in q_df.iterrows():
                db.add_order_intent(r['티커'], r['구분'], r['수량'], r['단가'])
            st.success("✅ SQLite에 기록 완료! bot.py가 즉시 체결을 시작합니다.")
    else: st.info("대기 중인 시그널이 없습니다.")

with tab4:
    st.header("🧪 시뮬레이션 및 백테스트")
    st.info("이곳의 백테스트는 백서에 정의된 3대 고급 안전장치 및 익일 시가 체결이 100% 적용된 결과입니다.")
    stocks_df = pd.DataFrame(db.get_watchlist())
    today_date = datetime.datetime.now(KST).date()
    
    st.subheader("🎯 Test 1. 포워드 테스트 (관심종목 vs 실전 계좌)")
    st.info("💡 **어떻게 비교하나요?** 포트폴리오 개설일로부터 AI가 현재 관심종목들을 운용했을 때의 **이론적 성과**와 현재 **내 실제 계좌 성과**를 1:1로 비교합니다.")
    col_fw_date, col_fw_btn = st.columns([3, 7])
    with col_fw_date:
        test1_start_date = st.date_input("📅 가상 운용 시작일", real_base_date, key="t4_date")
    with col_fw_btn:
        st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
        if st.button("▶️ 포워드 테스트 1:1 비교 실행", type="primary", use_container_width=True):
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner("포워드 테스트 구동 중..."):
                    eval_init_cash = real_invested_principal if real_invested_principal > 0 else float(total_cash)
                    res_fw = quant.run_quant_simulation(stocks_df, active_strat, eval_init_cash, test1_start_date, today_date, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if res_fw:
                        real_ret_pct = (rd['pnl'] / real_invested_principal) * 100 if real_invested_principal > 0 else 0.0
                        col_fw1, col_fw2 = st.columns(2)
                        with col_fw1: st.markdown(mts_metric_html("📈 AI 가상 운용 (이론)", f"{res_fw['final_port_ret']:+.2f}%", f"기말 자산: {res_fw['final_asset']:,.0f} 원"), unsafe_allow_html=True)
                        with col_fw2: st.markdown(mts_metric_html("🔌 나의 실전 계좌 (실제)", f"{real_ret_pct:+.2f}%", f"현재 자산: {rd['eval']:,.0f} 원"), unsafe_allow_html=True)
                        st.dataframe(pd.DataFrame(res_fw['summary_rows']), use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("🎯 Test 2. 관심종목 대상 장기 검증")
    c1, c2, c3 = st.columns([3,3,4])
    with c1: start_d = st.date_input("시작일", datetime.date(2023,1,1))
    with c2: end_d = st.date_input("종료일", today_date)
    with c3:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        if st.button("장기 Backtest 실행", type="primary", use_container_width=True):
            if stocks_df.empty: st.error("관심종목 리스트에 종목이 없습니다.")
            else:
                with st.spinner("구동 중..."):
                    res = quant.run_quant_simulation(stocks_df, active_strat, total_cash, start_d, end_d, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, min_hold_days, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days)
                    if res:
                        st.success("완료!")
                        r1, r2 = st.columns(2)
                        r1.markdown(mts_metric_html("초기 투입 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                        r2.markdown(mts_metric_html("기말 자산", f"{res['final_asset']:,.0f} 원", f"{res['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                        st.dataframe(pd.DataFrame(res['summary_rows']))

    st.markdown("---")
    st.subheader("💡 Test 3. 동적 포착 AI 자율매매 백테스트 (과거 주도주 발굴 ➡️ 시가 매수)")
    c1, c2 = st.columns([3, 7])
    with c1: yr = st.selectbox("연도", list(range(today_date.year, 2021, -1)))
    with c2:
        st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)
        if st.button(f"{yr}년도 자율매매 백테스트 실행", type="primary", use_container_width=True):
            with st.spinner(f"서버 부하 방지를 위해 우량주 100개 풀에서 {yr}년도 시뮬레이션 중..."):
                res = quant.run_yearly_realistic_backtest(active_strat, total_cash, yr, use_ma200_filter, whipsaw_buffer, sat_stop_loss, max_alloc_pct, ts_target_pct, ts_drop_pct, bull_market_boost, cooldown_days, min_hold_days)
                if res:
                    st.success("완료!")
                    logs = res['trade_logs']
                    win = len([l for l in logs if float(l['수익률'].replace('%','').replace('+','')) > 0])
                    rate = (win / len(logs)) * 100 if logs else 0
                    r1, r2, r3 = st.columns(3)
                    r1.markdown(mts_metric_html("초기 자산", f"{total_cash:,.0f} 원"), unsafe_allow_html=True)
                    r2.markdown(mts_metric_html("기말 자산", f"{res['final_asset']:,.0f} 원", f"{res['final_port_ret']:+.2f}%"), unsafe_allow_html=True)
                    r3.markdown(mts_metric_html("체결 횟수 / 승률", f"{len(logs)} 회", f"승률 {rate:.1f}%"), unsafe_allow_html=True)
                    if logs: st.dataframe(pd.DataFrame(logs))

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 & 시스템 헌장</h1>
    <hr>
    <p>본 백서는 사용자를 위한 <b>시스템 상세 매뉴얼</b>이자, 향후 시스템 업데이트 시 AI가 절대적으로 준수해야 할 <b>불변의 알고리즘 헌장(System Prompt)</b>입니다.</p>

    <h3>📌 1. 시스템 아키텍처 및 대원칙 (Grand Principles)</h3>
    <ul>
        <li><b>단일 아키텍처 통합 (MSA):</b> SQLite를 설정, 포지션, 킬 스위치, 주문 상태의 단일 원본(SSOT)으로 사용한다. UI(Streamlit)는 의도(Intent)만 생성하며, 실제 KIS 주문(POST)은 오직 <code>bot.py</code> 데몬 프로세스만이 수행하는 완벽한 1-way 구조를 준수한다.</li>
        <li><b>100% 실전 동일 환경 구축:</b> 모든 백테스트와 시뮬레이션 엔진은 실제 라이브 봇이 작동하는 환경과 완벽하게 동일한 조건으로 동작해야 한다.</li>
        <li><b>미래 참조 및 생존자 편향 완벽 차단:</b> 백테스트 시 치팅(Cheating) 행위를 금지한다.</li>
    </ul>

    <h3>🔎 2. 종목 발굴 메커니즘 (AI 스캐너 & 유니버스)</h3>
    <ul>
        <li><b>Step 1 (시장/시총 필터):</b> KOSPI 상위 200개 또는 KOSDAQ 상위 150개 우량주를 1차 후보군으로 선정. (Test 3 시뮬레이션 시에는 서버 부하를 고려하여 코스피 50 + 코스닥 50 = 총 100개로 압축 진행).</li>
        <li><b>Step 2 (200일선 추세 필터):</b> $Price \ge MA200$</li>
        <li><b>Step 3 (전략별 타점 필터):</b>
            <ul>
                <li><b>Core (추세추종):</b> $MA60_{t} > MA60_{t-10} \land MA20 \ge MA60 \times (1 + Buf)$</li>
                <li><b>Satellite (눌림목):</b> $-5.0\% \le \left( \frac{Price - MA20}{MA20} \right) \times 100 \le +3.0\%$</li>
            </ul>
        </li>
        <li><b>Step 4 (매력도 점수):</b> 85점~99점 점수화. (Core: 이격도 클수록, Satellite: 낮을수록 고득점).</li>
    </ul>

    <h3>💳 3. 자금 관리 및 3대 고급 안전장치</h3>
    <ul>
        <li><b>복리 비중 분할 매수:</b> $Target\_Fund = Total\_Equity \times (Max\_Alloc\_Pct / 100)$</li>
        <li><b>강세장 부스터 (Bull Market Booster):</b> $If \ Index > MA200 \Rightarrow Max\_Alloc\_Pct += 10.0\%$</li>
        <li><b>쿨다운 대기:</b> 2회 연속 손실 발생 시 일정 기간(cd_days) 매수 차단.</li>
        <li><b>최소 보유 기간:</b> 휩소 방지를 위해 매수 후 일정 기간(min_h) 동안 추세 이탈 매도 보류 (손절은 즉시 작동).</li>
    </ul>

    <h3>🛡️ 4. 리스크 관리 및 청산 (Exit Strategies)</h3>
    <ul>
        <li><b>장중 저가 칼손절 (최우선):</b> $Low\_Price \le Buy\_Price \times (1 - Stop\_Loss\_Pct)$</li>
        <li><b>트레일링 익절:</b> $Highest\_Price \ge Buy\_Price \times (1 + Target)$ 달성 후 $Low \le Highest \times (1 - Drop)$ 시 매도.</li>
        <li><b>추세선 이탈:</b> 장 마감 후 기준 이탈 시 익일 시가 청산.</li>
        <li><b>익일 시가 체결 (Test 3):</b> 시그널은 장 마감에 확정되며, 실제 체결은 다음 날 아침 <b>시가(Open)</b>로 처리하여 슬리피지 반영.</li>
    </ul>

    <hr>
    <h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 (절대 원칙)</h3>
    <p style='color: #DC2626;'>이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 Part 1~4에 기록된 <b>'대원칙, 계산 공식, API 구조(SQLite 단방향 통신, bot.py 단독 Writer), 백테스트 규칙(익일 시가/장중 저가), 3대 고급 안전장치 논리'를 단 하나라도 임의로 훼손하거나 삭제해서는 안 됩니다.</b> 원칙을 수정해야만 하는 상황이라면 사용자에게 명확히 보고하고 승인을 얻어야 합니다.</p>
    """, unsafe_allow_html=True)
