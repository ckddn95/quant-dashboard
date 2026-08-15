import requests
import json
from datetime import datetime

def get_kis_access_token(app_key, app_secret, is_mock=True):
    if not app_key or not app_secret: return None, "키 누락"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200: return res.json().get("access_token"), "OK"
        return None, f"토큰 발급 실패: {res.text}"
    except Exception as e: return None, str(e)

# 🛑 [핵심 패치 2] 가격 외에 거래정지(is_halted) 여부 반환
def fetch_kis_current_price_ext(app_key, app_secret, ticker, token, is_mock=True):
    if not token: return 0.0, False
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).strip().zfill(6)}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0':
            out = res.json()['output']
            # 51: 매매거래정지, 57: 매매정지
            is_halted = out.get('iscd_stat_cls_code') in ['51', '57'] 
            return float(out['stck_prpr']), is_halted
    except: pass
    return 0.0, False

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    if not token: return None, None, "토큰 누락"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-balance"
    tr_id = "VTTC8434R" if is_mock else "TTTC8434R"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    params = {"CANO": str(cano).strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            rj = res.json()
            if rj.get('rt_cd') == '0': return rj.get('output1', []), rj.get('output2', []), "OK"
            return None, None, rj.get('msg1')
    except Exception as e: return None, None, str(e)
    return None, None, "HTTP 에러"

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    if not token: return 0.0
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    tr_id = "VTTC8908R" if is_mock else "TTTC8908R"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    params = {"CANO": str(cano).strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return float(res.json().get('output', {}).get('ord_psbl_cash', 0))
    except: pass
    return 0.0

def execute_kis_order(app_key, app_secret, cano, acnt_prdt_cd, token, ticker, is_buy, qty, price=0, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = "VTTC0802U" if is_buy else "VTTC0801U"
    if not is_mock: tr_id = "TTTC0802U" if is_buy else "TTTC0801U"
    
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    ord_dvsn = "01" if price == 0 else "00"
    unpr = "0" if price == 0 else str(int(price))
    
    body = {"CANO": str(cano).strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "PDNO": str(ticker).zfill(6), "ORD_DVSN": ord_dvsn, "ORD_QTY": str(int(qty)), "ORD_UNPR": unpr}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        rj = res.json()
        if res.status_code == 200 and rj.get('rt_cd') == '0':
            return True, "OK", rj['output']['ODNO'], rj['output']['KRX_FWDG_ORD_ORGNO'], rj.get('rt_cd')
        return False, rj.get('msg1', '에러'), None, None, rj.get('rt_cd')
    except Exception as e: return False, str(e), None, None, "EXCEPTION"

def fetch_kis_order_executions(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True):
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    tr_id = "VTTC8001R" if is_mock else "TTTC8001R"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    today = datetime.now().strftime("%Y%m%d")
    params = {"CANO": str(cano).strip()[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).strip().zfill(2), "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_FIID_COND": "", "INQR_FIID_DATA": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200 and res.json().get('rt_cd') == '0':
            exec_dict = {}
            for order in res.json().get('output1', []):
                odno = order.get('odno')
                if odno: exec_dict[odno] = {'cum_qty': int(order.get('tot_ccld_qty', 0)), 'avg_price': float(order.get('avg_prvs', 0))}
            return exec_dict
    except: pass
    return {}
