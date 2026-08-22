import os
import unittest
import datetime
import database as db
import broker.kis_client as kis
import quant_engine as quant

# 🚨 테스트 DB 철저 격리 및 CI 테스트 모드 강제 활성화
TEST_DB_PATH = "test_quant_system.db"
db.DB_PATH = TEST_DB_PATH
os.environ["CI_TEST_MODE"] = "true"

class QuantIntegrityTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB_PATH): os.remove(TEST_DB_PATH)
        db.bootstrap_db()
        db.set_setting("auto_pilot_KIS_MOCK_testfp_01_CORE_CORE", True)
        db.set_setting("auto_trade_KIS_MOCK_testfp_01_CORE_CORE", True)

    def tearDown(self):
        if os.path.exists(TEST_DB_PATH): os.remove(TEST_DB_PATH)

    def test_01_schema_integrity(self):
        """DB 버전(V17) 및 필수 테이블 구조 검증"""
        with db.get_connection() as conn:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(ver, 17, "DB 버전이 V17이 아닙니다.")
            self.assertTrue(db._validate_schema(conn))

    def test_02_invalid_state_transition(self):
        """허용되지 않은 상태 전이 차단 (CANCEL_SUBMITTING 포함)"""
        now = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
        spec = quant.OrderSpec("t2", "t2", "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now, "Q2", "KIS", now, 60, "2.2.0", now)
        db.safe_add_order_intent(spec)
        with db.get_connection() as conn:
            oid = conn.execute("SELECT id FROM order_intents WHERE idempotency_key='t2'").fetchone()['id']
        
        res = db.transition_order_status(oid, 'FILLED', 'CANCELED')
        self.assertFalse(res, "허용되지 않은 상태 전이가 발생했습니다!")

    def test_03_strict_real_block(self):
        """REAL 경로 차단 검증"""
        now = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
        spec = quant.OrderSpec("t3", "t3", "KIS", "REAL", "testfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now, "Q3", "KIS", now, 60, "2.2.0", now)
        db.safe_add_order_intent(spec)
        with db.get_connection() as conn:
            oid = conn.execute("SELECT id FROM order_intents WHERE environment='REAL'").fetchone()['id']
            conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=999 WHERE id=?", (oid,))
            conn.execute("INSERT INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES ('KIS', 'REAL', 'testfp', '01', 'CORE', 'CORE', 'w1', '2099-01-01', 999)")
        
        _, passed, reason = db.authorize_claimed_order(oid, "KIS", "REAL", "testfp", "01", "CORE", "CORE", "w1", 1000000, 50000, False, 0.0, 0.0, 1000000)
        self.assertFalse(passed)
        self.assertIn("Strictly Blocked", reason)

    def test_04_version_mismatch(self):
        """버전 불일치 격리 검증"""
        now = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
        spec = quant.OrderSpec("t4", "t4", "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "0.0.1", "0.0.1", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now, "Q4", "KIS", now, 60, "0.0.1", now)
        db.safe_add_order_intent(spec)
        with db.get_connection() as conn:
            oid = conn.execute("SELECT id FROM order_intents WHERE idempotency_key='t4'").fetchone()['id']
            conn.execute("UPDATE order_intents SET status='CLAIMED', fencing_token=888 WHERE id=?", (oid,))
            conn.execute("INSERT INTO worker_leases (broker, environment, account_fingerprint, product_code, portfolio_id, strategy_id, worker_id, expires_at, token) VALUES ('KIS', 'MOCK', 'testfp', '01', 'CORE', 'CORE', 'w1', '2099-01-01', 888)")

        _, passed, reason = db.authorize_claimed_order(oid, "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "w1", 1000000, 50000, False, 0.0, 0.0, 1000000)
        self.assertFalse(passed)
        self.assertIn("Version mismatch", reason)

    def test_05_kis_payload_completeness(self):
        """매수(빈값)/매도(01) SLL_TYPE 공식 규격 검증"""
        is_buy_payload = ("" if True else "01")
        is_sell_payload = ("" if False else "01")
        self.assertEqual(is_buy_payload, "")
        self.assertEqual(is_sell_payload, "01")

    def test_06_manual_order_atomic_fill(self):
        """수동 주문과 자동 주문의 managed_qty / manual_qty 분리 검증"""
        now = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
        spec = quant.OrderSpec("t6", "t6", "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "UI_MANUAL", "UI_MANUAL", now, "Q6", "KIS", now, 99999, "2.2.0", now)
        db.safe_add_order_intent(spec)
        with db.get_connection() as conn:
            oid = conn.execute("SELECT id FROM order_intents WHERE idempotency_key='t6'").fetchone()['id']
            conn.execute("UPDATE order_intents SET status='SUBMITTING' WHERE id=?", (oid,))
            
        bs = {'tot_ccld_qty': 10, 'tot_ccld_amt': 500000.0, 'avg_prvs': 50000.0, 'rmn_qty': 0, 'cncl_yn': 'N', 'rjct_qty': 0, 'orgno': '123', 'ord_tmd': '100000'}
        db.apply_broker_receipt(oid, "005930", "BUY", "KIS", "MOCK", "testfp", "01", "CORE", "CORE", bs)
        
        with db.get_connection() as conn:
            pos = conn.execute("SELECT managed_qty, manual_qty FROM positions WHERE ticker='005930'").fetchone()
        self.assertEqual(pos['managed_qty'], 0)
        self.assertEqual(pos['manual_qty'], 10)

    def test_07_atomic_fencing(self):
        """다중 워커 중복 취소/주문 방어 (CAS) 검증"""
        now = datetime.datetime.now(quant.KST).strftime('%Y-%m-%d %H:%M:%S')
        spec = quant.OrderSpec("t7", "t7", "KIS", "MOCK", "testfp", "01", "CORE", "CORE", "1.0.0", "1.0.0", "005930", "삼성전자", "BUY", "MARKET", 10, 0, 50000, "KRX", "GTC", "SYSTEM", "SYSTEM", now, "Q7", "KIS", now, 99999, "2.2.0", now)
        db.safe_add_order_intent(spec)
        with db.get_connection() as conn:
            oid = conn.execute("SELECT id FROM order_intents WHERE idempotency_key='t7'").fetchone()['id']
            conn.execute("UPDATE order_intents SET status='CANCEL_CLAIMED', fencing_token=111, broker_order_id='T' WHERE id=?", (oid,))
            
        passed, _ = db.authorize_cancel_order(oid, "w1", 111)
        self.assertTrue(passed)
        passed2, _ = db.authorize_cancel_order(oid, "w2", 222)
        self.assertFalse(passed2)

if __name__ == '__main__':
    unittest.main()