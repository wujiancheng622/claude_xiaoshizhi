import unittest
from datetime import date
from decimal import Decimal
from fractions import Fraction

from smallcap import contracts as C
from smallcap.contracts import wire_ratio
from smallcap.execution import (BookView, ordinary_sell_qty, size_buy, startup_fit, startup_pass)
from smallcap.portfolio import HoldingView, plan_week
from smallcap.risk import SellLeg, SellPlanner
from smallcap.rules import QuantityGrid
from tests.acceptance import case
from tests.fixtures import (EventFactory, StubMarket, buy_order, close_day, exec_day, fill, golden, make_runner,
                            new_ledger, put_plan, seed_holdings, sell_order, test_fees, test_rulebook)

MAIN = QuantityGrid(100, 100, 100, 100, True, 1_000_000)
STAR = QuantityGrid(200, 1, 200, 1, True, 1_000_000)
D0 = date(2024, 1, 8)


def pool(n, industries=None):
    ids = [f"X{i + 1:04d}.SH" for i in range(n)]
    return {"security_ids": ids, "size_order": {s: i + 1 for i, s in enumerate(ids)}, "size": n, "health": "OK",
            "codes": {s: s for s in ids}, "industries": industries or {s: f"I{i % 10:02d}" for i, s in enumerate(ids)},
            "eligibility": {s: "TRUE" for s in ids}}


def score_for(p, order=None):
    order = order or list(p["security_ids"])
    return {"healthy": True, "flat": False, "score_fraction": {s: Fraction(len(order) - i) for i, s in enumerate(order)},
            "ranks": {s: i + 1 for i, s in enumerate(order)}, "order": order, "invalid_reasons": {}, "reason": None,
            "package_id": "TEST_ONLY_RANK", "coverage": Fraction(1)}


def plan(role="B1", holdings=(), p=None, sc=None, e=100000, open_buys=()):
    p = p or pool(400)
    out, _ = plan_week(role=role, plan_id="p", cutoff="c", e_decision=Fraction(e), holdings=list(holdings),
                       open_buy_sids=set(open_buys), pool=p, score=sc or score_for(p), counters={},
                       capital_revision=1, execute_session="2024-01-08")
    return out


def hv(sid, value, ind="I00", **kw):
    return HoldingView(sid, int(value / 10), Fraction(value), ind, **kw)


def view(e="100000", stock="0", cash="100000", holdings=None, ind=None):
    return BookView(Fraction(Decimal(e)), Fraction(Decimal(stock)), holdings or {}, ind or {}, {}, Fraction(0), {},
                    Decimal(cash))


def fee5(x, p=Decimal(10)):
    return C.cents(max(Decimal("0.00025") * x * p, Decimal(5)))


