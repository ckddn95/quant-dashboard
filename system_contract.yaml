whitepaper_version: "1.1.0"
contract_version: "1.1.0"
database_schema_version: "v2"

strategy:
  CORE:
    ma200: true
    buf: 0.015
    sl: -0.15
    alloc: 0.35
    ts_tgt: 0.30
    ts_drp: -0.10
    cd: 60
    min_h: 5
    boost: true

  SATELLITE:
    ma200: true
    buf: 0.010
    sl: -0.12
    alloc: 0.20
    ts_tgt: 0.20
    ts_drp: -0.07
    cd: 30
    min_h: 3
    boost: true

simulation_rules:
  execution_timing: "T+1 Open"
  intraday_adverse_first: true
  assumed_cost_pct_per_side: 0.0025
  test1_scan_freq: "DAILY"
  test2_test3_scan_freq: "WEEKLY"
  weekly_cutoff: "매주 마지막 KRX 거래일 종가"

allowed_state_transitions:
  INTENT_CREATED: [CLAIMED, CANCELED, QUARANTINED]
  CLAIMED: [SUBMITTING, RISK_REJECTED, CANCELED, EXPIRED]
  SUBMITTING: [ACKNOWLEDGED, UNKNOWN, REJECTED]
  ACKNOWLEDGED: [PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED, EXPIRED]
  UNKNOWN: [ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, REJECTED, CANCEL_REQUESTED, EXPIRED, RECONCILIATION_REQUIRED]
  PARTIALLY_FILLED: [FILLED, CANCEL_REQUESTED, EXPIRED, RECONCILIATION_REQUIRED]
  CANCEL_REQUESTED: [CANCEL_ACKNOWLEDGED, CANCEL_UNKNOWN, PARTIALLY_FILLED, FILLED, RECONCILIATION_REQUIRED]
  CANCEL_ACKNOWLEDGED: [CANCELED, PARTIALLY_FILLED, FILLED, CANCEL_UNKNOWN, RECONCILIATION_REQUIRED]
  CANCEL_UNKNOWN: [CANCEL_ACKNOWLEDGED, CANCELED, PARTIALLY_FILLED, FILLED, RECONCILIATION_REQUIRED]
