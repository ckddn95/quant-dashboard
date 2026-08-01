import streamlit as st
import FinanceDataReader as fdr
import yfinance as yf
import pandas as pd
import numpy as np
from xgboost import XGBRegressor
from datetime import datetime
import os
import json
import warnings
warnings.filterwarnings('ignore')

# 웹 페이지 설정
st.set_page_config(page_title="프리미엄 퀀트 대시보드", page_icon="📈", layout="wide")

st.title("🌅 프리미엄 반자동 퀀트 투자 대시보드")
st.markdown("종목을 자유롭게 추가·제거하고, AI 분석을 통해 내 포트폴리오를 관리하며, 가상의 AI 펀드와 수익률을 비교해보세요.")

# ==========================================
# 1. 한국거래소(KRX) 전체 종목 데이터 캐싱
# ==========================================
@st.cache_data
def get_krx_stocks():
    df_krx = fdr.StockListing('KRX')
    return dict(zip(df_krx['Name'], df_krx['Code']))

krx_stocks = get_krx_stocks()

# ==========================================
# 2. 내 포트폴리오 초기 세팅
# ==========================================
if 'portfolio' not in st.session_state:
    st.session_state.portfolio = {
        '삼성전자': {'code': '005930', 'qty': 50},
        'SK하이닉스': {'code': '000660', 'qty': 20},
        'LG에너지솔루션': {'code': '373220', 'qty': 10},
        '현대차': {'code': '005380', 'qty': 15},
        'POSCO홀딩스': {'code': '005490', 'qty': 10},
        '삼성바이오로직스': {'code': '207940', 'qty': 5},
        'KB금융': {'code': '105560', 'qty': 50}
    }

# ==========================================
# 3. 사이드바 설정 (투자금, 종목 검색, 저장)
# ==========================================
st.sidebar.header("⚙️ 내 포트폴리오 설정")

initial_cash_input = st.sidebar.text_input("현재 보유 현금 (KRW)", value="45,000,000")
try:
    initial_cash = float(initial_cash_input.replace(",", ""))
except ValueError:
    st.sidebar.error("숫자와 쉼표(,)만 입력해 주세요.")
    initial_cash = 45000000.0

st.sidebar.markdown("---")
st.sidebar.subheader("🔍 종목 검색 및 추가")

search_keyword = st.sidebar.text_input("종목명 검색 (예: 삼성, 카카오)", "")
if search_keyword:
    keywords = search_keyword.lower().split()
    filtered_stocks = {name: code for name, code in krx_stocks.items() if all(kw in str(name).lower() for kw in keywords)}
else:
    filtered_stocks = {}

options = ["선택 안 함"] + list(filtered_stocks.keys()) if filtered_stocks else ["검색어를 입력하세요"]
selected_label = st.sidebar.selectbox("조회된 종목 선택", options=options)
new_qty = st.sidebar.number_input("초기 보유 수량 입력", value=0, step=1)

if st.sidebar.button("종목 리스트에 추가"):
    if selected_label not in ["선택 안 함", "검색어를 입력하세요"]:
        new_code = filtered_stocks[selected_label]
        st.session_state.portfolio[selected_label] = {'code': new_code, 'qty': int(new_qty)}
        st.sidebar.success(f"'{selected_label}'이(가) 추가되었습니다!")
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("📋 현재 등록된 종목")

stocks_to_delete = []
for name, info in st.session_state.portfolio.items():
    col1, col2 = st.sidebar.columns([3, 1])
    col1.text(f"{name} ({info['code']})")
    if col2.button("삭제", key=f"del_{info['code']}"):
        stocks_to_delete.append(name)

for name in stocks_to_delete:
    del st.session_state.portfolio[name]
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("💾 설정 저장 및 불러오기")

port_json = json.dumps(st.session_state.portfolio, ensure_ascii=False)
st.sidebar.download_button("📥 내 포트폴리오 저장", data=port_json, file_name="my_quant_portfolio.json", mime="application/json")

uploaded_file = st.sidebar.file_uploader("📤 저장한 포트폴리오 불러오기", type=['json'])
if uploaded_file is not None:
    if st.sidebar.button("설정 적용하기"):
        st.session_state.portfolio = json.load(uploaded_file)
        st.sidebar.success("성공적으로 불러왔습니다!")
        st.rerun()

st.sidebar.markdown("---")
rebal_threshold = st.sidebar.slider(
    "리밸런싱 밴드 기준 (%) 💡", 1.0, 10.0, 3.0, 0.5,
    help="현재 비중과 목표 비중의 차이가 이 값(%) 이내면 매매하지 않습니다. 잦은 매매와 수수료 낭비를 막는 안전장치입니다."
) / 100.0


