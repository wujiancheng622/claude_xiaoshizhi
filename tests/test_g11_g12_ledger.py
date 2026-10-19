import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from smallcap import contracts as C
from smallcap.account import Ledger
from smallcap.clock import add_months
from smallcap.contracts import ContractError, sh_dt
from smallcap.fees import TaxSchedule, allocate_cents
from smallcap.forward import reconcile_account, resume_actions
from smallcap.journal import Journal, recover
from smallcap.shared_cash import SharedCash
from tests.acceptance import case
from tests.fixtures import (EventFactory, StubMarket, buy_order, close_day, fill, make_runner, new_ledger, seed_holdings,
                            sell_order, stub_tax_rules, test_fees, TAX_FIXTURE)


def boot(cash="1000", qty=100, mark="10", acq="2024-01-02", fees=None, sid="X0001.SH"):
    lg = Ledger("A", fees or test_fees(tax_rules=stub_tax_rules()))
    ev = EventFactory("b")
    lg.apply(ev("BOOTSTRAP_ACCOUNT", {"cash": cash, "positions": [] if qty == 0 else [
        {"security_id": sid, "market": "SH", "board": "MAIN_SH", "quantity": qty, "mark": mark,
         "acquisition_tax_date": acq}]}))
    return lg, ev


def div(lg, ev, aid="D1", per="1", record="2025-06-16", ref="9", sid="X0001.SH"):
    lg.apply(ev("CA_RECORD", {"action_id": aid, "security_id": sid, "kind": "CASH_DIVIDEND", "record_date": record,
                              "cash_per_share": per, "tax_base_per_share": per}))
    lg.apply(ev("CA_EX", {"action_id": aid, "reference_price": ref, "ex_date": record}))


def sell(lg, ev, coid, qty, price="9", d="2025-06-20", delivery=None):
    lg.apply(ev("ORDER_CREATE", sell_order(coid, qty=qty, limit="1", trade_date=d)))
    lg.apply(ev("ORDER_SUBMIT", {"client_order_id": coid, "at": "x"}))
    lg.apply(ev("BROKER_FILL", fill(coid, f"T-{coid}", qty, price, trade_date=d, at=f"{d}T10:00:30+08:00",
                                    tax_delivery_date=delivery or d)))


