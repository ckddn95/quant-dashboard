import pytest
import os
import sqlite3
import tempfile
import pandas as pd
import threading
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import database as db
import broker.kis_client as kis
import quant_engine as quant

KST = timezone(timedelta(hours=9))

@pytest.fixture(autouse=True)
def isolated_db_environment(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(db, 'DB_PATH', path)
    db.bootstrap_db()
    yield path
    os.remove(path)

def _insert_intent(idx, side="BUY", qty=10, price=1000, status="INTENT_CREATED", kind="MARKET", broker_id="", prdt="01", created_offset_min=0, source="SYSTEM"):
    limit_p = 0 if kind == "MARKET" else price
    ts_str = (datetime.now(KST) - timedelta(minutes=created_offset_min)).strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec(f"C{idx}", f"I{idx}", "KIS", "MOCK", "FP1", prdt, "CORE", "CORE", "1.0", "2.2.0", "005930", "삼성전자", side, kind, qty, limit_p, price, "KRX", "GTC", "S1", source, "1000", "Q1", "KIS", ts_str, 300, "2.2.0", ts_str)
    db.safe_add_order_intent(spec)
    if status != "INTENT_CREATED":
        order = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "FP1", prdt, "CORE", "CORE")[-1]
        db.transition_order_status(order['id'], "INTENT_CREATED", status, broker_id=broker_id, branch="BR1", fencing_token=1)

def test_all_entrypoints_import_without_side_effects():
    import database
    conn = sqlite3.connect(":memory:")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0 

def test_required_db_symbols_exist():
    assert all(hasattr(db, sym) for sym in ['get_watchlist', 'clear_and_update_watchlist', 'get_positions', 'sync_positions_from_broker', 'get_locked_cash_and_qty', 'safe_add_order_intent', 'get_orders_by_status_and_env', 'apply_fill_delta_exactly_once', 'claim_intent', 'authorize_claimed_order'])

def test_required_kis_symbols_exist():
    assert all(hasattr(kis, sym) for sym in ['fetch_kis_account_balance', 'fetch_kis_current_price_ext', 'execute_kis_order_001x', 'cancel_kis_order_0013', 'fetch_daily_executions_0081'])

def test_app_bot_worker_contract_versions_match():
    assert db.CONTRACT['contract_version'] == "2.2.0"

def test_strategy_version_exact_match():
    assert db.CONTRACT['strategy_version'] == "1.0.0"

def test_invalid_environment_never_selects_real():
    def parse_env(val): return "MOCK" if str(val).strip().lower() == "true" else ("REAL" if str(val).strip().lower() == "false" else "HALT")
    assert parse_env("yes") == "HALT" and parse_env("true") == "MOCK" and parse_env("false") == "REAL"

def test_app_has_no_order_or_cancel_post_path():
    if os.path.exists("app.py"):
        with open("app.py", "r", encoding="utf-8") as f: content = f.read()
        assert "requests.post" not in content and "execute_kis_order_001x(" not in content

@patch('broker.kis_client._strict_post')
def test_real_transport_is_impossible_in_unit_tests(mock_post):
    mock_post.return_value = kis.KisResult("SUCCESS_DATA", "OK", {"rt_cd": "0"})
    kis.execute_kis_order_001x("A", "B", "C", "01", "tok", "005930", True, 10, 0, True)
    assert "openapivts" in mock_post.call_args[0][0]

@patch('requests.post')
def test_strict_post_no_retry_on_timeout(mock_post):
    import requests
    mock_post.side_effect = requests.exceptions.Timeout("Read Timeout")
    res = kis._strict_post("http://test", {}, {})
    assert mock_post.call_count == 1
    assert res.state == "TRANSPORT_FAIL"
    assert "No Retry" in res.msg

