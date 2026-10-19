import unittest
from datetime import date, time
from decimal import Decimal
from fractions import Fraction

from smallcap import contracts as C
from smallcap.contracts import ContractError, sh_dt, wire_ratio
from smallcap.risk import (RiskView, SellLeg, SellPlanner, blocked_exit_value, detect, new_episode, risk_decision)
from smallcap.rules import QuantityGrid
from tests.acceptance import case
from tests.fixtures import (StubMarket, close_day, exec_day, make_runner, put_plan, seed_holdings)

MAIN = QuantityGrid(100, 100, 100, 100, True, 1_000_000)
D0 = date(2024, 1, 8)


def rv(E, values, ind=None, closes=None, facts=None, dd=None):
    values = {k: Fraction(Decimal(str(v))) for k, v in values.items()}
    return RiskView(Fraction(Decimal(str(E))), values, ind or {k: "I0" for k in values},
                    {k: 100 for k in values}, closes or {}, facts or {}, dd)


def types(starts):
    return sorted((e["type"], e["key"]) for e in starts)


def dec(**kw):
    base = dict(account_revision=1, episodes={}, run_state="NORMAL", equity=Fraction(100000), valuation_status="OK",
                account_limitations={}, unknown_industry_holdings=[], order_state_unknown=False, batch_blocks=[],
                security_exits={}, no_buy_in_batch=set(), hold_no_add=set(), risk_sell_sids=set(),
                blocked_exit_value=Fraction(0))
    base.update(kw)
    return risk_decision(**base)