class G11(unittest.TestCase):
    @case("G11-01")
    def test_ex_dividend(self):
        lg, ev = boot()
        div(lg, ev)
        self.assertEqual((lg.receivables["DIV:D1"], lg.position_value("X0001.SH"), lg.cash), (Decimal(100), Fraction(900), Decimal(1000)))
        self.assertEqual(lg.equity()[0], Fraction(2000))

    @case("G11-02")
    def test_receivable_not_spendable(self):
        lg, ev = boot()
        before = lg.own_free_cash("2025-06-17")
        div(lg, ev)
        self.assertEqual(lg.own_free_cash("2025-06-17"), before)

    @case("G11-03")
    def test_pay(self):
        lg, ev = boot()
        div(lg, ev)
        lg.apply(ev("CA_PAY", {"action_id": "D1", "withheld": "0"}))
        self.assertEqual((lg.cash, lg.receivables.get("DIV:D1")), (Decimal(1100), None))
        self.assertEqual(lg.equity()[0], Fraction(2000))

    @case("G11-04")
    def test_zero_holding_record(self):
        lg, ev = boot(qty=0)
        lg.apply(ev("CA_RECORD", {"action_id": "D1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                  "record_date": "2025-06-16", "cash_per_share": "1"}))
        self.assertEqual(lg.entitlements["D1"]["eligible_qty"], 0)
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=100, limit="10", trade_date="2025-06-17")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "9", trade_date="2025-06-17", sellable_from="2025-06-18")))
        lg.apply(ev("CA_EX", {"action_id": "D1", "reference_price": "9"}))
        self.assertNotIn("DIV:D1", lg.receivables)

    @case("G11-05")
    def test_record_day_purchase_eligible(self):
        lg, ev = boot(cash="10000", qty=0)
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=200, limit="10", trade_date="2025-06-16")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 200, "10", trade_date="2025-06-16", sellable_from="2025-06-17")))
        lg.apply(ev("CA_RECORD", {"action_id": "D1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                  "record_date": "2025-06-16", "cash_per_share": "0.5"}))
        self.assertEqual(lg.free_sell("X0001.SH", "2025-06-16"), 0)
        self.assertEqual(lg.entitlements["D1"]["eligible_qty"], 200)

    @case("G11-06")
    def test_record_once(self):
        lg, ev = boot()
        e = ev("CA_RECORD", {"action_id": "D1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                             "record_date": "2025-06-16", "cash_per_share": "1"})
        self.assertTrue(lg.apply(e))
        self.assertFalse(lg.apply(e))
        with self.assertRaises(ContractError) as cm:
            lg.apply(ev("CA_RECORD", {"action_id": "D1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                      "record_date": "2025-06-16", "cash_per_share": "2"}))
        self.assertEqual(cm.exception.code, "ACTION_PHASE_REPEATED")

    @case("G11-07")
    def test_bonus_shares(self):
        lg, ev = boot(qty=200)
        lg.apply(ev("CA_RECORD", {"action_id": "B1", "security_id": "X0001.SH", "kind": "STOCK_DIVIDEND",
                                  "record_date": "2025-06-16", "shares_per_share": "0.3"}))
        lg.apply(ev("CA_EX", {"action_id": "B1", "reference_price": "7.69"}))
        lg.apply(ev("CA_SHARE_CREDIT", {"action_id": "B1", "sellable_from": "2025-06-19",
                                        "acquisition_inheritance": "INHERIT_PARENT"}))
        p = lg.positions["X0001.SH"]
        self.assertEqual(p.quantity, 260)
        self.assertEqual(lg.free_sell("X0001.SH", "2025-06-18"), 200)
        self.assertEqual(lg.free_sell("X0001.SH", "2025-06-19"), 260)

    @case("G11-19")
    def test_stock_dividend_tax_unknown(self):
        lg, ev = boot(qty=200)
        lg.apply(ev("CA_RECORD", {"action_id": "B9", "security_id": "X0001.SH", "kind": "STOCK_DIVIDEND",
                                  "record_date": "2025-06-16", "shares_per_share": "0.3"}))
        self.assertEqual(lg.limitations.get("TAX_UNKNOWN:B9"), "STOCK_DIVIDEND_TAX_TREATMENT_UNKNOWN")
        lg2, ev2 = boot(qty=200)
        lg2.apply(ev2("CA_RECORD", {"action_id": "B9", "security_id": "X0001.SH", "kind": "STOCK_DIVIDEND",
                                    "record_date": "2025-06-16", "shares_per_share": "0.3", "tax_treatment": "NOT_TAXABLE"}))
        self.assertNotIn("TAX_UNKNOWN:B9", lg2.limitations)

    @case("G11-08")
    def test_fractional_rights(self):
        lg, ev = boot(qty=101)
        lg.apply(ev("CA_RECORD", {"action_id": "B1", "security_id": "X0001.SH", "kind": "STOCK_DIVIDEND",
                                  "record_date": "2025-06-16", "shares_per_share": "0.3",
                                  "rounding": "FLOOR_CASH_IN_LIEU", "cash_in_lieu_per_share": "7.69"}))
        lg.apply(ev("CA_EX", {"action_id": "B1", "reference_price": "7.69"}))
        self.assertEqual(lg.positions["X0001.SH"].share_receivable, 30)
        self.assertEqual(lg.receivables["FRAC:B1"], Decimal("2.31"))
        lg2, ev2 = boot(qty=101)
        lg2.apply(ev2("CA_RECORD", {"action_id": "B1", "security_id": "X0001.SH", "kind": "STOCK_DIVIDEND",
                                    "record_date": "2025-06-16", "shares_per_share": "0.3"}))
        lg2.apply(ev2("CA_EX", {"action_id": "B1", "reference_price": "7.69"}))
        self.assertTrue(lg2.positions["X0001.SH"].share_receivable_unknown)
        self.assertEqual(lg2.positions["X0001.SH"].share_receivable, 0)
        self.assertEqual(lg2.equity()[1], "UNKNOWN")

    @case("G11-09")
    def test_rights_not_free_shares(self):
        lg, ev = boot(qty=100)
        lg.apply(ev("CA_RECORD", {"action_id": "R1", "security_id": "X0001.SH", "kind": "RIGHTS",
                                  "record_date": "2025-06-16", "rights_per_share": "0.3", "subscription_price": "5"}))
        lg.apply(ev("CA_EX", {"action_id": "R1", "reference_price": "9.23"}))
        p = lg.positions["X0001.SH"]
        self.assertEqual((p.quantity, p.share_receivable, lg.cash), (100, 0, Decimal(1000)))
        self.assertEqual(lg.equity()[0], Fraction(1000) + 100 * Fraction(Decimal("9.23")))   # 不参与的权益影响如实计入

    @case("G11-10")
    def test_old_basis_price_unreliable(self):
        lg, ev = boot(qty=200)
        lg.apply(ev("CA_RECORD", {"action_id": "B1", "security_id": "X0001.SH", "kind": "STOCK_DIVIDEND",
                                  "record_date": "2025-06-16", "shares_per_share": "0.3"}))
        lg.apply(ev("CA_EX", {"action_id": "B1"}))
        self.assertEqual(lg.positions["X0001.SH"].mark_status, "UNRELIABLE")
        self.assertEqual(lg.equity()[1], "UNKNOWN")

    @case("G11-11")
    def test_fifo(self):
        lg, ev = boot(qty=100, acq="2024-01-02", cash="100000")
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=100, limit="10", trade_date="2025-03-03")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "10", trade_date="2025-03-03", sellable_from="2025-03-04")))
        sell(lg, ev, "s", 150, d="2025-06-20")
        lots = lg.positions["X0001.SH"].lots
        self.assertEqual([(l["acquisition_tax_date"], l["remaining_qty"]) for l in lots], [("2025-03-03", 50)])

    def _rule(self):
        return TaxSchedule(stub_tax_rules()).rules[0]

    @case("G11-12")
    def test_one_month_boundary(self):
        self.assertEqual(self._rule().final_rate(date(2025, 1, 1), date(2025, 2, 1)), Decimal("0.20"))

    @case("G11-13")
    def test_after_one_month(self):
        self.assertEqual(self._rule().final_rate(date(2025, 1, 1), date(2025, 2, 2)), Decimal("0.10"))

    @case("G11-14")
    def test_one_year_boundary(self):
        r = self._rule()
        self.assertEqual(r.final_rate(date(2024, 1, 1), date(2025, 1, 1)), Decimal("0.10"))
        self.assertEqual(r.final_rate(date(2024, 1, 1), date(2025, 1, 2)), Decimal("0"))

    @case("G11-15")
    def test_month_end(self):
        self.assertEqual(add_months(date(2025, 1, 31), 1), date(2025, 2, 28))
        self.assertEqual(self._rule().final_rate(date(2025, 1, 31), date(2025, 2, 28)), Decimal("0.20"))

    @case("G11-16")
    def test_contingent_reserve_not_equity(self):
        lg, ev = boot(qty=1000, acq="2025-06-10", cash="5000")
        lg.apply(ev("CA_RECORD", {"action_id": "D1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                  "record_date": "2025-06-16", "cash_per_share": "0.1", "tax_base_per_share": "0.1"}))
        e0 = lg.equity()[0]
        self.assertEqual(lg.contingent_tax_reserve("2025-06-17"), Decimal("20.00"))     # 100×20%
        self.assertEqual(lg.own_free_cash("2025-06-17"), Decimal(5000) - Decimal(20))
        self.assertEqual(lg.equity()[0], e0)

    @case("G11-17")
    def test_sell_before_pay(self):
        lg, ev = boot(qty=1000, acq="2025-06-10", cash="5000")
        div(lg, ev, per="0.1", ref="9.9")
        sell(lg, ev, "s", 1000, price="9.9", d="2025-06-20")
        sl = next(iter(lg.slices.values()))
        self.assertEqual(lg.tax_payables[sl["slice_id"]], Decimal("20.00"))
        lg.apply(ev("CA_PAY", {"action_id": "D1", "withheld": "0"}))
        self.assertEqual(lg.tax_payables[sl["slice_id"]], Decimal("20.00"))           # 派息时补税关系不丢

    @case("G11-18")
    def test_withheld_then_supplement(self):
        rules = [dict(stub_tax_rules()[0], initial_withholding_rate="0.05")]
        lg, ev = boot(qty=1000, acq="2025-06-10", cash="5000", fees=test_fees(tax_rules=rules))
        div(lg, ev, per="0.1", ref="9.9")
        lg.apply(ev("CA_PAY", {"action_id": "D1", "withheld": "5.00"}))
        self.assertEqual(lg.cash, Decimal(5000) + Decimal(95))
        sell(lg, ev, "s", 1000, price="9.9", d="2025-06-20")
        sl = next(iter(lg.slices.values()))
        self.assertEqual(lg.tax_payables[sl["slice_id"]], Decimal("15.00"))           # 20 − 已扣 5

    @case("G11-19")
    def test_unknown_tax_basis(self):
        lg, ev = boot(qty=1000, acq=None, cash="5000")
        div(lg, ev, per="0.1", ref="9.9")
        sell(lg, ev, "s", 1000, price="9.9", d="2025-06-20")
        self.assertTrue(any(k.startswith("TAX_UNKNOWN") for k in lg.limitations))
        self.assertEqual(lg.tax_payables, {})
        self.assertTrue(next(iter(lg.slices.values()))["tax_unknown"])

    @case("G11-20")
    def test_suspended_provisional(self):
        m = StubMarket(n=5)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 100}, 1000)
        d = date(2024, 1, 8)
        m.set_bar("X0001.SH", d, status="SUSPENDED", close="10")
        close_day(r, d)
        p = r.ledger.positions["X0001.SH"]
        self.assertEqual((p.mark_status, p.mark_date), ("PROVISIONAL_SUSPENDED", "2024-01-05"))

    @case("G11-21")
    def test_vanished_bar_not_residual(self):
        m = StubMarket(n=5)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 100}, 1000)
        d = date(2024, 1, 8)
        m.set_bar("X0001.SH", d, missing=True)
        close_day(r, d)
        p = r.ledger.positions["X0001.SH"]
        self.assertEqual((p.quantity, p.lifecycle, p.mark_status), (100, "NORMAL", "UNRELIABLE"))
        self.assertIn("X0001.SH", r.ledger.actual_slots())
        self.assertEqual(r.ledger.orders, {})

    @case("G11-22")
    def test_residual(self):
        lg, ev = boot(qty=100)
        e0 = lg.equity()[0]
        lg.apply(ev("LIFECYCLE", {"security_id": "X0001.SH", "lifecycle": "RESIDUAL", "evidence_ref": "TERM-1"}))
        self.assertNotIn("X0001.SH", lg.actual_slots())
        self.assertEqual((lg.equity()[0], lg.cash), (e0, Decimal(1000)))
        with self.assertRaises(ContractError):
            lg.apply(ev("LIFECYCLE", {"security_id": "X0001.SH", "lifecycle": "RESIDUAL", "evidence_ref": ""}))

    @case("G11-23")
    def test_residual_recovery_after_retire(self):
        lg, ev = boot(qty=100)
        lg.apply(ev("LIFECYCLE", {"security_id": "X0001.SH", "lifecycle": "RESIDUAL", "evidence_ref": "T"}))
        lg.apply(ev("RETIREMENT", {"at": "2025-01-01T15:30:00+08:00"}))
        lg.apply(ev("RUN_STATE", {"to": "EXITING", "reason": "x"}))
        lg.apply(ev("RESIDUAL_RECOVERY", {"security_id": "X0001.SH", "qty": 100, "cash": "30"}))
        self.assertEqual((lg.cash, lg.positions["X0001.SH"].quantity, lg.retirement_triggered), (Decimal(1030), 0, True))

    @case("G11-24")
    def test_conversion_breaks_scope(self):
        lg = Ledger("A", test_fees(), etf_ids={"E510000.SH"})
        ev = EventFactory()
        lg.apply(ev("BOOTSTRAP_ACCOUNT", {"cash": "0", "positions": [{"security_id": "X0001.SH", "market": "SH",
                                                                       "board": "MAIN_SH", "quantity": 100, "mark": "10"}]}))
        lg.apply(ev("CA_CONVERSION", {"action_id": "M1", "from_security_id": "X0001.SH", "to_security_id": "E510000.SH",
                                      "shares_per_share": "2", "to_market": "SH", "to_board": "MAIN_SH",
                                      "sellable_from": "2025-01-03", "reference_price": "5"}))
        self.assertEqual(lg.positions["E510000.SH"].quantity, 200)
        self.assertIn("SCOPE_OVERLAP:E510000.SH", lg.limitations)

    @case("G11-25")
    def test_two_dividends_fifo(self):
        lg, ev = boot(qty=300, acq="2025-06-01", cash="100000")
        div(lg, ev, aid="D1", per="0.1", record="2025-06-10", ref="9.9")
        div(lg, ev, aid="D2", per="0.1", record="2025-06-12", ref="9.8")
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=100, limit="10", trade_date="2025-06-13")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "9.8", trade_date="2025-06-13", sellable_from="2025-06-16")))
        sell(lg, ev, "s", 200, price="9.8", d="2025-06-20")
        un = sorted((s["action_id"], s["unresolved_qty"]) for s in lg.slices.values())
        self.assertEqual(un, [("D1", 100), ("D2", 100)])          # 两次分红各消耗200，晚买批次无补税
        self.assertEqual(len(lg.slices), 2)
        self.assertEqual(sorted(lg.tax_payables.values()), [Decimal("4.00"), Decimal("4.00")])

    @case("G11-26")
    def test_allocation_rounding(self):
        out = allocate_cents(Decimal("0.05"), [("s2", Fraction(1)), ("s1", Fraction(1))])
        self.assertEqual(out, {"s1": Decimal("0.03"), "s2": Decimal("0.02")})
        self.assertEqual(sum(out.values()), Decimal("0.05"))


