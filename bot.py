import streamlit as st  # 비밀키(secrets)를 쉽게 불러오기 위해 사용
import pandas as pd
import json
import datetime
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
import quant_engine as qe  # [핵심!] 우리가 만든 공통 두뇌를 임포트
import warnings

warnings.filterwarnings('ignore')
KST = datetime.timezone(datetime.timedelta(hours=9))
SPREADSHEET_ID = "1hFPs2y8UipaWHfM_VVgAqsq566HnHQLBONSwBX28TQ0"

# ==========================================
# 1. 헬퍼 함수 (통신 및 인증)
# ==========================================
def get_gspread_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(dict(st.secrets["google_sheets_json"]), scopes=scopes)
    return gspread.authorize(creds)

def send_telegram_message(message):
    try:
        tg_token = st.secrets.get("telegram", {}).get("bot_token")
        tg_chat_id = st.secrets.get("telegram", {}).get("chat_id")
        if not tg_token or not tg_chat_id: return False
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        res = requests.post(url, json={"chat_id": tg_chat_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
        return res.status_code == 200
    except: return False

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-balance"
    tr_id = "VTTC8434R" if is_mock else "TTTC8434R"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    params = {"CANO": str(cano).replace("-", "").strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return res.json().get('output1', []), res.json().get('output2', [])
    except: pass
    return None, None

def execute_kis_order(app_key, app_secret, token, cano, acnt_prdt_cd, ticker, qty, price, order_type="BUY", is_market=False, is_mock=True):
    if int(qty) <= 0: return False, "주문 수량 오류"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = ("VTTC0802U" if order_type == "BUY" else "VTTC0801U") if is_mock else ("TTTC0802U" if order_type == "BUY" else "TTTC0801U")
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id}
    body = {"CANO": str(cano).replace("-", "").strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "PDNO": str(ticker).strip().zfill(6), "ORD_DVSN": "01" if is_market else "00", "ORD_QTY": str(int(qty)), "ORD_UNPR": "0" if is_market else str(int(price))}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200:
            rj = res.json()
            if rj.get('rt_cd') == '0': return True, rj.get('msg1')
            else: return False, rj.get('msg1')
    except Exception as e: return False, str(e)

def log_daily_trade(p_data, s_name, order_type, price, qty, buy_price=0.0, status="✅ 체결완료", msg=""):
    today_str = datetime.datetime.now(KST).strftime('%Y-%m-%d')
    now_str = datetime.datetime.now(KST).strftime('%H:%M:%S')
    if p_data.get('daily_trades_date') != today_str:
        p_data['daily_trades'] = []
        p_data['daily_trades_date'] = today_str
    
    pnl = (price - buy_price) * qty if order_type == "SELL" else 0.0
    p_data.setdefault('daily_trades', []).append({
        '체결 시간': now_str, '종목명': s_name, '주문 구분': '매도(청산)' if order_type == "SELL" else '매수(진입)',
        '상태': status, '체결 단가': price, '체결 수량': qty, '체결 금액': price * qty, '실현 손익': pnl, '비고 (API 메시지)': msg
    })
    return p_data

# ==========================================
# 2. 메인 루프 (무한 반복하며 1분마다 감시)
# ==========================================
def run_bot():
    print(f"🤖 Core-Satellite 무인 매매 봇 가동 시작: {datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")
    send_telegram_message("🤖 *[시스템 알림]* 백그라운드 매매 엔진이 정상적으로 가동되었습니다.")
    
    last_run_time = {} # 포트폴리오별 마지막 실행 시간 기록

    while True:
        try:
            now = datetime.datetime.now(KST)
            client = get_gspread_client()
            worksheet = client.open_by_key(SPREADSHEET_ID).worksheet("Portfolios")
            records = worksheet.get_all_records()
            
            # 시장 공통 지표 1회 로드 (VIX, 시장 방향성)
            vix_val, vix_contrarian, vix_safe, kospi_ret_60, kosdaq_ret_60 = qe.fetch_market_data()
            
            for r in records:
                port_name = r.get("Name")
                try: p_data = json.loads(r.get("JSON_Data"))
                except: continue
                
                # 1. 제어 스위치 검증
                if p_data.get('kill_switch', False) or not p_data.get('auto_pilot', False) or not p_data.get('auto_trade_enabled', False):
                    continue # 멈춰있으면 패스
                    
                ap_min = p_data.get('ap_min', 10)
                
                # 2. 감시 주기가 도래했는지 확인
                last_time = last_run_time.get(port_name)
                if last_time and (now - last_time).total_seconds() < (ap_min * 60):
                    continue # 아직 주기가 안 됨
                    
                print(f"[{now.strftime('%H:%M:%S')}] '{port_name}' 포트폴리오 스캔 시작...")
                last_run_time[port_name] = now # 실행 시간 갱신
                
                # 3. KIS 계좌 정보 연동
                active_strat = p_data.get('strategy', '대형주 (Core)')
                kis_key = "core" if active_strat == "대형주 (Core)" else "satellite"
                k_data = st.secrets.get("kis_accounts", {}).get(kis_key, None)
                if not k_data: continue
                    
                app_key, app_secret = k_data.get("app_key"), k_data.get("app_secret")
                cano, acnt_prdt, is_mock = str(k_data.get("cano")), str(k_data.get("acnt_prdt", "01")), k_data.get("is_mock", False)
                token = p_data.get(f"kis_token_{app_key[-6:]}")
                if not token: continue

                holdings, summary = fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt, token, is_mock)
                if not summary: continue
                    
                real_total_eval = float(summary[0].get('tot_evlu_amt', 0))
                real_stocks = {str(i.get('pdno')).strip().zfill(6): i for i in holdings if int(i.get('hldg_qty', 0)) > 0}
                
                # 가용 현금 계산 (총평가금 - 주식평가금액)
                stocks_eval_sum = sum(float(v.get('prpr', 0)) * int(v.get('hldg_qty', 0)) for v in real_stocks.values())
                real_cash_avail = real_total_eval - stocks_eval_sum

                # 전략 파라미터 (고정값 또는 시트에서 로드)
                max_alloc_pct = 35 if active_strat == '대형주 (Core)' else 20
                is_bull = (active_strat == '대형주 (Core)' and kospi_ret_60 > 0) or (active_strat != '대형주 (Core)' and kosdaq_ret_60 > 0)
                if is_bull: max_alloc_pct = min(max_alloc_pct * 1.5, 100.0)
                
                target_buy_amt = real_total_eval * (max_alloc_pct / 100.0)
                
                exec_msgs = []
                needs_save = False
                
                # [정책 4] 수동 보유 종목 보호를 위한 봇 관리 리스트
                bot_managed = [str(s.get('티커')).strip().zfill(6) for s in p_data.get('stocks', [])]

                # ------------------------------------
                # [A] 매도 스캔 (보유 종목 검사)
                # ------------------------------------
                for ticker, r_data in list(real_stocks.items()):
                    if ticker not in bot_managed: continue # 봇 관리 종목이 아니면 무시 (수동 종목 보호)
                        
                    qty = int(r_data.get('hldg_qty', 0))
                    buy_price = float(r_data.get('pchs_avg_pric', 0))
                    c_price = float(r_data.get('prpr', 0))
                    s_name = r_data.get('prdt_name', ticker)
                    
                    if qty == 0 or buy_price == 0 or c_price == 0: continue

                    highest_price = p_data.get('ts_tracker', {}).get(ticker, buy_price)
                    if c_price > highest_price:
                        highest_price = c_price
                        p_data.setdefault('ts_tracker', {})[ticker] = highest_price
                        needs_save = True

                    res = qe.fetch_stock_status(ticker)
                    if not res: continue
                    yf_price, ma200, ma60, ma20, drawdown, vr, r60, r20, m60_up, is_a200, vs, yf_low, rvm, atv, dsp, vc = res
                    
                    # 공통 두뇌 로직 판독
                    res_q = qe.analyze_quant_strategy(
                        active_strat, c_price, buy_price, highest_price, ma200, ma60, ma20, yf_low, 
                        r60, r20, m60_up, drawdown, vs, rvm, vix_safe, vix_contrarian, 
                        True, 1.5, 30.0, -10.0, -15.0, dsp, vc
                    )
                    
                    is_sell, sell_reason = False, ""
                    if res_q['stop_loss_cond']: is_sell, sell_reason = True, "🔴 긴급손절"
                    elif res_q['trailing_stop_cond']: is_sell, sell_reason = True, "🔵 트레일링 익절"
                    elif res_q['exit_cond_trend']: is_sell, sell_reason = True, "🔴 추세이탈 매도"
                        
                    if is_sell:
                        succ, msg = execute_kis_order(app_key, app_secret, token, cano, acnt_prdt, ticker, qty, c_price, "SELL", True, is_mock)
                        if succ:
                            p_data = log_daily_trade(p_data, s_name, "SELL", c_price, qty, buy_price, "✅ 체결완료", msg)
                            exec_msgs.append(f"[{sell_reason}] *{s_name}* 전량 매도 완료")
                            real_cash_avail += (c_price * qty)
                            
                            pnl = (c_price - buy_price) * qty
                            cd_i = p_data.setdefault('cd_tracker', {}).get(ticker, {'losses': 0, 'until': '2000-01-01'})
                            if pnl < 0:
                                cd_i['losses'] += 1
                                if cd_i['losses'] >= 2:
                                    cd_i['until'] = (now.date() + datetime.timedelta(days=60)).strftime('%Y-%m-%d')
                            else: cd_i['losses'] = 0
                            p_data['cd_tracker'][ticker] = cd_i
                            if ticker in p_data.get('ts_tracker', {}): del p_data['ts_tracker'][ticker]
                            needs_save = True

                # ------------------------------------
                # [B] 매수 스캔 (관심 종목 검사)
                # ------------------------------------
                queue = []
                for s in p_data.get('stocks', []):
                    ticker = str(s['티커']).strip().zfill(6)
                    s_name = s.get('종목명', ticker)
                    
                    c_price = qe.fetch_kis_current_price(app_key, app_secret, ticker, token, is_mock)
                    res = qe.fetch_stock_status(ticker)
                    if not res or not c_price or c_price == 0: continue
                    yf_price, ma200, ma60, ma20, dd, vr, r60, r20, m60_up, is_a200, vs, yf_low, rvm, atv, dsp, vc = res
                    
                    if atv < 5000000000: continue # 거래대금 미달 패스
                    
                    cd_info = p_data.get('cd_tracker', {}).get(ticker, {'until': '2000-01-01'})
                    if now.date() < datetime.datetime.strptime(cd_info['until'], '%Y-%m-%d').date(): continue # 쿨다운 패스
                        
                    res_q = qe.analyze_quant_strategy(
                        active_strat, c_price, 0.0, 0.0, ma200, ma60, ma20, yf_low, 
                        r60, r20, m60_up, dd, vs, rvm, vix_safe, vix_contrarian, 
                        True, 1.5, 30.0, -10.0, -15.0, dsp, vc
                    )
                    
                    if res_q['entry_cond']:
                        held_qty = int(real_stocks.get(ticker, {}).get('hldg_qty', 0))
                        current_holding_amt = held_qty * c_price
                        add_amt = max(0, target_buy_amt - current_holding_amt)
                        add_qty = int(add_amt // c_price)
                        
                        if add_qty > 0 and (add_qty * c_price) <= real_total_eval:
                            queue.append({'score': res_q['ai_score'], 'ticker': ticker, 'name': s_name, 'qty': add_qty, 'price': c_price})
                            
                # 매수 큐 점수순 정렬 후 자금 분배
                queue = sorted(queue, key=lambda x: x['score'], reverse=True)
                for q in queue:
                    aff_qty = int(real_cash_avail // q['price'])
                    final_qty = min(q['qty'], aff_qty)
                    if final_qty > 0:
                        succ, msg = execute_kis_order(app_key, app_secret, token, cano, acnt_prdt, q['ticker'], final_qty, q['price'], "BUY", False, is_mock)
                        if succ:
                            p_data = log_daily_trade(p_data, q['name'], "BUY", q['price'], final_qty, 0.0, "✅ 체결완료", msg)
                            exec_msgs.append(f"🟢 [신규/추가매수] *{q['name']}* {final_qty}주")
                            real_cash_avail -= (final_qty * q['price'])
                            needs_save = True
                            
                # 시트 업데이트 및 텔레그램 발송
                if needs_save:
                    data_str = json.dumps(p_data, ensure_ascii=False)
                    cell = worksheet.find(port_name)
                    worksheet.update_cell(cell.row, 2, data_str)
                    
                if exec_msgs and p_data.get('tg_noti_order', True):
                    send_telegram_message("🤖 *[백그라운드 봇 매매 실행]*\n" + "\n".join(exec_msgs))

        except Exception as e:
            print(f"[{datetime.datetime.now(KST)}] 스캔 중 오류 발생: {e}")
            
        # 1분(60초)간 휴식 후 무한 루프
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
