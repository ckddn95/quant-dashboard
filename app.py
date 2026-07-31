import subprocess
import sys

try:
  import yfinance
  import FinanceDataReader
except ImportError:
  subprocess.check_call([
      sys.executable,
      "-m",
      "pip",
      "install",
      "--user",
      "yfinance",
      "FinanceDataReader",
  ])
  import yfinance
  import FinanceDataReader
import streamlit as st
import FinanceDataReader as fdr
import yfinance as yf
import pandas as pd
import numpy as np
from xgboost import XGBRegressor
from datetime import datetime
import os
import warnings
warnings.filterwarnings('ignore')

# 웹 페이지 설정
st.set_page_config(
    page_title="프리미엄 반자동 퀀트 대시보드",
    page_icon="📈",
    layout="wide"
)

st.title("🌅 프리미엄 반자동 퀀트 투자 대시보드")
st.markdown("종목을 자유롭게 추가·제거하고, 미국 마감 지표를 반영한 정밀 매매 지시서를 확인할 수 있는 웹 서비스입니다.")

# ==========================================
# 세션 상태 초기화 (동적 종목 추가/삭제 지원)
# ==========================================
if 'portfolio' not in st.session_state:
    st.session_state.portfolio = {
        'SK하이닉스': {'code': '000660', 'qty': 150},
        'POSCO홀딩스': {'code': '005490', 'qty': 40},
        '현대차': {'code': '005380', 'qty': 80},
        'NAVER': {'code': '035420', 'qty': 20},
        '한화솔루션': {'code': '009830', 'qty': 100}
    }

# ==========================================
# 사이드바 설정 (투자금 및 종목 관리)
# ==========================================
st.sidebar.header("⚙️ 포트폴리오 및 자금 설정")

initial_cash = st.sidebar.number_input("현재 보유 현금 (KRW)", value=45_000_000.0, step=1_000_000.0, format="%0.f")

st.sidebar.markdown("---")
st.sidebar.subheader("➕ 종목 추가하기")
new_name = st.sidebar.text_input("종목명 (예: 삼성전자)")
new_code = st.sidebar.text_input("종목코드 6자리 (예: 005930)")
new_qty = st.sidebar.number_input("초기 보유 수량", value=0, step=1)

if st.sidebar.button("종목 리스트에 추가"):
    if new_name and new_code:
        st.session_state.portfolio[new_name] = {'code': new_code.strip(), 'qty': int(new_qty)}
        st.sidebar.success(f"'{new_name}'이(가) 추가되었습니다!")
    else:
        st.sidebar.error("종목명과 코드를 올바르게 입력해주세요.")

st.sidebar.markdown("---")
st.sidebar.subheader("📋 현재 등록된 종목 관리")

# 종목 삭제 기능
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
st.sidebar.subheader("🛠️ 전략 파라미터")
rebal_threshold = st.sidebar.slider("리밸런싱 밴드 기준 (%)", min_value=1.0, max_value=10.0, value=3.0, step=0.5) / 100.0

run_button = st.sidebar.button("🚀 최신 데이터 분석 및 매매 지시서 생성", type="primary")

