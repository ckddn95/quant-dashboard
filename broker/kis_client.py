import requests
import json
import time
import random
import sqlite3
import os
import threading
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(BASE_DIR), "quant_system.db")

class KisResult:
    def __init__(self, state: str, msg: str, data=None):
        self.state = state
        self.msg = msg
        self.data = data

class AccountRateLimiter:
    def __init__(self, max_rate=15, period=1.0, rate_key="default"):
        self.max_rate = max_rate
        self.period = period
        self.rate_key = rate_key
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA busy_timeout=5000;')
        return conn

    def _init_db(self):
        try:
            with self._get_conn() as conn:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS api_rate_limits (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        rate_key TEXT,
                        timestamp REAL
                    )
                ''')
        except Exception:
            pass

    def acquire(self):
        while True:
            now = time.time()
            cutoff = now - self.period
            try:
                with self._get_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    # 만료된 타임스탬프 정리
                    conn.execute("DELETE FROM api_rate_limits WHERE rate_key=? AND timestamp < ?", (self.rate_key, cutoff))
                    # 현재 윈도우 내 호출 건수 확인
                    row = conn.execute("SELECT COUNT(*) as cnt FROM api_rate_limits WHERE rate_key=? AND timestamp >= ?", (self.rate_key, cutoff)).fetchone()
                    count = row[0] if row else 0

                    if count < self.max_rate:
                        conn.execute("INSERT INTO api_rate_limits (rate_key, timestamp) VALUES (?, ?)", (self.rate_key, now))
                        conn.execute("COMMIT")
                        break
                    else:
                        # 가장 오래된 타임스탬프 기준 대기 시간 계산
                        oldest = conn.execute("SELECT MIN(timestamp) FROM api_rate_limits WHERE rate_key=?", (self.rate_key,)).fetchone()
                        conn.execute("COMMIT")
                        sleep_time = self.period - (now - oldest[0]) + random.uniform(0.02, 0.05) if oldest and oldest[0] else 0.05
                        if sleep_time > 0:
                            time.sleep(sleep_time)
            except Exception:
                time.sleep(0.05)

_RATE_LIMITERS = {}
_RATE_LIMITER_LOCK = threading.Lock()

def get_rate_limiter(key: str) -> AccountRateLimiter:
    with _RATE_LIMITER_LOCK:
        if key not in _RATE_LIMITERS:
            _RATE_LIMITERS[key] = AccountRateLimiter(max_rate=15, period=1.0, rate_key=key)
        return _RATE_LIMITERS[key]

_TOKEN_CACHE = {}
_TOKEN_LOCK = threading.Lock()
_TOKEN_FLIGHT_LOCKS = {}

def get_base_url(is_mock: bool) -> str:
    return "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"

def _fetch_new_token_http(app_key: str, app_secret: str, is_mock: bool = True) -> tuple[str | None, str]:
    url = f"{get_base_url(is_mock)}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    data = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    res = _strict_post(url, headers=headers, data=data, rate_limit_key=f"{app_key}_{is_mock}")
    if res.state == "SUCCESS_DATA" and 'access_token' in res.data:
        return res.data['access_token'], "OK"
    return None, f"Token Error: {res.msg}"

def get_kis_access_token(app_key: str, app_secret: str, is_mock: bool = True, force_refresh: bool = False) -> tuple[str | None, str]:
    cache_key = f"{app_key}_{'MOCK' if is_mock else 'REAL'}"
    with _TOKEN_LOCK:
        if cache_key not in _TOKEN_FLIGHT_LOCKS:
            _TOKEN_FLIGHT_LOCKS[cache_key] = threading.Lock()
        flight_lock = _TOKEN_FLIGHT_LOCKS[cache_key]

    with flight_lock:
        now = time.time()
        if not force_refresh and cache_key in _TOKEN_CACHE:
            entry = _TOKEN_CACHE[cache_key]
            if entry["expires_at"] > now + 60:
                return entry["token"], "OK"

        token, msg = _fetch_new_token_http(app_key, app_secret, is_mock)
        if token:
            _TOKEN_CACHE[cache_key] = {
                "token": token,
                "expires_at": now + (3600 * 12)
            }
            return token, "OK"
        return None, msg

def _safe_get(url: str, headers: dict, params: dict = None, max_retries: int = 3, rate_limit_key: str = "default", auth_ctx: dict = None) -> KisResult:
    limiter = get_rate_limiter(rate_limit_key)
    did_401_refresh = False

    for attempt in range(max_retries):
        limiter.acquire()
        try:
            res = requests.get(url, headers=headers, params=params, timeout=10)
            if res.status_code == 429:
                if attempt == max_retries - 1:
                    return KisResult("TRANSPORT_FAIL", "Rate limit (429) Max Retries Exceeded", None)
                time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.2))
                continue

            if res.status_code in [401, 403]:
                if not did_401_refresh and auth_ctx and auth_ctx.get("app_key"):
                    did_401_refresh = True
                    new_token, _ = get_kis_access_token(auth_ctx["app_key"], auth_ctx["app_secret"], auth_ctx.get("is_mock", True), force_refresh=True)
                    if new_token:
                        headers["authorization"] = f"Bearer {new_token}"
                        continue
                return KisResult("BUSINESS_REJECT", f"Unauthorized ({res.status_code})", None)

            try: data = res.json()
            except ValueError: return KisResult("TRANSPORT_FAIL", "Invalid JSON response", None)

            if data.get('rt_cd') == '0':
                return KisResult("SUCCESS_DATA", "OK", {"data": data, "headers": res.headers})
            else:
                return KisResult("BUSINESS_REJECT", data.get('msg1', 'Business Error'), data)

        except requests.exceptions.Timeout:
            if attempt == max_retries - 1:
                return KisResult("TRANSPORT_FAIL", "Timeout Max Retries Exceeded", None)
            time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.2))
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                return KisResult("TRANSPORT_FAIL", f"HTTP Error: {str(e)}", None)
            time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.2))

    return KisResult("TRANSPORT_FAIL", "Unknown Error", None)

def _strict_post(url: str, headers: dict, data: dict, rate_limit_key: str = "default", auth_ctx: dict = None) -> KisResult:
    limiter = get_rate_limiter(rate_limit_key)
    limiter.acquire()

    try:
        res = requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
        if res.status_code in [401, 403]:
            if auth_ctx and auth_ctx.get("app_key"):
                new_token, _ = get_kis_access_token(auth_ctx["app_key"], auth_ctx["app_secret"], auth_ctx.get("is_mock", True), force_refresh=True)
                if new_token:
                    headers["authorization"] = f"Bearer {new_token}"
                    limiter.acquire()
                    res = requests.post(url, headers=headers, data=json.dumps(data), timeout=10)
        if res.status_code in [401, 403]:
            return KisResult("BUSINESS_REJECT", f"Unauthorized ({res.status_code})", None)

        if res.status_code == 429:
            return KisResult("BUSINESS_REJECT", "Rate limit (429)", None)

        if 500 <= res.status_code < 600:
            return KisResult("UNKNOWN", f"Server Error ({res.status_code}) - Treat as UNKNOWN", None)

        try: res_data = res.json()
        except ValueError: return KisResult("TRANSPORT_FAIL", "Invalid JSON response", None)

        if res_data.get('rt_cd') == '0' or 'access_token' in res_data:
            return KisResult("SUCCESS_DATA", "OK", res_data)
        else:
            err_msg = res_data.get('msg1', res_data.get('error_description', 'Business Error'))
            return KisResult("BUSINESS_REJECT", err_msg, res_data)

    except requests.exceptions.Timeout:
        return KisResult("UNKNOWN", "Timeout - Single-shot POST unconfirmed, treating as UNKNOWN", None)
    except requests.exceptions.RequestException as e:
        return KisResult("TRANSPORT_FAIL", f"HTTP Exception: {str(e)}", None)

def fetch_kis_account_balance(app_key: str, app_secret: str, cano: str, acnt_prdt: str, token: str, is_mock: bool = True) -> KisResult:
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8434R" if is_mock else "TTTC8434R"}
    auth_ctx = {"app_key": app_key, "app_secret": app_secret, "is_mock": is_mock}
    rate_key = f"{cano}_{is_mock}"
    
    holdings, summary = [], []
    ctx_area_fk100, ctx_area_nk100 = "", ""
    max_pages, page = 20, 0
    
    while page < max_pages:
        headers["tr_cont"] = "N" if page > 0 else ""
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": ctx_area_fk100, "CTX_AREA_NK100": ctx_area_nk100}
        
        res = _safe_get(url, headers=headers, params=params, rate_limit_key=rate_key, auth_ctx=auth_ctx)
        if res.state != "SUCCESS_DATA":
            return KisResult(res.state, res.msg, {"holdings": holdings, "summary": summary})
            
        data = res.data['data']
        holdings.extend(data.get('output1', []))
        if not summary: summary = data.get('output2', [])
        
        tr_cont = res.data['headers'].get('tr_cont', '')
        if tr_cont in ['M', 'F']:
            new_fk = data.get('ctx_area_fk100', '').strip()
            new_nk = data.get('ctx_area_nk100', '').strip()
            if not new_fk and not new_nk: break
            if new_fk == ctx_area_fk100 and new_nk == ctx_area_nk100: break
            ctx_area_fk100, ctx_area_nk100 = new_fk, new_nk
            page += 1
        else: break
        
    return KisResult("SUCCESS_DATA", "OK", {"holdings": holdings, "summary": summary})

def fetch_kis_orderable_cash(app_key: str, app_secret: str, cano: str, acnt_prdt: str, token: str, ticker: str, price: float, order_kind: str, is_mock: bool = True) -> KisResult:
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "VTTC8908R" if is_mock else "TTTC8908R"}
    auth_ctx = {"app_key": app_key, "app_secret": app_secret, "is_mock": is_mock}
    rate_key = f"{cano}_{is_mock}"
    
    dvsn = "01" if order_kind.upper() == "MARKET" else "00"
    safe_ticker = ticker if ticker.strip() != "" else "005930"
    safe_price = str(int(price)) if price > 0 else "0"
    
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "PDNO": safe_ticker, "ORD_UNPR": safe_price, "ORD_DVSN": dvsn, "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    
    res = _safe_get(url, headers=headers, params=params, rate_limit_key=rate_key, auth_ctx=auth_ctx)
    if res.state == "SUCCESS_DATA":
        out = res.data['data'].get('output', {})
        raw_nrcvb = out.get('nrcvb_buy_amt')
        if raw_nrcvb is not None and str(raw_nrcvb).strip() != "":
            cash_val = float(raw_nrcvb)
        else:
            cash_val = 0.0
            
        return KisResult("SUCCESS_DATA", "OK", cash_val)
    return res

def fetch_kis_current_price_ext(app_key: str, app_secret: str, ticker: str, token: str, is_mock: bool = True) -> KisResult:
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST01010100"}
    auth_ctx = {"app_key": app_key, "app_secret": app_secret, "is_mock": is_mock}
    rate_key = f"{app_key}_{is_mock}"
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
            "FID_INPUT_HOUR_1": "153000",
            "FID_PW_DATA_INCU_YN": "N"}
    
    res = _safe_get(url, headers=headers, params=params, max_retries=2, rate_limit_key=rate_key, auth_ctx=auth_ctx)
    
    if res.state == "SUCCESS_DATA":
        out = res.data['data'].get('output', {})
        if not out:
            return KisResult("SUCCESS_EMPTY", "No quote data", None)
        
        price = float(out.get('stck_prpr', 0))
        high = float(out.get('stck_hgpr', 0))
        low = float(out.get('stck_lwpr', 0))
        
        status_code = out.get('iscd_stat_cls_code', '00')
        is_halted = status_code in ['51', '57', '58', '59']
        
        if price <= 0:
            return KisResult("BUSINESS_REJECT", "Invalid Price <= 0", None)
            
        return KisResult("SUCCESS_DATA", "OK", {
            "price": price, "high": high, "low": low, "is_halted": is_halted
        })
    return res

def fetch_kis_minute_chart(app_key: str, app_secret: str, ticker: str, token: str, is_mock: bool = True) -> KisResult:
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": "FHKST03010200"}
    auth_ctx = {"app_key": app_key, "app_secret": app_secret, "is_mock": is_mock}
    rate_key = f"{app_key}_{is_mock}"
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
            "FID_INPUT_HOUR_1": "153000",
            "FID_PW_DATA_INCU_YN": "N", "FID_ETC_CLS_CODE": ""}
    
    res = _safe_get(url, headers=headers, params=params, max_retries=2, rate_limit_key=rate_key, auth_ctx=auth_ctx)
    if res.state == "SUCCESS_DATA":
        out = res.data['data'].get('output2', [])
        return KisResult("SUCCESS_DATA", "OK", out)
    return res

def fetch_daily_executions_0081(app_key: str, app_secret: str, cano: str, acnt_prdt: str, token: str, is_mock: bool = True, order_date: str = "") -> KisResult:
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    tr_id = "VTTC0081R" if is_mock else "TTTC0081R"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id}
    auth_ctx = {"app_key": app_key, "app_secret": app_secret, "is_mock": is_mock}
    rate_key = f"{cano}_{is_mock}"
    
    if not order_date: order_date = time.strftime('%Y%m%d')
    executions = []
    ctx_area_fk100, ctx_area_nk100 = "", ""
    max_pages, page = 20, 0
    
    while page < max_pages:
        headers["tr_cont"] = "N" if page > 0 else ""
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt, "INQR_STRT_DT": order_date, "INQR_END_DT": order_date, "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": ctx_area_fk100, "CTX_AREA_NK100": ctx_area_nk100}
        
        res = _safe_get(url, headers=headers, params=params, rate_limit_key=rate_key, auth_ctx=auth_ctx)
        if res.state != "SUCCESS_DATA": 
            if page > 0: return KisResult("TRANSPORT_FAIL", f"Pagination failed at page {page}: {res.msg}", None)
            else: return res
        
        data = res.data['data']
        if 'output1' in data and isinstance(data['output1'], list):
            executions.extend(data['output1'])
            
        tr_cont = res.data['headers'].get('tr_cont', '')
        if tr_cont in ['M', 'F']:
            new_fk = data.get('ctx_area_fk100', '').strip()
            new_nk = data.get('ctx_area_nk100', '').strip()
            if not new_fk and not new_nk: break
            if new_fk == ctx_area_fk100 and new_nk == ctx_area_nk100: break 
            ctx_area_fk100, ctx_area_nk100 = new_fk, new_nk
            page += 1
        else: break
        
    return KisResult("SUCCESS_DATA", "OK", executions)

def execute_kis_order_001x(app_key: str, app_secret: str, cano: str, acnt_prdt: str, token: str, ticker: str, is_buy: bool, qty: int, price: float, is_mock: bool = True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = ("VTTC0012U" if is_buy else "VTTC0011U") if is_mock else ("TTTC0012U" if is_buy else "TTTC0011U")
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    auth_ctx = {"app_key": app_key, "app_secret": app_secret, "is_mock": is_mock}
    rate_key = f"{cano}_{is_mock}"
    
    data = {
        "CANO": cano, 
        "ACNT_PRDT_CD": acnt_prdt, 
        "PDNO": ticker, 
        "ORD_DVSN": "01" if price == 0 else "00", 
        "ORD_QTY": str(int(qty)), 
        "ORD_UNPR": str(int(price)),
        "SLL_TYPE": "" if is_buy else "01",
        "EXCG_ID_DVSN_CD": "KRX",
        "CNDT_PRIC": "0"
    }
    
    res = _strict_post(url, headers=headers, data=data, rate_limit_key=rate_key, auth_ctx=auth_ctx)
    if res.state == "SUCCESS_DATA":
        out = res.data.get('output', {})
        return "ACKNOWLEDGED", res.data.get('msg1', ''), out.get('ODNO', ''), out.get('KRX_FWDG_ORD_ORGNO', ''), out.get('ORD_TMD', ''), res.data.get('rt_cd')
    elif res.state == "BUSINESS_REJECT":
        return "REJECTED", res.msg, "", "", "", ""
    return "UNKNOWN", res.msg, "", "", "", ""

def cancel_kis_order_0013(app_key: str, app_sec: str, cano: str, acnt_prdt: str, token: str, org_odno: str, org_branch: str, qty: int, is_mock: bool = True):
    url = f"{get_base_url(is_mock)}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    tr_id = "VTTC0013U" if is_mock else "TTTC0013U"
    headers = {"authorization": f"Bearer {token}", "appkey": app_key, "appsecret": app_secret, "tr_id": tr_id, "custtype": "P"}
    auth_ctx = {"app_key": app_key, "app_secret": app_secret, "is_mock": is_mock}
    rate_key = f"{cano}_{is_mock}"
    
    data = {
        "CANO": cano, 
        "ACNT_PRDT_CD": acnt_prdt, 
        "KRX_FWDG_ORD_ORGNO": org_branch if org_branch else "", 
        "ORGN_ODNO": org_odno, 
        "ORD_DVSN": "01", 
        "RVSE_CNCL_DVSN_CD": "02", 
        "ORD_QTY": str(int(qty)), 
        "ORD_UNPR": "0", 
        "QTY_ALL_ORD_YN": "Y" if qty == 0 else "N",
        "EXCG_ID_DVSN_CD": "KRX",
        "CNDT_PRIC": "0"
    }
    
    res = _strict_post(url, headers=headers, data=data, rate_limit_key=rate_key, auth_ctx=auth_ctx)
    if res.state == "SUCCESS_DATA":
        return "CANCEL_ACKNOWLEDGED", res.data.get('msg1', '')
    elif res.state == "BUSINESS_REJECT":
        return "REJECTED", res.msg
    return "CANCEL_UNKNOWN", res.msg