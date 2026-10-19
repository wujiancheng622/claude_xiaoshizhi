"""合成市场长链守恒：由事件流独立重算现金与股数，与唯一账本逐项相等；无超卖、无负现金、窗口后无遗留委托。"""
import unittest
from collections import defaultdict
from datetime import date
from decimal import Decimal

from smallcap.synthetic import SyntheticMarket
from tests.acceptance import case
from tests.fixtures import make_runner


def recompute(events):
    cash = Decimal(0)
    qty = defaultdict(int)
    fills_per_order = defaultdict(int)
    orders = {}
    for e in events:
        k, p = e["kind"], e["payload"]
        if k == "CAPITAL_FLOW":
            cash += Decimal(p["amount"])
        elif k == "ORDER_CREATE":
            orders[p["order"]["client_order_id"]] = p["order"]
        elif k == "BROKER_FILL":
            o = orders[p["client_order_id"]]
            amt = Decimal(p["price"]) * p["qty"]
            fills_per_order[p["client_order_id"]] += p["qty"]
            if o["side"] == "BUY":
                cash -= amt
                qty[o["security_id"]] += p["qty"]
            else:
                cash += amt
                qty[o["security_id"]] -= p["qty"]
        elif k == "TAX_SETTLEMENT":
            cash -= Decimal(p["amount"])
        elif k == "CA_CASH_COMPENSATION":
            cash += Decimal(p["amount"])
        elif k == "RESIDUAL_RECOVERY":
            cash += Decimal(p["cash"])
            qty[p["security_id"]] -= p["qty"]
    return cash, qty, fills_per_order, orders


class EngineInvariants(unittest.TestCase):
    def _check(self, mode, start, end):
        m = SyntheticMarket(seed=11, n=300, start=date(2019, 1, 2), end=end)
        r = make_runner(m, capital="500000", start_year=start, end=end, mode=mode)
        r.event_log = []
        r.run()
        L = r.ledger
        self.assertEqual(r.startup["status"], "STARTED")
        cash, qty, fills, orders = recompute(r.event_log)
        fees = sum((Decimal(t["fee"]) for t in L.trade_log), Decimal(0))
        divs = Decimal(0)
        for e in r.event_log:
            if e["kind"] == "CA_PAY":
                ent = L.entitlements[e["payload"]["action_id"]]
                if ent["eligible_qty"]:
                    divs += Decimal(ent["receivable"]) - Decimal(ent.get("withheld", "0"))
        self.assertEqual(cash - fees + divs, L.cash)                        # 现金守恒（逐事件重算）
        credits = defaultdict(int)
        for a in L.entitlements.values():
            if a.get("share_entitlement") and "SHARE_CREDIT" in a["phases"]:
                credits[a["security_id"]] += a["share_entitlement"]
        for sid, p in L.positions.items():
            self.assertEqual(p.quantity, qty[sid] + credits[sid], sid)    # 股数守恒
            self.assertGreaterEqual(p.quantity, 0)
        for coid, o in orders.items():
            self.assertLessEqual(fills[coid], o["qty"])                     # 无超原量成交
        self.assertEqual(L.open_orders(), [])                               # 窗口后无遗留委托
        self.assertTrue(all(pt["cash"] >= 0 for pt in L.ledger_points))     # 无负现金（未融资）
        self.assertEqual(len(L.seen), len({e["event_id"] for e in r.event_log}))
        return L

    @case("G17-04", "G17-07")
    def test_daily_proxy_long_chain(self):
        L = self._check("REPLAY_DAILY_PROXY", 2021, date(2024, 6, 28))
        self.assertGreater(len(L.trade_log), 300)

    @case("G17-04", "G13-04", "G13-06")
    def test_minute_window_chain(self):
        L = self._check("REPLAY_MINUTE_WINDOW", 2024, date(2024, 12, 31))
        self.assertGreater(len(L.trade_log), 50)


if __name__ == "__main__":
    unittest.main()
