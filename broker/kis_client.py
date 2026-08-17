import requests
import json
from typing import Tuple, Dict, List

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

# 🛑 공식 TR ID 강제: 매수(TTTC0802U/VTTC0802U), 매도(TTTC0801U/VTTC0801U)
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

# 나머지 fetch, cancel 함수는 이전과 동일 유지 (pagination 로직 완비 상태)
