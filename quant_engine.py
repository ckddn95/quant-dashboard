import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import datetime
import warnings

warnings.filterwarnings('ignore')

KST = datetime.timezone(datetime.timedelta(hours=9))

def load_krx_universe():
    """KRX 전체 종목을 가져옵니다. 통신 장애 시 3중 우회합니다."""
    try: 
        df = fdr.StockListing('KRX')
        if not df.empty and 'Code' in df.columns and 'Name' in df.columns:
            return df.dropna(subset=['Code', 'Name'])
    except: 
        pass
        
    try:
        df_kpi = fdr.StockListing('KOSPI')
        df_kdq = fdr.StockListing('KOSDAQ')
        
        if not df_kpi.empty and not df_kdq.empty:
            if 'Market' not in df_kpi.columns: df_kpi['Market'] = 'KOSPI'
            if 'Market' not in df_kdq.columns: df_kdq['Market'] = 'KOSDAQ'
            
            df = pd.concat([df_kpi, df_kdq], ignore_index=True)
            if not df.empty and 'Code' in df.columns and 'Name' in df.columns:
                return df.dropna(subset=['Code', 'Name'])
    except:
        pass
        
    return pd.DataFrame()

def fetch_market_data():
    """VIX 공포지수 및 코스피/코스닥 모멘텀을 가져옵니다."""
    try:
        k_close = fdr.DataReader('KS11')['Close'].tail(61)
        kospi_ret_60 = ((float(k_close.iloc[-1]) / float(k_close.iloc[-60])) - 1) * 100 if len(k_close) >= 60 else 0.0
        kq_close = fdr.DataReader('KQ11')['Close'].tail(61)
        kosdaq_ret_60 = ((float(kq_close.iloc[-1]) / float(kq_close.iloc[-60])) - 1) * 100 if len(kq_close) >= 60 else 0.0
    except:
        kospi_ret_60, kosdaq_ret_60 = 0.0, 0.0
        
    try:
        vix_df = yf.download("^VIX", period="3mo", progress=False)
        if vix_df.empty: raise ValueError("VIX Data Empty")
        vix_close = vix_df['Close'].dropna()
        vix_val, vix_ma3 = float(vix_close.iloc[-1]), float(vix_close.rolling(3).mean().iloc[-1])
        return vix_val, (vix_val >= 25.0) and (vix_val < vix_ma3), (vix_val < 30.0), kospi_ret_60, kosdaq_ret_60
    except:
        try:
            k_20 = fdr.DataReader('KS11')['Close'].tail(20)
            k_dd = (k_20.iloc[-1] / k_20.max()) - 1
            v_safe = True if k_dd > -0.10 else False 
            v_con = True if k_dd <= -0.15 else False 
            return 20.0, v_con, v_safe, kospi_ret_60, kosdaq_ret_60
        except: return 20.0, False, True, kospi_ret_60, kosdaq_ret_60

def fetch_stock_status(ticker_code):
    """특정 종목의 기술적 지표(이동평균, 거래량 감쇄 등)를 계산합니다."""
    try:
        tc = str(ticker_code).strip().zfill(6)
        start_dt = (datetime.datetime.now(KST).date() - datetime.timedelta(days=730)).strftime('%Y-%m-%d')
        
        df = fdr.DataReader(tc, start=start_dt)
        if not df.empty and len(df) > 0:
            close_p, vol = df['Close'].dropna(), df['Volume'].dropna()
            low_p = df['Low'].dropna() if 'Low' in df.columns else close_p
            if len(close_p) == 0: return None
            
            y_p, y_l = float(close_p.iloc[-1]), float(low_p.iloc[-1])
            ma200 = float(close_p.rolling(200).mean().iloc[-1]) if len(close_p) >= 200 else y_p
            ma60 = float(close_p.rolling(60).mean().iloc[-1]) if len(close_p) >= 60 else y_p
            ma60_10 = float(close_p.rolling(60).mean().iloc[-11]) if len(close_p) >= 70 else ma60
            ma20 = float(close_p.rolling(20).mean().iloc[-1]) if len(close_p) >= 20 else y_p
            
            tail_120 = close_p.tail(120)
            rh = float(tail_120.max())
            dd = ((y_p / rh) - 1) * 100 if rh > 0 else 0.0
            
            days_since_peak = len(tail_120) - 1 - int(np.argmax(tail_120.values))
            
            vol_5ma = float(vol.tail(6).iloc[:-1].mean()) if len(vol) >= 6 else float(vol.iloc[-1])
            avg_trade_val = vol_5ma * y_p 
            
            peak_vol_20 = float(vol.tail(20).max())
            vol_contraction = float(vol_5ma / peak_vol_20) if peak_vol_20 > 0 else 1.0
            
            vr = (float(vol.iloc[-1]) / vol_5ma * 100) if vol_5ma > 0 else 100.0
            r60 = ((y_p / float(close_p.iloc[-60])) - 1) * 100 if len(close_p) >= 60 else 0.0
            r20 = ((y_p / float(close_p.iloc[-20])) - 1) * 100 if len(close_p) >= 20 else 0.0
            vr_s = pd.Series(np.where(vol.rolling(5).mean().shift(1) > 0, vol / vol.rolling(5).mean().shift(1) * 100, 100.0), index=vol.index)
            rvm = float(vr_s.tail(20).max())
            
            return (y_p, ma200, ma60, ma20, dd, vr, r60, r20, (ma60 > ma60_10), (y_p >= ma200), rvm >= 200.0, y_l, rvm, avg_trade_val, days_since_peak, vol_contraction)
    except: pass
    return None