class G08(unittest.TestCase):
    @case("G08-01")
    def test_total_trigger_strict(self):
        vals = {f"S{i}": 6500 for i in range(10)}
        s, _, _ = detect(rv(100000, vals, {k: f"I{i}" for i, k in enumerate(vals)}), {}, at="t", first_eligible="d", seq=1)
        self.assertNotIn(("TOTAL_REDUCE", "ACCOUNT"), types(s))
        vals["S0"] = Decimal("6500.01")
        s, _, _ = detect(rv(100000, vals, {k: f"I{i}" for i, k in enumerate(vals)}), {}, at="t", first_eligible="d", seq=1)
        self.assertIn(("TOTAL_REDUCE", "ACCOUNT"), types(s))

    def _active(self, etype, key, target):
        e = new_episode(etype, key, 1, "t", "d", [key], target)
        return {e["episode_id"]: e}

    @case("G08-02")
    def test_total_continues_at_64(self):
        eps = self._active("TOTAL_REDUCE", "ACCOUNT", C.STOCK_TARGET)
        vals = {f"S{i}": 6400 for i in range(10)}
        s, e, _ = detect(rv(100000, vals, {k: f"I{i}" for i, k in enumerate(vals)}), eps, at="t", first_eligible="d", seq=2)
        self.assertEqual(e, [])

    @case("G08-03")
    def test_total_ends_at_60(self):
        eps = self._active("TOTAL_REDUCE", "ACCOUNT", C.STOCK_TARGET)
        vals = {f"S{i}": 6000 for i in range(10)}
        _, e, _ = detect(rv(100000, vals, {k: f"I{i}" for i, k in enumerate(vals)}), eps, at="t", first_eligible="d", seq=2)
        self.assertEqual(e, [("TOTAL_REDUCE:ACCOUNT:1", "TARGET_SATISFIED")])

    @case("G08-04")
    def test_single_trigger_strict(self):
        s, _, _ = detect(rv(100000, {"A": 5000}), {}, at="t", first_eligible="d", seq=1)
        self.assertNotIn(("SINGLE_REDUCE", "A"), types(s))
        s, _, _ = detect(rv(100000, {"A": "5000.01"}), {}, at="t", first_eligible="d", seq=1)
        self.assertIn(("SINGLE_REDUCE", "A"), types(s))
        self.assertEqual(s[0]["target_ratio"], wire_ratio(C.SINGLE_TARGET))

    @case("G08-05")
    def test_single_continues_until_2pct(self):
        eps = self._active("SINGLE_REDUCE", "A", C.SINGLE_TARGET)
        _, e, _ = detect(rv(100000, {"A": 4900}), eps, at="t", first_eligible="d", seq=2)
        self.assertEqual(e, [])
        _, e, _ = detect(rv(100000, {"A": 2000}), eps, at="t", first_eligible="d", seq=2)
        self.assertEqual(e, [("SINGLE_REDUCE:A:1", "TARGET_SATISFIED")])

    @case("G08-06")
    def test_industry_trigger_strict(self):
        vals = {f"S{i}": 4000 for i in range(5)}
        s, _, _ = detect(rv(100000, vals), {}, at="t", first_eligible="d", seq=1)
        self.assertNotIn(("INDUSTRY_REDUCE", "I0"), types(s))
        vals["S0"] = Decimal("4000.01")
        s, _, _ = detect(rv(100000, vals), {}, at="t", first_eligible="d", seq=1)
        self.assertIn(("INDUSTRY_REDUCE", "I0"), types(s))

    @case("G08-07")
    def test_merge_max_not_sum(self):
        fee = lambda s, q, p: Decimal(0)
        legs = {"A": SellLeg("A", 2600, Fraction(10), Decimal(10), "I0", 2600, MAIN, 10 ** 9, Fraction(26000)),
                "B": SellLeg("B", 4000, Fraction(10), Decimal(10), "I1", 4000, MAIN, 10 ** 9, Fraction(40000))}
        single = SellPlanner(Fraction(100000), dict(legs), fee).run(exits=[], singles=["A"], industries=[], total=False)
        ind = SellPlanner(Fraction(100000), dict(legs), fee).run(exits=[], singles=[], industries=["I0"], total=False)
        both = SellPlanner(Fraction(100000), dict(legs), fee).run(exits=[], singles=["A"], industries=["I0"], total=True)
        self.assertEqual(single["sell"]["A"], 2400)
        self.assertEqual(ind["sell"]["A"], 600)
        self.assertEqual(both["sell"]["A"], 2400)                   # 取最严格，不是 2400+600
        self.assertLessEqual(both["sell"]["A"] + both["sell"].get("B", 0), 2600 + 4000)

    @case("G08-08")
    def test_blocked_exit_10pct(self):
        self.assertNotIn("BLOCKED_EXIT_EXPOSURE", dec(blocked_exit_value=Fraction(9999))["global_buy_blocks"])
        self.assertIn("BLOCKED_EXIT_EXPOSURE", dec(blocked_exit_value=Fraction(10000))["global_buy_blocks"])
        self.assertEqual(blocked_exit_value({"A": 1000}, {"A": 0}, {"A": Decimal(10)}), Fraction(10000))
        self.assertEqual(blocked_exit_value({"A": 1000}, {"A": 400}, {"A": Decimal(10)}), Fraction(6000))

    @case("G08-09")
    def test_clear_blocked_but_industry_remains(self):
        eps = self._active("INDUSTRY_REDUCE", "I0", C.INDUSTRY_LIMIT)
        d = dec(episodes=eps, blocked_exit_value=Fraction(9000))
        self.assertEqual(d["global_buy_blocks"], ["INDUSTRY_REDUCE"])

    @case("G08-10")
    def test_dd20_warning_only(self):
        s, _, f = detect(rv(100000, {}, dd=Fraction(1, 5)), {}, at="t", first_eligible="d", seq=1)
        self.assertEqual((f["warning"], f["retire"]), (True, False))
        self.assertEqual(s, [])

    def _seeded(self, scenario="S0", mode="REPLAY_DAILY_PROXY"):
        m = StubMarket(n=40)
        r = make_runner(m, scenario=scenario, mode=mode)
        held = {f"X{i:04d}.SH": 600 for i in range(1, 11)}
        seed_holdings(r, date(2024, 1, 5), held, 40000)          # E=100000, NAV=1
        return m, r, held

    @case("G08-11", "G16-15")
    def test_dd25_retires(self):
        m, r, held = self._seeded()
        for s in held:
            m.set_bar(s, D0, close="5.83", open="10", ref_preclose="10")      # 40000+6000×5.83=74980 → DD≥25%
        close_day(r, D0)
        L = r.ledger
        self.assertTrue(L.retirement_triggered)
        self.assertEqual(L.run_state, "EXITING")
        self.assertTrue(any(e["type"] == "EMERGENCY_EXIT" for e in L.episodes.values()))
        exec_day(r, date(2024, 1, 9))
        self.assertTrue(all(L.positions[s].quantity == 0 for s in held if s in L.positions))
        self.assertFalse(any(o.side == "BUY" for o in L.orders.values()))

    @case("G08-12")
    def test_retirement_not_cleared(self):
        m, r, held = self._seeded()
        for s in held:
            m.set_bar(s, D0, close="5.83")
        close_day(r, D0)
        L = r.ledger
        d1 = date(2024, 1, 9)
        for s in held:
            m.set_bar(s, d1, close="7", open="5.83", ref_preclose="5.83", status="SUSPENDED")
        r.phase_key, r.k = "inflow", 0
        r.emit("CAPITAL_FLOW", {"flow_id": "f", "amount": "100000"}, sh_dt(d1, time(9)))
        close_day(r, d1)
        self.assertTrue(L.retirement_triggered)
        self.assertEqual(L.run_state, "EXITING")
        r.phase_key, r.k = "x", 0
        with self.assertRaises(ContractError):
            r.emit("RUN_STATE", {"to": "NORMAL", "reason": "TRY"}, sh_dt(d1, time(16)))

    @case("G08-13")
    def test_intraday_drawdown_no_trigger(self):
        m, r, held = self._seeded()
        for s in held:
            m.set_bar(s, D0, open="5.00", close="9.90", low="5.00", ref_preclose="10")
        put_plan(r, date(2024, 1, 5), {}, [], 100000, D0)
        exec_day(r, D0)
        self.assertFalse(r.ledger.retirement_triggered)
        close_day(r, D0)
        self.assertFalse(r.ledger.retirement_triggered)

    @case("G08-14")
    def test_st_confirmed_in_window(self):
        m = StubMarket(n=40)
        r = make_runner(m, mode="REPLAY_MINUTE_WINDOW")
        seed_holdings(r, date(2024, 1, 5), {f"X{i:04d}.SH": 200 for i in range(1, 11)}, 80000)
        from smallcap.contracts import wire_ratio as w
        # 截止 E=150000 时 X0001 为 1.33% → TARGET_AMOUNT 3000 → 10:00 补买100股
        put_plan(r, date(2024, 1, 5), {"X0001.SH": {"mode": "TARGET_AMOUNT", "target_amount": w(Fraction(3000)),
                                                     "prohibit_buy": False, "reason": "OUT_OF_BAND"}}, [], 150000, D0)
        for k in range(0, 6):                           # 10:00—10:05 价格高于买入保护上界，买单保持未成交
            m.minute_override[("X0001.SH", D0, k)] = {"open": Decimal("10.20"), "low": Decimal("10.15"),
                                                       "high": Decimal("10.25")}
        m.risk.append({"security_id": "X0001.SH", "status": "ST", "effective": D0,
                       "available_at": sh_dt(D0, time(10, 5, 30))})
        exec_day(r, D0)
        L = r.ledger
        buys = [o for o in L.orders.values() if o.side == "BUY" and o.security_id == "X0001.SH"]
        sells = [o for o in L.orders.values() if o.side == "SELL" and o.security_id == "X0001.SH"]
        self.assertTrue(buys and all(o.closed and o.filled_qty == 0 for o in buys))
        self.assertTrue(sells)
        first_sell = min(o.submitted_at for o in sells)
        self.assertGreaterEqual(first_sell, sh_dt(D0, time(10, 6)).isoformat())     # 确认后下一整分钟起
        self.assertEqual(L.positions["X0001.SH"].quantity if "X0001.SH" in L.positions else 0, 0)
        self.assertTrue(any(e["type"] == "SECURITY_EXIT" and e["key"] == "X0001.SH" for e in L.episodes.values()))

    @case("G08-15")
    def test_price_exit_not_undone_by_rebound(self):
        eps = {}
        s, _, _ = detect(rv(100000, {"A": 1990}, closes={"A": Decimal("1.99")}), eps, at="t", first_eligible="d", seq=1)
        self.assertIn(("SECURITY_EXIT", "A"), types(s))
        eps = {e["episode_id"]: e for e in s}
        _, e, _ = detect(rv(100000, {"A": 2010}, closes={"A": Decimal("2.01")}), eps, at="t", first_eligible="d", seq=2)
        self.assertEqual([x for x in e if x[0].startswith("SECURITY_EXIT")], [])

    @case("G08-16")
    def test_ex_date_nominal_drop_same_rule(self):
        s, _, _ = detect(rv(100000, {"A": 1950}, closes={"A": Decimal("1.95")}), {}, at="t", first_eligible="d", seq=1)
        self.assertEqual([e["detail"] for e in s if e["type"] == "SECURITY_EXIT"], ["PRICE_BELOW_2"])

    @case("G08-17")
    def test_retired_with_residual(self):
        m, r, held = self._seeded()
        L = r.ledger
        r.phase_key, r.k = "res", 0
        r.emit("LIFECYCLE", {"security_id": "X0010.SH", "lifecycle": "RESIDUAL", "evidence_ref": "TEST"}, sh_dt(D0, time(8)))
        r.emit("MARKS", {"marks": {"X0010.SH": {"price": "1.00", "mark_date": "2024-01-08",
                                                "status": "PROVISIONAL_REFERENCE", "bounds": ["0", "10"]}}},
               sh_dt(D0, time(8)))
        for s in held:
            m.set_bar(s, D0, close="5.83")             # 40000+9×600×5.83+600×1=72082 → DD≥25%
        close_day(r, D0)
        d1 = date(2024, 1, 9)
        exec_day(r, d1)
        close_day(r, d1)
        self.assertEqual(L.run_state, "RETIRED")
        self.assertEqual(L.positions["X0010.SH"].quantity, 600)
        self.assertEqual(L.positions["X0010.SH"].lifecycle, "RESIDUAL")
        self.assertIsNotNone(L.ledger_points[-1]["nav"])

    @case("G08-18")
    def test_nonpositive_equity(self):
        d = dec(equity=Fraction(-1))
        self.assertIn("INSOLVENT", d["global_buy_blocks"])
        s, e, f = detect(rv(-1, {"A": 100}), {}, at="t", first_eligible="d", seq=1)
        self.assertEqual((s, e), ([], []))

    @case("G08-19")
    def test_global_fault_still_risk_exit(self):
        m, r, held = self._seeded()
        modes = {s: {"mode": "HOLD_NO_ADD", "target_amount": None, "prohibit_buy": True, "reason": "POOL_UNCERTAIN"}
                 for s in held}
        put_plan(r, date(2024, 1, 5), modes, [], 100000, D0, ordinary_buy_allowed=False, score_status="UNHEALTHY",
                 score_reason="POOL_UNCERTAIN")
        m.risk.append({"security_id": "X0001.SH", "status": "TERMINATED", "effective": D0,
                       "available_at": sh_dt(date(2024, 1, 5), time(18))})
        r.phase_key, r.k = "pre", 0
        r._phase_preopen(D0)
        exec_day(r, D0)
        self.assertEqual(r.ledger.positions.get("X0001.SH").quantity if "X0001.SH" in r.ledger.positions else 0, 0)

    @case("G08-20")
    def test_false_trigger_manual_correction(self):
        m, r, held = self._seeded()
        for s in held:
            m.set_bar(s, D0, close="5.83")
        close_day(r, D0)
        exec_day(r, date(2024, 1, 9))
        L = r.ledger
        trades_before = list(L.trade_log)
        r.phase_key, r.k = "fix", 0
        with self.assertRaises(ContractError):
            r.emit("RETIREMENT_CORRECTION", {"evidence_ref": "", "reconciled": True, "manual_decision_ref": "x"},
                   sh_dt(date(2024, 1, 10), time(9)))
        r.k = 1
        r.emit("RETIREMENT_CORRECTION", {"evidence_ref": "DATA_FIX-1", "reconciled": True, "manual_decision_ref": "USER-OK"},
               sh_dt(date(2024, 1, 10), time(9)))
        self.assertFalse(L.retirement_triggered)
        self.assertEqual(L.trade_log, trades_before)              # 已发生交易不倒删
        self.assertEqual(L.retirement_manually_cleared[0]["evidence_ref"], "DATA_FIX-1")


if __name__ == "__main__":
    unittest.main()
