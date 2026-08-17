import requests
import json
from typing import Tuple, Dict, List
from datetime import datetime

def _get_clean_domain(is_mock: bool) -> str:
    raw_domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    return raw_domain.encode('ascii', 'ignore').decode('ascii').strip()

def get_kis_access_token(app_key: str, app_secret: str, is_mock: bool = True) -> Tuple[str, str]:
    if not app_key or not app_secret: return "", "Missing Keys"
    domain = _get_clean_domain(is_mock)
    url = f"{domain}/oauth2/tokenP".strip()
    try:
        res = requests.post(url, headers={"content-type": "application/json"}, data=json.dumps({"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}), timeout=10)
        if res.status_code == 200: return res.json().get("access_token", ""), "OK"
        return "", f"Auth Failed (HTTP {res.status_code}): {res.text}"
    except Exception as e: return "", str(e)

def fetch_kis_current_price_ext(app_key, app_secret, ticker, token, is_mock=True) -> Tuple[float, float, float, bool, str]:
    if not token: return 0.0, 0.0, 0.0, False, "No Token"
    domain = _get_clean_domain(is_mock)
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-price".strip()
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    try:
        res = requests.get(url, headers=headers, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).zfill(6)}, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0':
            out = res.json()['output']
            return float(out['stck_prpr']), float(out['stck_hgpr']), float(out['stck_lwpr']), out.get('iscd_stat_cls_code') in ['51', '57'], "OK"
    except Exception as e: return 0.0, 0.0, 0.0, False, str(e)
    return 0.0, 0.0, 0.0, False, "API Failed"

# 🛑 [Step 2 패치] 1분봉 실시간 캔들 조회 함수 (2연속 종가 확인용)
def fetch_kis_1min_candle(app_key, app_secret, ticker, token, is_mock=True) -> Tuple[float, str]:
    if not token: return 0.0, "No Token"
    domain = _get_clean_domain(is_mock)
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice".strip()
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST03010200"}
    now_time = datetime.now().strftime("%H%M%S")
    params = {"FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).zfill(6), "FID_INPUT_HOUR_1": now_time, "FID_PW_DATA_INCU_YN": "Y"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0':
            out = res.json().get('output2', [])
            if out: return float(out[0]['stck_prpr']), "OK" # 가장 최근 1분봉 종가 반환
    except Exception as e: return 0.0, str(e)
    return 0.0, "API Failed"

def execute_kis_order(app_key, app_secret, cano, acnt_prdt_cd, token, ticker, is_buy, qty, price=0, is_mock=True) -> Tuple[str, str, str, str, str]:
    domain = _get_clean_domain(is_mock)
    url = f"{domain}/uapi/domestic-stock/v1/trading/order-cash".strip()
    tr_id = ("VTTC0802U" if is_buy else "VTTC0801U") if is_mock else ("TTTC0802U" if is_buy else "TTTC0801U")
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    ord_dvsn, unpr = ("01", "0") if price == 0 else ("00", str(int(price)))
    body = {"CANO": str(cano)[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).zfill(2), "PDNO": str(ticker).zfill(6), "ORD_DVSN": ord_dvsn, "ORD_QTY": str(int(qty)), "ORD_UNPR": unpr}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        rj = res.json()
        if res.status_code == 200:
            if rj.get('rt_cd') == '0': return "ACKNOWLEDGED", rj.get('msg1', ''), rj['output']['ODNO'], rj['output']['KRX_FWDG_ORD_ORGNO'], rj.get('msg_cd', '')
            else: return "REJECTED", rj.get('msg1', ''), "", "", rj.get('msg_cd', '')
        return "UNKNOWN", f"HTTP {res.status_code}", "", "", ""
    except Exception as e: return "UNKNOWN", str(e), "", "", ""

# (fetch_kis_account_balance 등 나머지 함수 기존 동일 유지)
