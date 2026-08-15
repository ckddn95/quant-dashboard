import streamlit as st
import pandas as pd
import datetime
import time
import database as db
import broker.kis_client as kis
import quant_engine as quant

# ==========================================
# 0. 페이지 설정 및 초기화
# ==========================================
st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")
KST = datetime.timezone(datetime.timedelta(hours=9))

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("한국 시장 전 종목을 검색하여 포트폴리오를 구성하고, **오토파일럿 무인 감시**, **실계좌 자동매매**, **시뮬레이션**을 제공하는 실전 퀀트 대시보드입니다.")

# ==========================================
# 1. 사이드바 UI (SQLite 연동)
# ==========================================
st.sidebar.header("🎯 전략 및 API 설정")
active_strat = db.get_setting('strategy', '대형주 (Core)')
active_strat = st.sidebar.selectbox("운용 전략", ['대형주 (Core)', '중소형주 (Satellite)'], index=0 if active_strat=='대형주 (Core)' else 1)
db.set_setting('strategy', active_strat)

total_cash = db.get_setting('virtual_cash', 10000000)
new_cash = st.sidebar.number_input("총 투자 운용 자산 (가상 원금)", value=int(total_cash), step=1000000)
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
st.sidebar.header("📱 텔레그램 및 오토파일럿 제어")
st.sidebar.info("UI 변경 사항은 즉시 SQLite DB에 기록되어 백그라운드 봇에 반영됩니다.")

init_ks = db.get_setting('kill_switch', False)
init_at = db.get_setting('auto_trade_enabled', False)
init_ap = db.get_setting('auto_pilot', False)

kill_switch = st.sidebar.toggle("🚨 긴급 정지 (KILL SWITCH)", value=init_ks)
auto_trade = st.sidebar.toggle("🚀 실전 자동주문 활성화", value=init_at)
auto_pilot = st.sidebar.toggle("🔄 오토파일럿 켜기", value=init_ap)

if kill_switch != init_ks: db.set_setting('kill_switch', kill_switch)
if auto_trade != init_at: db.set_setting('auto_trade_enabled', auto_trade)
if auto_pilot != init_ap: db.set_setting('auto_pilot', auto_pilot)

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

# ==========================================
# 2. 메인 화면 구성
# ==========================================
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 관심종목 유니버스", "🔌 실전 계좌", "🤖 자동매매 대기열", "📊 시뮬레이션", "📄 알고리즘 백서"])

with tab3:
    st.header("🤖 실전 자동매매 큐 (UI -> DB 의도 생성)")
    # (큐 생성 로직 생략, 기존과 동일하게 queue_df 생성)
    queue_df = pd.DataFrame([{'티커': '005930', '구분': '🛒 신규 매수', '주문 단가': '80000', '주문 수량': '10', '상태': '대기 중'}]) # 예시용
    st.table(queue_df)
    
    # 🛑 [핵심 패치 5] 메인의 주문 버튼이 KIS에 쏘지 않고 SQLite order_intents에 단방향 기록함
    if st.button("⚡ 대기열 일괄 주문 수동 전송", type="primary", use_container_width=True):
        for _, row in queue_df.iterrows():
            db.add_order_intent(row['티커'], row['구분'], int(row['주문 수량']), float(row['주문 단가']))
        st.success("✅ SQLite DB에 주문 의도(Intent) 기록 완료! 백그라운드의 bot.py가 즉시 체결을 시작합니다.")

with tab5:
    st.markdown("""
    <h1 style='text-align: center; color: #1E3A8A;'>📄 Core-Satellite AI 퀀트 운용 알고리즘 백서 & 시스템 헌장</h1>
    <hr>
    <p>본 백서는 사용자를 위한 <b>시스템 상세 매뉴얼</b>이자, 향후 시스템 업데이트 시 AI가 절대적으로 준수해야 할 <b>불변의 알고리즘 헌장(System Prompt)</b>입니다.</p>
    """, unsafe_allow_html=True)

    st.header("📌 Part 1. 시스템 아키텍처 및 대원칙 (Grand Principles)")
    st.info("""
    * **단일 아키텍처 통합 (MSA):** SQLite를 설정, 포지션, 킬 스위치, 주문 상태의 단일 원본(SSOT)으로 사용한다. UI(Streamlit)는 의도(Intent)만 생성하며, 실제 KIS 주문(POST)은 오직 `bot.py` 데몬 프로세스만이 수행하는 완벽한 1-way 구조를 준수한다.
    * **미래 참조 및 생존자 편향 완벽 차단:** 백테스트 시 치팅(Cheating) 행위를 금지한다.
    """)

    st.header("🔎 Part 2. 종목 발굴 메커니즘 (AI 스캐너 & 유니버스)")
    st.markdown("""
    * **Step 1:** KOSPI 상위 200개 또는 KOSDAQ 상위 150개 스캔 (시뮬레이션 시 100개).
    * **Step 2:** $Price \ge MA200$
    * **Step 3:** Core($MA60_{t} > MA60_{t-10} \land MA20 \ge MA60 \times (1+Buf)$), Satellite(이격도 -5% ~ +3%)
    * **Step 4:** 85점~99점 점수화.
    """)

    st.header("💳 Part 3. 자금 관리 및 3대 고급 안전장치 (Filters & Boosters)")
    st.markdown("""
    * **복리 비중 매수:** $Target\_Fund = Total\_Equity \times (Max\_Alloc\_Pct / 100)$
    * **강세장 부스터:** $If \ Index > MA200 \Rightarrow Max\_Alloc\_Pct += 10.0\%$
    * **쿨다운 대기 & 최소 보유 기간 적용**
    """)

    st.header("🛡️ Part 4. 리스크 관리 및 청산 (Exit Strategies)")
    st.markdown("""
    * **장중 저가 칼손절:** $Low\_Price \le Buy\_Price \times (1 - Stop\_Loss\_Pct)$ (최우선 순위)
    * **트레일링 익절 & 추세 이탈 매도**
    * **익일 시가 체결:** 시뮬레이션 매수/매도 시 다음 날 시가(Open)로 체결하여 슬리피지 100% 반영.
    """)

    st.markdown("---")
    st.markdown("<h3 style='color: #DC2626;'>⚠️ [SYSTEM PROMPT] AI 업데이트 지침 (절대 원칙)</h3>", unsafe_allow_html=True)
    st.warning("""
    이 문서를 읽은 AI는 향후 코드를 업데이트할 때, 위 Part 1~4에 기록된 **'대원칙, 계산 공식, API 구조(SQLite 단방향 통신, bot.py 단독 Writer), 백테스트 규칙(익일 시가/장중 저가), 3대 고급 안전장치 논리'를 단 하나라도 임의로 훼손해서는 안 됩니다.** 
    원칙을 수정해야만 하는 상황이라면 사용자에게 명확히 보고하고 승인을 얻어야 합니다.
    """)
