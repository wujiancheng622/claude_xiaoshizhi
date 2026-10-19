import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from smallcap import contracts as C
from smallcap.account import Ledger
from smallcap.contracts import ContractError, sh_dt, wire_ratio
from smallcap.execution import (BookView, buy_ok, can_submit_new, capacity_cap, check_sell_funding, protection_bounds,
                                proxy_price, quote_fresh, realtime_limit_price)
from smallcap.fees import FeeSchedule
from smallcap.minute import _can_attempt
from smallcap.shared_cash import SharedCash
from tests.acceptance import case
from tests.fixtures import (EventFactory, StubMarket, buy_order, exec_day, fee_rule, fill, golden, make_runner,
                            new_ledger, put_plan, seed_holdings, sell_order, test_fee_payload, test_fees)

T = Decimal("0.01")
D0 = date(2024, 1, 8)


def submitted_buy(lg, ev, coid="o1", qty=300, limit="10", sid="X0001.SH", d="2026-01-05"):
    lg.apply(ev("ORDER_CREATE", buy_order(coid, sid=sid, qty=qty, limit=limit, trade_date=d)))
    lg.apply(ev("ORDER_SUBMIT", {"client_order_id": coid, "at": f"{d}T10:00:00+08:00"}))


class G09(unittest.TestCase):
    @case("G09-01")
    def test_buy_protection(self):
        up, lo = protection_bounds(Decimal(10), T, Decimal("9"), Decimal("11"))
        self.assertEqual(up, Decimal("10.03"))
        self.assertEqual(protection_bounds(Decimal(10), T, None, None)[0], Decimal("10.03"))   # 重试锚不变，不能抬到10.10

    @case("G09-02")
    def test_ask_above_bound_no_order(self):
        self.assertIsNone(realtime_limit_price("BUY", None, Decimal("10.04"), T, Decimal("10.03"), Decimal("9.97")))
        self.assertEqual(realtime_limit_price("BUY", None, Decimal("10.02"), T, Decimal("10.03"), Decimal("9.97")),
                         Decimal("10.02"))

    @case("G09-03")
    def test_bid_below_bound_no_sell(self):
        self.assertIsNone(realtime_limit_price("SELL", Decimal("9.96"), None, T, Decimal("10.03"), Decimal("9.97")))

    @case("G09-04")
    def test_quote_age(self):
        now = datetime(2026, 1, 5, 2, 0, 3, tzinfo=timezone.utc)
        self.assertTrue(quote_fresh(now - timedelta(seconds=3), now))
        self.assertFalse(quote_fresh(now - timedelta(seconds=3, microseconds=1), now))

    @case("G09-05")
    def test_window_clock(self):
        self.assertTrue(can_submit_new(sh_dt(D0, time(10, 29, 59))))
        self.assertFalse(can_submit_new(sh_dt(D0, time(10, 30, 0))))
        self.assertFalse(can_submit_new(sh_dt(D0, time(9, 59, 59))))

    @case("G09-06", "G12-12")
    def test_sent_unknown_no_resend(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        self.assertEqual(lg.orders["o1"].status, "SENT_UNKNOWN")
        with self.assertRaises(ContractError) as cm:
            lg.apply(ev("ORDER_CREATE", buy_order("o1-retry", qty=300)))
        self.assertEqual(cm.exception.code, "DUPLICATE_OPEN_ORDER")
        from smallcap.forward import resume_actions
        acts = resume_actions(lg, datetime(2026, 1, 6, tzinfo=timezone.utc))
        self.assertEqual([q["client_order_id"] for q in acts["query_orders"]], ["o1"])
        self.assertEqual(acts["resend"], [])

    @case("G09-07", "G09-08", "G09-09", "G09-07_TO_09")
    def test_golden_partial_cancel_chain(self):
        g = golden("G09-07_TO_09")
        lg, ev = new_ledger(cash="10000")
        submitted_buy(lg, ev)
        fills = {f"FILL_{x['trade_id']}" for x in g["input"]["fills"]}
        o = lg.orders["o1"]
        reserve_before = None
        for step in g["input"]["events"]:
            if step == "ACK":
                lg.apply(ev("BROKER_ACK", {"client_order_id": "o1", "broker_order_id": "B1"}))
            elif step == "FILL_t1" or step == "DUPLICATE_t1":
                before = (lg.positions["X0001.SH"].quantity if "X0001.SH" in lg.positions else 0, lg.cash)
                lg.apply(ev("BROKER_FILL", fill("o1", "t1", 100, "10")))
                if step == "DUPLICATE_t1":
                    self.assertEqual(before, (lg.positions["X0001.SH"].quantity, lg.cash))
            elif step == "CANCEL_REQUEST":
                lg.apply(ev("CANCEL_REQUEST", {"client_order_id": "o1", "request_id": "c1", "reason": "TIMEOUT"}))
                self.assertEqual(o.status, "CANCEL_PENDING")
                self.assertEqual(lg.buy_cash_reserved(o), Decimal(2000))      # G09-07 剩200仍占用
            elif step == "LATE_FILL_t2":
                lg.apply(ev("BROKER_FILL", fill("o1", "t2", 50, "10")))
                self.assertEqual(lg.positions["X0001.SH"].quantity, 150)       # G09-08
                reserve_before = lg.buy_cash_reserved(o)
            elif step == "CANCEL_CONFIRMED":
                lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "o1", "status": "CANCELED", "cum_qty": 150,
                                                "cum_value": "1500", "source_revision": 1}))
        e = g["expected"]
        self.assertEqual((o.filled_qty, o.filled_value, sum(o.fees_accrued.values())),
                         (e["filled_quantity"], Decimal(e["gross_amount"]), Decimal(e["commission"])))
        self.assertEqual(reserve_before, Decimal(e["reserve_before_cancel_confirmation"]))
        self.assertEqual(lg.buy_cash_reserved(o), Decimal(e["reserve_after_cancel_confirmation"]))
        self.assertEqual((o.status, lg.positions["X0001.SH"].quantity), ("CANCELED", 150))   # G09-09

    @case("G09-10")
    def test_duplicate_trade_idempotent(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 100, "10")))
        snap = (lg.cash, lg.positions["X0001.SH"].quantity, dict(lg.orders["o1"].fees_accrued))
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 100, "10")))       # 新事件ID、同成交键同内容
        self.assertEqual(snap, (lg.cash, lg.positions["X0001.SH"].quantity, dict(lg.orders["o1"].fees_accrued)))

    @case("G09-11")
    def test_same_trade_id_conflict(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 100, "10")))
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 120, "10")))
        o = lg.orders["o1"]
        self.assertEqual((o.status, o.filled_qty), ("RECONCILE_REQUIRED", 100))
        self.assertIn("ORDER_RECONCILE:o1", lg.limitations)

    @case("G09-12")
    def test_distinct_ids_same_values(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 100, "10")))
        lg.apply(ev("BROKER_FILL", fill("o1", "t2", 100, "10")))
        self.assertEqual(lg.positions["X0001.SH"].quantity, 200)

    @case("G09-13")
    def test_cumulative_query_diff(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        base = {"client_order_id": "o1", "observed_at": "2026-01-05T10:00:40+08:00",
                "fill_fields": {"sellable_from": "2026-01-06"}}
        lg.apply(ev("BROKER_CUMULATIVE", dict(base, cum_qty=100, cum_value="1000")))
        lg.apply(ev("BROKER_CUMULATIVE", dict(base, cum_qty=150, cum_value="1500")))
        lg.apply(ev("BROKER_CUMULATIVE", dict(base, cum_qty=150, cum_value="1500")))
        self.assertEqual(lg.positions["X0001.SH"].quantity, 150)
        self.assertEqual([t["qty"] for t in lg.trade_log], [100, 50])

    @case("G09-14")
    def test_report_exceeds_order(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 300, "10")))
        lg.apply(ev("BROKER_FILL", fill("o1", "t2", 100, "10")))
        o = lg.orders["o1"]
        self.assertEqual((o.status, o.filled_qty, lg.positions["X0001.SH"].quantity), ("RECONCILE_REQUIRED", 400, 400))

    @case("G09-15")
    def test_confirmed_breach_recorded_and_blocks(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 300, "10.50")))         # 超出限价的真实成交
        self.assertEqual(lg.positions["X0001.SH"].quantity, 300)
        self.assertTrue(any(k.startswith("CONFIRMED_EXECUTION_BREACH") for k in lg.limitations))
        from smallcap.risk import risk_decision
        d = risk_decision(account_revision=1, episodes={}, run_state="NORMAL", equity=Fraction(1), valuation_status="OK",
                          account_limitations=lg.limitations, unknown_industry_holdings=[], order_state_unknown=False,
                          batch_blocks=[], security_exits={}, no_buy_in_batch=set(), hold_no_add=set(),
                          risk_sell_sids=set(), blocked_exit_value=Fraction(0))
        self.assertIn("CONFIRMED_EXECUTION_BREACH", d["global_buy_blocks"])

    @case("G09-16")
    def test_max_three_orders(self):
        st = {"attempts": {("A", "BUY"): 3}, "last_submit": {}}
        self.assertFalse(_can_attempt(st, ("A", "BUY"), sh_dt(D0, time(10, 10))))
        m = StubMarket(n=40)
        r = make_runner(m, mode="REPLAY_MINUTE_WINDOW")
        seed_holdings(r, date(2024, 1, 5), {}, 100000)
        put_plan(r, date(2024, 1, 5), {"X0001.SH": {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)),
                                                     "prohibit_buy": False, "reason": "NEW_BUY"}}, ["X0001.SH"], 100000, D0)
        for k in range(0, 30):
            m.minute_override[("X0001.SH", D0, k)] = {"open": Decimal("10.40"), "low": Decimal("10.40"),
                                                       "high": Decimal("10.45")}
        exec_day(r, D0)
        orders = [o for o in r.ledger.orders.values() if o.security_id == "X0001.SH"]
        self.assertEqual(len(orders), 3)
        self.assertTrue(all(o.closed and o.filled_qty == 0 for o in orders))
        subs = sorted(o.submitted_at for o in orders)
        self.assertEqual(subs, [sh_dt(D0, time(10, k)).isoformat() for k in (0, 1, 2)])

    @case("G09-17")
    def test_out_of_window_fill_not_backfilled(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 300, "10", at="2026-01-05T10:31:00+08:00")))
        self.assertEqual(lg.trade_log[-1]["occurred_at"], "2026-01-05T10:31:00+08:00")
        self.assertTrue(any(x["kind"] == "WINDOW_DEVIATION" for x in lg.orders["o1"].observations))

    @case("G09-18")
    def test_failed_ordinary_sell_still_buys(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        held = {f"X{i:04d}.SH": 200 for i in range(1, 30)}
        seed_holdings(r, date(2024, 1, 5), held, 42000)
        modes = {"X0029.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK"},
                 "X0031.SH": {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)), "prohibit_buy": False,
                              "reason": "NEW_BUY"}}
        put_plan(r, date(2024, 1, 5), modes, ["X0031.SH"], 100000, D0)
        m.set_bar("X0029.SH", D0, open="9.00", close="9.00", ref_preclose="10")
        exec_day(r, D0)
        L = r.ledger
        self.assertEqual(L.positions["X0029.SH"].quantity, 200)
        self.assertEqual(L.positions["X0031.SH"].quantity, 200)

    @case("G09-19")
    def test_unknown_sell_blocks_buys(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 200}, 98000)
        r.phase_key, r.k = "s", 0
        r.emit("ORDER_CREATE", sell_order("s1", qty=200, trade_date="2024-01-08"), sh_dt(D0, time(9, 31)))
        r.emit("ORDER_SUBMIT", {"client_order_id": "s1", "at": "x"}, sh_dt(D0, time(9, 31)))
        self.assertIn("ORDER_STATE_UNKNOWN", r.account_global_blocks())

    @case("G09-20")
    def test_parallel_orders_one_per_side(self):
        lg, ev = new_ledger(cash="1000000")
        for i in range(30):
            lg.apply(ev("ORDER_CREATE", buy_order(f"o{i}", sid=f"X{i + 1:04d}.SH", qty=100)))
        self.assertEqual(len(lg.open_orders()), 30)
        with self.assertRaises(ContractError):
            lg.apply(ev("ORDER_CREATE", buy_order("dup", sid="X0001.SH", qty=100)))
        with self.assertRaises(ContractError) as cm:
            lg.apply(ev("ORDER_CREATE", sell_order("x", sid="X0001.SH", qty=100)))
        self.assertIn(cm.exception.code, ("CROSS_OPEN_ORDER", "OVERSELL_PREVENTED"))
        total_res = lg.cash_commitments()
        self.assertEqual(total_res, sum(lg.buy_cash_reserved(o) for o in lg.open_orders()))

    @case("G09-21")
    def test_tick_rounding_no_deadlock(self):
        up, lo = protection_bounds(Decimal("10.01"), T, Decimal("9.01"), Decimal("11.01"))
        self.assertEqual((up, lo), (Decimal("10.05"), Decimal("9.97")))
        self.assertEqual(proxy_price("BUY", Decimal("10.01"), C.HIGH_SLIPPAGE, T, Decimal("9.01"), Decimal("11.01")),
                         Decimal("10.05"))
        self.assertEqual(proxy_price("SELL", Decimal("10.01"), C.HIGH_SLIPPAGE, T, Decimal("9.01"), Decimal("11.01")),
                         Decimal("9.97"))
        self.assertEqual(protection_bounds(Decimal("10.99"), T, Decimal("9.89"), Decimal("11.00"))[0], Decimal("11.00"))

    @case("G09-22")
    def test_occurred_vs_received(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        e = ev("BROKER_FILL", fill("o1", "t1", 300, "10", at="2026-01-05T10:29:00+08:00",
                                   received="2026-01-05T10:31:00+08:00"), at="2026-01-05T10:31:00+08:00")
        lg.apply(e)
        self.assertEqual(lg.trade_log[-1]["occurred_at"], "2026-01-05T10:29:00+08:00")
        self.assertFalse(any(x["kind"] == "WINDOW_DEVIATION" for x in lg.orders["o1"].observations))
        self.assertEqual(e["known_at"], "2026-01-05T10:31:00+08:00")


class G10(unittest.TestCase):
    @case("G10-01")
    def test_capacity(self):
        self.assertEqual(capacity_cap([(Decimal(10_000_000), "TRADING")] * 20), Decimal(100000))

    def _cap_ledger(self):
        lg, ev = new_ledger(cash="1000000")
        submitted_buy(lg, ev, "b", qty=4000, limit="10")
        lg.apply(ev("BROKER_FILL", fill("b", "tb", 4000, "10", sellable_from="2026-01-05")))
        lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "b", "status": "FILLED", "cum_qty": 4000,
                                        "cum_value": "40000", "source_revision": 1}))
        lg.apply(ev("ORDER_CREATE", sell_order("s", qty=2000, limit="10")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "s", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("s", "ts", 2000, "10")))
        lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "s", "status": "FILLED", "cum_qty": 2000,
                                        "cum_value": "20000", "source_revision": 1}))
        return lg, ev

    @case("G10-02")
    def test_used_not_netted(self):
        lg, _ = self._cap_ledger()
        self.assertEqual(lg.capacity_used_on("X0001.SH", "2026-01-05"), Decimal(60000))

    @case("G10-03", "G10-04")
    def test_pending_and_release(self):
        lg, ev = self._cap_ledger()
        submitted_buy(lg, ev, "b2", qty=3000, limit="10")
        cap = Decimal(100000)
        left = cap - lg.capacity_used_on("X0001.SH", "2026-01-05") - lg.capacity_pending("X0001.SH", "2026-01-05")
        self.assertEqual(left, Decimal(10000))
        lg.apply(ev("CANCEL_REQUEST", {"client_order_id": "b2", "request_id": "c", "reason": "TIMEOUT"}))
        self.assertEqual(lg.capacity_pending("X0001.SH", "2026-01-05"), Decimal(30000))     # 未确认不释放
        lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "b2", "status": "CANCELED", "cum_qty": 0, "cum_value": "0",
                                        "source_revision": 1}))
        left = cap - lg.capacity_used_on("X0001.SH", "2026-01-05") - lg.capacity_pending("X0001.SH", "2026-01-05")
        self.assertEqual(left, Decimal(40000))

    @case("G10-05")
    def test_sell_capacity_upper_bound(self):
        lg, ev = self._cap_ledger()
        lg.apply(ev("ORDER_CREATE", sell_order("s2", qty=1000, limit="9.97", upper="11")))
        self.assertEqual(lg.capacity_pending("X0001.SH", "2026-01-05"), Decimal(11000))

    @case("G10-06")
    def test_no_upper_bound(self):
        lg, ev = self._cap_ledger()
        lg.apply(ev("ORDER_CREATE", sell_order("s3", qty=1000, limit="9.97", upper=None)))
        with self.assertRaises(ContractError) as cm:
            lg.capacity_pending("X0001.SH", "2026-01-05")
        self.assertEqual(cm.exception.code, "CAPACITY_PRICE_BOUND_UNKNOWN")

    @case("G10-06")
    def test_no_upper_bound_engine_skips(self):
        from tests.fixtures import trade_rule, test_rulebook_payload, stub_tax_rules
        from smallcap.rules import RuleBook
        m = StubMarket(n=5)
        r = make_runner(m)
        p = test_rulebook_payload()
        p["rules"] = [x for x in p["rules"] if x["board"] != "MAIN_SH"] + [trade_rule("EXB", method="EXCHANGE_BOUNDS")]
        r.rb = RuleBook(p)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 200}, 98000)
        put_plan(r, date(2024, 1, 5), {"X0001.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True,
                                                     "reason": "RANK"}}, [], 100000, D0)
        exec_day(r, D0)
        self.assertEqual(r.ledger.orders, {})
        self.assertIn({"kind": "SELL_SKIPPED", "security_id": "X0001.SH", "reason": "CAPACITY_PRICE_BOUND_UNKNOWN"},
                      r.ledger.decision_log)

    @case("G10-07")
    def test_used_survives_restart(self):
        lg, ev = self._cap_ledger()
        snap = json.loads(json.dumps(lg.snapshot()))
        lg2 = Ledger.restore(snap, test_fees())
        self.assertEqual(lg2.capacity_used_on("X0001.SH", "2026-01-05"), Decimal(60000))

    @case("G10-08")
    def test_next_day_reset(self):
        lg, ev = self._cap_ledger()
        submitted_buy(lg, ev, "b3", qty=100, limit="10")
        lg.apply(ev("EOD", {"date": "2026-01-05"}))
        self.assertEqual(lg.capacity_used_on("X0001.SH", "2026-01-06"), Decimal(0))
        self.assertEqual(lg.capacity_pending("X0001.SH", "2026-01-06"), Decimal(0))
        self.assertGreater(lg.cash_commitments(), 0)          # 旧未结订单资源仍占用，需核对

    @case("G10-09")
    def test_zero_fill_zero_fee(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev)
        lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "o1", "status": "CANCELED", "cum_qty": 0, "cum_value": "0",
                                        "source_revision": 1}))
        self.assertEqual(sum(lg.orders["o1"].fees_accrued.values(), Decimal(0)), 0)
        self.assertEqual(lg.cash, Decimal(100000))

    @case("G10-10")
    def test_cumulative_commission(self):
        lg, ev = new_ledger(cash="1000000")
        submitted_buy(lg, ev, qty=2400, limit="10")
        deltas = []
        for k, q in (("a", 100), ("b", 200), ("c", 2100)):
            c0 = lg.cash
            lg.apply(ev("BROKER_FILL", fill("o1", k, q, "10")))
            deltas.append(c0 - lg.cash - q * 10)
        self.assertEqual(deltas, [Decimal(5), Decimal(0), Decimal(1)])
        self.assertEqual(lg.orders["o1"].fees_accrued["COMMISSION"], Decimal(6))

    @case("G10-11")
    def test_new_order_new_minimum(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev, "a", qty=100)
        lg.apply(ev("BROKER_FILL", fill("a", "ta", 100, "10")))
        lg.apply(ev("CANCEL_REQUEST", {"client_order_id": "a", "request_id": "c", "reason": "TIMEOUT"}))
        lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "a", "status": "CANCELED", "cum_qty": 100, "cum_value": "1000",
                                        "source_revision": 1}))
        submitted_buy(lg, ev, "b", qty=100)
        lg.apply(ev("BROKER_FILL", fill("b", "tb", 100, "10")))
        self.assertEqual(sum(sum(o.fees_accrued.values()) for o in lg.orders.values()), Decimal(10))

    @case("G10-12")
    def test_extra_reserve_zero(self):
        fs = test_fees()
        self.assertEqual(fs.extra_fee_reserve("SH", "BUY", date(2026, 1, 5), Decimal(1000), 100, Decimal(2000), 200,
                                              Decimal(5), [(Decimal(1000), 100)]), Decimal(0))

    @case("G10-13")
    def test_settlement_difference_only(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev, qty=100)
        lg.apply(ev("BROKER_FILL", fill("o1", "t", 100, "10")))
        e0, c0 = lg.equity({"X0001.SH": Decimal(10)})[0], lg.cash
        lg.apply(ev("FEE_SETTLEMENT", {"client_order_id": "o1", "assessed_fee": "5.50", "already_accrued": "5.00",
                                       "already_paid": "5.00", "settlement_cash_change": "0.50"}))
        e1 = lg.equity({"X0001.SH": Decimal(10)})[0]
        self.assertEqual((c0 - lg.cash, e0 - e1), (Decimal("0.50"), Fraction(1, 2)))

    @case("G10-14")
    def test_included_fee_not_added(self):
        p = test_fee_payload(transfer_rate="0.00002")
        p["fee_rules"] = [r for r in p["fee_rules"] if r["fee_kind"] != "TRANSFER"] + [
            fee_rule("TRANSFER", "0.00002", included=True)]
        fs = FeeSchedule(p)
        self.assertNotIn("TRANSFER", fs.order_fees("SH", "BUY", date(2026, 1, 5), Decimal(100000), 10000))

    @case("G10-15")
    def test_fee_changes_denominator(self):
        v = BookView(Fraction(100000), Fraction(60000), {}, {}, {}, Fraction(0), {}, Decimal(40000))
        self.assertIn("STOCK_60", buy_ok(v, "N", "I9", 100, Decimal(10), Decimal(10), Decimal(5)))

    @case("G10-16")
    def test_broker_freeze_not_double_counted(self):
        sc = SharedCash(Decimal(0), Decimal(100000))
        rev = sc.broker_snapshot(Decimal(80000))
        self.assertTrue(sc.authorize("STOCK", "b1", Decimal(30000), rev).ok)
        sc.preapprove("c1", "STOCK", Decimal(30000), "o1")
        self.assertEqual(sc.broker_free(), Decimal(50000))
        sc.broker_frozen("c1", Decimal(50000))
        self.assertEqual(sc.broker_free(), Decimal(50000))        # 不再从已减的 available 再减 30000

    @case("G10-17")
    def test_no_double_slippage(self):
        lg, ev = new_ledger()
        submitted_buy(lg, ev, qty=100, limit="10.10")
        lg.apply(ev("BROKER_FILL", fill("o1", "t", 100, "10.03")))
        self.assertEqual(set(lg.orders["o1"].fees_accrued), {"COMMISSION", "STAMP", "TRANSFER"})
        self.assertEqual(lg.cash, Decimal(100000) - Decimal("1003") - Decimal(5))

    @case("G10-18")
    def test_cost_unfundable(self):
        self.assertEqual(check_sell_funding(Decimal(100), Decimal(150), Decimal(20)), "COST_UNFUNDABLE")
        self.assertIsNone(check_sell_funding(Decimal(100), Decimal(110), Decimal(20)))


if __name__ == "__main__":
    unittest.main()
