"""G19 关键闭合链：输入与预期逐字取自 verification/golden_vectors.json，经生产入口独立执行（不导入 oracle）。"""
import copy
import json
import tempfile
import unittest
from datetime import date, time
from decimal import Decimal
from fractions import Fraction

from smallcap import contracts as C
from smallcap.account import Ledger, check_exact_snapshot, dd_flags, reproject
from smallcap.bundle import load_bundle, normalize_study, schema_errors
from smallcap.contracts import ContractError, sh_dt, unwire_ratio, wire_ratio
from smallcap.execution import ordinary_sell_qty, retry_proposal
from smallcap.factors import score_package
from smallcap.fees import FeeSchedule, TaxSchedule
from smallcap.journal import Journal, recover
from smallcap.portfolio import HoldingView, plan_week
from smallcap.risk import new_episode, open_order_cancellations, satisfied_now, RiskView
from smallcap.rules import QuantityGrid
from tests.acceptance import case
from tests.fixtures import (ENGINEERING_BUNDLE, TAX_FIXTURE, EventFactory, StubMarket, buy_order, exec_day, fill, golden,
                            make_runner, put_plan, seed_holdings, sell_order, test_fee_payload, test_fees)

MAIN = QuantityGrid(100, 100, 100, 100, True, 1_000_000)
D0 = date(2024, 1, 8)


# ---------------------------------------------------------------- 资本流（G19-04—09）
def ledger_at_equity(equity: str, units="100000"):
    """经事件构造：入金 units 元（HWM=1），再以持仓估值使日终前 E=equity（未日终）。"""
    lg = Ledger("A", test_fees())
    ev = EventFactory("c")
    lg.apply(ev("CAPITAL_FLOW", {"flow_id": "init", "amount": units}))
    lg.apply(ev("ORDER_CREATE", buy_order("b", qty=1000, limit="10")))
    lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "x"}))
    lg.apply(ev("BROKER_FILL", fill("b", "t", 1000, "10")))
    stock = Decimal(equity) - lg.cash
    lg.apply(ev("MARKS", {"marks": {"X0001.SH": {"price": str(stock / 1000), "mark_date": "2026-01-05",
                                                 "status": "NORMAL"}}}))
    assert lg.equity()[0] == Fraction(Decimal(equity))
    return lg, ev


def eod_flags(lg, ev):
    lg.apply(ev("EOD", {"date": "2026-01-05"}))
    pt = lg.ledger_points[-1]
    return {"nav": pt["nav"], "hwm": pt["hwm"], "dd": pt["dd"], "warning": pt["dd"] >= C.DD_WARNING,
            "retire": pt["dd"] >= C.DD_RETIRE, "mdd_pass": pt["dd"] <= C.DD_LIMIT}


