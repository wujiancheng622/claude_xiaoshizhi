"""FORWARD_MANUAL 端到端（TEST_ONLY 快照）：只产生预约与人工清单；导入人工确认与券商回报；对账；恢复。"""
import copy
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from smallcap.bundle import load_bundle
from smallcap.forward import ForwardSession, import_broker_events, reconcile_account, resume_actions
from smallcap.journal import Journal
from smallcap.snapshot import SnapshotMarket, export_snapshot
from smallcap.synthetic import SyntheticMarket
from tests.acceptance import case
from tools.make_engineering_bundle import build


def forward_bundle():
    b = build("FORWARD_MANUAL")
    b["profiles"]["bootstrap"]["payload"]["cash_amount"] = "500000"
    return load_bundle(b)


class ForwardManual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m = SyntheticMarket(seed=5, n=300, start=date(2022, 1, 3), end=date(2024, 1, 31), with_events=False)
        cls.snap = SnapshotMarket(json.loads(json.dumps(export_snapshot(m, date(2022, 6, 1), date(2024, 1, 31)))))

    @case("G01-10", "G09-10", "G12-12", "G12-15")
    def test_forward_chain(self):
        b = forward_bundle()
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            fs = ForwardSession(b, j, self.snap)
            out = fs.step(date(2024, 1, 5), "INIT")
            self.assertEqual(out["startup"]["status"], "STARTED")
            self.assertEqual(fs.step(date(2024, 1, 5), "INIT")["status"], "ALREADY_DONE")
            fs.step(date(2024, 1, 8), "PREOPEN")
            ex = fs.step(date(2024, 1, 8), "EXEC")
            L = fs.runner.ledger
            cl = ex["checklist"]
            self.assertGreaterEqual(len(cl), 24)
            self.assertTrue(all(L.orders[c["client_order_id"]].status == "RESERVED" for c in cl))   # 未报出
            self.assertEqual(L.trade_log, [])
            kinds = {e["kind"] for e in j.read_committed()}
            self.assertNotIn("ORDER_SUBMIT", kinds)
            self.assertNotIn("BROKER_FILL", kinds)
            self.assertEqual(fs.step(date(2024, 1, 8), "EXEC")["status"], "ALREADY_DONE")
            # 预约已锁定现金与槽位：可用现金 = 入金 − 全部买单预约
            self.assertEqual(L.own_free_cash("2024-01-08"), Decimal(500000) - L.cash_commitments())
            first, second, third = cl[0], cl[1], cl[2]
            at = "2024-01-08T10:01:00+08:00"
            rows = [{"kind": "MANUAL_SUBMITTED", "client_order_id": first["client_order_id"], "occurred_at": at},
                    {"kind": "ACK", "client_order_id": first["client_order_id"], "broker_event_id": "a1",
                     "broker_order_id": "B1", "occurred_at": at},
                    {"kind": "FILL", "client_order_id": first["client_order_id"], "trade_id": "T-1", "qty": first["qty"],
                     "price": first["limit"], "trade_date": "2024-01-08", "occurred_at": "2024-01-08T10:01:20+08:00",
                     "sellable_from": "2024-01-09", "tax_acquisition_date": "2024-01-08"},
                    {"kind": "TERMINAL", "client_order_id": first["client_order_id"], "broker_event_id": "z1",
                     "status": "FILLED", "cum_qty": first["qty"],
                     "cum_value": str(Decimal(first["limit"]) * first["qty"]), "source_revision": 1,
                     "occurred_at": "2024-01-08T10:01:30+08:00"},
                    {"kind": "MANUAL_NOT_SUBMITTED", "client_order_id": second["client_order_id"], "occurred_at": at},
                    {"kind": "MANUAL_SUBMITTED", "client_order_id": third["client_order_id"], "occurred_at": at}]
            res = import_broker_events(L, j, rows)
            self.assertEqual((res["applied"], res["conflicts"]), (6, []))
            again = import_broker_events(L, j, rows)                       # 重复导入：完全幂等
            self.assertEqual((again["applied"], again["duplicates"]), (0, 6))
            self.assertEqual(L.orders[first["client_order_id"]].status, "FILLED")
            self.assertEqual(L.orders[second["client_order_id"]].status, "CANCELED")
            self.assertEqual(L.orders[third["client_order_id"]].status, "SENT_UNKNOWN")
            acts = resume_actions(L, __import__("datetime").datetime(2024, 1, 9, 1, tzinfo=__import__("datetime").timezone.utc))
            self.assertIn(third["client_order_id"], [q["client_order_id"] for q in acts["query_orders"]])
            # 未导入人工确认的预约：须先确认是否已报出，预约在此之前不释放
            pending = {q["client_order_id"]: q["status"] for q in acts["query_orders"]}
            self.assertEqual(pending.get(cl[3]["client_order_id"]), "RESERVED")
            self.assertEqual(acts["resend"], [])
            sid = first["security_id"]
            ok = reconcile_account(L, {"positions": {sid: {"volume": first["qty"]}}, "cash_book": str(L.cash)})
            self.assertTrue(ok.ok)
            bad = reconcile_account(L, {"positions": {sid: {"volume": first["qty"] - 100}}, "cash_book": str(L.cash)})
            self.assertEqual(bad.codes(), ["POSITION_MISMATCH"])
            self.assertEqual(L.positions[sid].quantity, first["qty"])         # 不覆盖余额
            j.release()
            # 断点：另一个进程恢复后，账户与日志一致，且不会重复入账
            j2 = Journal(d)
            j2.acquire()
            fs2 = ForwardSession(b, j2, self.snap)
            self.assertEqual(fs2.runner.ledger.cash, L.cash)
            self.assertEqual(fs2.step(date(2024, 1, 8), "EXEC")["status"], "ALREADY_DONE")
            j2.release()

    def test_blocked_without_capital(self):
        b = build("FORWARD_MANUAL")
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            with self.assertRaises(Exception) as cm:
                ForwardSession(load_bundle(b), j, self.snap)
            self.assertIn("ACTUAL_CAPITAL_NOT_PROVIDED", str(cm.exception))
            j.release()


if __name__ == "__main__":
    unittest.main()
