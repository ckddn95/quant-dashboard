import FinanceDataReader as fdr
import yfinance as yf
import pandas as pd
import numpy as np
from xgboost import XGBRegressor
from datetime import datetime
import matplotlib.pyplot as plt
from tqdm import tqdm
import warnings
warnings.filterwarnings('ignore')

# ==========================================
# 1. 코어-새틀라이트 통합 백테스트 환경 설정
# ==========================================
TOTAL_INITIAL_CASH = 30_000_000.0
CORE_RATIO = 0.70       # 💡 대형주 코어 포트폴리오 비중 70% (2,100만 원)
SAT_RATIO = 0.30        # 💡 중소형주 새틀라이트 비중 30% (900만 원)

BUY_FEE = 0.00015
SELL_FEE = 0.00195

# 종목 풀 정의
large_stocks = {
    '삼성전자': '005930',
    'LG에너지솔루션': '373220',
    '현대차': '005380',
    'POSCO홀딩스': '005490',
    '삼성바이오로직스': '207940',
    'KB금융': '105560'
}

small_mid_stocks = {
    '에코프로비엠': '247540',
    '엘앤에프': '066970',
    '리노공업': '058470',
    '솔브레인': '365550',
    '에스티팜': '237690',
    '클래시스': '214150',
    '파마리서치': '214450',
    '삼천당제약': '000250',
    '레인보우로보틱스': '277810',
    '에이비엘바이오': '298380',
    '실리콘투': '257720',
    '브이티': '018290',
    'ISC': '095340',
    'HPSP': '403870',
    '원익IPS': '240810'
}

def safe_datetime_index(obj):
    obj.index = pd.to_datetime(obj.index, utc=True).tz_localize(None).normalize()
    return obj

print("📊 [Step 1/4] 대형주 및 중소형주 풀 데이터 수집 중...")
FETCH_START = '2016-01-01'
FETCH_END = '2025-12-31'

def get_hist_series(ticker, col_name):
    try:
        data = yf.download(ticker, start=FETCH_START, end=FETCH_END, progress=False)
        if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
        s = data['Close'] if 'Close' in data.columns else data.iloc[:, 0]
        if isinstance(s, pd.DataFrame): s = s.iloc[:, 0]
        return safe_datetime_index(s.rename(col_name))
    except:
        return pd.Series(dtype=float, name=col_name)

h_vix = get_hist_series('^VIX', 'VIX_Fear_Index')
h_tnx = get_hist_series('^TNX', 'US_10Y_Yield')
h_soxx = get_hist_series('SOXX', 'Sector_SOXX')

try:
    h_ex = fdr.DataReader('USD/KRW', FETCH_START, FETCH_END)
    if isinstance(h_ex.columns, pd.MultiIndex): h_ex.columns = h_ex.columns.get_level_values(0)
    h_ex = safe_datetime_index(h_ex['Close'].rename('Exchange_Rate'))
except:
    h_ex = pd.Series(dtype=float, name='Exchange_Rate')

def load_stock_data(stock_dict, is_large=True):
    clean_data = {}
    features = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 
                'SMA_5', 'SMA_60', 'Daily_Return', 'RSI_14', 'Vol_Ratio_5']
    if not is_large:
        features.append('Trading_Value')

    for name, code in stock_dict.items():
        try:
            df = fdr.DataReader(code, FETCH_START, FETCH_END)
            if df is None or df.empty: continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            df = safe_datetime_index(df)
            
            raw_df = pd.concat([df[['Close', 'Volume']], h_ex, h_vix, h_tnx, h_soxx], axis=1).ffill().bfill()
            if not is_large:
                raw_df['Trading_Value'] = raw_df['Close'] * raw_df['Volume']
            
            raw_df['SMA_5'] = raw_df['Close'].rolling(window=5).mean()
            raw_df['SMA_60'] = raw_df['Close'].rolling(window=60).mean()
            if is_large:
                raw_df['SMA_120'] = raw_df['Close'].rolling(window=120).mean()
            
            raw_df['Daily_Return'] = raw_df['Close'].pct_change()
            
            delta = raw_df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / (loss + 1e-9)
            raw_df['RSI_14'] = 100 - (100 / (1 + rs))
            raw_df['Vol_Ratio_5'] = raw_df['Volume'] / (raw_df['Volume'].rolling(5).mean() + 1e-9)
            
            raw_df['Target_5D'] = raw_df['Close'].pct_change(5).shift(-5)
            raw_df['Target_20D'] = raw_df['Close'].pct_change(20).shift(-20)
            
            for col in features + ['Target_5D', 'Target_20D']: 
                if col in raw_df.columns:
                    raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')
            clean_data[name] = raw_df
        except Exception as e:
            print(f"⚠️ 경고: {name}({code}) 로드 실패 ({e})")
    return clean_data

