# SUBMITTING 직전의 멱등성 및 원자적 검증 (개념 코드)
def atomic_submit_gate(intent, ctx: RiskContext, current_fencing_token):
    # 1. 최신 상태 DB CAS (Compare-And-Swap) 조회
    db.execute("BEGIN IMMEDIATE")
    
    # 2. 계좌, 환경, 포트폴리오 일치 여부 확인
    if intent.account_id != ctx.account_id or intent.environment != ctx.env:
        db.rollback(); return False, "계좌/환경 불일치 (격리 위반)"
        
    # 3. 글로벌 킬 스위치 및 워커 펜싱 토큰 확인
    if ctx.is_kill_switch_on or current_fencing_token < db.get_lease_token():
        db.rollback(); return False, "제어권 상실 또는 킬 스위치 동작"
        
    # 4. 현금 잔고 및 노출 한도 재확인 (Risk Rejected)
    if ctx.usable_cash < intent.expected_order_value:
        db.transition_status(intent.id, 'CLAIMED', 'RISK_REJECTED')
        db.commit(); return False, "현금 부족"
        
    # 5. SUBMITTING으로 전환 후 락 해제
    db.execute("UPDATE order_intents SET status='SUBMITTING' WHERE id=? AND status='CLAIMED'", (intent.id,))
    db.commit()
    return True, "PASS"