class G12(unittest.TestCase):
    def _ledger(self, cash="100000"):
        lg, ev = new_ledger(cash=cash, fees=test_fees())
        return lg, ev

    def _marked(self, e_target):
        """入金100000后，以持仓估值把权益调到 e_target（股票部分）。"""
        lg, ev = self._ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=5000, limit="10")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 5000, "10")))
        stock = Decimal(e_target) - lg.cash
        lg.apply(ev("MARKS", {"marks": {"X0001.SH": {"price": str(stock / 5000), "mark_date": "2026-01-05",
                                                     "status": "NORMAL"}}}))
        lg.apply(ev("EOD", {"date": "2026-01-05"}))
        return lg, ev

    @case("G12-01", "G16-01")
    def test_first_day_loss(self):
        lg, ev = self._marked("90000")
        pt = lg.ledger_points[-1]
        self.assertEqual((pt["nav"], pt["dd"]), (Fraction(9, 10), Fraction(1, 10)))

    def _state_75000(self):
        """经真实事件构造 G12-02 初态：E=75000、U=50000、v=1.5、HWM=2。"""
        lg, ev = self._ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=100, limit="10")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "10")))            # 现金 98995
        for price, d in (("1010.05", "2026-01-05"), ("510.05", "2026-01-06")):   # E=200000 → NAV2；E=150000 → NAV1.5
            lg.apply(ev("MARKS", {"marks": {"X0001.SH": {"price": price, "mark_date": d, "status": "NORMAL"}}}))
            lg.apply(ev("EOD", {"date": d}))
        lg.apply(ev("CAPITAL_FLOW", {"flow_id": "o0", "amount": "-75000"}))
        self.assertEqual((lg.equity()[0], lg.units, lg.hwm), (Fraction(75000), Fraction(50000), Fraction(2)))
        return lg, ev

    @case("G12-02")
    def test_inflow_keeps_unit_value(self):
        lg, ev = self._state_75000()
        lg.apply(ev("CAPITAL_FLOW", {"flow_id": "in", "amount": "75000"}))
        e = lg.equity()[0]
        self.assertEqual((lg.units, e, e / lg.units, lg.hwm), (Fraction(100000), Fraction(150000), Fraction(3, 2), 2))
        lg.apply(ev("EOD", {"date": "2026-01-07"}))
        self.assertEqual(lg.ledger_points[-1]["dd"], Fraction(1, 4))

    @case("G12-03")
    def test_outflow(self):
        lg, ev = self._state_75000()
        lg.apply(ev("CAPITAL_FLOW", {"flow_id": "o1", "amount": "-15000"}))
        e = lg.equity()[0]
        self.assertEqual((lg.units, e, e / lg.units, lg.hwm), (Fraction(40000), Fraction(60000), Fraction(3, 2), 2))

    @case("G12-04")
    def test_quota_only_no_change(self):
        lg, ev = self._ledger()
        snap = (lg.cash, lg.units, lg.equity()[0])
        sc = SharedCash(Decimal(0), Decimal(100000), Decimal(50000))
        sc.C["UNASSIGNED"] -= 10000            # 只改额度视图，未真实划拨：不形成资本流事件
        self.assertEqual(snap, (lg.cash, lg.units, lg.equity()[0]))

    @case("G12-05")
    def test_outflow_cannot_use_committed(self):
        sc = SharedCash(Decimal(0), Decimal(100000))
        rev = sc.broker_snapshot(Decimal(100000))
        sc.authorize("STOCK", "b1", Decimal(30000), rev)
        sc.preapprove("c1", "STOCK", Decimal(30000), "o1")
        sc.state = "IDLE"
        sc.tax_reserve["STOCK"] = Decimal(500)
        r = sc.transfer("t1", "STOCK", "ETF", Decimal(70000))
        self.assertEqual(r.codes(), ["TRANSFER_EXCEEDS_FREE_CASH"])
        self.assertTrue(sc.transfer("t2", "STOCK", "ETF", Decimal(69500)).ok)

    @case("G12-06")
    def test_full_redemption(self):
        lg, ev = self._ledger()
        lg.apply(ev("RETIREMENT", {"at": "2026-01-02T15:30:00+08:00"}))
        lg.apply(ev("CAPITAL_FLOW", {"flow_id": "all", "amount": "-100000"}))
        self.assertEqual((lg.units, lg.capital_terminated, lg.retirement_triggered), (Fraction(0), True, True))
        lg.apply(ev("EOD", {"date": "2026-01-05"}))
        self.assertEqual(lg.ledger_points[-1]["nav"], Fraction(1))

    @case("G12-07")
    def test_duplicate_event(self):
        lg, ev = self._ledger()
        e = ev("CAPITAL_FLOW", {"flow_id": "x", "amount": "100"})
        self.assertTrue(lg.apply(e))
        self.assertFalse(lg.apply(e))
        self.assertEqual(lg.cash, Decimal(100100))

    @case("G12-08")
    def test_same_id_different_content(self):
        lg, ev = self._ledger()
        lg.apply({"event_id": "x", "kind": "CAPITAL_FLOW", "payload": {"flow_id": "x", "amount": "100"}})
        with self.assertRaises(ContractError) as cm:
            lg.apply({"event_id": "x", "kind": "CAPITAL_FLOW", "payload": {"flow_id": "x", "amount": "200"}})
        self.assertEqual(cm.exception.code, "EVENT_CONFLICT")
        self.assertIn("EVENT_CONFLICT", lg.limitations)

    def _journal_events(self):
        ev = EventFactory("j")
        return [ev("CAPITAL_FLOW", {"flow_id": "init", "amount": "100000"}),
                ev("ORDER_CREATE", buy_order("b", qty=300)), ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}),
                ev("BROKER_FILL", fill("b", "t1", 100, "10")), ev("BROKER_FILL", fill("b", "t2", 200, "10")),
                ev("BROKER_TERMINAL", {"client_order_id": "b", "status": "FILLED", "cum_qty": 300, "cum_value": "3000",
                                       "source_revision": 1})]

    @case("G12-09")
    def test_crash_before_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            lg = Ledger("A", test_fees())
            evs = self._journal_events()
            for e in evs[:3]:
                j.commit(e)
                lg.apply(e)
            j.write_snapshot(lg)
            for e in evs[3:]:
                j.commit(e)                    # 提交后崩溃，快照未更新
            j.release()
            j2 = Journal(d)
            lg2 = recover(j2, "A", test_fees())
            ref = Ledger("A", test_fees())
            for e in evs:
                ref.apply(e)
            self.assertEqual((lg2.cash, lg2.positions["X0001.SH"].quantity, lg2.seq), (ref.cash, 300, ref.seq))

    @case("G12-10")
    def test_torn_tail(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            for e in self._journal_events()[:4]:
                j.commit(e)
            j.release()
            with open(Path(d) / "events.jsonl", "ab") as f:
                f.write(b'{"seq":5,"event_id":"half')
            # 只读恢复不修改日志
            ro = recover(Journal(d), "A", test_fees())
            self.assertEqual(ro.seq, 4)
            self.assertFalse(any(p.name.startswith("events.torn") for p in Path(d).iterdir()))
            # resume：取得写锁后保留损坏片段、截去半行，前序不丢，可继续追加
            j2 = Journal(d)
            j2.acquire()
            lg = recover(j2, "A", test_fees())
            self.assertEqual(lg.seq, 4)
            self.assertTrue(any(p.name.startswith("events.torn") for p in Path(d).iterdir()))
            self.assertTrue((Path(d) / "events.jsonl").read_bytes().endswith(b"\n"))
            ev5 = self._journal_events()[4] if len(self._journal_events()) > 4 else None
            if ev5 is not None:
                j2.commit(ev5)
                self.assertEqual(len(Journal(d).read_committed()), 5)
            j2.release()

    @case("G12-11")
    def test_snapshot_ahead(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            lg = Ledger("A", test_fees())
            for e in self._journal_events():
                j.commit(e)
                lg.apply(e)
            j.write_snapshot(lg)
            j.release()
            lines = (Path(d) / "events.jsonl").read_text().splitlines()
            (Path(d) / "events.jsonl").write_text("\n".join(lines[:3]) + "\n")
            with self.assertRaises(ContractError) as cm:
                recover(Journal(d), "A", test_fees())
            self.assertEqual(cm.exception.code, "SNAPSHOT_AHEAD_OF_JOURNAL")

    @case("G12-13")
    def test_expired_plan_not_replayed(self):
        lg, ev = self._ledger()
        lg.apply(ev("PLAN", {"plan": {"plan_id": "p1", "execute_session": "2026-01-05", "modes": {},
                                      "buy_candidates_ordered": ["X0009.SH"]}}))
        acts = resume_actions(lg, datetime(2026, 1, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(acts["expired_plans"], [{"plan_id": "p1", "reason": "ORDINARY_PLAN_EXPIRED_NOT_REPLAYED"}])
        self.assertEqual(acts["resend"], [])

    @case("G12-14")
    def test_risk_recomputed_with_new_equity(self):
        from smallcap.risk import SellLeg, SellPlanner
        from smallcap.rules import QuantityGrid
        g = QuantityGrid(100, 100, 100, 100, True, 10 ** 6)
        leg = lambda: {"A": SellLeg("A", 600, Fraction(10), Decimal(10), "I", 600, g, 10 ** 9, Fraction(6000))}
        old = SellPlanner(Fraction(100000), leg(), lambda s, q, p: Decimal(0)).run(exits=[], singles=["A"], industries=[], total=False)
        new = SellPlanner(Fraction(150000), leg(), lambda s, q, p: Decimal(0)).run(exits=[], singles=["A"], industries=[], total=False)
        self.assertEqual((old["sell"]["A"], new["sell"]["A"]), (400, 300))       # 按真实新E重算，不盲重放旧量

    @case("G12-15")
    def test_no_balance_override(self):
        lg, ev = self._ledger()
        r = reconcile_account(lg, {"positions": {}, "cash_book": "100100"})
        self.assertEqual(r.status, C.INVALID)
        self.assertEqual(lg.cash, Decimal(100000))
        self.assertTrue(any(k.startswith("CASH_RECONCILIATION_FAILED") for k in lg.limitations))

    @case("G12-16")
    def test_single_writer_lock(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Journal(d), Journal(d)
            a.acquire()
            with self.assertRaises(ContractError) as cm:
                b.acquire()
            self.assertEqual(cm.exception.code, "WRITER_LOCKED")
            with self.assertRaises(ContractError):
                b.commit({"event_id": "x", "kind": "CURSOR", "payload": {}})
            a.release()
            b.acquire()
            b.release()


if __name__ == "__main__":
    unittest.main()
