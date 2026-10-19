import copy
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction

from smallcap import contracts as C
from smallcap.bundle import load_bundle
from smallcap.clock import stress_block_days
from smallcap.contracts import sh_dt, wire_ratio
from smallcap.evaluation import cell_record, qualify_config
from smallcap.execution import proxy_price, quote_fresh
from smallcap.matrix import run_matrix
from smallcap.risk import new_episode
from smallcap.synthetic import SyntheticMarket, synthetic_calendar
from tests.acceptance import case
from tests.fixtures import (ENGINEERING_BUNDLE, EXTERNAL_TEMPLATE, StubMarket, close_day, exec_day, make_runner,
                            put_plan, seed_holdings, test_package, test_rulebook_payload, test_fee_payload,
                            stub_tax_rules)

D0 = date(2024, 1, 8)
T = Decimal("0.01")


def new_buy_plan(r, sids, e=100000, d=D0):
    modes = {s: {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(e) / 50), "prohibit_buy": False,
                 "reason": "NEW_BUY"} for s in sids}
    return put_plan(r, date(2024, 1, 5), modes, sids, e, d)


def episode(r, etype, key, members, first="2024-01-08", target=None):
    ep = new_episode(etype, key, len(r.ledger.episodes) + 1, "2024-01-05T15:30:00+08:00", first, members, target)
    r.phase_key, r.k = f"ep/{ep['episode_id']}", 0
    r.emit("RISK_EPISODE_START", {"episode": ep}, sh_dt(date(2024, 1, 5), time(15, 30)))
    r._stress_for(ep, sh_dt(date(2024, 1, 5), time(15, 30)))
    return ep