# ==========================================
# 공통 AI 및 데이터 처리 함수
# ==========================================
def fetch_and_predict(codes_list):
    end_date = datetime.today().strftime('%Y-%m-%d')
    start_date = (datetime.today() - pd.DateOffset(years=8)).strftime('%Y-%m-%d')

    def get_yf_series(ticker, col_name):
        try:
            data = yf.download(ticker, start=start_date, end=end_date, progress=False)
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            s = data['Close'] if 'Close' in data.columns else data.iloc[:, 0]
            if isinstance(s, pd.DataFrame): s = s.iloc[:, 0]
            s = s.rename(col_name)
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s
        except:
            return pd.Series(dtype=float, name=col_name)

    vix = get_yf_series('^VIX', 'VIX_Fear_Index')
    tnx = get_yf_series('^TNX', 'US_10Y_Yield')
    soxx = get_yf_series('SOXX', 'Sector_SOXX')

    ex_rate = fdr.DataReader('USD/KRW', start_date, end_date)
    if isinstance(ex_rate.columns, pd.MultiIndex): ex_rate.columns = ex_rate.columns.get_level_values(0)
    ex_rate = ex_rate['Close'].rename('Exchange_Rate')
    ex_rate.index = pd.to_datetime(ex_rate.index).tz_localize(None).normalize()

    results = {}
    for name, code in codes_list.items():
        df_stock = fdr.DataReader(code, start_date, end_date)
        if isinstance(df_stock.columns, pd.MultiIndex): df_stock.columns = df_stock.columns.get_level_values(0)
        df_stock.index = pd.to_datetime(df_stock.index).tz_localize(None).normalize()

        raw_df = pd.concat([df_stock[['Close', 'Volume']], ex_rate, vix, tnx, soxx], axis=1).ffill().bfill()
        raw_df['SMA_5'] = raw_df['Close'].rolling(window=5).mean()
        raw_df['SMA_60'] = raw_df['Close'].rolling(window=60).mean()
        raw_df['Daily_Return'] = raw_df['Close'].pct_change()
        raw_df['Target_5D'] = raw_df['Close'].pct_change(5).shift(-5)

        features = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 'SMA_5', 'SMA_60', 'Daily_Return']
        for col in features + ['Target_5D']: raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')
        clean_df = raw_df.dropna(subset=features)
        
        if len(clean_df) > 100:
            X_train = clean_df.iloc[:-20][features]
            y_train_5d = clean_df.iloc[:-20]['Target_5D']
            model_5d = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.03, random_state=42).fit(X_train, y_train_5d)
            
            today_data = clean_df.iloc[-1]
            pred_5d = model_5d.predict(today_data[features].values.reshape(1, -1))[0]
            
            curr_price = today_data['Close']
            sma60 = today_data['SMA_60']
            curr_vix = today_data['VIX_Fear_Index']

            if curr_price < sma60 or curr_vix >= 28:
                target_weight = 0.0
            else:
                ai_score = np.clip((pred_5d + 0.005) / 0.025, 0.1, 1.0)
                trend_score = np.clip(curr_price / sma60, 0.5, 1.0)
                vix_score = np.clip(1.0 - (curr_vix - 15) / 25, 0.3, 1.0)
                target_weight = np.clip(ai_score * trend_score * vix_score, 0.01, 1.0)
            
            results[name] = {'price': curr_price, 'weight': target_weight}
    return results


# ==========================================
# 메인 화면 탭 구성 (3개 탭으로 분리)
# ==========================================
tab1, tab2, tab3 = st.tabs([
    "📊 내 포트폴리오 지시서", 
    "🤖 실전 AI 트래커 (일간)", 
    "⏪ 과거 장기 백테스트 (2021~2024)"
])

# 시뮬레이션용 대표 종목 고정
ai_target_stocks = {
    '삼성전자': '005930', 'SK하이닉스': '000660', 'LG에너지솔루션': '373220', '포스코퓨처엠': '003670',
    '현대차': '005380', '기아': '000270', 'POSCO홀딩스': '005490', '삼성바이오로직스': '207940',
    '셀트리온': '068270', 'KB금융': '105560', '신한지주': '055550'
}
BUY_FEE, SELL_FEE = 0.00015, 0.00195

