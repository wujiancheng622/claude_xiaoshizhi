import copy
import json
import random
import shutil
import tempfile
import unittest
from datetime import date, time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from smallcap import contracts as C
from smallcap.account import Ledger
from smallcap.contracts import ContractError, sh_dt, wire_ratio
from smallcap.data import impact_scope, DecisionUsage, normalize, tushare_mapping
from smallcap.evaluation import cell_record, qualify_config
from smallcap.execution import BookView, size_buy
from smallcap.journal import Journal
from smallcap.pool import build_pool
from smallcap.portfolio import HoldingView, plan_week
from smallcap.replay import SimulatedCrash, resume_cell
from smallcap.risk import new_episode, open_order_cancellations
from smallcap.shared_cash import SharedCash
from smallcap.synthetic import SyntheticMarket
from tests.acceptance import case
from tests.fixtures import (EventFactory, StubMarket, buy_order, close_day, exec_day, fill, make_runner, new_ledger,
                            put_plan, seed_holdings, sell_order, stub_tax_rules, test_fees, test_rulebook)

D0 = date(2024, 1, 8)


def first_build():
    """G17-01：10万现金、30只同价10元、零滑点（TEST_ZERO_SLIP）、每单最低5元、每3只一行业。"""
    inds = {f"X{i:04d}.SH": f"I{(i - 1) // 3:02d}" for i in range(1, 41)}
    m = StubMarket(n=40, industry_of=inds)
    r = make_runner(m)
    r.slip = Decimal(0)                  # TEST_ZERO_SLIP：不替代 BASE/HIGH
    seed_holdings(r, date(2024, 1, 5), {}, 100000)
    sids = [f"X{i:04d}.SH" for i in range(1, 31)]
    modes = {s: {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)), "prohibit_buy": False,
                 "reason": "NEW_BUY"} for s in sids}
    put_plan(r, date(2024, 1, 5), modes, sids, 100000, D0)
    exec_day(r, D0)
    return m, r, sids