large_data = load_stock_data(large_stocks, is_large=True)
small_data = load_stock_data(small_mid_stocks, is_large=False)

# ==========================================
# 2. 연도별 통합 시뮬레이션 실행
# ==========================================
print("🚀 [Step 2/4] 코어-새틀라이트 통합 자산배분 시뮬레이션 실행 중...")
years_to_test = [2021, 2022, 2023, 2024, 2025]
yearly_performance_results = []

large_names = list(large_data.keys())
small_names = list(small_data.keys())

for target_year in tqdm(years_to_test, desc="연도별 통합 시뮬레이션"):
    start_date_str = f"{target_year}-01-01"
    end_date_str = f"{target_year}-12-31"
    
    # 모델 학습 (대형주: 5D/20D, 중소형주: 20D)
    models_l_5d, models_l_20d = {}, {}
    for n, rdf in large_data.items():
        tdf = rdf[rdf.index < start_date_str].dropna(subset=['Close', 'Target_5D', 'Target_20D'])
        if len(tdf) > 50:
            feat_cols = ['Close', 'Volume', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 'SMA_5', 'SMA_60', 'SMA_120', 'Daily_Return', 'RSI_14', 'Vol_Ratio_5']
            models_l_5d[n] = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.03, random_state=42).fit(tdf[feat_cols], tdf['Target_5D'])
            models_l_20d[n] = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.03, random_state=42).fit(tdf[feat_cols], tdf['Target_20D'])

    models_s_20d = {}
    for n, rdf in small_data.items():
        tdf = rdf[rdf.index < start_date_str].dropna(subset=['Close', 'Target_20D'])
        if len(tdf) > 50:
            feat_cols = ['Close', 'Volume', 'Trading_Value', 'Exchange_Rate', 'VIX_Fear_Index', 'US_10Y_Yield', 'Sector_SOXX', 'SMA_5', 'SMA_60', 'Daily_Return', 'RSI_14', 'Vol_Ratio_5']
            models_s_20d[n] = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.03, random_state=42).fit(tdf[feat_cols], tdf['Target_20D'])

    year_dates = pd.date_range(start_date_str, end_date_str, freq='B')
    actual_dates = large_data[large_names[0]].index
    valid_dates = [d for d in year_dates if d in actual_dates]
    if not valid_dates: continue
    
    # 벤치마크 (전체 자산 단순 일시불 B&H)
    cash_bnh = TOTAL_INITIAL_CASH
    port_bnh = {n: 0 for n in large_names + small_names}
    bnh_bought = False

    # 코어-새틀라이트 포트폴리오 초기화
    cash_core = TOTAL_INITIAL_CASH * CORE_RATIO
    port_core = {n: 0 for n in large_names}
    
    cash_sat = TOTAL_INITIAL_CASH * SAT_RATIO
    port_sat = {n: 0 for n in small_names}
    sat_avg_prices = {n: 0.0 for n in small_names}

    for step_idx, current_date in enumerate(valid_dates):
        # 활성 가격 수집
        l_prices, s_prices, s_tvals = {}, {}, {}
        for n, rdf in large_data.items():
            sub = rdf[rdf.index <= current_date]
            if not sub.empty and pd.notna(sub.iloc[-1]['Close']): l_prices[n] = sub.iloc[-1]['Close']
        for n, rdf in small_data.items():
            sub = rdf[rdf.index <= current_date]
            if not sub.empty and pd.notna(sub.iloc[-1]['Close']): 
                s_prices[n] = sub.iloc[-1]['Close']
                s_tvals[n] = sub.iloc[-1]['Trading_Value']

        if not l_prices or not s_prices: continue

        # 1. 벤치마크 B&H 집행
        if not bnh_bought:
            all_active = {**l_prices, **s_prices}
            alloc = cash_bnh / len(all_active)
            for n, p in all_active.items():
                q = int(alloc // (p * (1 + BUY_FEE)))
                if q > 0: port_bnh[n] += q; cash_bnh -= q * p * (1 + BUY_FEE)
            bnh_bought = True

        # 2. 새틀라이트(중소형주) 일일 긴급 손절 검증 (-15%)
        for n in list(port_sat.keys()):
            if port_sat.get(n, 0) > 0 and n in s_prices:
                p = s_prices[n]
                bp = sat_avg_prices[n]
                if bp > 0 and (p - bp) / bp <= -0.15:
                    cash_sat += port_sat[n] * p * (1 - SELL_FEE)
                    port_sat[n] = 0
                    sat_avg_prices[n] = 0.0

        # 3. 코어(대형주) 월간(20영업일) 리밸런싱
        if step_idx % 20 == 0:
            scores_l = {}
            for n, p in l_prices.items():
                sub = large_data[n][large_data[n].index <= current_date]
                if not sub.empty and models_l_20d.get(n) is not None:
                    td = sub.iloc[-1]
                    feat_v = [td['Close'], td['Volume'], td['Exchange_Rate'], td['VIX_Fear_Index'], td['US_10Y_Yield'], td['Sector_SOXX'], td['SMA_5'], td['SMA_60'], td['SMA_120'], td['Daily_Return'], td['RSI_14'], td['Vol_Ratio_5']]
                    p5 = models_l_5d[n].predict(np.array(feat_v).reshape(1, -1))[0]
                    p20 = models_l_20d[n].predict(np.array(feat_v).reshape(1, -1))[0]
                    vix = td['VIX_Fear_Index']
                    sma120 = td['SMA_120']
                    if vix >= 32: scores_l[n] = 0.0
                    else:
                        blend = (p5 * 0.2) + (p20 * 0.8)
                        ai_s = np.clip((blend + 0.01) / 0.06, 0.1, 1.0)
                        tr_s = np.clip(p / sma120, 0.5, 1.0) if sma120 > 0 else 1.0
                        scores_l[n] = ai_s * tr_s

            total_v_core = cash_core + sum(port_core[n]*l_prices[n] for n in port_core if n in l_prices)
            sum_s_l = sum(scores_l.values())
            target_w_l = {n: (scores_l.get(n, 0.0) / sum_s_l if sum_s_l > 0 else 0.0) for n in l_prices}

            for n, w in target_w_l.items():
                if n in port_core:
                    p = l_prices[n]
                    curr_amt = port_core[n] * p
                    curr_w = curr_amt / total_v_core if total_v_core > 0 else 0
                    if abs(curr_w - w) >= 0.05 or w == 0.0:
                        tar_amt = total_v_core * w
                        if curr_amt > tar_amt:
                            sq = port_core[n] if w == 0.0 else int((curr_amt - tar_amt) // p)
                            if 0 < sq <= port_core[n]:
                                port_core[n] -= sq; cash_core += sq * p * (1 - SELL_FEE)
                        elif tar_amt > curr_amt:
                            bq = int((tar_amt - curr_amt) // p)
                            cost = bq * p * (1 + BUY_FEE)
                            while cost > cash_core and bq > 0:
                                bq -= 1; cost = bq * p * (1 + BUY_FEE)
                            if bq > 0:
                                port_core[n] += bq; cash_core -= cost

        # 4. 새틀라이트(중소형주) 반기별(120영업일) 리밸런싱 (상위 5개 모멘텀)
        if step_idx % 120 == 0:
            scores_s = {}
            for n, p in s_prices.items():
                if s_tvals.get(n, 0) < 10_000_000_000: continue
                sub = small_data[n][small_data[n].index <= current_date]
                if not sub.empty and models_s_20d.get(n) is not None:
                    td = sub.iloc[-1]
                    feat_v = [td['Close'], td['Volume'], td['Trading_Value'], td['Exchange_Rate'], td['VIX_Fear_Index'], td['US_10Y_Yield'], td['Sector_SOXX'], td['SMA_5'], td['SMA_60'], td['Daily_Return'], td['RSI_14'], td['Vol_Ratio_5']]
                    p20 = models_s_20d[n].predict(np.array(feat_v).reshape(1, -1))[0]
                    v_boost = np.clip(td['Vol_Ratio_5'], 0.5, 3.0)
                    scores_s[n] = p20 * v_boost

            top_s = sorted(scores_s, key=scores_s.get, reverse=True)[:5]
            target_w_s = {n: (1.0 / 5 if n in top_s else 0.0) for n in s_prices}
            total_v_sat = cash_sat + sum(port_sat[n]*s_prices[n] for n in port_sat if n in s_prices)

            for n, w in target_w_s.items():
                if n in port_sat:
                    p = s_prices[n]
                    curr_amt = port_sat[n] * p
                    tar_amt = total_v_sat * w
                    if curr_amt > tar_amt * 1.03 or w == 0.0:
                        sq = port_sat[n] if w == 0.0 else int((curr_amt - tar_amt) // p)
                        if 0 < sq <= port_sat[n]:
                            port_sat[n] -= sq; cash_sat += sq * p * (1 - SELL_FEE)
                            if port_sat[n] == 0: sat_avg_prices[n] = 0.0

            for n, w in target_w_s.items():
                if n in port_sat and w > 0:
                    p = s_prices[n]
                    curr_amt = port_sat[n] * p
                    tar_amt = total_v_sat * w
                    if tar_amt > curr_amt * 1.03:
                        bq = int((tar_amt - curr_amt) // p)
                        cost = bq * p * (1 + BUY_FEE)
                        while cost > cash_sat and bq > 0:
                            bq -= 1; cost = bq * p * (1 + BUY_FEE)
                        if bq > 0:
                            oq = port_sat[n]
                            obp = sat_avg_prices[n]
                            port_sat[n] += bq; cash_sat -= cost
                            if oq + bq > 0:
                                sat_avg_prices[n] = ((oq * obp) + (bq * p)) / (oq + bq)

    # --- 연도말 최종 정산 ---
    final_l_prices = {n: large_data[n].loc[large_data[n].index <= valid_dates[-1]].iloc[-1]['Close'] for n in large_names}
    final_s_prices = {n: small_data[n].loc[small_data[n].index <= valid_dates[-1]].iloc[-1]['Close'] for n in small_names}
    
    val_bnh = cash_bnh + sum(port_bnh[n] * final_l_prices[n] for n in large_names if n in final_l_prices) + sum(port_bnh[n] * final_s_prices[n] for n in small_names if n in final_s_prices)
    val_core = cash_core + sum(port_core[n] * final_l_prices[n] for n in port_core if n in final_l_prices)
    val_sat = cash_sat + sum(port_sat[n] * final_s_prices[n] for n in port_sat if n in final_s_prices)
    val_hybrid_total = val_core + val_sat

    ret_bnh = ((val_bnh / TOTAL_INITIAL_CASH) - 1) * 100
    ret_hybrid = ((val_hybrid_total / TOTAL_INITIAL_CASH) - 1) * 100

    yearly_performance_results.append({
        'Year': target_year,
        '1. 전체 일시불 (Buy & Hold)': ret_bnh,
        '2. 코어-새틀라이트 통합 퀀트': ret_hybrid
    })

# ==========================================
# 3. 최종 리포트 출력
# ==========================================
print("\n" + "="*85)
print("🏆 [코어-새틀라이트 통합 자산배분] 5개년 연도별 전략 수익률(%) 비교 리포트")
print("="*85)
df_perf = pd.DataFrame(yearly_performance_results).set_index('Year')
print(df_perf.applymap(lambda x: f"{x:+.2f}%"))
print("="*85)

avg_returns = df_perf.mean()
print("\n" + "="*85)
print("📊 5개년 평균 수익률 및 최종 종합 순위")
print("="*85)
final_ranking = avg_returns.sort_values(ascending=False)
for idx, (strat, avg_ret) in enumerate(final_ranking.items(), 1):
    print(f"[{idx}위] {strat:<40} | 5개년 평균 수익률: {avg_ret:+.2f}%")
print("="*85)
