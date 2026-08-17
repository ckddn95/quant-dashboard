import requests
import json
from typing import Tuple, Dict, List

def get_kis_access_token(app_key: str, app_secret: str, is_mock: bool = True) -> Tuple[str, str]:
    if not app_key or not app_secret: return "", "Missing Keys"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/oauth2/tokenP"
    try:
        res = requests.post(url, headers={"content-type": "application/json"}, data=json.dumps({"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}), timeout=10)
        if res.status_code == 200: return res.json().get("access_token", ""), "OK"
        return "", f"Auth Failed: {res.text}"
    except Exception as e: return "", str(e)

def fetch_kis_current_price_ext(app_key, app_secret, ticker, token, is_mock=True) -> Tuple[float, float, float, bool, str]:
    if not token: return 0.0, 0.0, 0.0, False, "No Token"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    try:
        res = requests.get(url, headers=headers, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).zfill(6)}, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0':
            out = res.json()['output']
            return float(out['stck_prpr']), float(out['stck_hgpr']), float(out['stck_lwpr']), out.get('iscd_stat_cls_code') in ['51', '57'], "OK"
    except Exception as e: return 0.0, 0.0, 0.0, False, str(e)
    return 0.0, 0.0, 0.0, False, "API Failed"

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True) -> Tuple[List, List, str]:
    if not token: return [], [], "No Token"
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8434R" if is_mock else "TTTC8434R", "custtype": "P"}
    params = {"CANO": str(cano)[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).zfill(2), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    
    stocks, summary = [], []
    while True:
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                rj = res.json()
                if rj.get('rt_cd') == '0': 
                    stocks.extend(rj.get('output1', []))
                    if not summary: summary = rj.get('output2', [])
                    if res.headers.get('tr_cont') in ['F', 'M']:
                        params['CTX_AREA_FK100'] = rj.get('ctx_area_fk100', '')
                        params['CTX_AREA_NK100'] = rj.get('ctx_area_nk100', '')
                        headers['tr_cont'] = 'N'
                        continue
                    return stocks, summary, "OK"
                return [], [], rj.get('msg1')
        except Exception as e: return [], [], str(e)
        break
    return [], [], "HTTP Error"

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True) -> float:
    if not token: return 0.0
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8908R" if is_mock else "TTTC8908R", "custtype": "P"}
    params = {"CANO": str(cano)[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).zfill(2), "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return float(res.json().get('output', {}).get('ord_psbl_cash', 0))
    except: pass
    return 0.0

def execute_kis_order(app_key, app_secret, cano, acnt_prdt_cd, token, ticker, is_buy, qty, price=0, is_mock=True) -> Tuple[str, str, str, str, str]:
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/order-cash"
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

def cancel_kis_order(app_key, app_secret, cano, acnt_prdt_cd, token, odno, branch, is_mock=True) -> Tuple[str, str]:
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC0803U" if is_mock else "TTTC0803U", "custtype": "P"}
    body = {"CANO": str(cano)[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).zfill(2), "KRX_FWDG_ORD_ORGNO": str(branch), "ORGN_ODNO": str(odno), "ORD_DVSN": "01", "RVSE_CNCL_DVSN": "02", "ORD_QTY": "0", "ORD_UNPR": "0"}
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        if res.status_code == 200 and res.json().get('rt_cd') == '0': return "CANCEL_ACKNOWLEDGED", "OK"
        return "CANCEL_UNKNOWN", res.json().get('msg1', 'Cancel Error')
    except Exception as e: return "CANCEL_UNKNOWN", str(e)

def fetch_kis_order_executions(app_key, app_secret, cano, acnt_prdt_cd, token, is_mock=True) -> Dict[str, Dict]:
    domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
    url = f"{domain}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8001R" if is_mock else "TTTC8001R", "custtype": "P"}
    today = datetime.now().strftime("%Y%m%d")
    params = {"CANO": str(cano)[:8], "ACNT_PRDT_CD": str(acnt_prdt_cd).zfill(2), "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_FIID_COND": "", "INQR_FIID_DATA": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    
    exec_dict = {}
    while True:
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 200 and res.json().get('rt_cd') == '0':
                rj = res.json()
                for order in rj.get('output1', []):
                    odno = order.get('odno')
                    if odno: exec_dict[odno] = {'cum_qty': int(order.get('tot_ccld_qty', 0)), 'avg_price': float(order.get('avg_prvs', 0))}
                if res.headers.get('tr_cont') in ['F', 'M']:
                    params['CTX_AREA_FK100'], params['CTX_AREA_NK100'] = rj.get('ctx_area_fk100', ''), rj.get('ctx_area_nk100', '')
                    headers['tr_cont'] = 'N'
                    continue
        except: pass
        break
    return exec_dict
