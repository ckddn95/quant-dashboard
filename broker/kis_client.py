import requests
import json
import time

def get_base_url(is_mock):
    return "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"

def _safe_request(method, url, headers, data=None, params=None, max_retries=3):
    """지수 백오프(Exponential Backoff)를 통한 Rate Limit 방어"""
    for attempt in range(max_retries):
        try:
            if method == 'GET':
                res = requests.get(url, headers=headers, params=params, timeout=10)
            else:
                res = requests.post(url, headers=headers, data=data, timeout=10)
                
            if res.status_code in [429, 503]:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return res
        except requests.exceptions.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(0.5 * (2 ** attempt))
    return None

def get_kis_access_token(app_key, app_secret, is_mock=True):
    url = f"{get_base_url(is_mock)}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    data = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    try:
        res = _safe_request('POST', url, headers=headers, data=json.dumps(data))
        if res and res.status_code == 200:
            return res.json().get("access_token"), "OK"
        return None, f"Token Error: {res.text if res else 'No Response'}"
    except Exception as e:
        return None, str(e)

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt, token, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8434R" if is_mock else "TTTC8434R"}
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    res = _safe_request('GET', url, headers=headers, params=params)
    if res and res.status_code == 200:
        data = res.json()
        if data.get('rt_cd') == '0':
            return data.get('output1', []), data.get('output2', []), "OK"
        return [], [], data.get('msg1', 'Unknown Error')
    return [], [], f"API Error: HTTP {res.status_code if res else 'Fail'}"

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt, token, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8908R" if is_mock else "TTTC8908R"}
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": "", "ORD_UNPR": "", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    res = _safe_request('GET', url, headers=headers, params=params)
    if res and res.status_code == 200:
        data = res.json()
        if data.get('rt_cd') == '0' and data.get('output'):
            return float(data['output'].get('ord_psbl_cash', 0))
    return 0.0

def fetch_kis_current_price_ext(app_key, app_secret, ticker, token, is_mock=True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    res = _safe_request('GET', url, headers=headers, params=params, max_retries=2)
    if res and res.status_code == 200:
        data = res.json()
        if data.get('rt_cd') == '0' and data.get('output'):
            out = data['output']
            return float(out.get('stck_prpr', 0)), float(out.get('stck_hgpr', 0)), float(out.get('stck_lwpr', 0)), False, "OK"
    return 0.0, 0.0, 0.0, False, f"API Error: HTTP {res.status_code if res else 'Fail'}"

def execute_kis_order_legacy_080x(app_key, app_secret, cano, acnt_prdt, token, ticker, is_buy, qty, price, is_mock=True):
    """🚨 [DEPRECATED] 레거시 구형 주문. 신규 워커는 001x 사용 필수."""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = ("VTTC0802U" if is_buy else "VTTC0801U") if is_mock else ("TTTC0802U" if is_buy else "TTTC0801U")
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P", "hashkey": ""}
    data = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": ticker, "ORD_DVSN": "01" if price == 0 else "00", "ORD_QTY": str(int(qty)), "ORD_UNPR": str(int(price))}
    try:
        res = _safe_request('POST', url, headers=headers, data=json.dumps(data))
        if res and res.status_code == 200:
            resp = res.json()
            if resp.get('rt_cd') == '0':
                return "ACKNOWLEDGED", resp.get('msg1', ''), resp.get('output', {}).get('ODNO', ''), resp.get('output', {}).get('KRX_FWDG_ORD_ORGNO', ''), resp.get('rt_cd')
            return "REJECTED", resp.get('msg1', ''), "", "", resp.get('rt_cd')
        return "UNKNOWN", f"HTTP {res.status_code if res else 'Fail'}", "", "", ""
    except Exception as e:
        return "UNKNOWN", str(e), "", "", ""

def execute_kis_order_current_001x(app_key, app_secret, cano, acnt_prdt, token, ticker, is_buy, qty, price, is_mock=True):
    """✅ [RECOMMENDED] 공식 문서 기반 최신 주문 (KRX 전용)"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = ("VTTC0012U" if is_buy else "VTTC0011U") if is_mock else ("TTTC0012U" if is_buy else "TTTC0011U")
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    
    data = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt,
        "PDNO": ticker,
        "ORD_DVSN": "01" if price == 0 else "00",
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": str(int(price)),
        "EXCG_ID_DVSN_CD": "KRX",
        "ORD_CVM_DVSN_CD": "00"
    }
    try:
        res = _safe_request('POST', url, headers=headers, data=json.dumps(data))
        if res and res.status_code == 200:
            resp = res.json()
            if resp.get('rt_cd') == '0':
                return "ACKNOWLEDGED", resp.get('msg1', ''), resp.get('output', {}).get('ODNO', ''), resp.get('output', {}).get('KRX_FWDG_ORD_ORGNO', ''), resp.get('rt_cd')
            return "REJECTED", resp.get('msg1', ''), "", "", resp.get('rt_cd')
        return "UNKNOWN", f"HTTP {res.status_code if res else 'Fail'}", "", "", ""
    except Exception as e:
        return "UNKNOWN", str(e), "", "", ""