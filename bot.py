import time
import uuid
import os
import hashlib
from datetime import datetime
import database as db
import broker.kis_client as kis
import quant_engine as quant

WORKER_ID = str(uuid.uuid4())
TOKEN_CACHE = {"token": "", "expires_at": 0}
BOUND_STRATEGY = os.getenv('STRATEGY', 'CORE') 

def get_valid_token(api_key, api_sec, is_mock):
    now = time.time()
    if TOKEN_CACHE["token"] and TOKEN_CACHE["expires_at"] > now + 300: return TOKEN_CACHE["token"]
    token, _ = kis.get_kis_access_token(api_key, api_sec, is_mock)
    if token: TOKEN_CACHE["token"], TOKEN_CACHE["expires_at"] = token, now + 43200
    return token

def main_loop():
    print(f"🤖 {BOUND_STRATEGY} Quant Worker Started... (Worker ID: {WORKER_ID})")
    api_key, api_sec, cano = os.getenv('KIS_APP_KEY'), os.getenv('KIS_APP_SECRET'), os.getenv('KIS_CANO')
    acnt_prdt, is_mock = os.getenv('KIS_ACNT_PRDT', '01'), os.getenv('KIS_IS_MOCK', 'True').lower() == 'true'
    env_str = "MOCK" if is_mock else "REAL"

    if not api_key or not api_sec or not cano: print("🚨 Fatal: Missing Config."); return
    
    # 🛑 보안 핑거프린트 생성 (DB 질의용)
    acc_fp = hashlib.sha256(cano.encode()).hexdigest()[:16]

    while True:
        try:
            token = get_valid_token(api_key, api_sec, is_mock)
            if not token: time.sleep(10); continue
            
            port_id = strat_id = BOUND_STRATEGY
            sys_status = db.get_system_status("KIS", env_str, acc_fp, port_id)
            
            if sys_status["contract_version"] != db.CONTRACT["contract_version"]: print("🚨 Version Mismatch."); time.sleep(10); continue
            if sys_status["kill_switch"]: print("🛑 KILL SWITCH ACTIVE"); time.sleep(10); continue
            if not sys_status["auto_trade"]: time.sleep(5); continue
            
            # Lease 및 Fencing Token 검증
            lease_ok, lease_token = db.acquire_worker_lease("KIS", env_str, acc_fp, port_id, WORKER_ID)
            if not lease_ok: time.sleep(10); continue

            rd = db.get_setting(f'last_real_data_{env_str}_{acc_fp}_{port_id}', {'eval': 0, 'pnl': 0, 'cash': 0})
            
            while True:
                # 주문 처리 로직 (원자적 검증 포함 - 기존과 동일하되 acc_fp 사용)
                order = db.claim_next_order("KIS", env_str, acc_fp, port_id, WORKER_ID, lease_token)
                if not order: break 
                
                check_sys = db.get_system_status("KIS", env_str, acc_fp, port_id)
                if check_sys["kill_switch"] or not check_sys["auto_trade"]:
                    db.transition_order_status(order['id'], 'CLAIMED', 'CANCELED', code="SYS_BLOCKED"); break
                    
                if not db.transition_order_status(order['id'], 'CLAIMED', 'SUBMITTING'): continue
                
                # API 전송 (평문 CANO 사용)
                status, msg, odno, branch, code = kis.execute_kis_order(api_key, api_sec, cano, acnt_prdt, token, order['ticker'], order['side']=="BUY", order['qty'], 0, is_mock)
                if status == "ACKNOWLEDGED": db.transition_order_status(order['id'], 'SUBMITTING', 'ACKNOWLEDGED', broker_id=odno, branch=branch, code=code)
                else: db.transition_order_status(order['id'], 'SUBMITTING', status, code=code)

            time.sleep(3) 
        except Exception as e: print(f"Bot Error: {e}"); time.sleep(10)

if __name__ == "__main__": main_loop()
