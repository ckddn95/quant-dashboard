import requests
import json
import time
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

def get_base_url(is_mock):
    return "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"

def _safe_get(url, headers, params=None, max_retries=3):
    """✅ 4.2항: 예외/HTTP 상태를 세분화하여 Dict(Typed Result) 형태로 안전 반환"""
    for attempt in range(max_retries):
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 429: # Rate Limit
                if attempt == max_retries - 1: return {"status": "RATE_LIMIT", "msg": "Rate limit exceeded (429)", "raw": None}
                time.sleep(0.5 * (2 ** attempt))
                continue
            if res.status_code in [401, 403]:
                return {"status": "UNAUTHORIZED", "msg": f"Token expired or unauthorized ({res.status_code})", "raw": None}
            try:
                data = res.json()
                return {"status": "SUCCESS", "msg": "OK", "raw": res, "data": data}
            except ValueError:
                return {"status": "INVALID_JSON", "msg": "Failed to parse JSON response", "raw": res}
        except requests.exceptions.Timeout:
            if attempt == max_retries - 1: return {"status": "TIMEOUT", "msg": "Connection timed out", "raw": None}
            time.sleep(0.5 * (2 ** attempt))
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1: return {"status": "HTTP_ERROR", "msg": str(e), "raw": None}
            time.sleep(0.5 * (2 ** attempt))
    return {"status": "UNKNOWN", "msg": "Unknown Error", "raw": None}

def _strict_post(url, headers, data):
    """🚨 POST는 이중지출 방지를 위해 절대 재시도하지 않음. 예외 세분화 반환"""
    try:
        res = requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
        if res.status_code == 429: return {"status": "RATE_LIMIT", "msg": "Rate limit exceeded", "data": {}}
        if res.status_code in [401, 403]: return {"status": "UNAUTHORIZED", "msg": "Token expired", "data": {}}
        try: return {"status": "SUCCESS", "data": res.json()}
        except ValueError: return {"status": "INVALID_JSON", "msg": "Invalid JSON response", "data": {}}
    except requests.exceptions.Timeout: return {"status": "TIMEOUT", "msg": "Connection timed out", "data": {}}
    except requests.exceptions.RequestException as e: return {"status": "HTTP_ERROR", "msg": str(e), "data": {}}

def get_kis_access_token(app_key, app_secret, is_mock=True):
    url = f"{get_base_url(is_mock)}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    data = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    res = _strict_post(url, headers=headers, data=data)
    if res['status'] == "SUCCESS" and 'access_token' in res['data']:
        return res['data']['access_token'], "OK"
    return None, f"Token Error: {res.get('msg', 'Failed to acquire token')}"

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt, token, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8434R" if is_mock else "TTTC8434R"}
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    res = _safe_get(url, headers=headers, params=params)
    if res['status'] == "SUCCESS":
        data = res['data']
        if data.get('rt_cd') == '0':
            return {"status": "SUCCESS", "holdings": data.get('output1', []), "summary": data.get('output2', []), "msg": "OK"}
        return {"status": "API_ERROR", "holdings": [], "summary": [], "msg": data.get('msg1', 'Unknown API Error')}
    return {"status": res['status'], "holdings": [], "summary": [], "msg": res['msg']}

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt, token, ticker, price, order_kind, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8908R" if is_mock else "TTTC8908R"}
    dvsn = "01" if order_kind == "MARKET" else "00"
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": ticker, "ORD_UNPR": str(int(price)), "ORD_DVSN": dvsn, "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    res = _safe_get(url, headers=headers, params=params)
    if res['status'] == "SUCCESS" and res['data'].get('rt_cd') == '0':
        return float(res['data'].get('output', {}).get('ord_psbl_cash', 0))
    return 0.0