class G06(unittest.TestCase):
    @case("G06-01")
    def test_first_plan_top30_industry_skip(self):
        inds = {f"X{i + 1:04d}.SH": ("I00" if i < 12 else f"I{(i % 9) + 1:02d}") for i in range(400)}
        out = plan(p=pool(400, inds))
        self.assertEqual(len(out["buy_candidates_ordered"]), 30)
        self.assertEqual(out["buy_candidates_ordered"][:10], [f"X{i + 1:04d}.SH" for i in range(10)])
        self.assertNotIn("X0011.SH", out["buy_candidates_ordered"])       # 第11只I00超过20%在信号时跳过
        self.assertIn({"security_id": "X0011.SH", "reason": "INDUSTRY_LIMIT"}, out["skipped"])

    @case("G06-02")
    def test_b1_retain_45(self):
        p = pool(400)
        order = [s for s in p["security_ids"]]
        held = [hv(order[29], 2000), hv(order[44], 2000), hv(order[45], 2000)]
        out = plan(holdings=held, p=p)
        self.assertEqual([out["modes"][h.security_id]["mode"] for h in held], ["HOLD_SHARES", "HOLD_SHARES", "EXIT"])

    @case("G06-03")
    def test_b2_retain_30(self):
        p = pool(400)
        held = [hv(p["security_ids"][30], 2000)]
        self.assertEqual(plan(role="B2", holdings=held, p=p)["modes"][held[0].security_id]["mode"], "EXIT")
        self.assertEqual(plan(role="B1", holdings=held, p=p)["modes"][held[0].security_id]["mode"], "HOLD_SHARES")

    @case("G06-04")
    def test_off_pool_exit_despite_score(self):
        p = pool(400)
        out = plan(holdings=[hv("Y0401.SH", 2000)], p=p)
        self.assertEqual(out["modes"]["Y0401.SH"], {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True,
                                                     "reason": "OFF_POOL"})

    @case("G06-05")
    def test_frozen_exit_occupies_slot(self):
        p = pool(400)
        held = [hv(s, 2000) for s in p["security_ids"][:29]] + [hv("Y0999.SH", 2000, frozen=True)]
        out = plan(holdings=held, p=p)
        self.assertEqual(out["modes"]["Y0999.SH"]["mode"], "EXIT")
        self.assertEqual(out["free_slots_planned"], 0)
        self.assertEqual(out["buy_candidates_ordered"], [])

    @case("G06-06")
    def test_failed_sell_recounts_actual_slots(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        held = {f"X{i:04d}.SH": 200 for i in range(1, 31)}
        seed_holdings(r, date(2024, 1, 5), held, 40000)
        modes = {s: {"mode": "HOLD_SHARES", "target_amount": None, "prohibit_buy": False, "reason": "IN_BAND"} for s in held}
        modes["X0030.SH"] = {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK_46_GT_45"}
        modes["X0031.SH"] = {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)), "prohibit_buy": False,
                             "reason": "NEW_BUY"}
        put_plan(r, date(2024, 1, 5), modes, ["X0031.SH"], 100000, D0)
        m.set_bar("X0030.SH", D0, open="9.00", close="9.00", ref_preclose="10")      # 开盘跌停卖不出
        exec_day(r, D0)
        L = r.ledger
        self.assertEqual(L.positions["X0030.SH"].quantity, 200)
        self.assertNotIn("X0031.SH", {o.security_id for o in L.orders.values()})
        self.assertIn({"kind": "BUY_SKIPPED", "security_id": "X0031.SH", "reason": "SLOT_BLOCKED"}, L.decision_log)

    @case("G06-07")
    def test_one_share_left_occupies_slot(self):
        lg, ev = new_ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=100)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "10", sellable_from="2026-01-05")))
        lg.apply(ev("ORDER_CREATE", sell_order("s", qty=100, trade_date="2026-01-06")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "s", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("s", "t2", 99, "10", trade_date="2026-01-06")))
        self.assertIn("X0001.SH", lg.actual_slots())
        lg.apply(ev("BROKER_FILL", fill("s", "t3", 1, "10", trade_date="2026-01-06")))
        lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "s", "status": "FILLED", "cum_qty": 100, "cum_value": "1000",
                                        "source_revision": 1}))
        self.assertNotIn("X0001.SH", lg.actual_slots())

    @case("G06-08")
    def test_in_transit_buy_counted_once(self):
        lg, ev = new_ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=300)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 100, "10")))
        self.assertEqual(lg.actual_slots(), {"X0001.SH"})

    @case("G06-09")
    def test_each_still_2pct(self):
        p = pool(10)
        out = plan(p=p, sc=score_for(p))
        self.assertEqual(len(out["buy_candidates_ordered"]), 10)
        self.assertEqual({out["modes"][s]["target_amount"]["num"] for s in out["buy_candidates_ordered"]}, {"2000"})

    @case("G06-10")
    def test_fixed_target_not_scaled(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        held = {f"X{i:04d}.SH": 200 for i in range(1, 21)}
        seed_holdings(r, date(2024, 1, 5), held, 60000)          # E=100000 at cutoff
        put_plan(r, date(2024, 1, 5), {"X0031.SH": {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)),
                                                     "prohibit_buy": False, "reason": "NEW_BUY"}}, ["X0031.SH"], 100000, D0)
        for s in held:                                             # 执行日净资产上涨到 110000
            m.set_bar(s, D0, open="12.50", close="12.50")
        m.set_bar("X0031.SH", D0, open="10.50", close="10.50")
        exec_day(r, D0)
        o = [o for o in r.ledger.orders.values() if o.security_id == "X0031.SH"][0]
        self.assertEqual(o.qty, 100)        # 2000/10.5 → 100；若按 2200 会买 200

    @case("G06-11")
    def test_band_edges(self):
        p = pool(400)
        sids = p["security_ids"]
        vals = {sids[0]: "1500", sids[1]: "2500", sids[2]: "1499.99", sids[3]: "2500.01"}
        held = [HoldingView(s, 100, Fraction(Decimal(v)), "I00") for s, v in vals.items()]
        out = plan(holdings=held, p=p)
        self.assertEqual([out["modes"][s]["mode"] for s in vals],
                         ["HOLD_SHARES", "HOLD_SHARES", "TARGET_AMOUNT", "TARGET_AMOUNT"])

    @case("G06-12")
    def test_target_amount_direction_follows_price(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        # 截止：X0001 700股×2=1400（1.4%）→ TARGET_AMOUNT 原为补买；X0002 200股×10（2%）→ HOLD_SHARES
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 700, "X0002.SH": 200}, 96600, mark="2")
        t2000 = wire_ratio(Fraction(2000))
        put_plan(r, date(2024, 1, 5), {"X0001.SH": {"mode": "TARGET_AMOUNT", "target_amount": t2000, "prohibit_buy": False,
                                                     "reason": "OUT_OF_BAND"},
                                        "X0002.SH": {"mode": "HOLD_SHARES", "target_amount": None, "prohibit_buy": False,
                                                     "reason": "IN_BAND"}}, [], 100000, D0)
        m.set_bar("X0001.SH", D0, open="5", close="5", ref_preclose="5")      # 隔夜大涨：3500>2000 → 由补买变减持
        m.set_bar("X0002.SH", D0, open="5", close="5", ref_preclose="5")      # HOLD_SHARES 跌价不自动补仓
        exec_day(r, D0)
        sides = {(o.security_id, o.side, o.qty) for o in r.ledger.orders.values()}
        self.assertEqual(sides, {("X0001.SH", "SELL", 300)})                 # (3500−2000)/5=300

    @case("G06-13")
    def test_no_buyback_after_risk_sell(self):
        m = StubMarket(n=40)
        r = make_runner(m, mode="REPLAY_MINUTE_WINDOW")
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 600, "X0002.SH": 200}, 92000)
        from smallcap.risk import new_episode
        r.phase_key, r.k = "ep", 0
        ep = new_episode("SINGLE_REDUCE", "X0001.SH", 1, "2024-01-05T15:30:00+08:00", "2024-01-08", ["X0001.SH"],
                         C.SINGLE_TARGET)
        r.emit("RISK_EPISODE_START", {"episode": ep}, r.cal.cutoff(date(2024, 1, 5)))
        put_plan(r, date(2024, 1, 5), {"X0001.SH": {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)),
                                                     "prohibit_buy": False, "reason": "OUT_OF_BAND"}}, [], 100000, D0)
        exec_day(r, D0)
        L = r.ledger
        sells = [o for o in L.orders.values() if o.side == "SELL"]
        buys = [o for o in L.orders.values() if o.side == "BUY"]
        self.assertTrue(sells and L.positions["X0001.SH"].quantity < 600)
        self.assertEqual(buys, [])

    @case("G06-14")
    def test_capital_change_invalidates_buys(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        held = {f"X{i:04d}.SH": 200 for i in range(1, 30)}
        seed_holdings(r, date(2024, 1, 5), held, 42000)
        modes = {"X0029.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK"},
                 "X0031.SH": {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)), "prohibit_buy": False,
                              "reason": "NEW_BUY"}}
        put_plan(r, date(2024, 1, 5), modes, ["X0031.SH"], 100000, D0)
        from datetime import time
        from smallcap.contracts import sh_dt
        r.phase_key, r.k = "flow", 0
        r.emit("CAPITAL_FLOW", {"flow_id": "in1", "amount": "50000"}, sh_dt(D0, time(8)))   # 信号后转入资金
        exec_day(r, D0)
        L = r.ledger
        self.assertEqual(L.positions.get("X0029.SH") and L.positions["X0029.SH"].quantity, 0)   # 原 EXIT 继续
        self.assertNotIn("X0031.SH", {o.security_id for o in L.orders.values()})
        self.assertTrue(any(d.get("global_buy_blocks") == ["CAPITAL_CHANGED"] for d in L.decision_log))

    @case("G06-15")
    def test_failed_industry_exit_counts(self):
        inds = {f"X{i:04d}.SH": "I00" for i in range(1, 11)}
        inds["X0031.SH"] = "I00"
        m = StubMarket(n=40, industry_of=inds)
        r = make_runner(m)
        held = {f"X{i:04d}.SH": 200 for i in range(1, 11)}
        seed_holdings(r, date(2024, 1, 5), held, 80000)
        modes = {"X0010.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK"},
                 "X0031.SH": {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)), "prohibit_buy": False,
                              "reason": "NEW_BUY"}}
        put_plan(r, date(2024, 1, 5), modes, ["X0031.SH"], 100000, D0)
        m.set_bar("X0010.SH", D0, open="9.00", close="9.00", ref_preclose="10")
        exec_day(r, D0)
        L = r.ledger
        self.assertNotIn("X0031.SH", {o.security_id for o in L.orders.values()})
        self.assertIn({"kind": "BUY_SKIPPED", "security_id": "X0031.SH", "reason": ["INDUSTRY_20"]}, L.decision_log)

    @case("G06-16")
    def test_insufficient_cash_keeps_cash(self):
        m = StubMarket(n=40)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {}, 100000)
        r.phase_key, r.k = "restr", 0
        from smallcap.contracts import sh_dt
        from datetime import time
        r.emit("CASH_RESTRICTION", {"restriction_id": "R1", "amount": "95000", "until": None,
                                    "reason": "BROKER_CONFIRMED_HOLD"}, sh_dt(D0, time(8)))
        buys = [f"X{i:04d}.SH" for i in range(1, 6)]
        modes = {s: {"mode": "TARGET_AMOUNT", "target_amount": wire_ratio(Fraction(2000)), "prohibit_buy": False,
                     "reason": "NEW_BUY"} for s in buys}
        put_plan(r, date(2024, 1, 5), modes, buys, 100000, D0)
        exec_day(r, D0)
        bought = sorted(o.security_id for o in r.ledger.orders.values())
        self.assertEqual(bought, ["X0001.SH", "X0002.SH"])       # 5000 可用 → 两只 2005 → 第三只不足
        self.assertTrue(set(bought) <= set(buys))
        self.assertIn({"kind": "BUY_SKIPPED", "security_id": "X0003.SH", "reason": ["INSUFFICIENT_CASH"]},
                      r.ledger.decision_log)


