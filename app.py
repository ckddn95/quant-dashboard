import streamlit as st
import pandas as pd
import database as db

st.set_page_config(page_title="Core-Satellite Quant System", page_icon="🚀", layout="wide")
db.init_db()

st.title("Core-Satellite Independent Asset Allocation Quant System")
st.markdown("⚠️ **V6.5 업데이트:** 구글 시트 연동을 제거하고 초고속/안전 SQLite 데이터베이스를 적용했습니다.")

# 기본 포트폴리오 생성
base_cfg = db.get_config("MAIN_PORT")
if not base_cfg:
    db.save_config("MAIN_PORT", {
        "strategy_name": "대형주 (Core)", "use_ma200_filter": 1, "ma_buffer_pct": 0.015,
        "stop_loss_pct": -0.15, "ts_target_pct": 0.30, "ts_drop_pct": -0.10, "min_liquidity": 5e9, "max_alloc_pct": 0.35,
        "cash": 10000000, "is_mock": 1, "kill_switch": 0, "auto_trade_enabled": 0, "auto_pilot": 0,
        "app_key": "", "app_secret": "", "cano": "", "prdt_cd": "01"
    })
    base_cfg = db.get_config("MAIN_PORT")

st.sidebar.header("🔌 한국투자증권 실계좌(MOCK) 연동")
new_app_key = st.sidebar.text_input("APP KEY", value=base_cfg.get('app_key', ''))
new_app_secret = st.sidebar.text_input("APP SECRET", value=base_cfg.get('app_secret', ''), type="password")
new_cano = st.sidebar.text_input("계좌번호 (앞 8자리)", value=base_cfg.get('cano', ''))
if st.sidebar.button("API 설정 저장"):
    base_cfg['app_key'], base_cfg['app_secret'], base_cfg['cano'] = new_app_key, new_app_secret, new_cano
    db.save_config("MAIN_PORT", base_cfg)
    st.sidebar.success("저장 완료! (봇이 자동으로 연동합니다)")

st.sidebar.markdown("---")
st.sidebar.subheader("🚨 긴급 제어 및 자동매매")
new_ks = st.sidebar.toggle("🚨 킬 스위치 (KILL SWITCH)", value=bool(base_cfg.get('kill_switch')))
new_at = st.sidebar.toggle("🚀 자동주문 집행 허용", value=bool(base_cfg.get('auto_trade_enabled')))
new_ap = st.sidebar.toggle("🔄 오토파일럿 스캐너 가동", value=bool(base_cfg.get('auto_pilot')))

if new_ks != bool(base_cfg.get('kill_switch')) or new_at != bool(base_cfg.get('auto_trade_enabled')) or new_ap != bool(base_cfg.get('auto_pilot')):
    base_cfg['kill_switch'], base_cfg['auto_trade_enabled'], base_cfg['auto_pilot'] = int(new_ks), int(new_at), int(new_ap)
    db.save_config("MAIN_PORT", base_cfg)
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("⚙️ 전략 파라미터 (비율 소수점 입력)")
new_sl = st.sidebar.number_input("긴급 손절 컷 (예: -15% -> -0.15)", value=base_cfg.get('stop_loss_pct', -0.15), step=0.01)
if new_sl != base_cfg.get('stop_loss_pct'):
    base_cfg['stop_loss_pct'] = new_sl
    db.save_config("MAIN_PORT", base_cfg)
    st.rerun()

tab1, tab2 = st.tabs(["📝 관심종목 등록 & 조회", "📋 매매 현황 및 주문 대기열"])

with tab1:
    st.markdown("#### 직접 종목 검색하여 감시 목록(Watchlist)에 추가")
    t_input = st.text_input("종목코드(6자리) 입력", placeholder="예: 005930")
    t_name = st.text_input("종목명 입력")
    if st.button("➕ 관제 리스트에 등록"):
        import sqlite3
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT OR IGNORE INTO watchlist (port_name, ticker, stock_name) VALUES (?, ?, ?)", ("MAIN_PORT", t_input.zfill(6), t_name))
        st.success(f"{t_name} 등록 완료!")
    
    st.markdown("---")
    st.markdown("#### 현재 등록된 감시 리스트")
    import sqlite3
    with sqlite3.connect(db.DB_PATH) as conn:
        wl_df = pd.read_sql("SELECT ticker AS '종목코드', stock_name AS '종목명' FROM watchlist WHERE port_name='MAIN_PORT'", conn)
        st.dataframe(wl_df, use_container_width=True, hide_index=True)

with tab2:
    st.subheader("📋 실시간 매매 로그 및 주문 대기열")
    st.markdown("`bot.py`가 DB를 읽어 대신 주문을 처리합니다. UI에서 새로고침해도 중복 주문이 나가지 않습니다.")
    
    import sqlite3
    with sqlite3.connect(db.DB_PATH) as conn:
        order_df = pd.read_sql("SELECT order_id AS ID, ticker AS 종목코드, stock_name AS 종목명, side AS 구분, intent_qty AS 수량, status AS 진행상태, msg AS 메세지, created_at AS 발생시간 FROM order_ledger ORDER BY order_id DESC LIMIT 30", conn)
        st.dataframe(order_df, use_container_width=True, hide_index=True)

    if st.button("🔄 화면 최신화 (봇 처리결과 확인)"):
        st.rerun()
