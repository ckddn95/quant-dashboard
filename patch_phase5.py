import os
import re

def sync_system_contract():
    filepath = "system_contract.yaml"
    if not os.path.exists(filepath):
        print(f"❌ {filepath} 파일이 없습니다.")
        return
        
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    new_rules = """
# [Phase 5] 퀀트 시스템 운용 원칙 동기화 (V17 마이그레이션 완료)
rules:
  execution:
    - name: "1분봉 2연속 확인 원칙"
      description: "매수 및 추세매도는 반드시 '완성된 1분봉 2개'가 연속으로 신호를 만족할 때만 진입한다."
    - name: "즉각 대응 원칙"
      description: "손절 및 트레일링 스탑은 1분봉 완성을 기다리지 않고, 실시간 호가(Tick)를 기준으로 즉시 발동한다."
    - name: "수동/자동 물량 격리"
      description: "수동으로 매수한 물량(Manual)은 봇이 익절/손절 계산에 포함시키지 않으며 독립적으로 관리한다."
  
  canary_deployment:
    status: "POST_BLOCKED"  # 현재 모든 REAL 매매는 기술적으로 차단됨
    approval_gate:
      required_steps:
        - "단일 계좌, 단일 전략(Core or Satellite) 명시적 선택"
        - "유동성이 풍부한 승인 종목 1개 제한 (예: 005930)"
        - "최초 주문은 무조건 1주, 혹은 최대 10만 원 이하 소액(Canary) 진행"
        - "시스템 관리자가 PC 앞에서 체결-DB 반영을 육안 확인 후 무인 운영 승인"
"""
    if "canary_deployment:" not in content:
        content += "\n" + new_rules
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print("✅ system_contract.yaml: 알고리즘 백서 및 Canary 실전 승인 게이트 동기화 완료!")
    else:
        print("✅ system_contract.yaml: 이미 최신 백서 규칙이 적용되어 있습니다.")

if __name__ == "__main__":
    sync_system_contract()