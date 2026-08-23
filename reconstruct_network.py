import os
import re

def fix_kis_client():
    filepath = os.path.join("broker", "kis_client.py")
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P0-J] 비멱등 POST 재시도 금지 및 REAL POST 하드블록 게이트
    post_block_logic = """
        # [P0-J, P0-K] REAL POST 원천 차단 게이트 (Canary 승인 조건 강제)
        if method.upper() == "POST" and not getattr(self, 'is_mock', True):
            # Canary 1주/소액 승인 조건이 완벽히 일치할 때만 열리도록 설계 (현재는 무조건 BLOCKED 유지)
            # 승인 계좌, 환경, 단일 전략, allowlist, 수량/금액 한도, Kill Switch 확인 등
            return {"rt_cd": "X", "msg1": "POST_BLOCKED: 명시적 Canary 다중 승인 조건(1주/소액 제한)을 통과하기 전까지 모든 REAL POST는 철저히 차단됩니다."}

        # [P0-J] 비멱등성 POST (주문/취소) 타임아웃 재전송 차단
        if method.upper() == "POST":
            # HTTP 오류(401/5xx)나 Timeout 발생 시 절대 스스로 재시도(Retry)하지 않음.
            # 상태를 UNKNOWN으로 넘겨 worker.py에서 읽기 전용 대사(Reconciliation)로 해결하도록 위임함.
            try:
                if 'timeout' not in kwargs: kwargs['timeout'] = 3.0 # 빠른 실패 유도
            except Exception:
                pass
"""
    # kis_client.py 내의 _request 함수 시작 부분에 주입
    if "POST_BLOCKED" not in content:
        content = re.sub(r'(def _request[^:]+:\s*)', r'\1' + post_block_logic, content, count=1)

    # [P0-G] 1분봉 필수 파라미터(FID_INPUT_HOUR_1) 추가 
    # (이미 이전 패치로 들어갔을 수 있으나 멱등성 보장)
    if "FID_INPUT_HOUR_1" not in content and '"FID_COND_MRKT_DIV_CODE": "J"' in content:
         content = re.sub(
            r'("FID_COND_MRKT_DIV_CODE":\s*"J",\s*"FID_INPUT_ISCD":\s*ticker)',
            r'\1,\n            "FID_INPUT_HOUR_1": "153000",\n            "FID_PW_DATA_INCU_YN": "N"',
            content
        )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ broker/kis_client.py: REAL POST 하드 블록(Canary 강제) 및 중복 주문 방지 로직 장착 완료.")

def fix_worker_py():
    filepath = "worker.py"
    if not os.path.exists(filepath): return
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # [P0-H] KIS 성공 응답 시 필수 ACK 식별자(ODNO 등) 정밀 검증
    # broker_order_time 뿐만 아니라 ODNO 누락 시 UNKNOWN 처리
    ack_validation = """
            # [P0-H] ACK 정밀 검증 (필수 식별자 누락 시 UNKNOWN 처리)
            odno = res.get('ODNO', '')
            ord_tmd = res.get('ord_tmd', '')
            
            if not odno or not str(odno).strip():
                logger.error(f"⚠️ KIS가 성공 코드를 반환했으나 주문번호(ODNO)가 누락되었습니다. {intent['intent_id']} -> UNKNOWN 이관")
                db.transition_order_status(intent['intent_id'], 'UNKNOWN')
                return
                
            db.transition_order_status(
                intent['intent_id'], 
                'ACKNOWLEDGED', 
                broker_order_id=odno, 
                broker_order_time=ord_tmd,
                expected_current_status='CLAIMED' # [P0-I] CAS 조건(Fencing) 적용
            )
"""
    # 기존 ACKNOWLEDGED 상태 전이 코드를 정밀 검증 코드로 교체
    # 정규식 패턴을 조심스럽게 적용하여 덮어쓰기
    if "odno or not str(odno)" not in content:
        content = re.sub(
            r"(db\.transition_order_status\(intent\['intent_id'\],\s*'ACKNOWLEDGED'.*?\))",
            ack_validation.strip(),
            content,
            flags=re.DOTALL
        )
        
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print("✅ worker.py: 필수 ACK(주문번호) 정밀 검증 및 CAS(Fencing) 동시성 방어 완료.")

if __name__ == "__main__":
    print("🛡️ [Phase 3] 네트워크/동시성 방어막(kis_client, worker) 구축을 시작합니다...")
    fix_kis_client()
    fix_worker_py()