class G07(unittest.TestCase):
    def _size(self, grid, gap, m="10", cash="100000"):
        v = view(cash=cash)
        return size_buy(v, sid="A", industry="I", grid=grid, p=Decimal(m), m=Decimal(m), gap_value=Fraction(gap),
                        fee_fn=lambda x: fee5(x, Decimal(m)), capacity_left_value=Decimal("1e9"))

    @case("G07-01")
    def test_main_board_step(self):
        self.assertEqual(self._size(MAIN, 5000 - 2600)[0], 200)

    @case("G07-02")
    def test_star_240(self):
        self.assertEqual(self._size(STAR, 2400)[0], 240)

    @case("G07-03")
    def test_star_199_no_order(self):
        self.assertEqual(self._size(STAR, 1990), (0, ["ROUNDING_NO_ORDER"]))

    @case("G07-04")
    def test_full_odd_exit_and_partial_grid(self):
        self.assertEqual(MAIN.max_sell(260, 260, 260), 260)
        self.assertEqual(MAIN.max_sell(160, 260, 260), 100)
        self.assertEqual(ordinary_sell_qty(MAIN, 260, Decimal(10), Fraction(0), 260), 260)
        self.assertEqual(ordinary_sell_qty(MAIN, 260, Decimal(10), Fraction(600), 260), 200)

    @case("G07-05")
    def test_bought_today_not_sellable(self):
        lg, ev = new_ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=260)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 260, "10", sellable_from="2026-01-06")))
        self.assertEqual(lg.positions["X0001.SH"].quantity, 260)
        self.assertEqual(lg.free_sell("X0001.SH", "2026-01-05"), 0)
        self.assertEqual(lg.free_sell("X0001.SH", "2026-01-06"), 260)

    def _held_1000(self):
        lg, ev = new_ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=1000)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t", 1000, "10", sellable_from="2026-01-05")))
        lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "b", "status": "FILLED", "cum_qty": 1000, "cum_value": "10000",
                                        "source_revision": 1}))
        # 300 股冻结：本地已映射，且已体现在券商净可卖 700 中
        lg.apply(ev("SHARE_FREEZE", {"security_id": "X0001.SH", "qty": 300, "reflected_in_broker_can_sell": True}))
        lg.apply(ev("BROKER_POSITION_VIEW", {"can_sell": {"X0001.SH": 700}}))
        return lg, ev

    @case("G07-06")
    def test_free_sell_no_double_freeze(self):
        lg, _ = self._held_1000()
        self.assertEqual(lg.free_sell("X0001.SH", "2026-01-06"), 700)       # 不再减成 400

    @case("G07-07")
    def test_free_sell_preapproved(self):
        lg, ev = self._held_1000()
        lg.apply(ev("ORDER_CREATE", sell_order("pre", qty=100, trade_date="2026-01-06")))   # 已批准、尚未报出
        self.assertEqual(lg.orders["pre"].status, "RESERVED")
        self.assertEqual(lg.free_sell("X0001.SH", "2026-01-06"), 600)
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "pre", "at": "x"}))
        lg.apply(ev("BROKER_POSITION_VIEW", {"can_sell": {"X0001.SH": 600}}))  # 报出后券商已冻结
        self.assertEqual(lg.free_sell("X0001.SH", "2026-01-06"), 600)

    @case("G07-08", "G08_FEE_CLOSURE")
    def test_fee_aware_total_reduce(self):
        g = golden("G07-08")
        legs = {"A": SellLeg("A", 6600, Fraction(10), Decimal(10), "I", 6600, MAIN, 10 ** 9, Fraction(66000))}
        planner = SellPlanner(Fraction(100000) + Fraction(0), legs,
                              lambda s, q, p: Decimal(q) * p * Decimal("0.001") + Decimal(5))
        planner.E = Fraction(100000)
        res = planner.run(exits=[], singles=[], industries=[], total=True)
        self.assertEqual(res["sell"]["A"], g["expected"]["minimum_legal_sell"])
        self.assertEqual(res["e_after"], Fraction(99988))
        self.assertEqual(planner.v_after("A"), Fraction(59000))
        x600 = {"A": 600}
        self.assertTrue(planner.violated(("TOTAL", "ACCOUNT"), x600))
        gf = golden("G08_FEE_CLOSURE")["input"]
        self.assertGreater(Fraction(gf["single_value"]), C.SINGLE_TARGET * (Fraction(gf["equity_before_extra_cost"]) -
                                                                            Fraction(gf["extra_cost"])))

    @case("G07-09")
    def test_unreachable_sells_max(self):
        legs = {"A": SellLeg("A", 6600, Fraction(10), Decimal(10), "I", 500, MAIN, 10 ** 9, Fraction(66000))}
        res = SellPlanner(Fraction(100000), legs, lambda s, q, p: Decimal(q) * p * Decimal("0.001") + 5).run(
            exits=[], singles=[], industries=[], total=True)
        self.assertEqual(res["sell"]["A"], 500)
        self.assertIn("TOTAL:ACCOUNT", res["unreachable"])

    @case("G07-10", "G17-13")
    def test_fee_closure_loop(self):
        # A 恰在 2.0% 线上；行业 I1 纠偏的卖出费用使 E_after 下降 → A 又超线 → 闭合复核追加 A 的合法卖量
        legs = {"A": SellLeg("A", 200, Fraction(10), Decimal(10), "I0", 200, MAIN, 10 ** 9, Fraction(2000)),
                "B": SellLeg("B", 2200, Fraction(10), Decimal(10), "I1", 2200, MAIN, 10 ** 9, Fraction(22000))}
        fee = lambda s, q, p: Decimal(q) * p * Decimal("0.001") + Decimal(5)
        planner = SellPlanner(Fraction(100000), legs, fee)
        res = planner.run(exits=[], singles=["A"], industries=["I1"], total=False)
        self.assertFalse(planner.violated(("SINGLE", "A")))
        self.assertFalse(planner.violated(("INDUSTRY", "I1")))
        self.assertEqual(res["sell"]["A"], 100)           # 初始满足，费用闭合后追加一档
        self.assertEqual(res["sell"]["B"], 300)

    @case("G07-11")
    def test_frozen_shares_not_scaled_for_fees(self):
        lg, ev = new_ledger()
        for coid, sid, q in (("a", "X0001.SH", 5000), ("b", "X0002.SH", 10000)):
            lg.apply(ev("ORDER_CREATE", buy_order(coid, sid=sid, qty=q, limit="1")))
            lg.apply(ev("ORDER_SUBMIT", {"client_order_id": coid, "at": "x"}))
        lg.apply(ev("BROKER_FILL", fill("a", "ta", 5000, "1")))
        e0, _ = lg.equity()
        lg.apply(ev("BROKER_FILL", fill("b", "tb", 10000, "1")))
        e1, _ = lg.equity()
        self.assertEqual(lg.positions["X0001.SH"].quantity, 5000)
        self.assertEqual(e0 - e1, 5)             # 只减少费用；冻结股数不按比例缩小

    @case("G07-12", "G17-01")
    def test_startup_sizing_golden(self):
        g = golden("G17-01")["input"]
        exp = golden("G17-01")["expected"]
        names = g["security_ids"]
        res = startup_fit(capital=Decimal(g["capital"]), names=names, close={s: Decimal(g["price"]) for s in names},
                          industry_of=g["industries"], grid_of=lambda s: MAIN,
                          fee_fn=lambda s, q, p: C.cents(max(Decimal(g["commission"]) * q * p, Decimal(g["minimum"]))),
                          slippage=Decimal(g["slippage"]), tick_of=lambda s: Decimal("0.01"))
        self.assertEqual([res["quantities"][s] for s in names], exp["quantities"])
        self.assertEqual((res["stock_value"], res["cash"], res["fees"], res["equity"], res["names"]),
                         (Decimal(exp["stock_value"]), Decimal(exp["cash"]), Decimal(exp["fees"]), Decimal(exp["equity"]),
                          exp["names"]))
        self.assertEqual(res["status"], "STARTED")

    @case("G07-13")
    def test_startup_boundary_pass(self):
        self.assertTrue(startup_pass(24, Decimal(50000), Decimal(100000)))

    @case("G07-14")
    def test_startup_23_names_fail(self):
        self.assertFalse(startup_pass(23, Decimal(60000), Decimal(100000)))

    @case("G07-15")
    def test_startup_ratio_fail(self):
        self.assertFalse(startup_pass(26, Decimal("49999.99"), Decimal(100000)))

    @case("G07-16")
    def test_runtime_no_forced_24(self):
        import inspect
        import smallcap.replay as rp
        import smallcap.minute as mn
        src = inspect.getsource(rp.CellRunner._proxy) + inspect.getsource(rp.CellRunner._proxy_buys) + inspect.getsource(mn)
        self.assertNotIn("STARTUP_MIN", src)
        self.assertNotIn("startup_fit", src)
        m = StubMarket(n=40)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {f"X{i:04d}.SH": 200 for i in range(1, 21)}, 60000)
        put_plan(r, date(2024, 1, 5), {}, [], 100000, D0)
        exec_day(r, D0)
        self.assertEqual([o for o in r.ledger.orders.values() if o.side == "BUY"], [])

    @case("G07-17", "G16-20")
    def test_capital_configs_independent(self):
        from smallcap.evaluation import qualify_config
        def cells(status):
            return [{"start": s, "scenario": sc, "startup_status": status, "retirement_triggered": False,
                     "MDD_exact": {"num": "1", "den": "10"}, "mdd_over_30": False, "CAGR_decimal": "0.03",
                     "package_availability": "PASS", "evidence_reasons": [], "annual_fee_rate": "0.01"}
                    for s in ("A0", "A1", "A2", "A3") for sc in ("S0", "S1", "S2", "S3")]
        q100 = qualify_config(cells("NOT_STARTED_UNFIT"), test_package=False, external_verified={"x": True},
                              benchmark_specified=True)
        q500 = qualify_config(cells("STARTED"), test_package=False, external_verified={"x": True}, benchmark_specified=True)
        self.assertEqual(q100["status"], "REJECTED")
        self.assertEqual(q500["status"], "REVIEWABLE")

    @case("G07-18")
    def test_sequential_reservation(self):
        lg, ev = new_ledger(cash="5000")
        lg.apply(ev("ORDER_CREATE", buy_order("b1", sid="X0001.SH", qty=300, limit="10")))
        free = lg.own_free_cash("2026-01-05")
        self.assertEqual(free, Decimal("5000") - Decimal("3005"))
        v = view(cash=str(free))
        b, why = size_buy(v, sid="X0002.SH", industry="I", grid=MAIN, p=Decimal(10), m=Decimal(10),
                          gap_value=Fraction(3000), fee_fn=lambda x: fee5(x), capacity_left_value=Decimal("1e9"))
        self.assertEqual(b, 100)                   # 1995 可用 → 只能 100 股，不会两单都按 3000 报

    @case("G07-19")
    def test_resubmit_after_partial_cancel(self):
        target_remaining = 300 - 50
        self.assertEqual(MAIN.max_buy(target_remaining), 200)
        self.assertFalse(MAIN.legal_buy(250))

    @case("G07-20")
    def test_merged_order_legality(self):
        seq = MAIN.sell_sequence(100, 1000, 1000, 10 ** 9)
        self.assertEqual(seq[:3], [100, 200, 300])            # 只允许使合并量 100+x 合法的追加量
        self.assertNotIn(50, seq)
        self.assertTrue(all(MAIN.legal_sell(100 + x, 1000, 1000) for x in seq))


if __name__ == "__main__":
    unittest.main()