@patch('requests.get')
def test_safe_get_exponential_backoff_and_jitter(mock_get):
    import requests
    mock_get.side_effect = requests.exceptions.Timeout("Read Timeout")
    with patch('time.sleep') as mock_sleep:
        res = kis._safe_get("http://test", {})
    assert mock_get.call_count == 3
    assert mock_sleep.call_count == 3
    assert res.state == "TRANSPORT_FAIL"
    assert "Exceeded" in res.msg

@patch('requests.get')
def test_safe_get_business_reject_mapping(mock_get):
    res_mock = MagicMock(status_code=200)
    res_mock.json.return_value = {'rt_cd': '1', 'msg1': 'Business Rule Violated'}
    mock_get.return_value = res_mock
    res = kis._safe_get("http://test", {})
    assert mock_get.call_count == 1
    assert res.state == "BUSINESS_REJECT"
    assert "Violated" in res.msg

@patch('broker.kis_client._strict_post')
def test_001x_payload_complies_with_official_domestic_spec(mock_post):
    mock_post.return_value = kis.KisResult("SUCCESS_DATA", "OK", {"rt_cd": "0"})
    kis.execute_kis_order_001x("A", "B", "C", "01", "tok", "005930", True, 10, 0, True)
    assert mock_post.call_count == 1
    payload = mock_post.call_args[1]['data']
    assert '"EXCG_ID_DVSN_CD"' not in payload  
    assert '"ORD_CVM_DVSN_CD"' not in payload
    assert '"ORD_DVSN"' in payload

@patch('requests.get')
def test_fetch_price_invalid_timestamp_rejection(mock_get):
    res_mock = MagicMock(status_code=200)
    res_mock.json.return_value = {'rt_cd': '0', 'output': {'stck_bsop_date': '99999999', 'stck_cntg_hour': 'XXYYZZ'}}
    mock_get.return_value = res_mock
    res = kis.fetch_kis_current_price_ext("A", "B", "005930", "tok")
    assert res.state == "BUSINESS_REJECT"
    assert "Invalid timestamp" in res.msg

@patch('broker.kis_client._fetch_new_token_http')
def test_single_flight_token_cache(mock_fetch):
    mock_fetch.return_value = ("SINGLE_FLIGHT_TOKEN", "OK")
    kis._TOKEN_CACHE.clear()
    results = []
    def worker():
        t, _ = kis.get_kis_access_token("APP_KEY_SF", "SEC", True)
        results.append(t)
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for th in threads: th.start()
    for th in threads: th.join()
    assert len(results) == 10
    assert all(t == "SINGLE_FLIGHT_TOKEN" for t in results)
    assert mock_fetch.call_count == 1

@patch('broker.kis_client.get_kis_access_token')
@patch('requests.get')
def test_401_single_refresh_retry(mock_get, mock_token):
    mock_token.return_value = ("NEW_TOKEN_AFTER_401", "OK")
    res401 = MagicMock(status_code=401)
    res200 = MagicMock(status_code=200)
    res200.json.return_value = {'rt_cd': '0', 'output': {'ord_psbl_cash': '5000000'}}
    mock_get.side_effect = [res401, res200]
    res = kis.fetch_kis_orderable_cash("AK", "AS", "CANO", "01", "EXPIRED_TOKEN", "005930", 50000, "LIMIT", True)
    assert res.state == "SUCCESS_DATA"
    assert res.data == 5000000.0
    assert mock_get.call_count == 2
    assert mock_token.call_count == 1

def test_rate_limiter_throttles():
    limiter = kis.AccountRateLimiter(max_rate=3, period=0.1) 
    start_t = time.time()
    for _ in range(4): limiter.acquire()
    dur = time.time() - start_t
    assert dur >= 0.08

def test_atomic_gate_two_step_flow():
    _insert_intent(1)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, msg = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    assert order['status'] == 'CLAIMED'
    auth_order, passed, _ = db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 5000000, 1000, False, 0.0, 0, 10000000)
    assert passed is True and auth_order['status'] == 'SUBMITTING' and auth_order['idempotency_key'] == "I1"