class G13(unittest.TestCase):
    @case("G13-01")
    def test_limit_up_no_replacement(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {}, 100000)
        new_buy_plan(r, ["X0031.SH"])
        m.set_bar("X0031.SH", D0, open="11.00", close="11.00", ref_preclose="10")
        exec_day(r, D0)
        self.assertEqual(r.ledger.orders, {})
        self.assertEqual(r.ledger.cash, Decimal(100000))

    @case("G13-02")
    def test_10am_st_not_backfilled(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 200}, 98000)
        m.risk.append({"security_id": "X0001.SH", "status": "ST", "effective": D0, "available_at": sh_dt(D0, time(10))})
        r.phase_key, r.k = "pre", 0
        r._phase_preopen(D0)
        exec_day(r, D0)
        self.assertEqual(r.ledger.positions["X0001.SH"].quantity, 200)        # 09:30 代理不回填
        close_day(r, D0)
        d1 = date(2024, 1, 9)
        exec_day(r, d1)
        self.assertNotIn("X0001.SH", {s for s, p in r.ledger.positions.items() if p.quantity})

    @case("G13-03")
    def test_open_limit_down_no_daily_fill(self):
        self.assertIsNone(proxy_price("SELL", Decimal("9.00"), C.BASE_SLIPPAGE, T, Decimal("9.00"), Decimal("11.00")))

    def _minute(self, holdings=None, cash=100000):
        m = StubMarket(n=40)
        r = make_runner(m, mode="REPLAY_MINUTE_WINDOW")
        seed_holdings(r, date(2024, 1, 5), holdings or {}, cash)
        return m, r

    @case("G13-04")
    def test_minute_first_interval(self):
        m, r = self._minute()
        new_buy_plan(r, ["X0031.SH"])
        exec_day(r, D0)
        o = next(iter(r.ledger.orders.values()))
        self.assertEqual(o.submitted_at, sh_dt(D0, time(10)).isoformat())
        f = next(iter(o.fills.values()))
        self.assertTrue(sh_dt(D0, time(10)) <= C.parse_ts(f["occurred_at"]) < sh_dt(D0, time(10, 1)))
        self.assertEqual(C.parse_ts(f["received_at"]), sh_dt(D0, time(10, 1)))

    @case("G13-05")
    def test_fill_before_timeout_cancel(self):
        m, r = self._minute()
        new_buy_plan(r, ["X0031.SH"])
        m.minute_override[("X0031.SH", D0, 0)] = {"volume": 100}
        for k in range(1, 30):
            m.minute_override[("X0031.SH", D0, k)] = {"open": Decimal("10.40"), "low": Decimal("10.40"),
                                                       "high": Decimal("10.45")}
        exec_day(r, D0)
        first = sorted(r.ledger.orders.values(), key=lambda o: o.attempt)[0]
        self.assertEqual((first.filled_qty, first.status), (100, "CANCELED"))   # 先计 Bar 内成交，再撤

    @case("G13-06")
    def test_proceeds_visible_next_minute(self):
        m, r = self._minute({"X0001.SH": 200}, 98000)
        r.phase_key, r.k = "restr", 0
        r.emit("CASH_RESTRICTION", {"restriction_id": "R", "amount": "98000", "until": None, "reason": "TEST_HOLD"},
               sh_dt(D0, time(8)))                                   # 可付款现金为0：只能用卖出回款
        modes = {"X0001.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK"},
                 "X0031.SH": {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)), "prohibit_buy": False,
                              "reason": "NEW_BUY"}}
        put_plan(r, date(2024, 1, 5), modes, ["X0031.SH"], 100000, D0)
        exec_day(r, D0)
        buy = [o for o in r.ledger.orders.values() if o.side == "BUY"][0]
        sell = [o for o in r.ledger.orders.values() if o.side == "SELL"][0]
        self.assertEqual(sell.submitted_at, sh_dt(D0, time(10)).isoformat())
        self.assertGreaterEqual(buy.submitted_at, sh_dt(D0, time(10, 1)).isoformat())

    @case("G13-07")
    def test_1029_last_new_order(self):
        m, r = self._minute()
        new_buy_plan(r, ["X0031.SH"])
        for k in range(0, 29):
            m.minute_override[("X0031.SH", D0, k)] = {"open": Decimal("10.40"), "low": Decimal("10.40"),
                                                       "high": Decimal("10.45")}
        r.ledger.decision_log.clear()
        exec_day(r, D0)
        subs = sorted(o.submitted_at for o in r.ledger.orders.values())
        self.assertTrue(all(s < sh_dt(D0, time(10, 30)).isoformat() for s in subs))
        from smallcap.execution import can_submit_new
        self.assertTrue(can_submit_new(sh_dt(D0, time(10, 29))))
        self.assertFalse(can_submit_new(sh_dt(D0, time(10, 30))))

    @case("G13-08")
    def test_fact_at_100030_acts_next_minute(self):
        m, r = self._minute({"X0001.SH": 200}, 98000)
        m.risk.append({"security_id": "X0001.SH", "status": "ST", "effective": D0,
                       "available_at": sh_dt(D0, time(10, 0, 30))})
        exec_day(r, D0)
        s = [o for o in r.ledger.orders.values() if o.side == "SELL"][0]
        self.assertEqual(s.submitted_at, sh_dt(D0, time(10, 1)).isoformat())
        self.assertEqual(r.cal.first_window_after(sh_dt(D0, time(10, 0, 30))), sh_dt(D0, time(10, 1)))

    @case("G13-09")
    def test_price_outside_range_no_fill(self):
        m, r = self._minute()
        new_buy_plan(r, ["X0031.SH"])
        for k in range(0, 30):        # 候选价 10.01 不在已实现区间 [10.02,10.05]
            m.minute_override[("X0031.SH", D0, k)] = {"open": Decimal("10.00"), "low": Decimal("10.02"),
                                                       "high": Decimal("10.05")}
        exec_day(r, D0)
        self.assertTrue(all(o.filled_qty == 0 for o in r.ledger.orders.values()))

    @case("G13-10")
    def test_bar_volume_cap(self):
        m, r = self._minute()
        new_buy_plan(r, ["X0031.SH"])
        m.minute_override[("X0031.SH", D0, 0)] = {"volume": 50}
        for k in range(1, 30):
            m.minute_override[("X0031.SH", D0, k)] = {"open": Decimal("10.40"), "low": Decimal("10.40"),
                                                       "high": Decimal("10.45")}
        exec_day(r, D0)
        first = sorted(r.ledger.orders.values(), key=lambda o: o.attempt)[0]
        self.assertEqual((first.qty, first.filled_qty), (200, 50))

    @case("G13-11")
    def test_missing_minute_bars(self):
        m, r = self._minute()
        new_buy_plan(r, ["X0031.SH"])
        m.minute_bars = lambda sid, d: []
        exec_day(r, D0)
        self.assertEqual(r.ledger.orders, {})
        self.assertEqual(r.ledger.cash, Decimal(100000))

    def _cells(self, mode, evidence):
        return [{"start": s, "scenario": sc, "startup_status": "STARTED", "retirement_triggered": False,
                 "MDD_exact": {"num": "1", "den": "10"}, "mdd_over_30": False, "CAGR_decimal": "0.30",
                 "package_availability": "PASS", "evidence_reasons": list(evidence), "annual_fee_rate": "0.01",
                 "execution_mode": mode}
                for s in ("A0", "A1", "A2", "A3") for sc in ("S0", "S1", "S2", "S3")]

    @case("G13-12")
    def test_modes_reported_separately(self):
        m = StubMarket(n=40, end=date(2024, 2, 2))
        recs = {}
        for mode in ("REPLAY_DAILY_PROXY", "REPLAY_MINUTE_WINDOW"):
            r = make_runner(m, mode=mode)
            r.run()
            recs[mode] = cell_record(r.spec, r, end_d=m.data_end)
        self.assertIn("NOT_WINDOW_CERTIFIED", recs["REPLAY_DAILY_PROXY"]["evidence_reasons"])
        self.assertNotIn("NOT_WINDOW_CERTIFIED", recs["REPLAY_MINUTE_WINDOW"]["evidence_reasons"])
        self.assertNotEqual(recs["REPLAY_DAILY_PROXY"]["execution_mode"], recs["REPLAY_MINUTE_WINDOW"]["execution_mode"])

    @case("G13-13")
    def test_daily_not_deployable(self):
        q = qualify_config(self._cells("REPLAY_DAILY_PROXY", ["NOT_WINDOW_CERTIFIED"]), test_package=False,
                           external_verified={"x": True}, benchmark_specified=True)
        self.assertEqual(q["status"], "INSUFFICIENT_EVIDENCE")
        self.assertTrue(any(u.startswith("NOT_WINDOW_CERTIFIED") for u in q["unknowns"]))

    @case("G13-14")
    def test_freshness_by_mode(self):
        now = sh_dt(D0, time(10, 1))
        self.assertTrue(quote_fresh(now - timedelta(seconds=59), now, mode="MINUTE_PROXY"))
        self.assertFalse(quote_fresh(now - timedelta(seconds=59), now, mode="TICK"))