class G17(unittest.TestCase):
    @case("G17-01")
    def test_first_build(self):
        m, r, sids = first_build()
        L = r.ledger
        qty = [L.positions[s].quantity for s in sids]
        self.assertEqual(qty, [200] * 29 + [100])
        self.assertEqual((L.stock_value(), L.cash, L.equity()[0]), (Fraction(59000), Decimal("40850.00"), Fraction(99850)))
        self.assertEqual(sum(Decimal(t["fee"]) for t in L.trade_log), Decimal(150))
        self.assertEqual(len(L.trade_log), 30)

    @case("G17-02")
    def test_dividend_after_build(self):
        m, r, sids = first_build()
        L = r.ledger
        d1, d2 = date(2024, 1, 9), date(2024, 1, 10)
        m.actions.append({"action_id": "DIV-X0001", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND", "record_date": D0,
                          "ex_date": d1, "pay_date": d2, "cash_per_share_cents": 100})
        close_day(r, D0)
        m.set_bar("X0001.SH", d1, ref_preclose="9", open="9", close="9")
        for d in (d1,):
            r.phase_key, r.k, r._qc = f"{d}/PREOPEN", 0, {}
            r._phase_preopen(d)
        self.assertEqual((L.stock_value(), L.receivables["DIV:DIV-X0001"], L.cash, L.equity()[0]),
                         (Fraction(58800), Decimal(200), Decimal("40850.00"), Fraction(99850)))
        r.phase_key, r.k, r._qc = f"{d2}/PREOPEN", 0, {}
        r._phase_preopen(d2)
        self.assertEqual((L.cash, L.receivables.get("DIV:DIV-X0001"), L.equity()[0]),
                         (Decimal("41050.00"), None, Fraction(99850)))

    @case("G17-03")
    def test_next_week_rounding_no_order(self):
        m, r, sids = first_build()
        L = r.ledger
        close_day(r, D0)
        pool = {"security_ids": sids, "size_order": {s: i + 1 for i, s in enumerate(sids)}, "size": 30, "health": "OK",
                "codes": {s: s for s in sids}, "industries": {s: m.master[s]["industry"] for s in sids},
                "eligibility": {s: "TRUE" for s in sids}}
        score = {"healthy": True, "flat": False, "score_fraction": {s: Fraction(30 - i) for i, s in enumerate(sids)},
                 "ranks": {s: i + 1 for i, s in enumerate(sids)}, "order": sids, "invalid_reasons": {}, "reason": None,
                 "package_id": "TEST_ONLY_RANK", "coverage": Fraction(1)}
        e = L.equity()[0]
        self.assertEqual(e, Fraction(99850))
        holds = [HoldingView(s, L.positions[s].quantity, L.position_value(s), L.positions[s].industry) for s in sids]
        plan, _ = plan_week(role="B1", plan_id="w2", cutoff="c", e_decision=e, holdings=holds, open_buy_sids=set(),
                            pool=pool, score=score, counters={}, capital_revision=L.capital_revision,
                            execute_session="2024-01-15")
        self.assertEqual(C.unwire_ratio(plan["modes"]["X0030.SH"]["target_amount"]), Fraction(1997))
        r.phase_key, r.k = "plan2", 0
        r.emit("PLAN", {"plan": dict(plan, industries={})}, sh_dt(date(2024, 1, 12), time(20)))
        exec_day(r, date(2024, 1, 15))
        self.assertEqual(L.positions["X0030.SH"].quantity, 100)
        self.assertIn({"kind": "BUY_SKIPPED", "security_id": "X0030.SH", "reason": ["ROUNDING_NO_ORDER"]}, L.decision_log)

    @case("G17-04")
    def test_arrival_order_equals_chronological(self):
        def run(order):
            lg, ev = new_ledger()
            lg.apply(ev("ORDER_CREATE", buy_order("b", qty=300)))
            lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
            facts = {"f1": ("BROKER_FILL", fill("b", "t1", 100, "10")),
                     "cx": ("CANCEL_REQUEST", {"client_order_id": "b", "request_id": "c", "reason": "RISK"}),
                     "f2": ("BROKER_FILL", fill("b", "t2", 200, "10", at="2026-01-05T10:00:50+08:00",
                                                received="2026-01-05T10:02:00+08:00")),
                     "term": ("BROKER_TERMINAL", {"client_order_id": "b", "status": "CANCELED", "cum_qty": 300,
                                                  "cum_value": "3000", "source_revision": 2}),
                     "f2dup": ("BROKER_FILL", fill("b", "t2", 200, "10", at="2026-01-05T10:00:50+08:00",
                                                   received="2026-01-05T10:02:00+08:00"))}
            for k in order:
                kind, p = facts[k]
                lg.apply(ev(kind, p, eid=f"x/{k}"))
            o = lg.orders["b"]
            return (o.status, o.filled_qty, lg.cash, lg.positions["X0001.SH"].quantity, dict(o.fees_accrued))
        chrono = run(["f1", "cx", "f2", "term"])
        arrival = run(["f1", "cx", "term", "f2", "f2dup"])
        self.assertEqual(chrono, arrival)
        self.assertEqual(chrono[0], "FILLED")

    @case("G17-05")
    def test_two_strategy_chain(self):
        sc = SharedCash(Decimal(100000), Decimal(100000))
        rev = sc.broker_snapshot(Decimal(200000))
        stock = Ledger("STOCK", test_fees())
        ev = EventFactory("s")
        stock.apply(ev("CAPITAL_FLOW", {"flow_id": "init", "amount": "100000"}))
        self.assertTrue(sc.authorize("STOCK", "b1", Decimal(3010), rev).ok)
        stock.apply(ev("ORDER_CREATE", buy_order("o1", qty=300, capital_revision=stock.capital_revision)))
        sc.preapprove("c1", "STOCK", stock.buy_cash_reserved(stock.orders["o1"]), "o1")
        stock.apply(ev("ORDER_SUBMIT", {"client_order_id": "o1", "at": "x"}))
        sc.broker_frozen("c1", Decimal(200000) - Decimal("3005.00"))
        stock.apply(ev("BROKER_FILL", fill("o1", "t1", 100, "10")))
        sc.fill("c1", Decimal("1005.00"))
        stock.apply(ev("CANCEL_REQUEST", {"client_order_id": "o1", "request_id": "c", "reason": "TIMEOUT"}))
        self.assertEqual(sc.commitments["c1"]["state"], "BROKER_FROZEN")          # 撤单请求不释放
        stock.apply(ev("BROKER_TERMINAL", {"client_order_id": "o1", "status": "CANCELED", "cum_qty": 100, "cum_value": "1000",
                                           "source_revision": 1}))
        sc.cancel_confirmed("c1")
        self.assertTrue(sc.end_batch().ok)
        self.assertEqual(sc.C["STOCK"], stock.cash)
        tr = sc.transfer("T1", "ETF", "STOCK", Decimal(10000))
        stock.apply(ev("MARKS", {"marks": {"X0001.SH": {"price": "10", "mark_date": "x", "status": "NORMAL"}}}))
        stock.apply({"event_id": "T1", "kind": "CAPITAL_FLOW", "payload": {"flow_id": "T1", "transfer_id": "T1",
                                                                           "amount": tr.value["flows"]["STOCK"]}})
        self.assertEqual(sc.C["ETF"] + sc.C["STOCK"] + Decimal("1005.00"), Decimal(200000))     # 共同现金守恒
        self.assertEqual(sc.C["STOCK"], stock.cash)
        stock.apply(ev("ORDER_CREATE", buy_order("o2", sid="X0002.SH", qty=100, capital_revision=1)))
        self.assertEqual(open_order_cancellations(stock, None, plan_capital_revision=1), [("o2", "CAPITAL_CHANGED")])

    def _synthetic_run(self, end, n=90):
        m = SyntheticMarket(seed=3, n=n, start=date(2022, 1, 3), end=end, with_events=False)
        r = make_runner(m, end=end, capital="500000")       # 10万在合成价位下首次适配不通过（规则正确结果）
        r.event_log = []
        r.run()
        return r

    @case("G17-06")
    def test_future_append_prefix(self):
        short = self._synthetic_run(date(2024, 3, 29))
        long = self._synthetic_run(date(2024, 6, 28))
        self.assertEqual(short.startup["status"], "STARTED")
        cut = "2024-03-29T20:00:00+08:00"
        # RUN_BINDING 记录研究身份（含共同终点D，两次运行D不同），不是决策/成交/账本事实；其余前缀须逐条一致
        a = [(e["event_id"], e["kind"], e["payload"]) for e in short.event_log if e["kind"] != "RUN_BINDING"]
        b = [(e["event_id"], e["kind"], e["payload"]) for e in long.event_log
             if e["occurred_at"] <= cut and e["kind"] != "RUN_BINDING"]
        self.assertGreater(len(a), 200)
        self.assertEqual(a, b)

    @case("G17-07")
    def test_shuffle_and_duplicates(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        facts = [r.security_facts(s, D0, sh_dt(D0, time(20))) for s in m.universe()]
        base = build_pool("d", D0, facts, r.cal, r.rb, {"MAIN_SH"}, set())
        rng = random.Random(5)
        for _ in range(5):
            f2 = facts + facts[:5]
            rng.shuffle(f2)
            dedup = {f.security_id: f for f in f2}.values()
            self.assertEqual(build_pool("d", D0, list(dedup), r.cal, r.rb, {"MAIN_SH"}, set())["security_ids"],
                             base["security_ids"])
        lg, ev = new_ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=200)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "10")))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "10")))
        self.assertEqual(lg.positions["X0001.SH"].quantity, 100)
        lg.apply(ev("BROKER_FILL", fill("b", "t", 150, "10")))
        self.assertEqual(lg.orders["b"].status, "RECONCILE_REQUIRED")

    @case("G17-08")
    def test_crash_after_every_event(self):
        m = StubMarket(n=35, end=date(2024, 1, 12))
        ref = make_runner(m)
        with tempfile.TemporaryDirectory() as d0:
            jr = Journal(d0)
            jr.acquire()
            ref.journal = jr
            ref.run()
            jr.release()
            total = jr.last_seq
            ref_snap = ref.ledger.snapshot()
        self.assertGreater(total, 100)
        for k in range(1, total + 1):
            with tempfile.TemporaryDirectory() as d:
                j = Journal(d)
                j.acquire()
                r = make_runner(m, journal=j, crash_after=k)
                with self.assertRaises(SimulatedCrash):
                    r.run()
                j.release()
                j2 = Journal(d)
                j2.acquire()
                r2 = resume_cell(r.spec, m, test_rulebook(), test_fees(tax_rules=stub_tax_rules()), j2)
                j2.release()
                self.assertEqual(json.dumps(r2.ledger.snapshot(), sort_keys=True, default=str),
                                 json.dumps(ref_snap, sort_keys=True, default=str), f"crash after seq {k}")

    @case("G17-09")
    def test_unit_scaling_invariant(self):
        rows_k = [{"record_id": str(i), "security_id": f"S{i}", "period": "2024-01-08", "amount": str(1000 * i)}
                  for i in range(1, 6)]
        rows_y = [dict(r, amount=str(1000 * 1000 * int(r["security_id"][1:]))) for r in rows_k]
        mk = tushare_mapping("daily", "amount")
        my = dict(mk, source_unit="CNY", multiply="1", canonical_unit="amount_cny_raw")
        a = [normalize(r, mk).value for r in rows_k]
        b = [normalize(r, my).value for r in rows_y]
        self.assertEqual(a, b)

    @case("G17-10")
    def test_risk_sells_ignore_scores(self):
        res = []
        for rank_order in (list(range(1, 11)), list(range(10, 0, -1))):
            m = StubMarket(n=40)
            r = make_runner(m)
            held = {f"X{i:04d}.SH": 700 for i in range(1, 11)}
            seed_holdings(r, date(2024, 1, 5), held, 30000)         # 股票 70% → 总暴露纠偏
            episode = new_episode("TOTAL_REDUCE", "ACCOUNT", 1, "t", "2024-01-08", list(held), C.STOCK_TARGET)
            r.phase_key, r.k = "ep", 0
            r.emit("RISK_EPISODE_START", {"episode": episode}, sh_dt(date(2024, 1, 5), time(15, 30)))
            modes = {s: {"mode": "HOLD_SHARES", "target_amount": None, "prohibit_buy": False, "reason": "IN_BAND",
                         "rank": rank_order[i]} for i, s in enumerate(held)}
            put_plan(r, date(2024, 1, 5), modes, [], 100000, D0)
            exec_day(r, D0)
            res.append(sorted((o.security_id, o.qty) for o in r.ledger.orders.values()))
        self.assertEqual(res[0], res[1])
        self.assertTrue(res[0])

    @case("G17-11")
    def test_blocked_exit_losses_kept(self):
        m = StubMarket(n=40)
        r = make_runner(m, scenario="S3")
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 1000, "X0002.SH": 1000}, 80000)
        ep = new_episode("EMERGENCY_EXIT", "ACCOUNT", 1, "t", "2024-01-08", ["X0001.SH", "X0002.SH"], None)
        r.phase_key, r.k = "ep", 0
        r.emit("RETIREMENT", {"at": "2024-01-05T15:30:00+08:00"}, sh_dt(date(2024, 1, 5), time(15, 30)))
        r.emit("RUN_STATE", {"to": "EXITING", "reason": "t"}, sh_dt(date(2024, 1, 5), time(15, 30)))
        r.emit("RISK_EPISODE_START", {"episode": ep}, sh_dt(date(2024, 1, 5), time(15, 30)))
        r._stress_for(ep, sh_dt(date(2024, 1, 5), time(15, 30)))
        m.actions.append({"action_id": "D", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND", "record_date": D0,
                          "ex_date": date(2024, 1, 9), "pay_date": date(2024, 1, 10), "cash_per_share_cents": 50})
        for i, d in enumerate((D0, date(2024, 1, 9), date(2024, 1, 10))):
            for s in ("X0001.SH", "X0002.SH"):
                px = str(Decimal(9) - i)
                m.set_bar(s, d, open=px, close=px, ref_preclose=str(Decimal(10) - i if i == 0 else Decimal(9) - i + 1))
            r.phase_key, r.k, r._qc = f"{d}/PREOPEN", 0, {}
            r._phase_preopen(d)
            exec_day(r, d)
            close_day(r, d)
        L = r.ledger
        self.assertEqual(L.orders, {})                               # 三日阻断：无卖出、无回款
        self.assertEqual(L.cash, Decimal(80000) + Decimal(500))       # 只有真实到账分红
        self.assertEqual(L.ledger_points[-1]["equity"], Fraction(80500) + 2000 * 7)

    @case("G17-12")
    def test_correction_replays_from_earliest(self):
        def market(corrected):
            m = StubMarket(n=40, end=date(2024, 1, 26))
            if corrected:
                m.fvals_by_date[("X0035.SH", date(2024, 1, 12))] = Decimal(5000)     # 修正：第二周分数
            return m
        tmp = Path(tempfile.mkdtemp())
        try:
            jA = Journal(tmp / "A")
            jA.acquire()
            rA = make_runner(market(False), journal=jA, role="B2")     # B2：30名边界，使修正改变持仓
            rA.run()
            jA.release()
            usages = [DecisionUsage(e["payload"]["plan"]["plan_id"], C.parse_ts(e["occurred_at"]),
                                    frozenset({("factor", s, e["payload"]["plan"]["plan_id"]) for s in ["X0035.SH"]}))
                      for e in jA.read_committed() if e["kind"] == "PLAN"]
            imp = impact_scope([("factor", "X0035.SH", "plan-2024-01-12")], usages)
            self.assertEqual(imp.value["replay_from"], "plan-2024-01-12")
            evs = jA.read_committed()
            cut = max(i for i, e in enumerate(evs) if e["kind"] == "CURSOR" and e["payload"]["date"] == "2024-01-12"
                      and e["payload"]["phase"] == "CLOSE")
            jB = Journal(tmp / "B")
            jB.acquire()
            for e in evs[:cut + 1]:
                jB.commit({k: e[k] for k in ("event_id", "kind", "occurred_at", "known_at", "payload")})
            rB = resume_cell(rA.spec, market(True), test_rulebook(), test_fees(tax_rules=stub_tax_rules()), jB)
            jB.release()
            rC = make_runner(market(True), role="B2")
            rC.run()
            self.assertEqual(json.dumps(rB.ledger.snapshot()["state"]["positions"], sort_keys=True),
                             json.dumps(rC.ledger.snapshot()["state"]["positions"], sort_keys=True))
            self.assertEqual(rB.ledger.cash, rC.ledger.cash)
            self.assertNotEqual(rA.ledger.snapshot()["state"]["positions"].keys(), set())
            buys = lambda r: {(t["sid"], t["date"]) for t in r.ledger.trade_log if t["side"] == "BUY"}
            self.assertIn(("X0035.SH", "2024-01-15"), buys(rC))           # 修正改变了第二周起的路径
            self.assertNotIn("X0035.SH", {sid for sid, _ in buys(rA)})
            self.assertEqual(rB.ledger.trade_log, rC.ledger.trade_log)     # 从最早受影响决策前重放 = 全量更正重放
        finally:
            shutil.rmtree(tmp)

    @case("G17-14")
    def test_retire_then_cash_metrics(self):
        m = StubMarket(n=40, end=date(2024, 6, 28))
        for d in m.calendar.sessions_between(date(2024, 1, 15), date(2024, 6, 28)):
            for s in m.master:
                m.set_bar(s, d, close="5", open="5", ref_preclose="5")
        r = make_runner(m)
        r.run()
        rec = cell_record(r.spec, r, end_d=m.data_end)
        self.assertTrue(rec["retirement_triggered"])
        self.assertIn("RETIRED_TRIGGERED", rec["qualification_reasons"])
        self.assertEqual(rec["end_D"], "2024-06-28")
        self.assertIsNotNone(rec["CAGR_decimal"])
        self.assertGreater(rec["assessed_week_count"], 20)              # 停用后周度可用性统计仍完整
        self.assertEqual(rec["run_state_end"], "RETIRED")

    @case("G17-15")
    def test_no_fake_low_risk_winner(self):
        m = StubMarket(n=40, end=date(2024, 2, 2))
        m.fvals = {s: Decimal(7) for s in m.master}                     # 因子恒定 → FLAT_SCORE → 无买入
        r = make_runner(m)
        r.run()
        self.assertEqual(r.startup["status"], "NOT_STARTED_UNFIT")
        rec = cell_record(r.spec, r, end_d=m.data_end)
        cells = [dict(rec, start=s, scenario=sc) for s in ("A0", "A1", "A2", "A3") for sc in C.SCENARIOS]
        q = qualify_config(cells, test_package=False, external_verified={"x": True}, benchmark_specified=True)
        self.assertEqual(q["status"], "REJECTED")

    @case("G17-16")
    def test_reservation_then_price_and_fee_change(self):
        lg, ev = new_ledger(cash="10000")
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=300, limit="10")))
        free0 = lg.own_free_cash("2026-01-05")
        self.assertEqual(free0, Decimal(10000) - Decimal(3005))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 300, "10")))
        lg.apply(ev("FEE_SETTLEMENT", {"client_order_id": "b", "assessed_fee": "6.00", "already_accrued": "5.00",
                                       "already_paid": "5.00", "settlement_cash_change": "1.00"}))
        free1 = lg.own_free_cash("2026-01-05")
        self.assertEqual(free1, Decimal(10000) - Decimal(3006))
        v = BookView(lg.equity({"X0001.SH": Decimal(12)})[0], Fraction(3600), {"X0001.SH": Fraction(3600)},
                     {"I00": Fraction(3600)}, {"X0001.SH": "I00"}, Fraction(0), {}, free1)
        b, why = size_buy(v, sid="X0002.SH", industry="I01", grid=test_rulebook().rules[0].grid, p=Decimal(12),
                          m=Decimal(12), gap_value=Fraction(6994), fee_fn=lambda x: Decimal(5),
                          capacity_left_value=Decimal("1e9"))
        self.assertEqual(b, 0)                     # 价格上涨与费用修订后，旧预览的 500 股不再放行
        self.assertIn("SINGLE_5", why)


if __name__ == "__main__":
    unittest.main()