def analyze_quant_strategy(strat_name, c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, ret_60, ret_20, ma60_slope_positive, drawdown, vol_surged, recent_vol_max, vix_safe, vix_contrarian, use_ma200_filter, buf_pct, ts_target_pct, ts_drop_pct, sat_stop_loss_pct, days_since_peak, vol_contraction):
    """지표를 바탕으로 최종 매수/매도/홀딩 여부와 스코어를 판별합니다."""
    buf = buf_pct / 100.0 if buf_pct else 0.0
    sat_stop_loss = sat_stop_loss_pct / 100.0 if sat_stop_loss_pct else -0.15
    ts_target = ts_target_pct / 100.0 if ts_target_pct else 0.30
    ts_drop = ts_drop_pct / 100.0 if ts_drop_pct else -0.05
    
    user_ret = ((c_price / buy_price) - 1) if buy_price > 0 else 0.0
    diff_ma = ((ma20 / ma60) - 1) if ma60 > 0 else 0.0
    dist_ma20 = ((c_price / ma20) - 1) if ma20 > 0 else 0.0
    
    is_above_ma200 = (c_price >= ma200)
    ma200_cond = is_above_ma200 if use_ma200_filter else True
    current_low = min(yf_low, c_price)
    
    is_ts_active = (user_ret >= ts_target)
    drawdown_from_high = ((c_price / highest_price) - 1) if highest_price > 0 else 0.0
    trailing_stop_triggered = is_ts_active and (drawdown_from_high <= ts_drop)
    
    res = {
        'ai_score': 0.0, 'entry_cond': False, 'exit_cond_trend': False,
        'stop_loss_cond': False, 'trailing_stop_cond': trailing_stop_triggered,
        'diff_ma_pct': diff_ma * 100, 'dist_ma20_pct': dist_ma20 * 100,
        'user_ret_pct': user_ret * 100, 'is_above_ma200': is_above_ma200,
        'vol_surged': vol_surged, 'drawdown': drawdown,
        'days_since_peak': days_since_peak, 'vol_contraction': vol_contraction
    }
    
    if strat_name == '대형주 (Core)':
        res['ai_score'] = round((ret_20 * 0.5) + (ret_60 * 0.3) + (diff_ma * 100 * 0.2), 2)
        res['entry_cond'] = is_above_ma200 and ((ma20 >= ma60 * (1 + buf) and ma60_slope_positive and ret_20 > 0 and vix_safe) or vix_contrarian)
        res['exit_cond_trend'] = (ma20 < ma60 * (1 - buf/2)) and not vix_contrarian 
        res['stop_loss_cond'] = (user_ret <= -0.15) 
    else: 
        res['ai_score'] = round((recent_vol_max / 100.0) * 0.4 + (ret_60 * 0.3) + (ret_20 * 0.3), 2)
        is_dip = (-0.05 <= dist_ma20 <= 0.03) or (current_low <= ma20 * 1.01)
        sat_normal_buy = is_dip and vol_surged and (days_since_peak <= 45) and (vol_contraction <= 0.50)
        res['entry_cond'] = is_above_ma200 and (sat_normal_buy or vix_contrarian) and drawdown >= -0.30 and ma60_slope_positive and ret_20 > -0.03
        res['exit_cond_trend'] = (c_price < ma20 * (1 - buf/2)) and not vix_contrarian 
        res['stop_loss_cond'] = (user_ret <= sat_stop_loss)
        
    return res