def fetch_kis_current_price_ext(app_key, app_secret, ticker, token, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    received_at = datetime.now(KST)
    
    res = _safe_get(url, headers=headers, params=params, max_retries=2)
    if res['status'] == "SUCCESS":
        data = res['data']
        if data.get('rt_cd') == '0' and data.get('output'):
            out = data['output']
            broker_time_str = out.get('stck_bsop_date', '') + out.get('stck_cntg_hour', '')
            try:
                broker_time = datetime.strptime(broker_time_str, "%Y%m%d%H%M%S").replace(tzinfo=KST)
                freshness_sec = (received_at - broker_time).total_seconds()
            except:
                broker_time = received_at
                freshness_sec = 0.0
            
            is_halted = out.get('iscd_stat_cls_code') in ['51', '52', '53', '55'] 
            return {
                "status": "SUCCESS", "ticker": ticker, "exchange": "KRX",
                "price": float(out.get('stck_prpr', 0)), "high": float(out.get('stck_hgpr', 0)), "low": float(out.get('stck_lwpr', 0)),
                "broker_time": broker_time, "received_at": received_at,
                "source": "KIS", "is_halted": is_halted, "freshness_sec": freshness_sec,
                "executable": not is_halted, "msg": "OK"
            }
        return {"status": "API_ERROR", "msg": data.get('msg1', 'API Error'), "executable": False}
    return {"status": res['status'], "msg": res['msg'], "executable": False}

def fetch_daily_executions_0081(app_key, app_secret, cano, acnt_prdt, token, is_mock=True, order_date=""):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8001R" if is_mock else "TTTC8001R"}
    if not order_date: order_date = time.strftime('%Y%m%d')
    
    executions = []
    ctx_area_fk100, ctx_area_nk100 = "", ""
    max_pages = 20
    page = 0
    
    while page < max_pages:
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "INQR_STRT_DT": order_date, "INQR_END_DT": order_date,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "01",
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
            "CTX_AREA_FK100": ctx_area_fk100, "CTX_AREA_NK100": ctx_area_nk100
        }
        res = _safe_get(url, headers=headers, params=params)
        if res['status'] != "SUCCESS": break
        
        data = res['data']
        if data.get('rt_cd') != '0': break
        executions.extend(data.get('output1', []))
        
        if res['raw'].headers.get('tr_cont') in ['M', 'F']:
            new_fk = data.get('ctx_area_fk100', '')
            new_nk = data.get('ctx_area_nk100', '')
            if new_fk == ctx_area_fk100 and new_nk == ctx_area_nk100: break 
            ctx_area_fk100, ctx_area_nk100 = new_fk, new_nk
            page += 1
        else: break
        
    return executions

def execute_kis_order_001x(app_key, app_secret, cano, acnt_prdt, token, ticker, is_buy, qty, price, is_mock=True):
    """✅ 지시사항 8항: ORD_TMD 파싱 및 세분화 예외 반환"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = ("VTTC0012U" if is_buy else "VTTC0011U") if is_mock else ("TTTC0012U" if is_buy else "TTTC0011U")
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    
    data = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": ticker,
        "ORD_DVSN": "01" if price == 0 else "00", "ORD_QTY": str(int(qty)), "ORD_UNPR": str(int(price)),
        "EXCG_ID_DVSN_CD": "KRX", "ORD_CVM_DVSN_CD": "00"
    }
    res = _strict_post(url, headers=headers, data=data)
    
    if res['status'] == "SUCCESS":
        data = res['data']
        if data.get('rt_cd') == '0':
            out = data.get('output', {})
            return "ACKNOWLEDGED", data.get('msg1', ''), out.get('ODNO', ''), out.get('KRX_FWDG_ORD_ORGNO', ''), out.get('ORD_TMD', ''), data.get('rt_cd')
        return "REJECTED", data.get('msg1', ''), "", "", "", data.get('rt_cd')
    
    return "UNKNOWN", res['msg'], "", "", "", ""

def cancel_kis_order_0013(app_key, app_secret, cano, acnt_prdt, token, org_odno, org_branch, qty, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    tr_id = "VTTC0013U" if is_mock else "TTTC0013U"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    
    data = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "KRX_FWDG_ORD_ORGNO": org_branch, "ORGN_ODNO": org_odno,
        "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",
        "ORD_QTY": str(int(qty)), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y" if qty == 0 else "N",
        "EXCG_ID_DVSN_CD": "KRX", "ORD_CVM_DVSN_CD": "00"
    }
    res = _strict_post(url, headers=headers, data=data)
    
    if res['status'] == "SUCCESS":
        data = res['data']
        if data.get('rt_cd') == '0': return "CANCEL_ACKNOWLEDGED", data.get('msg1', '')
        return "REJECTED", data.get('msg1', '')
    
    return "CANCEL_UNKNOWN", res['msg']