class Capital(unittest.TestCase):
    def _flows(self, cid):
        g = golden(cid)
        lg, ev = ledger_at_equity(g["input"]["equity"], g["input"]["units"]["num"])
        self.assertEqual(lg.hwm, unwire_ratio(g["input"]["hwm"]))
        for i, f in enumerate(g["input"]["flows"]):
            lg.apply(ev("CAPITAL_FLOW", {"flow_id": f"f{i}", "amount": f}))
        units = wire_ratio(lg.units)
        fl = eod_flags(lg, ev)
        e = g["expected"]
        self.assertEqual(units, e["units"])
        self.assertEqual((wire_ratio(fl["nav"]), wire_ratio(fl["dd"]), wire_ratio(fl["hwm"]), fl["retire"]),
                         (e["nav"], e["dd"], e["hwm"], e["retire"]))
        # 生产风险模块据同源日终DD触发应急段
        from smallcap.risk import detect
        starts, _, flags = detect(RiskView(lg.equity()[0], {}, {}, {}, dd=fl["dd"]), {}, at="t", first_eligible="d", seq=1)
        self.assertEqual(flags["retire"], e["retire"])
        self.assertEqual([s["type"] for s in starts], ["EMERGENCY_EXIT"])

    @case("G19-04")
    def test_g19_04(self):
        self._flows("G19-04")

    @case("G19-05")
    def test_g19_05(self):
        self._flows("G19-05")

    @case("G19-06")
    def test_g19_06(self):
        self._flows("G19-06")

    @case("G19-07")
    def test_g19_07(self):
        g = golden("G19-07")
        for item in g["input"]["cases"]:
            lg, ev = ledger_at_equity(item["equity"], g["input"]["unit_count"])
            lg.apply(ev("CAPITAL_FLOW", {"flow_id": "one", "amount": "1"}))
            fl = eod_flags(lg, ev)
            self.assertEqual([fl["warning"], fl["retire"], fl["mdd_pass"]], item["flags"], item["equity"])

    @case("G19-08")
    def test_g19_08(self):
        g = golden("G19-08")
        lg, ev = ledger_at_equity("75000")
        for i, f in enumerate(g["input"]["flows_before"]):
            lg.apply(ev("CAPITAL_FLOW", {"flow_id": f"b{i}", "amount": f}))
        snap = json.loads(json.dumps(lg.snapshot()))
        restored = Ledger.restore(snap, test_fees())
        for i, f in enumerate(g["input"]["flows_after"]):
            e = ev("CAPITAL_FLOW", {"flow_id": f"a{i}", "amount": f})
            lg.apply(e)
            restored.apply(e)
        self.assertEqual(json.dumps(lg.snapshot(), sort_keys=True), json.dumps(restored.snapshot(), sort_keys=True))
        fl = eod_flags(restored, ev)
        self.assertEqual((wire_ratio(restored.units), wire_ratio(fl["nav"]), wire_ratio(fl["dd"])),
                         (g["expected"]["units"], g["expected"]["nav"], g["expected"]["dd"]))
        # 非日终切片：NAV 可高于上一正式 HWM，恢复不得拒绝或改写 HWM
        up, ev2 = ledger_at_equity("110000")
        rest = Ledger.restore(json.loads(json.dumps(up.snapshot())), test_fees())
        self.assertEqual((rest.hwm, rest.nav()), (Fraction(1), Fraction(11, 10)))
        self.assertEqual(eod_flags(rest, ev2)["dd"], 0)
        bad = json.loads(json.dumps(lg.snapshot()))
        bad["exact"]["nav"] = wire_ratio(Fraction(751, 1000))
        with self.assertRaises(ContractError):
            Ledger.restore(bad, test_fees())

    @case("G19-09")
    def test_g19_09(self):
        g = golden("G19-09")
        rejected = 0
        for b in g["input"]["bad"]:
            try:
                unwire_ratio(b)
            except ContractError:
                rejected += 1
        self.assertEqual(rejected, g["expected"]["rejected_count"])
        lg, _ = ledger_at_equity("75000")
        d1 = json.loads(json.dumps(lg.snapshot()))
        d1["detail_version"] = "V5D1"
        with self.assertRaises(ContractError) as cm:
            Ledger.restore(d1, test_fees())
        self.assertEqual(cm.exception.code, "EXACT_STATE_REPLAY_REQUIRED")
        ex = json.loads((__import__("tests.fixtures", fromlist=["HANDOFF"]).HANDOFF / "inputs/exact_state.example.json").read_text())
        self.assertEqual(check_exact_snapshot(ex["payload"])["dd"], Fraction(1, 4))
        ex["payload"]["detail_version"] = "V5D1"
        with self.assertRaises(ContractError):
            check_exact_snapshot(ex["payload"])


