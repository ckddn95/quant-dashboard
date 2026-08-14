import requests
import json
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

@dataclass
class BrokerResult:
    success: bool
    status: str  # FILLED, PARTIALLY_FILLED, REJECTED, UNKNOWN, ERROR
    msg: str
    filled_qty: int = 0
    filled_price: float = 0.0

class KISClient:
    def __init__(self, app_key: str, app_secret: str, cano: str, prdt_cd: str, is_mock: bool = True):
        self.app_key = app_key
        self.app_secret = app_secret
        self.cano = cano.replace("-", "").strip()[:8]
        self.prdt_cd = prdt_cd.strip().zfill(2)
        self.is_mock = is_mock
        
        # P0 조건: 기본값 Paper(Mock) 강제 및 실계좌 차단
        if not self.is_mock:
            raise PermissionError("BLOCKED_FOR_LIVE: P0 안전 조건 최종 검수 전까지 실계좌(LIVE) 모드를 활성화할 수 없습니다.")
            
        self.domain = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
        self.token = self._get_token()

    def _get_token(self) -> str:
        url = f"{self.domain}/oauth2/tokenP"
        body = {"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret}
        res = requests.post(url, json=body, timeout=10)
        res.raise_for_status()
        return res.json().get("access_token")

    def fetch_current_price(self, ticker: str) -> Optional[float]:
        url = f"{self.domain}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {"authorization": f"Bearer {self.token}", "appkey": self.app_key, "appsecret": self.app_secret, "tr_id": "FHKST01010100"}
        res = requests.get(url, headers=headers, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).zfill(6)}, timeout=5)
        if res.status_code == 200 and res.json().get('rt_cd') == '0':
            return float(res.json()['output']['stck_prpr'])
        return None

    def fetch_account_balance(self) -> Tuple[List[Dict], Optional[Dict]]:
        url = f"{self.domain}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if self.is_mock else "TTTC8434R"
        headers = {"authorization": f"Bearer {self.token}", "appkey": self.app_key, "appsecret": self.app_secret, "tr_id": tr_id, "custtype": "P"}
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        res = requests.get(url, headers=headers, params=params, timeout=10)
        if res.status_code == 200 and res.json().get('rt_cd') == '0':
            return res.json().get('output1', []), res.json().get('output2', [{}])[0]
        return [], None

    def execute_order(self, ticker: str, qty: int, price: float, order_type: str = "BUY", is_market: bool = False) -> BrokerResult:
        if qty <= 0: return BrokerResult(False, "REJECTED", "수량 오류")
        url = f"{self.domain}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = ("VTTC0802U" if order_type == "BUY" else "VTTC0801U") if self.is_mock else ("TTTC0802U" if order_type == "BUY" else "TTTC0801U")
        headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self.token}", "appkey": self.app_key, "appsecret": self.app_secret, "tr_id": tr_id}
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.prdt_cd, "PDNO": str(ticker).zfill(6), "ORD_DVSN": "01" if is_market else "00", "ORD_QTY": str(int(qty)), "ORD_UNPR": "0" if is_market else str(int(price))}
        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            if res.status_code == 200:
                rj = res.json()
                if rj.get('rt_cd') == '0':
                    return BrokerResult(True, "FILLED", rj.get('msg1', ''), qty, price)
                return BrokerResult(False, "REJECTED", f"거절: {rj.get('msg1')}")
            return BrokerResult(False, "ERROR", f"HTTP 오류 {res.status_code}")
        except Exception as e:
            return BrokerResult(False, "UNKNOWN", f"망 오류: {str(e)}")
