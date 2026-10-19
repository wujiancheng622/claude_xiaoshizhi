import unittest
from datetime import date, time
from decimal import Decimal

from smallcap import contracts as C
from smallcap.account import Ledger
from smallcap.contracts import sh_dt
from smallcap.execution import can_submit_new
from smallcap.shared_cash import SharedCash, check_etf_view, double_count_check
from tests.acceptance import case
from tests.fixtures import EventFactory, test_fees, weekday_calendar


def sc_with(stock="100000", etf="0", broker="100000"):
    sc = SharedCash(Decimal(etf), Decimal(stock))
    rev = sc.broker_snapshot(Decimal(broker))
    return sc, rev


class G14(unittest.TestCase):
    @case("G14-01")
    def test_preapproval_reduces_own_free(self):
        sc, rev = sc_with()
        sc.authorize("STOCK", "b1", Decimal(30000), rev)
        sc.preapprove("c1", "STOCK", Decimal(30000), "o1")
        self.assertEqual(sc.own_free("STOCK"), Decimal(70000))

    @case("G14-02")
    def test_transition_to_freeze(self):
        sc, rev = sc_with()
        sc.authorize("STOCK", "b1", Decimal(30000), rev)
        sc.preapprove("c1", "STOCK", Decimal(30000), "o1")
        sc.broker_frozen("c1", Decimal(70000))
        self.assertEqual(sc.own_free("STOCK"), Decimal(70000))

    @case("G14-03")
    def test_two_gates(self):
        sc = SharedCash(Decimal(0), Decimal(82000))
        rev = sc.broker_snapshot(Decimal(80000))
        sc.authorize("STOCK", "b1", Decimal(10000), rev)
        sc.preapprove("c1", "STOCK", Decimal(10000), "o1")      # 已批准、尚未体现在券商冻结
        sc.due["STOCK"] = Decimal(2000)                         # 待付、未体现在券商可用中
        self.assertEqual(sc.own_free("STOCK"), Decimal(70000))  # 自有剩 70000
        self.assertEqual(sc.broker_free(), Decimal(68000))      # 80000−10000−2000
        self.assertEqual(sc.batch_limit("STOCK"), Decimal(68000))

    @case("G14-04")
    def test_serial_authorization(self):
        sc = SharedCash(Decimal(100000), Decimal(100000))
        rev = sc.broker_snapshot(Decimal(200000))
        self.assertTrue(sc.authorize("ETF", "e1", Decimal(80000), rev).ok)
        r = sc.authorize("STOCK", "s1", Decimal(80000), rev)
        self.assertEqual(r.status, C.INVALID)
        self.assertIn("SERIAL_BATCH_BUSY", r.codes())
        self.assertIn("STALE_SNAPSHOT", r.codes())

    @case("G14-05")
    def test_etf_proceeds_stay_etf(self):
        sc, _ = sc_with(stock="10000", etf="0")
        sc.credit_proceeds("ETF", Decimal(50000))
        self.assertEqual((sc.C["ETF"], sc.C["STOCK"]), (Decimal(50000), Decimal(10000)))

    def _views(self):
        etf, stock = Ledger("ETF", test_fees()), Ledger("STOCK", test_fees())
        e1, e2 = EventFactory("e"), EventFactory("s")
        etf.apply(e1("CAPITAL_FLOW", {"flow_id": "init", "amount": "100000"}))
        stock.apply(e2("CAPITAL_FLOW", {"flow_id": "init", "amount": "100000"}))
        return etf, stock

    @case("G14-06")
    def test_atomic_transfer(self):
        sc, rev = sc_with(stock="100000", etf="100000", broker="200000")
        etf, stock = self._views()
        r = sc.transfer("T1", "ETF", "STOCK", Decimal(10000))
        self.assertTrue(r.ok)
        for lg, owner in ((etf, "ETF"), (stock, "STOCK")):
            lg.apply({"event_id": "T1", "kind": "CAPITAL_FLOW", "payload": {"flow_id": "T1", "transfer_id": "T1",
                                                                         "amount": r.value["flows"][owner]}})
        self.assertEqual((etf.cash, stock.cash), (Decimal(90000), Decimal(110000)))
        self.assertEqual(sc.C["ETF"] + sc.C["STOCK"] + sc.C["UNASSIGNED"], Decimal(200000))

    @case("G14-07")
    def test_transfer_recovery_one_side(self):
        sc, rev = sc_with(stock="100000", etf="100000", broker="200000")
        etf, stock = self._views()
        r = sc.transfer("T1", "ETF", "STOCK", Decimal(10000))
        ev = lambda owner: {"event_id": "T1", "kind": "CAPITAL_FLOW",
                            "payload": {"flow_id": "T1", "transfer_id": "T1", "amount": r.value["flows"][owner]}}
        etf.apply(ev("ETF"))                        # 另一侧视图尚未更新时中断
        self.assertEqual(sc.transfer("T1", "ETF", "STOCK", Decimal(10000)).value, "DUPLICATE")
        stock.apply(ev("STOCK"))
        self.assertFalse(etf.apply(ev("ETF")))      # 恢复时不第二次扣转出
        self.assertEqual((etf.cash, stock.cash, sc.C["ETF"]), (Decimal(90000), Decimal(110000), Decimal(90000)))

    @case("G14-08")
    def test_unknown_previous_batch(self):
        sc, rev = sc_with()
        sc.mark_unknown("STOCK", "o-prev")
        r = sc.authorize("STOCK", "b2", Decimal(1000), rev)
        self.assertIn("ORDER_STATE_UNKNOWN", r.codes())

    @case("G14-09")
    def test_emergency_priority_over_unstarted(self):
        sc = SharedCash(Decimal(100000), Decimal(100000))
        rev = sc.broker_snapshot(Decimal(200000))
        sc.authorize("ETF", "e1", Decimal(50000), rev)
        r = sc.request_priority("STOCK")
        self.assertEqual(r.value, "REVOKED_UNSTARTED_OTHER")
        self.assertEqual(sc.state, "IDLE")
        self.assertTrue(sc.authorize("STOCK", "s-em", Decimal(0), sc.revision, increases_risk=False).ok)

    @case("G14-10")
    def test_wait_for_inflight(self):
        sc = SharedCash(Decimal(100000), Decimal(100000))
        rev = sc.broker_snapshot(Decimal(200000))
        sc.authorize("ETF", "e1", Decimal(50000), rev)
        sc.preapprove("c1", "ETF", Decimal(50000), "etf-o1")
        sc.broker_frozen("c1", Decimal(150000))
        r = sc.request_priority("STOCK")
        self.assertEqual(r.value, "WAIT_OTHER_ORDERS")
        self.assertEqual(sc.state, "EXECUTING")

    @case("G14-11")
    def test_late_coordination(self):
        d = date(2026, 1, 5)
        self.assertFalse(can_submit_new(sh_dt(d, time(10, 31))))
        cal = weekday_calendar(date(2025, 12, 1), date(2026, 2, 1))
        self.assertEqual(cal.first_window_after(sh_dt(d, time(10, 31))), sh_dt(date(2026, 1, 6), time(10)))

    @case("G14-12")
    def test_etf_total_asset_rejected(self):
        self.assertEqual(check_etf_view({"equity_source": "BROKER_TOTAL_ASSET"}).status, C.INVALID)
        self.assertEqual(check_etf_view({}).status, C.UNKNOWN)
        self.assertTrue(check_etf_view({"equity_source": "OWN_ALLOCATED_VIEW"}).ok)

    @case("G14-13")
    def test_double_count(self):
        r = double_count_check({"ETF": "60000", "STOCK": "60000"}, Decimal(100000))
        self.assertEqual(r.codes(), ["DOUBLE_COUNTED_CASH"])

    @case("G14-14")
    def test_unexplained_difference(self):
        sc, rev = sc_with(stock="100000", etf="0", broker="100000")
        r = sc.reconcile(Decimal("100100"))
        self.assertEqual(r.codes(), ["CASH_RECONCILIATION_FAILED"])
        self.assertEqual(sc.C["UNASSIGNED"], Decimal(0))
        self.assertIn("CASH_RECONCILIATION_FAILED", sc.authorize("STOCK", "b", Decimal(1), sc.revision).codes())

    @case("G14-15")
    def test_explained_rounding(self):
        sc, _ = sc_with()
        self.assertTrue(sc.reconcile(Decimal("100000.01"), [{"kind": "DISPLAY_ROUNDING", "amount": "0.01"}]).ok)
        self.assertEqual(sc.reconcile(Decimal("100000.02"), [{"kind": "DISPLAY_ROUNDING", "amount": "0.02"}]).codes(),
                         ["ROUNDING_EXPLANATION_TOO_LARGE"])

    @case("G14-16")
    def test_necessary_sell_allowed(self):
        sc, rev = sc_with()
        sc.mark_unknown("ETF", "etf-unknown")
        self.assertFalse(sc.authorize("STOCK", "buy", Decimal(1000), rev).ok)
        self.assertTrue(sc.authorize("STOCK", "sell", Decimal(0), rev, increases_risk=False).ok)


if __name__ == "__main__":
    unittest.main()
