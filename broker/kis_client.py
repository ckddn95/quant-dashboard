import requests
import json
import time

def get_base_url(is_mock):
    return "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"

def _safe_get(url, headers, params=None, max_retries=3):
    """조회(GET)는 429/503 시 지수 백오프를 통해 안전하게 재시도"""
    for attempt in range(max_retries):
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code in [429, 503]:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return res
        except requests.exceptions.RequestException:
            if attempt == max_retries - 1: raise
            time.sleep(0.5 * (2 ** attempt))
    return None

def _strict_post(url, headers, data):
    """🚨 주문/취소(POST)는 이중 지출 방지를 위해 절대 재시도하지 않음"""
    try:
        return requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
    except requests.exceptions.RequestException:
        # Timeout 또는 네트워크 단절 시 즉시 예외 발생 (이후 UNKNOWN 마킹됨)
        raise

def get_kis_access_token(app_key, app_secret, is_mock=True):
    url = f"{get_base_url(is_mock)}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    data = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    try:
        res = _strict_post(url, headers=headers, data=data)
        if res and res.status_code == 200: return res.json().get("access_token"), "OK"
        return None, f"Token Error: {res.text if res else 'No Response'}"
    except Exception as e: return None, str(e)

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt, token, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8434R" if is_mock else "TTTC8434R"}
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    res = _safe_get(url, headers=headers, params=params)
    if res and res.status_code == 200 and res.json().get('rt_cd') == '0':
        return res.json().get('output1', []), res.json().get('output2', []), "OK"
    return [], [], "API Error"

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt, token, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8908R" if is_mock else "TTTC8908R"}
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": "", "ORD_UNPR": "", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    res = _safe_get(url, headers=headers, params=params)
    if res and res.status_code == 200: return float(res.json().get('output', {}).get('ord_psbl_cash', 0))
    return 0.0

def fetch_daily_executions(app_key, app_secret, cano, acnt_prdt, token, is_mock=True, order_date=""):
    """✅ [0081R] 당일 체결 내역 대사용 (Pagination 지원)"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8001R" if is_mock else "TTTC8001R"}
    if not order_date: order_date = time.strftime('%Y%m%d')
    
    executions = []
    ctx_area_fk100, ctx_area_nk100 = "", ""
    
    while True:
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "INQR_STRT_DT": order_date, "INQR_END_DT": order_date,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "01",
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
            "CTX_AREA_FK100": ctx_area_fk100, "CTX_AREA_NK100": ctx_area_nk100
        }
        res = _safe_get(url, headers=headers, params=params)
        if not res or res.status_code != 200: break
        data = res.json()
        if data.get('rt_cd') != '0': break
        
        executions.extend(data.get('output1', []))
        
        # Pagination 처리
        if res.headers.get('tr_cont') in ['M', 'F']:
            ctx_area_fk100 = data.get('ctx_area_fk100', '')
            ctx_area_nk100 = data.get('ctx_area_nk100', '')
        else: break
        
    return executions

def execute_kis_order_001x(app_key, app_secret, cano, acnt_prdt, token, ticker, is_buy, qty, price, is_mock=True):
    """✅ [001x] KRX-only 공식 주문 어댑터"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = ("VTTC0012U" if is_buy else "VTTC0011U") if is_mock else ("TTTC0012U" if is_buy else "TTTC0011U")
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    
    data = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": ticker,
        "ORD_DVSN": "01" if price == 0 else "00", "ORD_QTY": str(int(qty)), "ORD_UNPR": str(int(price)),
        "EXCG_ID_DVSN_CD": "KRX", "ORD_CVM_DVSN_CD": "00"
    }
    try:
        res = _strict_post(url, headers=headers, data=data)
        resp = res.json()
        if resp.get('rt_cd') == '0':
            return "ACKNOWLEDGED", resp.get('msg1', ''), resp.get('output', {}).get('ODNO', ''), resp.get('output', {}).get('KRX_FWDG_ORD_ORGNO', ''), resp.get('rt_cd')
        return "REJECTED", resp.get('msg1', ''), "", "", resp.get('rt_cd')
    except Exception as e:
        # POST 타임아웃 발생. 절대 080x로 재시도하지 않음.
        return "UNKNOWN", f"Connection/Timeout Error: {str(e)}", "", "", ""

def cancel_kis_order_001x(app_key, app_secret, cano, acnt_prdt, token, org_odno, org_branch, qty, is_mock=True):
    """✅ [0013] KRX-only 공식 정정/취소 어댑터"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    tr_id = "VTTC0013U" if is_mock else "TTTC0013U"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    
    data = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "KRX_FWDG_ORD_ORGNO": org_branch, "ORGN_ODNO": org_odno,
        "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", # 02: 취소
        "ORD_QTY": str(int(qty)), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y" if qty == 0 else "N",
        "EXCG_ID_DVSN_CD": "KRX", "ORD_CVM_DVSN_CD": "00"
    }
    try:
        res = _strict_post(url, headers=headers, data=data)
        resp = res.json()
        if resp.get('rt_cd') == '0': return "CANCEL_ACKNOWLEDGED", resp.get('msg1', '')
        return "REJECTED", resp.get('msg1', '')
    except Exception as e:
        return "CANCEL_UNKNOWN", str(e)