# ---------------------------------------------------------------- 订单链（G19-10—19）
class Orders(unittest.TestCase):
    def _ledger(self, init):
        lg = Ledger("A", FeeSchedule(test_fee_payload()))     # TEST_ONLY：万2.5最低5元，其他费税0，即时入账
        ev = EventFactory("o")
        if init["side"] == "BUY":
            lg.apply(ev("CAPITAL_FLOW", {"flow_id": "init", "amount": init["cash"]}))
            lg.apply(ev("ORDER_CREATE", buy_order("o1", qty=init["requested"], limit=init["limit"], capital_revision=1)))
        else:
            lg.apply(ev("BOOTSTRAP_ACCOUNT", {"cash": init["cash"], "positions": [
                {"security_id": "X0001.SH", "market": "SH", "board": "MAIN_SH", "quantity": init["shares"],
                 "mark": init["limit"], "acquisition_tax_date": "2025-01-02"}]}))
            lg.apply(ev("ORDER_CREATE", sell_order("o1", qty=init["requested"], limit=init["limit"], capital_revision=1)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "o1", "at": "2026-01-05T10:00:00+08:00"}))
        if init["status"] == "ACKED":
            lg.apply(ev("BROKER_ACK", {"client_order_id": "o1", "broker_order_id": "B1"}))
        self.assertEqual(lg.orders["o1"].status, init["status"])
        return lg, ev

    def _apply(self, lg, ev, e, sell_episode=None):
        k = e["kind"]
        if k == "FILL":
            lg.apply(ev("BROKER_FILL", fill("o1", e["trade_id"], e["qty"], e["price"]), eid=f"fill/{e['trade_id']}/{e['id']}"
                        if e["id"] != e["trade_id"] else None))
        elif k == "ACK":
            lg.apply(ev("BROKER_ACK", {"client_order_id": "o1", "broker_order_id": "B1"}))
        elif k == "CANCEL_REQUEST":
            lg.apply(ev("CANCEL_REQUEST", {"client_order_id": "o1", "request_id": e["id"], "reason": e["reason"]}))
        elif k == "CANCEL_FAILED":
            lg.apply(ev("CANCEL_FAILED", {"client_order_id": "o1", "original_still_open": e["original_still_open"]}))
        elif k == "TERMINAL":
            lg.apply(ev("BROKER_TERMINAL", {"client_order_id": "o1", "status": e["status"], "cum_qty": e["cum_qty"],
                                            "cum_value": e["cum_gross"], "source_revision": e["source_revision"]}))
        elif k == "CAPITAL_FLOW":
            lg.apply(ev("MARKS", {"marks": {"X0001.SH": {"price": "10", "mark_date": "2026-01-05", "status": "NORMAL"}}}))
            lg.apply(ev("CAPITAL_FLOW", {"flow_id": e["id"], "amount": e["amount"]}))
            for coid, why in open_order_cancellations(lg, None, plan_capital_revision=1):
                lg.apply(ev("CANCEL_REQUEST", {"client_order_id": coid, "request_id": "d94", "reason": why}))
        elif k == "QUOTE_RISK_REFRESH":
            lg.apply(ev("MARKS", {"marks": {"X0001.SH": {"price": e["mark"], "mark_date": "2026-01-05", "status": "NORMAL"}}}))
            p = lg.positions["X0001.SH"]
            view = RiskView(lg.equity()[0], {"X0001.SH": p.quantity * Fraction(Decimal(e["mark"]))}, {"X0001.SH": "I0"},
                            {"X0001.SH": p.quantity})
            if satisfied_now(sell_episode, view):
                lg.apply(ev("RISK_EPISODE_END", {"episode_id": sell_episode["episode_id"], "reason": "TARGET_SATISFIED",
                                                 "at": "x"}))
            needed = {} if satisfied_now(sell_episode, view) else {"X0001.SH": lg.orders["o1"].remaining_possible}
            for coid, why in open_order_cancellations(lg, needed):
                lg.apply(ev("CANCEL_REQUEST", {"client_order_id": coid, "request_id": "d94", "reason": why}))
        else:
            raise AssertionError(k)

    def _output(self, lg):
        o = lg.orders["o1"]
        pos = lg.positions.get("X0001.SH")
        props = retry_proposal(o, target_qty=o.qty, grid=MAIN, attempts_left=1, capital_revision=lg.capital_revision,
                               decision={"global_buy_blocks": [], "security_buy_blocks": {}},
                               cash_free=lg.own_free_cash("2026-01-05", Decimal(0)),
                               fee_fn=lambda q: test_fees().preview_new_order_fees("SH", o.side, date(2026, 1, 5),
                                                                                    q * o.limit, q),
                               free_sell=lg.free_sell("X0001.SH", "2026-01-06") if o.side == "SELL" else None)
        return {"status": o.status, "shares": pos.quantity if pos else 0, "cash": C.dec_str(lg.cash),
                "filled_qty": o.filled_qty, "gross": C.dec_str(o.filled_value),
                "commission": C.dec_str(o.fees_accrued.get("COMMISSION", Decimal("0.00"))),
                "remaining_qty": o.remaining_possible, "cash_reserved": C.dec_str(lg.buy_cash_reserved(o)),
                "sell_qty_reserved": o.remaining_possible if o.side == "SELL" else 0,
                "capacity_used": C.dec_str(lg.capacity_used_on("X0001.SH", o.trade_date)),
                "capacity_reserved": C.dec_str(lg.capacity_pending("X0001.SH", o.trade_date)),
                "cancel_required": o.cancel_required, "cancel_reason": (o.cancel_reason or "").split(":")[0],
                "replacement_orders": len(props), "replacement_proposals": props,
                "capital_invalid": lg.capital_revision != o.capital_revision}

    def _same(self, actual, expected):
        for k, v in expected.items():
            a = actual[k]
            if k in ("cash", "gross", "commission", "cash_reserved", "capacity_used", "capacity_reserved"):
                self.assertEqual(Decimal(a), Decimal(v), k)
            else:
                self.assertEqual(a, v, k)

    def _chain(self, cid, restore_after=None):
        g = golden(cid)
        init = g["input"]["initial"]
        lg, ev = self._ledger(init)
        ep = None
        if init["side"] == "SELL":
            ep = new_episode("SINGLE_REDUCE", "X0001.SH", 1, "t", "2026-01-05", ["X0001.SH"], C.SINGLE_TARGET)
            lg.apply(ev("RISK_EPISODE_START", {"episode": ep}))
        trace = {t["step"]: t for t in g["input"].get("trace", [])}
        for step, e in enumerate(g["input"]["events"], 1):
            self._apply(lg, ev, e, ep)
            if step in trace:
                self._same(self._output(lg), {k: v for k, v in trace[step].items() if k != "step"})
            if restore_after == step:
                lg = Ledger.restore(json.loads(json.dumps(lg.snapshot())), FeeSchedule(test_fee_payload()))
        out = self._output(lg)
        self._same(out, {k: v for k, v in g["expected"].items() if k != "restore_matches_uninterrupted"})
        return lg, out

    @case("G19-10")
    def test_g19_10(self):
        self._chain("G19-10")

    @case("G19-11")
    def test_g19_11(self):
        self._chain("G19-11")

    @case("G19-12")
    def test_g19_12(self):
        self._chain("G19-12")

    @case("G19-13")
    def test_g19_13(self):
        lg, _ = self._chain("G19-13")
        with self.assertRaises(ContractError):        # 原单仍活动：禁止新同向单
            lg.apply({"event_id": "x", "kind": "ORDER_CREATE", "payload": buy_order("o2", qty=200)})

    @case("G19-14")
    def test_g19_14(self):
        self._chain("G19-14")

    @case("G19-15")
    def test_g19_15(self):
        self._chain("G19-15")

    @case("G19-16")
    def test_g19_16(self):
        lg, _ = self._chain("G19-16")
        self.assertIn("ORDER_RECONCILE:o1", lg.limitations)

    @case("G19-17")
    def test_g19_17(self):
        self._chain("G19-17")

    @case("G19-18")
    def test_g19_18(self):
        self._chain("G19-18")

    @case("G19-19")
    def test_g19_19(self):
        g = golden("G19-19")
        lg_restored, out_r = self._chain("G19-19", restore_after=g["input"]["break_after"])
        lg_cont, out_c = self._chain("G19-19")
        self.assertEqual(out_r, out_c)
        # 断点后重复回报：不重扣、不重报
        lg_restored.apply({"event_id": "dup", "kind": "BROKER_FILL", "payload": fill("o1", "t2", 200, "10")})
        self.assertEqual(self._output(lg_restored), out_c)
        # 经日志落盘的断点恢复（真实 journal 路径）
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            init = g["input"]["initial"]
            base = Ledger("A", FeeSchedule(test_fee_payload()))
            evs = [{"event_id": "i", "kind": "CAPITAL_FLOW", "payload": {"flow_id": "init", "amount": init["cash"]}},
                   {"event_id": "c", "kind": "ORDER_CREATE", "payload": buy_order("o1", qty=300, capital_revision=1)},
                   {"event_id": "s", "kind": "ORDER_SUBMIT", "payload": {"client_order_id": "o1", "at": "x"}},
                   {"event_id": "a", "kind": "BROKER_ACK", "payload": {"client_order_id": "o1", "broker_order_id": "B"}},
                   {"event_id": "f1", "kind": "BROKER_FILL", "payload": fill("o1", "t1", 100, "10")},
                   {"event_id": "cx", "kind": "CANCEL_REQUEST", "payload": {"client_order_id": "o1", "request_id": "c",
                                                                           "reason": "TIMEOUT"}}]
            for e in evs:
                j.commit(e)
                base.apply(e)
            j.write_snapshot(base)
            j.release()
            j2 = Journal(d)
            j2.acquire()
            rec = recover(j2, "A", FeeSchedule(test_fee_payload()))
            last = {"event_id": "f2", "kind": "BROKER_FILL", "payload": fill("o1", "t2", 200, "10")}
            j2.commit(last)
            rec.apply(last)
            j2.release()
            self.assertEqual((rec.orders["o1"].status, rec.cash, rec.positions["X0001.SH"].quantity),
                             ("FILLED", Decimal("6995.00"), 300))