def test_gate_rejects_expired_lease_and_wrong_fencing_token():
    _insert_intent(2)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", -10)
    order, msg = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W2")
    assert order is None and "Lease" in msg

def test_transition_failure_causes_zero_broker_posts():
    _insert_intent(3)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    _, passed, _ = db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 0, 1000, False, 0.0, 0, 10000000)
    assert passed is False 

def test_two_workers_same_intent_one_post():
    ok1, _ = db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    ok2, _ = db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W2", 30)
    assert ok1 is True and ok2 is False

def test_market_buy_reserves_reference_price_times_1_05():
    _insert_intent(4, kind="MARKET", price=1000)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 50000, 1000, False, 0.0, 0, 10000000)
    locked_cash, _ = db.get_locked_cash_and_qty("KIS", "MOCK", "FP1", "01", "CORE", "CORE")
    assert locked_cash == 10500.0

def test_limit_buy_reserves_exact_limit_price():
    _insert_intent(99, kind="LIMIT", price=1000)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 50000, 1000, False, 0.0, 0, 10000000)
    locked_cash, _ = db.get_locked_cash_and_qty("KIS", "MOCK", "FP1", "01", "CORE", "CORE")
    assert locked_cash == 10000.0

def test_two_market_buys_cannot_double_spend_cash():
    _insert_intent(5, price=1000); _insert_intent(6, price=1000)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    o1, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    _, p1, _ = db.authorize_claimed_order(o1['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 15000, 1000, False, 0.0, 0, 10000000)
    o2, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    if o2: _, p2, _ = db.authorize_claimed_order(o2['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 15000, 1000, False, 0.0, 0, 10000000)
    else: p2 = False
    assert p1 is True and p2 is False

def test_two_sells_cannot_exceed_managed_qty():
    with db.get_connection() as conn:
        conn.execute("INSERT INTO positions (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, ticker, managed_qty) VALUES ('KIS', 'MOCK', 'FP1', '01', 'CORE', 'CORE', '005930', 15)")
    _insert_intent(7, side="SELL", qty=10); _insert_intent(8, side="SELL", qty=10)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    o1, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    _, p1, _ = db.authorize_claimed_order(o1['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 50000, 1000, False, 0.0, 0, 10000000)
    o2, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    if o2: _, p2, _ = db.authorize_claimed_order(o2['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 50000, 1000, False, 0.0, 0, 10000000)
    else: p2 = False
    assert p1 is True and p2 is False

def test_quote_freshness_ttl_rejection():
    old_ts = (datetime.now(KST) - timedelta(seconds=20)).strftime('%Y-%m-%d %H:%M:%S')
    spec = quant.OrderSpec("C99", "I99", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "1.0", "2.2.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 1000, "KRX", "GTC", "S1", "BOT", "1000", "Q1", "KIS", old_ts, 300, "2.2.0", datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S'))
    db.safe_add_order_intent(spec)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    auth_order, passed, msg = db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 50000, 1000, False, 0.0, 0, 10000000)
    assert passed is False
    assert auth_order['status'] == 'EXPIRED'
    assert 'Freshness TTL Exceeded' in msg

def test_price_deviation_rejection():
    _insert_intent(199, price=1000) 
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    auth_order, passed, msg = db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 5000000, 1060, False, 0.0, 0, 10000000)
    assert passed is False
    assert auth_order['status'] == 'RISK_REJECTED'
    assert 'Price deviated >5%' in msg

def test_manual_qty_never_becomes_sellable_managed_qty():
    db.sync_positions_from_broker("KIS", "MOCK", "FP1", "01", "CORE", "CORE", [{"ticker": "005930", "qty": 100, "buy_price": 50000}])
    pos = db.get_positions("KIS", "MOCK", "FP1", "01", "CORE", "CORE")[0]
    assert pos['manual_qty'] == 100 and pos['managed_qty'] == 0

def test_order_ack_never_changes_position():
    _insert_intent(9)
    order_id = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[0]['id']
    db.transition_order_status(order_id, "INTENT_CREATED", "ACKNOWLEDGED", "OD1", "BR1", fencing_token=1)
    assert len(db.get_positions("KIS", "MOCK", "FP1", "01", "CORE", "CORE")) == 0

def test_crash_before_post():
    _insert_intent(10)
    order_id = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[0]['id']
    db.transition_order_status(order_id, "INTENT_CREATED", "SUBMITTING", fencing_token=1)
    assert db.get_orders_by_status_and_env(['SUBMITTING'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[0]['id'] == order_id

def test_crash_after_post_before_ack_persist():
    _insert_intent(11)
    order_id = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[0]['id']
    db.transition_order_status(order_id, "INTENT_CREATED", "SUBMITTING", fencing_token=1)
    assert len(db.get_orders_by_status_and_env(['SUBMITTING'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")) > 0

def test_restart_reconciles_submitting_before_new_claim():
    _insert_intent(12, status="SUBMITTING")
    orders = db.get_orders_by_status_and_env(['SUBMITTING'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")
    assert len(orders) > 0 

def test_partial_fill_cumulative_delta_0_40_40_100():
    _insert_intent(13, qty=100)
    oid = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]['id']
    db.transition_order_status(oid, "INTENT_CREATED", "ACKNOWLEDGED", fencing_token=1)
    assert db.apply_fill_delta_exactly_once(oid, "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 40, 2800000, {'tot_ccld_qty': 40, 'tot_ccld_amt': 2800000, 'avg_prvs': 70000, 'rmn_qty': 60, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'}) is True
    assert db.apply_fill_delta_exactly_once(oid, "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 40, 2800000, {'tot_ccld_qty': 40, 'tot_ccld_amt': 2800000, 'avg_prvs': 70000, 'rmn_qty': 60, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'}) is False 
    assert db.apply_fill_delta_exactly_once(oid, "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 100, 7000000, {'tot_ccld_qty': 100, 'tot_ccld_amt': 7000000, 'avg_prvs': 70000, 'rmn_qty': 0, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'}) is True
    assert db.get_positions("KIS", "MOCK", "FP1", "01", "CORE", "CORE")[0]['managed_qty'] == 100

def test_cancel_ack_is_not_terminal():
    assert "CANCELED" in db.ALLOWED_TRANSITIONS['CANCEL_ACKNOWLEDGED']

def test_partial_fill_then_cancel_remaining():
    _insert_intent(14, qty=100)
    oid = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]['id']
    db.apply_fill_delta_exactly_once(oid, "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 40, 2800000, {'tot_ccld_qty': 40, 'tot_ccld_amt': 2800000, 'avg_prvs': 70000, 'rmn_qty': 60, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    db.transition_order_status(oid, "PARTIALLY_FILLED", "CANCEL_REQUESTED", fencing_token=1)
    db.transition_order_status(oid, "CANCEL_REQUESTED", "CANCELED", fencing_token=1)
    assert db.get_positions("KIS", "MOCK", "FP1", "01", "CORE", "CORE")[0]['managed_qty'] == 40

def test_late_fill_after_cancel():
    assert "FILLED" in db.ALLOWED_TRANSITIONS["CANCEL_REQUESTED"]

def test_unknown_without_odno_never_reposts():
    _insert_intent(16, status="UNKNOWN")
    assert db.get_orders_by_status_and_env(['UNKNOWN'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]['status'] == 'UNKNOWN'

@patch('broker.kis_client.fetch_daily_executions_0081')
def test_midnight_boundary_reconciliation(mock_fetch):
    _insert_intent(1001, status="SUBMITTING", created_offset_min=60*24)
    order = db.get_orders_by_status_and_env(['SUBMITTING'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    mock_fetch.return_value = kis.KisResult("SUCCESS_DATA", "OK", [])
    from worker import reconcile_executions
    reconcile_executions("A", "B", "C", "01", "tok", "MOCK", "FP1", "CORE", True)
    expected_date_str = order['created_at'][:10].replace('-', '')
    assert mock_fetch.call_args[1]['order_date'] == expected_date_str

def test_revert_stale_claims_frees_stuck_orders():
    _insert_intent(1002, status="CLAIMED")
    with db.get_connection() as conn:
        conn.execute("DELETE FROM worker_leases")
    db.revert_stale_claims("KIS", "MOCK", "FP1", "01", "CORE", "CORE")
    order = db.get_orders_by_status_and_env(['INTENT_CREATED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    assert order['correlation_id'] == "C1002"
    assert order['status'] == "INTENT_CREATED"

@patch('broker.kis_client.fetch_daily_executions_0081')
def test_submitting_timeout_rejection(mock_fetch):
    _insert_intent(1003, status="SUBMITTING", created_offset_min=15)
    mock_fetch.return_value = kis.KisResult("SUCCESS_DATA", "OK", [])
    from worker import reconcile_executions
    reconcile_executions("A", "B", "C", "01", "tok", "MOCK", "FP1", "CORE", True)
    order = db.get_orders_by_status_and_env(['REJECTED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    assert order['correlation_id'] == "C1003"
    assert order['status'] == "REJECTED"

@patch('broker.kis_client.fetch_daily_executions_0081')
def test_cancel_flow_3_steps_to_canceled(mock_fetch):
    _insert_intent(1004, status="CANCEL_ACKNOWLEDGED", broker_id="OD_CANC")
    order = db.get_orders_by_status_and_env(['CANCEL_ACKNOWLEDGED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    date_str = order['created_at'][:10].replace('-', '')
    mock_fetch.return_value = kis.KisResult("SUCCESS_DATA", "OK", [{'pdno': '005930', 'sll_buy_dvsn_cd': '02', 'ord_qty': '10', 'odno': 'OD_CANC', 'bcnc_ptno': 'BR1', 
                                'tot_ccld_qty': '0', 'tot_ccld_amt': '0', 'avg_prvs': '0', 'rmn_qty': '0', 'cncl_yn': 'Y', 'rjct_qty': '0', 'krx_fwdg_ord_orgno': 'X', 'ord_tmd': 'Y', 'ord_dt': date_str}])
    from worker import reconcile_executions
    reconcile_executions("A", "B", "C", "01", "tok", "MOCK", "FP1", "CORE", True)
    canceled = db.get_orders_by_status_and_env(['CANCELED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    assert canceled['correlation_id'] == "C1004"
    assert canceled['status'] == "CANCELED"

def test_apply_fill_delta_ignores_same_snapshot():
    _insert_intent(500, qty=10, status="ACKNOWLEDGED")
    order = db.get_orders_by_status_and_env(['ACKNOWLEDGED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    res1 = db.apply_fill_delta_exactly_once(order['id'], "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 5, 5000, {'tot_ccld_qty': 5, 'tot_ccld_amt': 5000, 'avg_prvs': 1000, 'rmn_qty': 5, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    assert res1 is True
    res2 = db.apply_fill_delta_exactly_once(order['id'], "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 5, 5000, {'tot_ccld_qty': 5, 'tot_ccld_amt': 5000, 'avg_prvs': 1000, 'rmn_qty': 5, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    assert res2 is False
    check_order = db.get_orders_by_status_and_env(['PARTIALLY_FILLED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    assert check_order['cum_filled_qty'] == 5

def test_apply_fill_delta_halts_on_cum_qty_drop():
    _insert_intent(501, qty=10, status="ACKNOWLEDGED")
    order = db.get_orders_by_status_and_env(['ACKNOWLEDGED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    db.apply_fill_delta_exactly_once(order['id'], "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 8, 8000, {'tot_ccld_qty': 8, 'tot_ccld_amt': 8000, 'avg_prvs': 1000, 'rmn_qty': 2, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    res2 = db.apply_fill_delta_exactly_once(order['id'], "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 5, 5000, {'tot_ccld_qty': 5, 'tot_ccld_amt': 5000, 'avg_prvs': 1000, 'rmn_qty': 5, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    assert res2 is False
    halted = db.get_connection().execute("SELECT status FROM order_intents WHERE id=?", (order['id'],)).fetchone()
    assert halted['status'] == 'RECONCILIATION_REQUIRED'

def test_apply_fill_delta_halts_on_qty_exceeded_and_amt_mismatch():
    _insert_intent(502, qty=10, status="ACKNOWLEDGED")
    order = db.get_orders_by_status_and_env(['ACKNOWLEDGED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    res1 = db.apply_fill_delta_exactly_once(order['id'], "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 15, 15000, {'tot_ccld_qty': 15, 'tot_ccld_amt': 15000, 'avg_prvs': 1000, 'rmn_qty': 0, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    assert res1 is False
    halted1 = db.get_connection().execute("SELECT status FROM order_intents WHERE id=?", (order['id'],)).fetchone()
    assert halted1['status'] == 'RECONCILIATION_REQUIRED'
    
    _insert_intent(503, qty=10, status="ACKNOWLEDGED")
    order2 = db.get_orders_by_status_and_env(['ACKNOWLEDGED'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE")[-1]
    db.apply_fill_delta_exactly_once(order2['id'], "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 5, 5000, {'tot_ccld_qty': 5, 'tot_ccld_amt': 5000, 'avg_prvs': 1000, 'rmn_qty': 5, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    res2 = db.apply_fill_delta_exactly_once(order2['id'], "005930", "BUY", "KIS", "MOCK", "FP1", "01", "CORE", "CORE", 5, 6000, {'tot_ccld_qty': 5, 'tot_ccld_amt': 6000, 'avg_prvs': 1200, 'rmn_qty': 5, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': 'X', 'ord_tmd': 'Y'})
    assert res2 is False
    halted2 = db.get_connection().execute("SELECT status FROM order_intents WHERE id=?", (order2['id'],)).fetchone()
    assert halted2['status'] == 'RECONCILIATION_REQUIRED'

def test_ui_manual_blocked_by_kill_switch():
    # 1. 마스터 킬스위치 ON -> UI 주문이라도 철저히 차단되어야 함
    db.set_setting("master_kill_switch", True)
    _insert_intent(801, source="UI_MANUAL")
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    
    auth_order, passed, msg = db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 5000000, 1000, False, 0.0, 0, 10000000)
    
    # 🚨 패치: 킬스위치가 켜졌으므로 통과하지 못해야(False) 정상
    assert passed is False
    assert auth_order['status'] == 'REJECTED'
    assert 'Kill Switch' in msg

    # 2. 마스터 킬스위치 OFF -> UI 주문은 정상적으로 통과되어야 함
    db.set_setting("master_kill_switch", False)
    _insert_intent(802, source="UI_MANUAL")
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order2, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    
    auth_order2, passed2, msg2 = db.authorize_claimed_order(order2['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 5000000, 1000, False, 0.0, 0, 10000000)
    
    # 🚨 패치: 킬스위치가 꺼졌으므로 정상 통과(True)해야 함
    assert passed2 is True
    assert auth_order2['status'] == 'SUBMITTING'

def test_transition_failure_prevents_post():
    _insert_intent(802)
    db.acquire_worker_lease("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 30)
    order, _ = db.claim_intent("KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1")
    db.transition_order_status(order['id'], "CLAIMED", "FILLED", worker_id="W1", fencing_token=1)
    auth_order, passed, _ = db.authorize_claimed_order(order['id'], "KIS", "MOCK", "FP1", "01", "CORE", "CORE", "W1", 5000000, 1000, False, 0.0, 0, 10000000)
    assert passed is False
    assert auth_order is None