# ------------------------------------------
# 탭 1: 내 실제 포트폴리오 관리
# ------------------------------------------
with tab1:
    if st.button("🚀 내 계좌 최신 분석 및 매매 지시서 생성", type="primary"):
        if not st.session_state.portfolio:
            st.error("등록된 종목이 없습니다. 사이드바에서 종목을 추가해 주세요.")
        else:
            with st.spinner("데이터 수집 및 AI 모델 분석 중... ⏳"):
                codes_list = {k: v['code'] for k, v in st.session_state.portfolio.items()}
                ai_results = fetch_and_predict(codes_list)
                
                total_stock_eval = 0.0
                signals = {}
                for name, res in ai_results.items():
                    qty = st.session_state.portfolio[name]['qty']
                    eval_val = qty * res['price']
                    total_stock_eval += eval_val
                    signals[name] = {'price': res['price'], 'eval': eval_val, 'weight': res['weight'], 'qty': qty}

                total_account_val = initial_cash + total_stock_eval
                max_slot_ratio = 1.0 / len(st.session_state.portfolio)
                slot_budget = total_account_val * max_slot_ratio

                st.success("✨ 내 계좌 분석이 완료되었습니다!")
                col1, col2, col3 = st.columns(3)
                col1.metric("총 계좌 평가액", f"{total_account_val:,.0f} 원")
                col2.metric("보유 현금", f"{initial_cash:,.0f} 원")
                col3.metric("주식 평가액", f"{total_stock_eval:,.0f} 원")

                st.markdown("### 📋 종목별 정밀 리밸런싱 가이드")
                display_data = []
                for name, info in signals.items():
                    p, eval_val, t_weight, qty = info['price'], info['eval'], info['weight'], info['qty']
                    target_amount = slot_budget * t_weight
                    current_weight_ratio = eval_val / total_account_val if total_account_val > 0 else 0
                    target_weight_ratio = target_amount / total_account_val
                    diff_ratio = target_weight_ratio - current_weight_ratio
                    diff_amount = target_amount - eval_val

                    if abs(diff_ratio) < rebal_threshold:
                        action = "⏸️ 유지 (밴드 내)"
                    elif diff_amount > 0:
                        buy_qty = int(diff_amount // p)
                        action = f"🛒 매수 추천 (+{buy_qty}주)" if buy_qty > 0 else "⏸️ 관망"
                    else:
                        sell_qty = int(round(abs(diff_amount) / p))
                        action = f"📉 매도 추천 (-{sell_qty}주)" if sell_qty > 0 else "⏸️ 관망"

                    display_data.append({
                        "종목명": name, "현재가": f"{p:,.0f} 원", "보유수량": f"{qty}주", 
                        "평가금액": f"{eval_val:,.0f} 원", "목표비중": f"{t_weight*100:.1f}%", 
                        "괴리율": f"{diff_ratio*100:+.1f}%", "매매 지시": action
                    })

                st.dataframe(pd.DataFrame(display_data), use_container_width=True)

# ------------------------------------------
# 탭 2: 실시간 AI 트래커
# ------------------------------------------
with tab2:
    st.subheader("🤖 AI 실전 매매 트래커 (초기자본: 1,000만 원)")
    st.info("이 탭은 오늘부터 매일 버튼을 눌러 AI 가상 펀드의 실력을 추적하는 공간입니다.")
    
    sim_file = "ai_sim_state.json"
    log_file = "ai_sim_log.csv"
    
    if os.path.exists(sim_file):
        with open(sim_file, 'r', encoding='utf-8') as f: sim_state = json.load(f)
    else:
        sim_state = {"cash": 10000000.0, "portfolio": {name: 0 for name in ai_target_stocks.keys()}, "last_run_date": ""}

    st.write(f"**현재 가상 보유 현금:** {sim_state['cash']:,.0f} 원")

    if st.button("🎯 오늘 장 기준 매매 실행 (하루 1회)"):
        today_str = datetime.today().strftime('%Y-%m-%d')
        with st.spinner("가상 매매 진행 중..."):
            ai_res = fetch_and_predict(ai_target_stocks)
            total_fund_val = sim_state['cash'] + sum(sim_state['portfolio'][n] * r['price'] for n, r in ai_res.items())
            max_weight = 1.0 / len(ai_target_stocks)
            trade_logs = []
            
            # 매도
            for name, res in ai_res.items():
                p = res['price']
                curr_qty = sim_state['portfolio'][name]
                tar_amt = total_fund_val * max_weight * res['weight']
                if curr_qty * p > tar_amt:
                    sell_qty = int(round((curr_qty * p - tar_amt) / p))
                    if 0 < sell_qty <= curr_qty:
                        sim_state['portfolio'][name] -= sell_qty
                        sim_state['cash'] += (sell_qty * p) * (1 - SELL_FEE)
                        trade_logs.append(f"📉 {name} {sell_qty}주 매도")
            
            # 매수
            for name, res in ai_res.items():
                p = res['price']
                tar_amt = total_fund_val * max_weight * res['weight']
                if sim_state['portfolio'][name] * p < tar_amt:
                    buy_qty = int((tar_amt - sim_state['portfolio'][name] * p) // p)
                    cost = (buy_qty * p) * (1 + BUY_FEE)
                    while cost > sim_state['cash'] and buy_qty > 0:
                        buy_qty -= 1
                        cost = (buy_qty * p) * (1 + BUY_FEE)
                    if buy_qty > 0:
                        sim_state['portfolio'][name] += buy_qty
                        sim_state['cash'] -= cost
                        trade_logs.append(f"🛒 {name} {buy_qty}주 매수")
            
            final_eval = sum(sim_state['portfolio'][n] * r['price'] for n, r in ai_res.items())
            final_total = sim_state['cash'] + final_eval
            
            sim_state['last_run_date'] = today_str
            with open(sim_file, 'w', encoding='utf-8') as f: json.dump(sim_state, f, ensure_ascii=False)

            log_data = {'Date': [today_str], 'Total_Val': [final_total], 'Cash': [sim_state['cash']], 'Stock_Val': [final_eval]}
            df_log = pd.DataFrame(log_data)
            df_log.to_csv(log_file, mode='a' if os.path.exists(log_file) else 'w', header=not os.path.exists(log_file), index=False, encoding='utf-8-sig')
                
            st.success(f"✅ {today_str} 매매 완료! (총 평가액: {final_total:,.0f} 원)")
            for log in trade_logs: st.write(log)
            st.rerun()

    if os.path.exists(log_file):
        history_df = pd.read_csv(log_file).drop_duplicates(subset=['Date'], keep='last').set_index('Date')
        st.line_chart(history_df['Total_Val'])

# ------------------------------------------
# 탭 3: 2021~2024 장기 시뮬레이션 (새로 추가됨)
# ------------------------------------------
with tab3:
    st.subheader("⏪ 2021~2024 퀀트 전략 백테스트")
    st.warning("⚠️ 웹 환경 보호(서버 과부하 방지)를 위해 **월간(20영업일) 리밸런싱** 기준으로 진행됩니다. 완료까지 약 30초~1분 정도 소요될 수 있습니다.")
    
    if st.button("▶️ 2021~2024 백테스트 실행하기", type="primary"):
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        try:
            status_text.text("1/3. 2016~2024년 주가 및 거시경제 데이터를 불러오는 중...")
            
            def get_hist_series(ticker, col_name):
                data = yf.download(ticker, start='2016-01-01', end='2024-12-31', progress=False)
                if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
                s = data['Close'] if 'Close' in data.columns else data.iloc[:, 0]
                return s.rename(col_name).tz_localize(None).normalize()

            h_vix = get_hist_series('^VIX', 'VIX_Fear_Index')
            h_tnx = get_hist_series('^TNX', 'US_10Y_Yield')
            h_soxx = get_hist_series('SOXX', 'Sector_SOXX')
            
            h_ex = fdr.DataReader('USD/KRW', '2016-01-01', '2024-12-31')
            if isinstance(h_ex.columns, pd.MultiIndex): h_ex.columns = h_ex.columns.get_level_values(0)
            h_ex = h_ex['Close'].rename('Exchange_Rate').tz_localize(None).normalize()

            stock_data = {}
            for name, code in ai_target_stocks.items():
                df = fdr.DataReader(code, '2016-01-01', '2024-12-31')
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                stock_data[name] = df[['Close', 'Volume']].tz_localize(None).normalize()

            valid_dates = pd.date_range('2021-01-01', '2024-12-31', freq='B')
            bt_portfolio = {name: 0 for name in ai_target_stocks.keys()}
            bt_cash = 10000000.0
            bt_history = []

            # 20영업일(약 1달) 단위 시뮬레이션
            step_size = 20 
            total_steps = len(range(0, len(valid_dates), step_size))
            
            for step_idx, i in enumerate(range(0, len(valid_dates), step_size)):
                current_date = valid_dates[i]
                status_text.text(f"2/3. AI 가상 펀드 운용 중... ({current_date.strftime('%Y-%m')} 진행 중)")
                progress_bar.progress((step_idx + 1) / total_steps)
                
                signals = {}
                current_eval = 0
                
                for name, df in stock_data.items():
                    past_df = df[df.index <= current_date]
                    if len(past_df) < 100: continue
                        
                    raw_df = pd.concat([past_df, h_ex[h_ex.index <= current_date], h_vix[h_vix.index <= current_date], 
                                        h_tnx[h_tnx.index <= current_date], h_soxx[h_soxx.index <= current_date]], axis=1).ffill().bfill()
                    
                    raw_df['SMA_5'] = raw_df['Close'].rolling(window=5).mean()
                    raw_df['SMA_60'] = raw_df['Close'].rolling(window=60).mean()
                    raw_df['Daily_Return'] = raw_df['Close'].pct_change()
                    raw_df['Target_5D'] = raw_df['Close'].pct_change(5).shift(-5)
                    
                    features = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 'SMA_5', 'SMA_60', 'Daily_Return']
                    clean_df = raw_df.dropna(subset=features + ['Target_5D'])
                    
                    if len(clean_df) > 100:
                        X_train = clean_df.iloc[:-20][features]
                        y_train_5d = clean_df.iloc[:-20]['Target_5D']
                        model = XGBRegressor(n_estimators=50, max_depth=3, learning_rate=0.05, random_state=42).fit(X_train, y_train_5d)
                        
                        today_data = raw_df.iloc[-1]
                        if pd.isna(today_data['Close']): continue
                        
                        pred_5d = model.predict(today_data[features].values.reshape(1, -1))[0]
                        p, sma60, v = today_data['Close'], today_data['SMA_60'], today_data['VIX_Fear_Index']
                        
                        if p < sma60 or v >= 28: weight = 0.0
                        else:
                            ai_s, tr_s, vx_s = np.clip((pred_5d + 0.005)/0.025, 0.1, 1), np.clip(p/sma60, 0.5, 1), np.clip(1-(v-15)/25, 0.3, 1)
                            weight = np.clip(ai_s * tr_s * vx_s, 0.01, 1.0)
                            
                        signals[name] = {'price': p, 'weight': weight}
                        current_eval += bt_portfolio[name] * p

                if not signals: continue
                total_val = bt_cash + current_eval
                max_w = 1.0 / len(ai_target_stocks)
                
                # 매도 -> 매수 로직 (세금 100% 반영)
                for name, res in signals.items():
                    p, tar = res['price'], total_val * max_w * res['weight']
                    curr = bt_portfolio[name] * p
                    if curr > tar:
                        sell_qty = int(round((curr - tar) / p))
                        if 0 < sell_qty <= bt_portfolio[name]:
                            bt_portfolio[name] -= sell_qty
                            bt_cash += (sell_qty * p) * (1 - SELL_FEE)
                            
                for name, res in signals.items():
                    p, tar = res['price'], total_val * max_w * res['weight']
                    curr = bt_portfolio[name] * p
                    if curr < tar:
                        buy_qty = int((tar - curr) // p)
                        cost = (buy_qty * p) * (1 + BUY_FEE)
                        while cost > bt_cash and buy_qty > 0:
                            buy_qty -= 1
                            cost = (buy_qty * p) * (1 + BUY_FEE)
                        if buy_qty > 0:
                            bt_portfolio[name] += buy_qty
                            bt_cash -= cost

                final_eval = sum(bt_portfolio[n] * signals[n]['price'] for n in signals if n in bt_portfolio)
                bt_history.append({'Date': current_date.strftime('%Y-%m-%d'), 'Total_Val': bt_cash + final_eval})

            status_text.text("3/3. 차트 생성 중...")
            df_bt = pd.DataFrame(bt_history).set_index('Date')
            
            st.success("🎉 4년 장기 시뮬레이션 완료!")
            
            final_val = df_bt['Total_Val'].iloc[-1]
            cagr = ((final_val / 10000000.0) ** (1/4) - 1) * 100
            tot_ret = (final_val / 10000000.0 - 1) * 100
            
            c1, c2, c3 = st.columns(3)
            c1.metric("초기 자본", "10,000,000 원")
            c2.metric("최종 자산 (2024년 말)", f"{final_val:,.0f} 원")
            c3.metric("누적 수익률 (CAGR)", f"{tot_ret:+.2f} %", f"연 {cagr:+.2f}%")
            
            st.line_chart(df_bt['Total_Val'])
            
        except Exception as e:
            st.error(f"오류가 발생했습니다: {e}")
