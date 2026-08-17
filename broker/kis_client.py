import requests
import json

def get_base_url(is_mock):
    # 모의투자(True)와 실전투자(False) 도메인 자동 분기
    return "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"

def get_kis_access_token(app_key, app_secret, is_mock=True):
    """KIS API 접근용 Access Token 발급"""
    url = f"{get_base_url(is_mock)}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    data = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret
    }
    try:
        res = requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
        if res.status_code == 200:
            return res.json().get("access_token"), "OK"
        return None, f"Token Error: {res.text}"
    except Exception as e:
        return None, str(e)

def fetch_kis_account_balance(app_key, app_secret, cano, acnt_prdt, token, is_mock=True):
    """계좌 잔고 및 보유 종목 조회"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "VTTC8434R" if is_mock else "TTTC8434R"
    }
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('rt_cd') == '0':
                return data.get('output1', []), data.get('output2', []), "OK"
            return [], [], data.get('msg1', 'Unknown Error')
        return [], [], f"HTTP {res.status_code}"
    except Exception as e:
        return [], [], str(e)

def fetch_kis_orderable_cash(app_key, app_secret, cano, acnt_prdt, token, is_mock=True):
    """주문 가능 현금(예수금) 조회"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "VTTC8908R" if is_mock else "TTTC8908R"
    }
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt,
        "PDNO": "",
        "ORD_UNPR": "",
        "ORD_DVSN": "01",
        "CMA_EVLU_AMT_ICLD_YN": "N",
        "OVRS_ICLD_YN": "N"
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('rt_cd') == '0' and data.get('output'):
                return float(data['output'].get('ord_psbl_cash', 0))
        return 0.0
    except:
        return 0.0

def fetch_kis_current_price_ext(app_key, app_secret, ticker, token, is_mock=True):
    """실시간 현재가, 고가, 저가 조회"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010100"
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get('rt_cd') == '0' and data.get('output'):
                out = data['output']
                cp = float(out.get('stck_prpr', 0))
                hp = float(out.get('stck_hgpr', 0))
                lp = float(out.get('stck_lwpr', 0))
                return cp, hp, lp, False, "OK"
        return 0.0, 0.0, 0.0, False, f"API Error: {res.status_code}"
    except Exception as e:
        return 0.0, 0.0, 0.0, False, str(e)

def execute_kis_order(app_key, app_secret, cano, acnt_prdt, token, ticker, is_buy, qty, price, is_mock=True):
    """실전/모의 자동매매 주문 실행 (시장가/지정가)"""
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-cash"
    
    # tr_id 매수/매도 및 실전/모의 분기
    if is_mock:
        tr_id = "VTTC0802U" if is_buy else "VTTC0801U"
    else:
        tr_id = "TTTC0802U" if is_buy else "TTTC0801U"
        
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
        "hashkey": ""
    }
    
    ord_dvsn = "01" if price == 0 else "00" # 01: 시장가, 00: 지정가
    
    data = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt,
        "PDNO": ticker,
        "ORD_DVSN": ord_dvsn,
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": str(int(price))
    }
    
    try:
        res = requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
        if res.status_code == 200:
            resp = res.json()
            if resp.get('rt_cd') == '0':
                out = resp.get('output', {})
                return "ACKNOWLEDGED", resp.get('msg1', ''), out.get('ODNO', ''), out.get('KRX_FWDG_ORD_ORGNO', ''), resp.get('rt_cd')
            else:
                return "REJECTED", resp.get('msg1', ''), "", "", resp.get('rt_cd')
        return "UNKNOWN", f"HTTP {res.status_code}", "", "", ""
    except Exception as e:
        return "UNKNOWN", str(e), "", "", ""