# ---------------------------------------------------------------- 两级禁买（G19-01—03）
class RiskPipeline(unittest.TestCase):
    def _run(self, cid):
        g = golden(cid)
        blocked = [Decimal(x) for x in g["input"]["blocked_values"]]
        m = StubMarket(n=60)
        r = make_runner(m)
        held = {}
        nstable = 29 - len(blocked)
        if g["input"]["total_reduce"]:
            stable = [2300] * 20 + [2250] * 8
        else:
            stable = [1000] * nstable
        # 价格10元：2300→230股、2250→225股（真实持股可为零股）
        for i, v in enumerate(stable):
            held[f"X{i + 1:04d}.SH"] = v // 10
        bsids = [f"X{40 + i:04d}.SH" for i in range(len(blocked))]
        for s, v in zip(bsids, blocked):
            held[s] = int(v / 10)
        held["X0050.SH"] = 100                                   # 普通退出 1000 元
        cash = Decimal(100005) - sum(Decimal(q) * 10 for q in held.values())
        seed_holdings(r, date(2024, 1, 5), held, cash)
        for s in bsids:
            m.set_bar(s, D0, status="SUSPENDED")
            m.risk.append({"security_id": s, "status": "ST", "effective": date(2024, 1, 5),
                           "available_at": sh_dt(date(2024, 1, 4), time(18))})
        # 日终风险识别（ST确定退出；若总暴露>65%形成 TOTAL_REDUCE）
        r.phase_key, r.k = "eod", 0
        r.emit("EOD", {"date": "2024-01-05"}, sh_dt(date(2024, 1, 5), time(15, 30)))
        r._eod_risk(date(2024, 1, 5), sh_dt(date(2024, 1, 5), time(15, 30)))
        from smallcap.contracts import wire_ratio as w
        modes = {"X0050.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "RANK"},
                 "X0059.SH": {"mode": "TARGET_AMOUNT", "target_amount": w(Fraction(2000)), "prohibit_buy": False,
                              "reason": "NEW_BUY"}}
        put_plan(r, date(2024, 1, 5), modes, ["X0059.SH"], 100000, D0)
        decisions = []
        import smallcap.replay as rp
        orig = rp.risk_decision

        def spy(**kw):
            d = orig(**kw)
            decisions.append(d)
            return d
        rp.risk_decision = spy
        try:
            exec_day(r, D0)
        finally:
            rp.risk_decision = orig
        L = r.ledger
        buys = [{"security_id": "NEW", "side": o.side, "qty": o.qty} for o in L.orders.values() if o.side == "BUY"]
        names_after_sell = len({s for s, p in L.positions.items() if p.quantity > 0 and s != "X0059.SH"})
        return g, decisions[-1], buys, names_after_sell

    @case("G19-01")
    def test_g19_01(self):
        g, dec, buys, names = self._run("G19-01")
        self.assertEqual(dec["global_buy_blocks"], g["expected"]["global_buy_blocks"])
        self.assertEqual(names, g["expected"]["actual_names_after_sell"])
        self.assertEqual(buys, [{k: v for k, v in o.items() if k != "limit"} for o in g["expected"]["orders"]])
        self.assertEqual(dec["security_buy_blocks"]["X0040.SH"], ["SECURITY_EXIT"])     # 只禁ST股本身
        self.assertNotIn("X0059.SH", dec["security_buy_blocks"])

    @case("G19-02")
    def test_g19_02(self):
        g, dec, buys, _ = self._run("G19-02")
        self.assertEqual(dec["global_buy_blocks"], g["expected"]["global_buy_blocks"])
        self.assertEqual(buys, g["expected"]["orders"])

    @case("G19-03")
    def test_g19_03(self):
        g, dec, buys, _ = self._run("G19-03")
        self.assertEqual(dec["global_buy_blocks"], g["expected"]["global_buy_blocks"])
        self.assertEqual(buys, g["expected"]["orders"])


