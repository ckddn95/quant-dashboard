import requests
import json
import time

def get_base_url(is_mock):
    return "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"

def _safe_get(url, headers, params=None, max_retries=3):
    """GET 요청에 한해 지수 백오프 기반 재시도 (Rate Limit 방어)"""
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
    """🚨 POST(주문/취소)는 이중 지출 방지를 위해 절대 내부에서 재전송하지 않음"""
    try:
        return requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"POST Timeout/Connection Error: {e}")

def get_kis_access_token(app_key, app_secret, is_mock=True):
    url = f"{get_base_url(is_mock)}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    data = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    try:
        res = _strict_post(url, headers=headers, data=data)
        if res and res.status_code == 200: return res.json().get("access_token"), "OK"
        return None, f"Token Error: {res.text if res else 'No Response'}"
    except Exception as e: return None, str(e)

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt, token, ticker, price, order_kind, is_mock=True):
    """실제 Ticker와 가격을 반영한 정밀 주문 가능 현금 조회"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8908R" if is_mock else "TTTC8908R"}
    dvsn = "01" if order_kind == "MARKET" else "00"
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": ticker, "ORD_UNPR": str(int(price)), "ORD_DVSN": dvsn, "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    res = _safe_get(url, headers=headers, params=params)
    if res and res.status_code == 200:
        return float(res.json().get('output', {}).get('ord_psbl_cash', 0))
    return 0.0

def fetch_daily_executions_0081(app_key, app_secret, cano, acnt_prdt, token, is_mock=True, order_date=""):
    """✅ [0081R] 당일 체결 내역 대사용 (Pagination 완벽 지원)"""
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
        
        # Pagination 연속 조회 처리 (tr_cont 헤더 확인)
        if res.headers.get('tr_cont') in ['M', 'F']:
            ctx_area_fk100 = data.get('ctx_area_fk100', '')
            ctx_area_nk100 = data.get('ctx_area_nk100', '')
        else: break
        
    return executions

def execute_kis_order_001x(app_key, app_secret, cano, acnt_prdt, token, ticker, is_buy, qty, price, is_mock=True):
    """✅ [001x] 현금 주문 POST. 실패 시 UNKNOWN 반환, 재시도 금지"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = ("VTTC0012U" if is_buy else "VTTC0011U") if is_mock else ("TTTC0012U" if is_buy else "TTTC0011U")
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    
    data = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": ticker,
        "ORD_DVSN": "01" if price == 0 else "00", "ORD_QTY": str(int(qty)), "ORD_UNPR": str(int(price)),
        "EXCG_ID_DVSN_CD": "KRX", # 다크풀(NXT/SOR) 송출 방어
        "ORD_CVM_DVSN_CD": "00"
    }
    try:
        res = _strict_post(url, headers=headers, data=data)
        resp = res.json()
        if resp.get('rt_cd') == '0':
            return "ACKNOWLEDGED", resp.get('msg1', ''), resp.get('output', {}).get('ODNO', ''), resp.get('output', {}).get('KRX_FWDG_ORD_ORGNO', ''), resp.get('rt_cd')
        return "REJECTED", resp.get('msg1', ''), "", "", resp.get('rt_cd')
    except Exception as e:
        # Time-out 시 080x로 Fallback 절대 금지
        return "UNKNOWN", str(e), "", "", ""

def cancel_kis_order_0013(app_key, app_secret, cano, acnt_prdt, token, org_odno, org_branch, qty, is_mock=True):
    """✅ [0013] 주문 취소 POST"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    tr_id = "VTTC0013U" if is_mock else "TTTC0013U"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    
    data = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "KRX_FWDG_ORD_ORGNO": org_branch, "ORGN_ODNO": org_odno,
        "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", # 취소
        "ORD_QTY": str(int(qty)), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y" if qty == 0 else "N",
        "EXCG_ID_DVSN_CD": "KRX", "ORD_CVM_DVSN_CD": "00"
    }
    try:
        res = _strict_post(url, headers=headers, data=data)
        resp = res.json()
        # 성공해도 CANCELED가 아니라 CANCEL_ACKNOWLEDGED 반환
        if resp.get('rt_cd') == '0': return "CANCEL_ACKNOWLEDGED", resp.get('msg1', '')
        return "REJECTED", resp.get('msg1', '')
    except Exception as e:
        return "CANCEL_UNKNOWN", str(e)