class G15(unittest.TestCase):
    @case("G15-01")
    def test_start_anchor(self):
        cal = synthetic_calendar(date(2015, 12, 1), date(2016, 3, 1))
        self.assertEqual(cal.first_week_end_in_year(2016), date(2016, 1, 8))
        self.assertNotEqual(cal.first_week_end_in_year(2016), cal.session_on_or_after(date(2016, 1, 1)))

    @case("G15-02")
    def test_missing_a0_kept(self):
        b = load_bundle(copy.deepcopy(ENGINEERING_BUNDLE))
        res = run_matrix(b, market=None)
        self.assertEqual(res["cells"], 96)
        a0 = [r for r in res["records"] if r["start"] == "A0"]
        self.assertEqual(len(a0), 24)
        self.assertTrue(all(r["startup_status"] == "NOT_STARTED_UNKNOWN" for r in a0))
        # 有数据但 A0 预热不足：证据不足（不是不适配），单元保留
        m = StubMarket(n=40, start=date(2010, 12, 1), end=date(2011, 3, 31))
        for x in m.master.values():
            x["list_date"] = date(2005, 1, 4)
        r = make_runner(m, start_year=2011, end=date(2011, 3, 31))
        r.run()
        self.assertEqual(r.startup, {"status": "NOT_STARTED_UNKNOWN", "reason": "POOL_UNCERTAIN"})
        rec = cell_record(r.spec, r, end_d=m.data_end)
        self.assertEqual((rec["start"], rec["startup_status"]), ("A0", "NOT_STARTED_UNKNOWN"))

    @case("G15-03")
    def test_slice_not_new_start(self):
        from smallcap.evaluation import window_slice
        s = window_slice([("2020-12-31", Fraction(2)), ("2021-01-08", Fraction(21, 10))], "2021-01-08")
        self.assertEqual(s["kind"], "TIME_WINDOW_SLICE")
        self.assertFalse(s["is_new_capital_start"])
        m = StubMarket(n=40, start=date(2020, 1, 2), end=date(2021, 2, 26))
        r = make_runner(m, start_year=2021)
        r.run()
        self.assertEqual(r.ledger.ledger_points[0]["date"], "2021-01-08")
        self.assertEqual(r.ledger.ledger_points[0]["nav"], 1)

    @case("G15-04")
    def test_s2_ordinary_delay(self):
        m = StubMarket(n=40)
        r = make_runner(m, scenario="S2")
        seed_holdings(r, date(2024, 1, 5), {}, 100000)
        new_buy_plan(r, ["X0031.SH"])
        self.assertIsNone(r.ordinary_plan_for(D0))
        self.assertIsNotNone(r.ordinary_plan_for(date(2024, 1, 9)))
        exec_day(r, D0)
        self.assertEqual(r.ledger.orders, {})
        exec_day(r, date(2024, 1, 9))
        o = next(iter(r.ledger.orders.values()))
        self.assertEqual((o.security_id, o.trade_date), ("X0031.SH", "2024-01-09"))

    @case("G15-05")
    def test_s2_risk_delay_once(self):
        m = StubMarket(n=40)
        r = make_runner(m, scenario="S2")
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 200}, 98000)
        m.set_bar("X0001.SH", date(2024, 1, 9), status="SUSPENDED")
        ep = episode(r, "SECURITY_EXIT", "X0001.SH", ["X0001.SH"])
        self.assertEqual(r._effective_first(ep), date(2024, 1, 9))
        exec_day(r, D0)
        exec_day(r, date(2024, 1, 9))
        self.assertEqual(r.ledger.positions["X0001.SH"].quantity, 200)
        exec_day(r, date(2024, 1, 10))                      # 次日重计划不再滚动延迟
        self.assertEqual(r.ledger.positions.get("X0001.SH").quantity if "X0001.SH" in r.ledger.positions else 0, 0)

    def _s3(self, etype, key, members, holdings, industry_of=None):
        m = StubMarket(n=40, industry_of=industry_of)
        r = make_runner(m, scenario="S3")
        seed_holdings(r, date(2024, 1, 5), holdings, 100000 - 10 * sum(holdings.values()))
        ep = episode(r, etype, key, members, target=C.SINGLE_TARGET if etype == "SINGLE_REDUCE" else None)
        return m, r, ep

    @case("G15-06")
    def test_s3_single_d0_d2(self):
        m, r, ep = self._s3("SECURITY_EXIT", "X0001.SH", ["X0001.SH"], {"X0001.SH": 200})
        days = [D0, date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11)]
        self.assertEqual(list(stress_block_days(r.cal, D0)), days[:3])
        for d in days[:3]:
            exec_day(r, d)
            self.assertEqual(r.ledger.positions["X0001.SH"].quantity, 200)
        exec_day(r, days[3])
        self.assertNotIn("X0001.SH", {s for s, p in r.ledger.positions.items() if p.quantity})

    @case("G15-07")
    def test_s3_industry_blocks_ordinary(self):
        inds = {"X0001.SH": "I00", "X0002.SH": "I00", "X0003.SH": "I01"}
        m, r, ep = self._s3("INDUSTRY_REDUCE", "I00", ["X0001.SH", "X0002.SH"],
                            {"X0001.SH": 1100, "X0002.SH": 1100, "X0003.SH": 200}, inds)
        modes = {"X0002.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK"},
                 "X0003.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK"}}
        put_plan(r, date(2024, 1, 5), modes, [], 100000, D0)
        exec_day(r, D0)
        L = r.ledger
        self.assertEqual((L.positions["X0001.SH"].quantity, L.positions["X0002.SH"].quantity), (1100, 1100))
        self.assertNotIn("X0003.SH", {s for s, p in L.positions.items() if p.quantity})   # 范围外普通退出照常

    @case("G15-08", "G15-09")
    def test_s3_total_blocks_all_stock(self):
        m, r, ep = self._s3("EMERGENCY_EXIT", "ACCOUNT", ["X0001.SH", "X0002.SH"], {"X0001.SH": 200, "X0002.SH": 200})
        r.ledger.etf_ids = frozenset({"E510000.SH"})
        exec_day(r, D0)
        self.assertEqual(sorted(s for s, p in r.ledger.positions.items() if p.quantity), ["X0001.SH", "X0002.SH"])
        self.assertTrue(r.stress_blocked("X0039.SH", D0))          # 全部本股票策略证券
        self.assertEqual([b["scope"] for b in r.ledger.stress_blocks], ["ALL"])
        self.assertNotIn("E510000.SH", r.ledger.stress_blocks[0]["members"])   # 不自动冻结 ETF 证券

    @case("G15-10")
    def test_stress_drawdown(self):
        m = StubMarket(n=40)
        r = make_runner(m, scenario="S3")
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 5000}, 30000, mark="14")     # 峰值 100000
        m.set_bar("X0001.SH", D0, close="9")                                           # 75000 = 45000+30000
        close_day(r, D0)
        self.assertTrue(r.ledger.retirement_triggered)
        d1 = date(2024, 1, 9)
        m.set_bar("X0001.SH", d1, open="9", close="7.92", ref_preclose="9")            # 再跌 12%
        exec_day(r, d1)
        close_day(r, d1)
        pt = r.ledger.ledger_points[-1]
        self.assertEqual((pt["equity"], pt["dd"]), (Fraction(69600), Fraction(304, 1000)))
        self.assertEqual(r.ledger.cash, Decimal(30000))                                # 退出指令无现金回款

    @case("G15-11")
    def test_d3_still_blocked_by_market(self):
        m, r, ep = self._s3("SECURITY_EXIT", "X0001.SH", ["X0001.SH"], {"X0001.SH": 200})
        d3 = date(2024, 1, 11)
        m.set_bar("X0001.SH", d3, status="SUSPENDED")
        for d in (D0, date(2024, 1, 9), date(2024, 1, 10), d3):
            exec_day(r, d)
        self.assertEqual(r.ledger.positions["X0001.SH"].quantity, 200)

    @case("G15-12", "G15-13")
    def test_overlap_union_and_fixed_d0(self):
        m, r, ep1 = self._s3("SECURITY_EXIT", "X0001.SH", ["X0001.SH"], {"X0001.SH": 200, "X0002.SH": 200})
        ep2 = episode(r, "SECURITY_EXIT", "X0002.SH", ["X0002.SH"], first="2024-01-09")
        self.assertTrue(r.stress_blocked("X0001.SH", D0) and not r.stress_blocked("X0002.SH", D0))
        self.assertTrue(r.stress_blocked("X0001.SH", date(2024, 1, 10)) and r.stress_blocked("X0002.SH", date(2024, 1, 10)))
        self.assertFalse(r.stress_blocked("X0001.SH", date(2024, 1, 11)))
        self.assertTrue(r.stress_blocked("X0002.SH", date(2024, 1, 11)))
        close_day(r, D0)                       # 每日重算不重启三日
        self.assertEqual(len(r.ledger.stress_blocks), 2)
        self.assertEqual(r.ledger.stress_blocks[0]["days"][0], "2024-01-08")

    @case("G15-14")
    def test_satisfied_no_forced_sell(self):
        m, r, ep = self._s3("SINGLE_REDUCE", "X0001.SH", ["X0001.SH"], {"X0001.SH": 600})
        m.set_bar("X0001.SH", D0, open="3", close="3", ref_preclose="3")            # 自然满足
        exec_day(r, D0)
        self.assertFalse(r.ledger.episodes[ep["episode_id"]]["active"])
        self.assertEqual(r.ledger.orders, {})
        self.assertTrue(r.stress_blocked("X0001.SH", date(2024, 1, 9)))             # 剩余冲击仍适用

    @case("G15-15")
    def test_s2_s3_independent(self):
        b = load_bundle(copy.deepcopy(ENGINEERING_BUNDLE))
        from smallcap.matrix import matrix_specs
        specs = matrix_specs(b.study, test_package(), {"MAIN_SH"}, set(), set(), "p")
        self.assertEqual(sorted({s.scenario for s in specs}), ["S0", "S1", "S2", "S3"])
        m = StubMarket(n=40)
        r2, r3 = make_runner(m, scenario="S2"), make_runner(m, scenario="S3")
        self.assertEqual((r2.slip, r3.slip), (C.HIGH_SLIPPAGE, C.HIGH_SLIPPAGE))
        ep = new_episode("SECURITY_EXIT", "X", 1, "t", "2024-01-08", ["X"], None)
        self.assertEqual(r3._effective_first(ep), D0)            # S3 不叠加 S2 延迟
        self.assertEqual(r2._effective_first(ep), date(2024, 1, 9))

    @case("G15-16")
    def test_retired_continues_to_d(self):
        m = StubMarket(n=40, end=date(2024, 3, 29))
        r = make_runner(m)
        for d in r.cal.sessions_between(date(2024, 1, 15), date(2024, 3, 29)):
            for s in m.master:
                m.set_bar(s, d, close="5", open="5", ref_preclose="5")
        r.run()
        L = r.ledger
        self.assertTrue(L.retirement_triggered)
        self.assertEqual(L.ledger_points[-1]["date"], "2024-03-29")
        self.assertLess(L.retirement_trigger_at, "2024-01-20")

    @case("G15-17")
    def test_unknown_tail_insufficient(self):
        cells = [{"start": s, "scenario": sc, "startup_status": "STARTED", "retirement_triggered": False,
                  "MDD_exact": None if (s, sc) == ("A3", "S3") else {"num": "1", "den": "10"}, "mdd_over_30": False,
                  "CAGR_decimal": "0.1", "package_availability": "PASS", "evidence_reasons": [], "annual_fee_rate": "0"}
                 for s in ("A0", "A1", "A2", "A3") for sc in C.SCENARIOS]
        q = qualify_config(cells, test_package=False, external_verified={"x": True}, benchmark_specified=True)
        self.assertEqual(q["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("MDD_UNKNOWN:A3/S3", q["unknowns"])

    @case("G15-18")
    def test_96_cells_identity(self):
        b = load_bundle(copy.deepcopy(EXTERNAL_TEMPLATE))
        res = run_matrix(b, market=None)
        keys = [r["cell_key"] for r in res["records"]]
        self.assertEqual((len(keys), len(set(keys))), (96, 96))
        self.assertTrue(all(r["startup_status"] == "NOT_STARTED_UNKNOWN" for r in res["records"]))
        self.assertIn("END_DATE_NOT_FIXED", res["matrix_blocks"])


if __name__ == "__main__":
    unittest.main()