# ==========================================
# 데이터 분석 및 메인 로직
# ==========================================
if run_button:
    if not st.session_state.portfolio:
        st.error("등록된 종목이 없습니다. 사이드바에서 종목을 추가해 주세요.")
    else:
        with st.spinner("미국 마감 지표 및 주가 데이터를 수집하고 AI 모델을 분석 중입니다... ⏳"):
            end_date = datetime.today().strftime('%Y-%m-%d')
            start_date = (datetime.today() - pd.DateOffset(years=8)).strftime('%Y-%m-%d')

            def get_yf_series(ticker, col_name):
                try:
                    data = yf.download(ticker, start=start_date, end=end_date, progress=False)
                    if isinstance(data.columns, pd.MultiIndex):
                        data.columns = data.columns.get_level_values(0)
                    s = data['Close'] if 'Close' in data.columns else data.iloc[:, 0]
                    if isinstance(s, pd.DataFrame):
                        s = s.iloc[:, 0]
                    s = s.rename(col_name)
                    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                    return s
                except Exception:
                    return pd.Series(dtype=float, name=col_name)

            vix = get_yf_series('^VIX', 'VIX_Fear_Index')
            tnx = get_yf_series('^TNX', 'US_10Y_Yield')
            soxx = get_yf_series('SOXX', 'Sector_SOXX')

            ex_rate = fdr.DataReader('USD/KRW', start_date, end_date)
            if isinstance(ex_rate.columns, pd.MultiIndex):
                ex_rate.columns = ex_rate.columns.get_level_values(0)
            ex_rate = ex_rate['Close'].rename('Exchange_Rate')
            ex_rate.index = pd.to_datetime(ex_rate.index).tz_localize(None).normalize()

            signals = {}
            total_stock_eval = 0.0
            max_slot_ratio = 1.0 / len(st.session_state.portfolio)

            for name, info in st.session_state.portfolio.items():
                code = info['code']
                qty = info['qty']
                
                df_stock = fdr.DataReader(code, start_date, end_date)
                if isinstance(df_stock.columns, pd.MultiIndex):
                    df_stock.columns = df_stock.columns.get_level_values(0)
                df_stock.index = pd.to_datetime(df_stock.index).tz_localize(None).normalize()

                raw_df = pd.concat([df_stock[['Close', 'Volume']], ex_rate, vix, tnx, soxx], axis=1).ffill().bfill()
                raw_df['SMA_5'] = raw_df['Close'].rolling(window=5).mean()
                raw_df['SMA_20'] = raw_df['Close'].rolling(window=20).mean()
                raw_df['SMA_60'] = raw_df['Close'].rolling(window=60).mean()
                raw_df['Daily_Return'] = raw_df['Close'].pct_change()
                
                raw_df['Target_1D'] = raw_df['Daily_Return'].shift(-1)
                raw_df['Target_5D'] = raw_df['Close'].pct_change(5).shift(-5)

                features = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 'SMA_5', 'SMA_20', 'SMA_60', 'Daily_Return']
                for col in features + ['Target_1D', 'Target_5D']:
                    raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')

                clean_df = raw_df.dropna(subset=features)
                
                X_train = clean_df.iloc[:-20][features]
                y_train_5d = clean_df.iloc[:-20]['Target_5D']
                
                model_5d = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.03, random_state=42).fit(X_train, y_train_5d)
                
                today_data = clean_df.iloc[-1]
                pred_5d = model_5d.predict(today_data[features].values.reshape(1, -1))[0]
                
                curr_price = today_data['Close']
                sma60 = today_data['SMA_60']
                curr_vix = today_data['VIX_Fear_Index']
                
                eval_val = qty * curr_price
                total_stock_eval += eval_val

                if curr_price < sma60 or curr_vix >= 28:
                    target_weight = 0.0
                else:
                    ai_score = np.clip((pred_5d + 0.005) / 0.025, 0.1, 1.0)
                    trend_score = np.clip(curr_price / sma60, 0.5, 1.0)
                    vix_score = np.clip(1.0 - (curr_vix - 15) / 25, 0.3, 1.0)
                    
                    target_weight = ai_score * trend_score * vix_score
                    target_weight = np.clip(target_weight, 0.01, 1.0)

                signals[name] = {
                    'price': curr_price, 
                    'eval': eval_val, 
                    'weight': target_weight, 
                    'qty': qty
                }

            total_account_val = initial_cash + total_stock_eval
            slot_budget = total_account_val * max_slot_ratio

            # 대시보드 출력
            st.success("✨ 분석이 완료되었습니다!")
            
            col1, col2, col3 = st.columns(3)
            col1.metric("총 계좌 평가액", f"{total_account_val:,.0f} 원")
            col2.metric("보유 현금 잔고", f"{initial_cash:,.0f} 원", f"{initial_cash/total_account_val*100:.1f}%")
            col3.metric("주식 평가 금액", f"{total_stock_eval:,.0f} 원", f"{total_stock_eval/total_account_val*100:.1f}%")

            st.markdown("---")
            st.subheader("📋 종목별 정밀 리밸런싱 가이드")

            display_data = []
            for name, info in signals.items():
                p = info['price']
                eval_val = info['eval']
                t_weight = info['weight']
                qty = info['qty']
                
                target_amount = slot_budget * t_weight
                current_weight_ratio = eval_val / total_account_val if total_account_val > 0 else 0
                target_weight_ratio = target_amount / total_account_val
                diff_ratio = target_weight_ratio - current_weight_ratio
                diff_amount = target_amount - eval_val

                if abs(diff_ratio) < rebal_threshold:
                    action = "⏸️ 유지 (밴드 내)"
                elif diff_amount > 0:
                    buy_qty = int((diff_amount * 0.5) // p)
                    action = f"🛒 매수 추천 (+{buy_qty}주)" if buy_qty > 0 else "⏸️ 관망"
                else:
                    sell_qty = int((abs(diff_amount) * 0.5) // p)
                    action = f"📉 매도 추천 (-{sell_qty}주)" if sell_qty > 0 else "⏸️ 관망"

                display_data.append({
                    "종목명": name,
                    "현재가 (원)": f"{p:,.0f}",
                    "보유수량": f"{qty}주",
                    "평가금액 (원)": f"{eval_val:,.0f}",
                    "목표비중": f"{t_weight*100:.1f}%",
                    "괴리율": f"{diff_ratio*100:+.1f}%",
                    "매매 지시": action
                })

            df_display = pd.DataFrame(display_data)
            st.dataframe(df_display, use_container_width=True)

            # CSV 자동 일지 기록
            log_file = "quant_portfolio_log.csv"
            log_data = {
                'Date': [datetime.today().strftime('%Y-%m-%d')],
                'Total_Val': [total_account_val],
                'Cash': [initial_cash],
                'Stock_Eval': [total_stock_eval]
            }
            for name, info in signals.items():
                log_data[f'{name}_Qty'] = [info['qty']]

            df_log = pd.DataFrame(log_data)
            if not os.path.exists(log_file):
                df_log.to_csv(log_file, index=False, encoding='utf-8-sig')
            else:
                df_log.to_csv(log_file, mode='a', header=False, index=False, encoding='utf-8-sig')

            st.info(f"📁 오늘의 계좌 상태가 자동으로 '{log_file}' 일지 파일에 기록되었습니다.")
else:
    st.info("👈 왼쪽 사이드바에서 원하는 종목을 추가/삭제하고 **[최신 데이터 분석 및 매매 지시서 생성]** 버튼을 클릭하세요.")