# ---------------------------------------------------------------- FLAT_SCORE（G19-20—22）
class Flat(unittest.TestCase):
    def _flat(self, cid):
        g = golden(cid)
        ids = [f"X{i + 1:04d}.SH" for i in range(40)]
        pool = {"security_ids": ids, "size_order": {s: i + 1 for i, s in enumerate(ids)}, "size": 40, "health": "OK",
                "codes": {s: s for s in ids}, "industries": {s: "I0" for s in ids}, "eligibility": {s: "TRUE" for s in ids}}
        pkg = copy.deepcopy(ENGINEERING_BUNDLE["profiles"]["factor"]["payload"])
        f0 = pkg["factors"][0]
        pkg["factors"] = [dict(f0, factor_id="A"), dict(f0, factor_id="B", direction="LOWER_BETTER")]
        from smallcap.factors import FactorValue
        vals = {s: FactorValue(Decimal(i), "VALID") for i, s in enumerate(ids)}
        sc = score_package(pkg, pool, {"A": vals, "B": vals}, pool["codes"])
        self.assertTrue(sc["flat"])
        qty = g["input"]["qty"]
        sp, xp = Decimal(g["input"]["signal_price"]), Decimal(g["input"]["execution_price"])
        e = Fraction(100000)
        plan, _ = plan_week(role="B1", plan_id="p", cutoff="c", e_decision=e,
                            holdings=[HoldingView("X0001.SH", qty, qty * Fraction(sp), "I0")], open_buy_sids=set(),
                            pool=pool, score=sc, counters={}, capital_revision=1, execute_session=D0.isoformat())
        mode = plan["modes"]["X0001.SH"]
        m = StubMarket(n=40)
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": qty}, Decimal(100000) - qty * sp, mark=str(sp))
        put_plan(r, date(2024, 1, 5), plan["modes"], plan["buy_candidates_ordered"], 100000, D0,
                 ordinary_buy_allowed=plan["ordinary_buy_allowed"], score_status=plan["score_status"],
                 score_reason=plan["score_reason"])
        m.set_bar("X0001.SH", D0, open=str(xp), close=str(xp), ref_preclose=str(xp))
        exec_day(r, D0)
        orders = list(r.ledger.orders.values())
        side = orders[0].side if orders else None
        q = orders[0].qty if orders else 0
        self.assertEqual({"mode": mode["mode"], "qty": q, "side": side, "prohibit_buy": mode["prohibit_buy"]},
                         g["expected"])
        self.assertFalse(any(o.side == "BUY" for o in orders))

    @case("G19-20")
    def test_g19_20(self):
        self._flat("G19-20")

    @case("G19-21")
    def test_g19_21(self):
        self._flat("G19-21")

    @case("G19-22")
    def test_g19_22(self):
        self._flat("G19-22")


# ---------------------------------------------------------------- 税则日期（G19-23—24）
class Tax(unittest.TestCase):
    @case("G19-23")
    def test_g19_23(self):
        g = golden("G19-23")
        inp = g["input"]
        payload = test_fee_payload(tax_rules=TAX_FIXTURE["rules"])
        fees = FeeSchedule(payload)
        res = {}
        for rd in inp["record_dates"]:
            lg = Ledger("A", fees)
            ev = EventFactory("t")
            lg.apply(ev("BOOTSTRAP_ACCOUNT", {"cash": "0", "positions": [
                {"security_id": "X0001.SH", "market": "SH", "board": "MAIN_SH", "quantity": 100, "mark": "10",
                 "acquisition_tax_date": inp["acquired"]}]}))
            lg.apply(ev("CA_RECORD", {"action_id": "D", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                      "record_date": rd, "cash_per_share": "1", "tax_base_per_share": "1"}))
            lg.apply(ev("CA_EX", {"action_id": "D", "reference_price": "9"}))
            rule = fees.tax.by_id(lg.entitlements["D"]["binding"]["selected_rule_id"])
            withheld = C.cents(Decimal(inp["tax_base"]) * rule.initial_withholding_rate)
            lg.apply(ev("CA_PAY", {"action_id": "D", "withheld": str(withheld)}))
            lg.apply(ev("ORDER_CREATE", sell_order("s", qty=100, limit="9", trade_date=inp["disposed"])))
            lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "s", "at": "x"}))
            lg.apply(ev("BROKER_FILL", fill("s", "T", 100, "9", trade_date=inp["disposed"],
                                            at=f"{inp['disposed']}T10:00:30+08:00", tax_delivery_date=inp["disposed"])))
            sl = lg.slices["D/X0001.SH/L1"]
            disp = sl["disposals"][0]
            res[rd] = (lg.entitlements["D"]["binding"], C.cents(Decimal(disp["final_tax"])), C.cents(Decimal(disp["additional"])))
            self.assertEqual(lg.entitlements["D"]["binding"]["rule_selection_date"], rd)
            self.assertEqual(lg.entitlements["D"]["binding"]["selection_date_basis"], "EQUITY_RECORD_DATE")
        e = g["expected"]
        self.assertEqual([res[r][0]["selected_rule_id"] for r in inp["record_dates"]], e["selected_rules"])
        self.assertEqual([str(res[r][1]) for r in inp["record_dates"]], e["final_taxes"])
        self.assertEqual([str(res[r][2]) for r in inp["record_dates"]], e["additional_taxes"])
        # 派息日/卖出日不重新选择税制
        sched = TaxSchedule(TAX_FIXTURE["rules"])
        self.assertEqual([sched.select(date.fromisoformat(r)).rule_id for r in inp["record_dates"]], e["selected_rules"])
        with self.assertRaises(ContractError):
            sched.select(date.fromisoformat(inp["pay_date"]))
        self.assertTrue(e["payment_date_does_not_select_rule"])

    @case("G19-24")
    def test_g19_24(self):
        g = golden("G19-24")
        sched = TaxSchedule(TAX_FIXTURE["rules"])
        out = []
        for d in g["input"]["dates"]:
            try:
                out.append(sched.select(date.fromisoformat(d)).rule_id)
            except ContractError as ex:
                out.append(ex.code)
        self.assertEqual(out, g["expected"]["rules"])
        dup = copy.deepcopy(TAX_FIXTURE["rules"])
        dup.append(dict(copy.deepcopy(dup[-1]), rule_id="CONFLICT"))
        with self.assertRaises(ContractError) as cm:
            TaxSchedule(dup).select(date(2020, 1, 1))
        self.assertEqual(cm.exception.code, "TAX_RULE_OVERLAP")
        bad = copy.deepcopy(TAX_FIXTURE["rules"])
        bad[0]["selection_date_basis"] = "PAY_DATE"
        with self.assertRaises(ContractError) as cm:
            TaxSchedule(bad)
        self.assertEqual(cm.exception.code, "TAX_SELECTION_BASIS")


# ---------------------------------------------------------------- Bundle（G19-25）
class BundleMap(unittest.TestCase):
    @case("G19-25")
    def test_g19_25(self):
        g = golden("G19-25")["expected"]
        b = copy.deepcopy(ENGINEERING_BUNDLE)
        n = load_bundle(b).study
        self.assertEqual(n["source_profile_id"] == b["profiles"]["sources"]["profile_id"], g["source_id_matches"])
        self.assertEqual(n["factor_package_id"], g["factor_package_id"])
        extra = copy.deepcopy(b)
        extra["study"]["source_profile_id"] = "INJECTED"
        self.assertEqual(bool(schema_errors(extra)), g["wire_extra_profile_rejected"])
        with self.assertRaises(ContractError):
            normalize_study(extra)
        for mode in ("DAILY_PROXY", "MINUTE_WINDOW"):
            z = copy.deepcopy(b)
            z["study"]["execution_mode"] = mode
            self.assertTrue(schema_errors(z))
            with self.assertRaises(ContractError):
                load_bundle(z)
        bad = copy.deepcopy(b)
        bad["profiles"]["sources"]["payload"] = {"source_profile_id": "DIFFERENT"}
        with self.assertRaises(ContractError) as cm:
            normalize_study(bad)
        self.assertEqual(cm.exception.code, "PROFILE_REFERENCE_MISMATCH")
        self.assertTrue(g["payload_reference_mismatch_rejected"] and g["aliases_rejected"])


# ---------------------------------------------------------------- 晚到更正（G19-26）
class Late(unittest.TestCase):
    @case("G19-26", "G17-12")
    def test_g19_26(self):
        g = golden("G19-26")
        inp, exp = g["input"], g["expected"]
        orig_copy = copy.deepcopy(inp["original"])

        def facts(items):
            out = []
            for e in items:
                k = e["kind"]
                if k == "INIT":
                    out.append({"event_id": e["id"], "kind": "BOOTSTRAP_ACCOUNT", "effective_key": e["effective_seq"],
                                "payload": {"cash": e["cash"], "positions": [
                                    {"security_id": "X0001.SH", "market": "SH", "board": "MAIN_SH", "quantity": e["qty"],
                                     "mark": e["price"]}]}})
                elif k == "MARK":
                    out.append({"event_id": e["id"], "kind": "MARKS", "effective_key": e["effective_seq"],
                                "payload": {"marks": {"X0001.SH": {"price": e["price"], "mark_date": "x",
                                                                   "status": "NORMAL"}}}})
                elif k == "CASHFLOW":
                    out.append({"event_id": e["id"], "kind": "CAPITAL_FLOW", "effective_key": e["effective_seq"],
                                "payload": {"flow_id": e["id"], "amount": e["amount"]}})
                elif k == "RECEIVABLE":
                    out += [{"event_id": e["id"] + "/record", "kind": "CA_RECORD", "effective_key": e["effective_seq"],
                             "payload": {"action_id": e["id"], "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                         "record_date": "2026-01-05", "cash_per_share": "1", "tax_base_per_share": "1"}},
                            {"event_id": e["id"] + "/ex", "kind": "CA_EX", "effective_key": e["effective_seq"],
                             "payload": {"action_id": e["id"], "cash_amount": e["amount"], "reference_price": "9"}}]
            return out

        old, _ = reproject("A", facts(inp["original"]), "OLD", test_fees())
        self.assertEqual(wire_ratio(old.units), exp["old_units"])
        new_facts = facts(inp["original"] + [inp["late"]])
        new, info = reproject("A", new_facts, "CORRECTED", test_fees())
        new.apply({"event_id": "eod", "kind": "EOD", "payload": {"date": "2026-01-05"}})
        pt = new.ledger_points[-1]
        self.assertEqual(C.dec_str(Decimal(new.equity()[0].numerator) / Decimal(new.equity()[0].denominator)),
                         exp["corrected_equity"])
        self.assertEqual([wire_ratio(new.units), wire_ratio(pt["nav"]), wire_ratio(pt["hwm"]), wire_ratio(pt["dd"])],
                         [exp["corrected_units"], exp["corrected_nav"], exp["corrected_hwm"], exp["corrected_dd"]])
        self.assertEqual((new.positions["X0001.SH"].quantity, C.dec_str(new.cash),
                          C.dec_str(sum(new.receivables.values()))), (exp["shares"], exp["cash"], exp["receivable"]))
        self.assertEqual([f["amount"] for f in new.capital_flows], exp["actual_flows"])
        self.assertEqual(inp["original"], orig_copy)
        # 每个检查点（同一更正投影修订）续算一致；旧修订快照被拒绝、从起点重建
        ordered = info["applied_order"]
        by_id = {f["event_id"]: f for f in new_facts}
        for k in range(1, len(ordered) + 1):
            part, _ = reproject("A", [by_id[i] for i in ordered[:k]], "CORRECTED", test_fees())
            cont, inf = reproject("A", new_facts, "CORRECTED", test_fees(), checkpoint=json.loads(json.dumps(part.snapshot())))
            self.assertEqual((cont.units, cont.cash, cont.equity()[0]), (new.units, new.cash, new.equity()[0]))
            self.assertFalse(inf["checkpoint_rejected"])
        stale, inf = reproject("A", new_facts, "CORRECTED", test_fees(), checkpoint=old.snapshot())
        self.assertTrue(inf["checkpoint_rejected"])
        self.assertEqual(stale.units, new.units)
        dup, _ = reproject("A", new_facts + facts([inp["late"]]), "CORRECTED", test_fees())
        self.assertEqual((dup.units, dup.equity()[0]), (new.units, new.equity()[0]))
        self.assertTrue(exp["original_facts_unchanged"] and exp["all_checkpoints_agree"] and exp["duplicate_correction_idempotent"])


if __name__ == "__main__":
    unittest.main()
