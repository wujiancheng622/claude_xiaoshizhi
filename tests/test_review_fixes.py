"""复查修复的独立回归测试（2026-10-19）。每条对应 V5D2 条款；夹具均为 TEST_ONLY。"""
import copy
import gzip
import json
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from smallcap.account import Ledger
from smallcap.bundle import load_bundle
from smallcap.contracts import ContractError, sh_dt, wire_ratio
from smallcap.data import build_facts
from smallcap.evaluation import annual_returns, provisional_decisive
from smallcap.forward import ForwardSession, forward_preconditions, import_broker_events, reconcile_account
from smallcap.journal import Journal, recover
from smallcap.matrix import run_matrix
from smallcap.portfolio import plan_week
from smallcap.replay import CellRunner, CellSpec, resume_cell
from smallcap.risk import RiskView, SellLeg, SellPlanner, detect, open_order_cancellations, risk_decision
from smallcap.rules import QuantityGrid, RuleBook
from tests.acceptance import case
from tests.fixtures import (SETTLEMENT, EventFactory, StubMarket, buy_order, close_day, exec_day, fill, make_runner,
                            new_ledger, put_plan, seed_holdings, sell_order, stub_tax_rules, test_fees,
                            test_rulebook_payload, trade_rule)
from tests.test_g06_g07_plan_sizing import hv, plan, pool
from tools.make_engineering_bundle import build

D0 = date(2024, 1, 8)


def taxed_ledger():
    """登记前持有 300 股（取得日 2026-01-05）、已登记现金分红每股 1 元（税表：1个月内20%）。"""
    lg, ev = new_ledger(fees=test_fees(tax_rules=stub_tax_rules()))
    lg.apply(ev("ORDER_CREATE", buy_order("b", qty=300)))
    lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "2026-01-05T10:00:00+08:00"}))
    lg.apply(ev("BROKER_FILL", fill("b", "t1", 300, "10", tax_acquisition_date="2026-01-05")))
    lg.apply(ev("EOD", {"date": "2026-01-05"}))
    lg.apply(ev("CA_RECORD", {"action_id": "div1", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                              "record_date": "2026-01-06", "security_class": "INDIVIDUAL_A_SHARE",
                              "dividend_type": "CASH", "cash_per_share": "1", "tax_base_per_share": "1"}))
    return lg, ev


class LedgerFixes(unittest.TestCase):
    @case("G19-13", "G09-07")
    def test_followup_cancel_once_per_failure(self):
        """D09.4：撤单确认失败且原单仍活动，只在新的可信修订下输出一次后续取消；未决期间不重复。"""
        lg, ev = new_ledger()
        lg.apply(ev("ORDER_CREATE", buy_order("o1", qty=300)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "o1", "at": "2026-01-05T10:00:00+08:00"}))
        lg.apply(ev("BROKER_ACK", {"client_order_id": "o1", "broker_order_id": "B1"}))
        lg.apply(ev("CANCEL_REQUEST", {"client_order_id": "o1", "request_id": "c1", "reason": "TARGET_SATISFIED"}))
        self.assertEqual(open_order_cancellations(lg, None), [])                 # 未决不重复
        lg.apply(ev("CANCEL_FAILED", {"client_order_id": "o1", "original_still_open": True}))
        self.assertEqual(open_order_cancellations(lg, None), [])                 # 同一修订不立即重发
        lg.apply(ev("BROKER_FILL", fill("o1", "t1", 100, "10")))               # 新的可信订单修订
        self.assertEqual(open_order_cancellations(lg, None), [("o1", "FOLLOWUP_AFTER_CANCEL_FAILED")])
        lg.apply(ev("CANCEL_REQUEST", {"client_order_id": "o1", "request_id": "c2", "reason": "FOLLOWUP"}))
        self.assertEqual(open_order_cancellations(lg, None), [])
        o = lg.orders["o1"]
        self.assertEqual((o.status, o.remaining_possible, o.cancel_actions), ("CANCEL_PENDING", 200, 2))

    @case("G12-05")
    def test_outflow_limited_to_free_cash(self):
        """D12.5/D14.4：转出不得动用买单预约；券商已确认的真实出金照实入账并记录政策违规、停止新增。"""
        lg, ev = new_ledger("100000")
        lg.apply(ev("ORDER_CREATE", buy_order("o1", qty=3000, limit="10")))     # 预约约30000+费用
        free = lg.own_free_cash("2026-01-05")
        with self.assertRaises(ContractError) as cm:
            lg.apply(ev("CAPITAL_FLOW", {"flow_id": "out1", "amount": str(-(free + 1))}))
        self.assertEqual(cm.exception.code, "CAPITAL_OUTFLOW_EXCEEDS_FREE_CASH")
        self.assertEqual(lg.cash, Decimal(100000))
        lg.apply(ev("CAPITAL_FLOW", {"flow_id": "out2", "amount": str(-(free + 1)), "confirmed_external": True}))
        self.assertIn("CAPITAL_FLOW_POLICY_BREACH:out2", lg.limitations)
        e, st = lg.equity()
        dec = risk_decision(account_revision=lg.revision, episodes={}, run_state="NORMAL", equity=e,
                            valuation_status=st, account_limitations=lg.limitations, unknown_industry_holdings=[],
                            order_state_unknown=False, batch_blocks=[], security_exits={}, no_buy_in_batch=set(),
                            hold_no_add=set(), risk_sell_sids=set(), blocked_exit_value=Fraction(0))
        self.assertIn("CAPITAL_FLOW_POLICY_BREACH", dec["global_buy_blocks"])

    @case("G11-17")
    def test_sold_out_before_ex_keeps_entitlement(self):
        """登记后、除息前全部卖出：日终清理不得删除尚有权益阶段的证券；除息/派息照常入账、补税不丢。"""
        lg, ev = taxed_ledger()
        lg.apply(ev("ORDER_CREATE", sell_order("s", qty=300, trade_date="2026-01-07")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "s", "at": "2026-01-07T10:00:00+08:00"}))
        lg.apply(ev("BROKER_FILL", fill("s", "t2", 300, "10", trade_date="2026-01-07", tax_delivery_date="2026-01-07")))
        lg.apply(ev("EOD", {"date": "2026-01-07"}))
        self.assertIn("X0001.SH", lg.positions)                                  # 未清理
        lg.apply(ev("CA_EX", {"action_id": "div1", "ex_date": "2026-01-08", "reference_price": "9"}))
        self.assertEqual(lg.receivables.get("DIV:div1"), Decimal(300))
        lg.apply(ev("CA_PAY", {"action_id": "div1", "withheld": "0"}))
        self.assertEqual(sum(lg.tax_payables.values()), Decimal(60))             # 1个月内20%补税保留
        lg.apply(ev("EOD", {"date": "2026-01-09"}))
        self.assertNotIn("X0001.SH", lg.positions)                               # 全部阶段完成后才清理

    @case("G11-19", "G11-18")
    def test_withholding_on_unknown_tax_no_phantom_refund(self):
        """税制未知的已转让部分：派息预扣照实作为已付税，不得冲减出虚假的应退税。"""
        lg, ev = new_ledger(fees=test_fees(tax_rules=[]))                         # 无适用税制 → TAX_RULE_MISSING
        lg.apply(ev("ORDER_CREATE", buy_order("b", qty=300)))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "b", "at": "2026-01-05T10:00:00+08:00"}))
        lg.apply(ev("BROKER_FILL", fill("b", "t1", 300, "10")))
        lg.apply(ev("CA_RECORD", {"action_id": "d", "security_id": "X0001.SH", "kind": "CASH_DIVIDEND",
                                  "record_date": "2026-01-06", "security_class": "INDIVIDUAL_A_SHARE",
                                  "dividend_type": "CASH", "cash_per_share": "1", "tax_base_per_share": "1"}))
        lg.apply(ev("ORDER_CREATE", sell_order("s", qty=300, trade_date="2026-01-07")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "s", "at": "2026-01-07T10:00:00+08:00"}))
        lg.apply(ev("BROKER_FILL", fill("s", "t2", 300, "10", trade_date="2026-01-07")))
        lg.apply(ev("CA_EX", {"action_id": "d", "ex_date": "2026-01-08", "reference_price": "9"}))
        lg.apply(ev("CA_PAY", {"action_id": "d", "withheld": "30"}))
        self.assertTrue(all(v >= 0 for v in lg.tax_payables.values()))
        self.assertTrue(any(k.startswith("TAX_UNKNOWN") for k in lg.limitations))

    @case("G11-11", "G11-17")
    def test_sale_tax_preview_matches_booking(self):
        """D07.4/D11.4：卖出预览的新增股息差别税与实际成交入账同一FIFO与取整，且预览不改变账本。"""
        lg, ev = taxed_ledger()
        before = json.dumps(lg.snapshot(), sort_keys=True, default=str)
        self.assertEqual(lg.preview_sale_tax("X0001.SH", 100, "2026-01-07"), Decimal(20))
        self.assertEqual(lg.preview_sale_tax("X0001.SH", 0, "2026-01-07"), Decimal(0))
        self.assertIsNone(lg.preview_sale_tax("X0001.SH", 301, "2026-01-07"))
        self.assertEqual(json.dumps(lg.snapshot(), sort_keys=True, default=str), before)
        lg.apply(ev("ORDER_CREATE", sell_order("s", qty=100, trade_date="2026-01-07")))
        lg.apply(ev("ORDER_SUBMIT", {"client_order_id": "s", "at": "2026-01-07T10:00:00+08:00"}))
        lg.apply(ev("BROKER_FILL", fill("s", "t2", 100, "10", trade_date="2026-01-07", tax_delivery_date="2026-01-07")))
        self.assertEqual(sum(lg.tax_payables.values()), Decimal(20))

    @case("G11-19")
    def test_sell_planner_tax_unknown_not_zero(self):
        """税未知不得按0证明风险目标已满足：目标标为未证实（TAX_UNKNOWN），卖量仍照常推进。"""
        g = QuantityGrid(100, 100, 100, 100, True, 1000000)
        legs = {"A": SellLeg("A", 1000, Fraction(10), Decimal(10), "I1", 1000, g, 1000, Fraction(10000))}
        known = SellPlanner(Fraction(100000), legs, lambda s, x, p: Decimal(0)).run(
            exits=[], singles=["A"], industries=[], total=False)
        unknown = SellPlanner(Fraction(100000), legs, lambda s, x, p: Decimal(0), lambda s, x: None).run(
            exits=[], singles=["A"], industries=[], total=False)
        self.assertEqual(known["sell"], unknown["sell"])
        self.assertEqual(known["unreachable"], [])
        self.assertEqual(unknown["tax_unknown"], ["A"])
        self.assertIn("TAX_UNKNOWN:SINGLE:A", unknown["unreachable"])

    @case("G12-10")
    def test_reader_does_not_truncate(self):
        """只读恢复不修改日志；写锁持有者先截去半行（保留碎片）再追加。"""
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            j.commit({"event_id": "a", "kind": "CAPITAL_FLOW", "payload": {"flow_id": "a", "amount": "100"}})
            j.release()
            with open(Path(d) / "events.jsonl", "ab") as f:
                f.write(b'{"seq":2')
            raw = (Path(d) / "events.jsonl").read_bytes()
            self.assertEqual(len(Journal(d).read_committed()), 1)
            self.assertEqual((Path(d) / "events.jsonl").read_bytes(), raw)
            w = Journal(d)
            w.acquire()
            w.commit({"event_id": "b", "kind": "CAPITAL_FLOW", "payload": {"flow_id": "b", "amount": "100"}})
            w.release()
            self.assertEqual([e["event_id"] for e in Journal(d).read_committed()], ["a", "b"])

    @case("G12-11")
    def test_snapshot_rejects_bad_hwm(self):
        lg, ev = new_ledger()
        lg.apply(ev("EOD", {"date": "2026-01-05"}))
        snap = lg.snapshot()
        snap["state"]["hwm"] = wire_ratio(Fraction(1, 2))
        snap["exact"]["hwm"] = wire_ratio(Fraction(1, 2))
        with self.assertRaises(ContractError) as cm:
            Ledger.restore(snap, test_fees())
        self.assertEqual(cm.exception.code, "EXACT_CACHE_MISMATCH")


class RuleAndSettlementFixes(unittest.TestCase):
    @case("G01-08")
    def test_unknown_settlement_mapping_unsupported(self):
        """H04.1/H04.2：未识别的清算日期映射返回 RULE_UNSUPPORTED，不默认按成交日。"""
        p = test_rulebook_payload()
        p["settlements"][0]["tax_delivery_date_field"] = "SOMEDAY"
        with self.assertRaises(ContractError) as cm:
            RuleBook(p)
        self.assertEqual(cm.exception.code, "RULE_UNSUPPORTED")
        b = build("REPLAY_DAILY_PROXY")
        b["profiles"]["rules"]["payload"]["settlements"][0]["fee_settlement_timing"] = "MONTHLY"
        with self.assertRaises(ContractError):
            load_bundle(b)

    @case("G01-08")
    def test_fee_rule_settlement_reference(self):
        b = build("REPLAY_DAILY_PROXY")
        b["profiles"]["fees"]["payload"]["fee_rules"][0]["settlement_id"] = "NOPE"
        with self.assertRaises(ContractError) as cm:
            load_bundle(b)
        self.assertEqual(cm.exception.code, "PROFILE_REFERENCE_MISMATCH")

    @case("G10-13", "G11-12")
    def test_next_session_fee_and_tax_dates(self):
        """清算合同 NEXT_SESSION：费用先计提为应付、下一市场日按已评金额实扣；税务交割日取下一市场日。"""
        m = StubMarket(n=40)
        r = make_runner(m)
        st = dict(SETTLEMENT, fee_settlement_timing="NEXT_SESSION", tax_delivery_date_field="NEXT_SESSION")
        r.rb = RuleBook(dict(test_rulebook_payload(), settlements=[st]))
        held = {"X0001.SH": 200}
        seed_holdings(r, date(2024, 1, 5), held, 98000)
        modes = {"X0001.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "OFF_POOL"}}
        put_plan(r, date(2024, 1, 5), modes, [], 100000, D0)
        exec_day(r, D0)
        L = r.ledger
        self.assertEqual(len(L.trade_log), 1)
        coid = L.trade_log[0]["order"]
        fee = L.orders[coid].fees_accrued["COMMISSION"]
        self.assertEqual(L.fee_payables[coid], fee)                               # 计提未扣
        cash_before = L.cash
        r.phase_key, r.k, r._qc = "pre", 0, {}
        r._phase_preopen(date(2024, 1, 9))
        self.assertNotIn(coid, L.fee_payables)
        self.assertEqual(L.cash, cash_before - fee)                               # 现金只实扣一次

    @case("G01-06", "G04-10")
    def test_rule_not_public_at_cutoff_is_unknown(self):
        """规则公开时刻晚于信号截止：不得使用未来门槛，池资格为未知。"""
        from smallcap.pool import UNK, eligibility
        from tests.test_g04_g05_pool_factors import BOARDS, CAL, FIN, sf
        p = test_rulebook_payload()
        p["rules"][0] = trade_rule("LATE", public_at="2030-01-01T00:00:00+08:00")
        rb = RuleBook(p)
        cut = sh_dt(date(2024, 1, 5), time(20))
        self.assertEqual(eligibility(sf(1), date(2024, 1, 5), CAL, rb, BOARDS, FIN, cut)[1]["market_value"], UNK)
        with self.assertRaises(ContractError) as cm:
            rb.lookup("SH", "MAIN_SH", date(2024, 1, 5), cut)
        self.assertEqual(cm.exception.code, "RULE_NOT_YET_PUBLIC")


class RuleScopeByRiskStatus(unittest.TestCase):
    """2026-10-19 用户确认：同期多套规则按证券当时的风险状态选择（风险警示股另有涨跌幅）。"""

    def payload(self, scope_normal="NORMAL"):
        p = test_rulebook_payload()
        normal = trade_rule("MAIN-NORMAL", up="0.10", down="0.10")
        normal["security_scope_ref"] = scope_normal
        st = trade_rule("MAIN-ST", up="0.05", down="0.05")
        st["security_scope_ref"] = "RISK_WARNING"
        p["rules"] = [r for r in p["rules"] if r["board"] != "MAIN_SH"] + [normal, st]
        return p

    @case("G01-08", "G08-14")
    def test_scope_selected_by_risk_status(self):
        rb = RuleBook(self.payload())
        self.assertEqual(rb.lookup("SH", "MAIN_SH", D0, risk_status="NORMAL").rule_id, "MAIN-NORMAL")
        self.assertEqual(rb.lookup("SH", "MAIN_SH", D0, risk_status="ST").rule_id, "MAIN-ST")
        with self.assertRaises(ContractError) as cm:
            rb.lookup("SH", "MAIN_SH", D0, risk_status=None)                     # 状态未知不猜
        self.assertEqual(cm.exception.code, "RULE_UNKNOWN")
        m = StubMarket(n=40)
        r = make_runner(m)
        r.rb = rb
        m.risk.append({"security_id": "X0002.SH", "status": "ST", "effective": date(2024, 1, 2),
                       "available_at": sh_dt(date(2024, 1, 2), time(18))})
        bar = m.bar("X0001.SH", D0)
        self.assertEqual(r.bounds("X0001.SH", D0, bar)[2], Decimal("11.00"))     # 普通股 10%
        self.assertEqual(r.bounds("X0002.SH", D0, bar)[2], Decimal("10.50"))     # 风险警示股 5%

    @case("G01-08")
    def test_unrecognized_overlapping_scope_rejected(self):
        b = build("REPLAY_DAILY_PROXY")
        b["profiles"]["rules"]["payload"] = self.payload(scope_normal="MAIN_BOARD_ORDINARY")
        with self.assertRaises(ContractError) as cm:
            load_bundle(b)
        self.assertEqual(cm.exception.code, "RULE_SCOPE_UNSUPPORTED")
        b["profiles"]["rules"]["payload"] = self.payload()
        load_bundle(b)


class RiskAndPlanFixes(unittest.TestCase):
    @case("G08-15")
    def test_holding_mv_floor_triggers_security_exit(self):
        """V5 §7.2：持仓适用市值底线失效 → SECURITY_EXIT（MV_FLOOR_FAIL），与价格<2同一处理。"""
        m = StubMarket(n=40)
        r = make_runner(m)
        p = test_rulebook_payload()
        p["rules"][0] = trade_rule("FLOOR", floor_status="VALUE", floor="500000000")
        r.rb = RuleBook(p)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 200}, 98000)
        m.master["X0001.SH"]["shares"] = 10_000_000                              # 收盘总市值 1 亿 < 5 亿
        close_day(r, D0)
        eps = [e for e in r.ledger.episodes.values() if e["type"] == "SECURITY_EXIT"]
        self.assertEqual([(e["key"], e["detail"]) for e in eps], [("X0001.SH", "MV_FLOOR_FAIL")])

    @case("G08-18")
    def test_security_exit_detected_when_insolvent(self):
        v = RiskView(Fraction(-1), {"A": Fraction(100)}, {"A": "I1"}, {"A": 100}, risk_facts={"A": "ST"})
        s, e, f = detect(v, {}, at="t", first_eligible="d", seq=1)
        self.assertEqual([(x["type"], x["key"]) for x in s], [("SECURITY_EXIT", "A")])

    @case("G06-05", "G06-07")
    def test_exit_with_open_sell_occupies_slot(self):
        """D06.2：未结束卖单占用槽位；名单层只可假设未冻结、无在途卖单的计划退出完成。"""
        p = pool(400)
        held = [hv(s, 2000) for s in p["security_ids"][:29]] + [hv("Y0999.SH", 2000)]
        free = plan(holdings=held, p=p)
        self.assertEqual(free["free_slots_planned"], 1)
        held[-1].open_sell = True
        busy = plan(holdings=held, p=p)
        self.assertEqual(busy["free_slots_planned"], 0)
        self.assertEqual(busy["buy_candidates_ordered"], [])

    @case("G06-01")
    def test_industry_planning_uses_planned_values(self):
        """D06.2 第6步：行业20%用规划后保留市值（越带老仓按固定2%中心金额）检查。"""
        ids = [f"X{i + 1:04d}.SH" for i in range(400)]
        inds = {s: ("IX" if i < 10 else f"I{i % 9:02d}") for i, s in enumerate(ids)}
        p = pool(400, inds)
        olds = ids[1:10]
        # 同业9只老仓各2.6%（越带→TARGET_AMOUNT 2%）：规划后9×2%=18%，再加新股2%=20%（含等号）可买；
        # 若误用当前市值 9×2.6%=23.4% 则会错误跳过排名第一的同业新股
        held = [hv(s, 2600, ind="IX") for s in olds]
        out = plan(holdings=held, p=p)
        self.assertTrue(all(out["modes"][s]["mode"] == "TARGET_AMOUNT" for s in olds))
        self.assertIn(ids[0], out["buy_candidates_ordered"])

    @case("G06-07")
    def test_slots_exceeded_records_and_blocks_adds(self):
        p = pool(400)
        held = [hv(s, 2000) for s in p["security_ids"][:31]]
        out = plan(holdings=held, p=p)
        self.assertTrue(out["slots_exceeded"])
        self.assertFalse(out["ordinary_buy_allowed"])
        self.assertEqual(out["score_reason"], "SLOTS_EXCEEDED")
        self.assertEqual(len([m for m in out["modes"].values() if m["mode"] != "EXIT"]), 31)   # 不删权益

    @case("G10-18")
    def test_cost_unfundable_sell_skipped(self):
        """D11.3：卖出税费超过回款与可用现金 → COST_UNFUNDABLE，不暗中融资。"""
        m = StubMarket(n=40, price="3")
        r = make_runner(m)
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 1}, 0, mark="3")        # 1股零股全卖：回款<最低佣金5元
        modes = {"X0001.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "OFF_POOL"}}
        put_plan(r, date(2024, 1, 5), modes, [], 100000, D0)
        exec_day(r, D0)
        self.assertEqual(r.ledger.trade_log, [])
        self.assertIn({"kind": "SELL_SKIPPED", "security_id": "X0001.SH", "reason": "COST_UNFUNDABLE"},
                      r.ledger.decision_log)


class ForwardFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from smallcap.snapshot import SnapshotMarket, export_snapshot
        from smallcap.synthetic import SyntheticMarket
        m = SyntheticMarket(seed=5, n=300, start=date(2022, 1, 3), end=date(2024, 1, 31), with_events=False)
        cls.snap = SnapshotMarket(json.loads(json.dumps(export_snapshot(m, date(2022, 6, 1), date(2024, 1, 31)))))

    def bundle(self, **boot):
        b = build("FORWARD_MANUAL")
        b["profiles"]["bootstrap"]["payload"].update({"cash_amount": "500000", **boot})
        return load_bundle(b)

    @case("G09-11", "G12-15")
    def test_rejected_broker_row_not_committed_and_recoverable(self):
        """回报先在副本上校验：被拒的确认回报不落盘（否则日志无法重放）；冲突以 LIMITATION 事件持久化。"""
        b = self.bundle()
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            fs = ForwardSession(b, j, self.snap)
            fs.step(date(2024, 1, 5), "INIT")
            fs.step(date(2024, 1, 8), "PREOPEN")
            cl = fs.step(date(2024, 1, 8), "EXEC")["checklist"]
            L = fs.runner.ledger
            o = cl[0]
            rows = [{"kind": "MANUAL_SUBMITTED", "client_order_id": o["client_order_id"],
                     "occurred_at": "2024-01-08T10:01:00+08:00"},
                    {"kind": "FILL", "client_order_id": "NO_SUCH_ORDER", "trade_id": "T-X", "qty": 100, "price": "10",
                     "trade_date": "2024-01-08", "occurred_at": "2024-01-08T10:01:20+08:00",
                     "sellable_from": "2024-01-09"}]
            res = import_broker_events(L, j, rows)
            self.assertEqual(res["applied"], 1)
            self.assertEqual([c["code"] for c in res["conflicts"]], ["UNKNOWN_ORDER"])
            self.assertIn("EVENT_CONFLICT:broker/FILL/T-X", L.limitations)
            kinds = [e["kind"] for e in j.read_committed()]
            self.assertNotIn("BROKER_FILL", kinds)
            j.release()
            j2 = Journal(d)
            j2.acquire()
            lg2 = recover(j2, "x", fs.runner.fees, use_snapshot=False)            # 完整重放成功
            self.assertIn("EVENT_CONFLICT:broker/FILL/T-X", lg2.limitations)
            self.assertEqual(lg2.orders[o["client_order_id"]].status, "SENT_UNKNOWN")
            j2.release()

    @case("G12-15")
    def test_reconcile_limitations_survive_recovery(self):
        b = self.bundle()
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            fs = ForwardSession(b, j, self.snap)
            fs.step(date(2024, 1, 5), "INIT")
            L = fs.runner.ledger
            bad = reconcile_account(L, {"positions": {"Z9999.SH": {"volume": 100}}, "cash_book": str(L.cash)}, None, j,
                                    "2024-01-06T09:00:00+08:00")
            self.assertEqual(bad.codes(), ["POSITION_MISMATCH"])
            j.release()
            j2 = Journal(d)
            j2.acquire()
            lg2 = recover(j2, "x", fs.runner.fees, use_snapshot=False)
            self.assertIn("POSITION_MISMATCH:Z9999.SH", lg2.limitations)
            ok = reconcile_account(lg2, {"positions": {}, "cash_book": str(lg2.cash)}, None, j2,
                                   "2024-01-06T10:00:00+08:00")
            self.assertTrue(ok.ok)
            j2.release()
            self.assertNotIn("POSITION_MISMATCH:Z9999.SH", recover(Journal(d), "x", fs.runner.fees).limitations)

    def test_existing_account_bootstrap_import_then_init(self):
        """D16.2：已有真实账户接入另走 Bootstrap：先导入人工核对的 BOOTSTRAP_ACCOUNT，再 INIT（不入金、不做全现金适配）。"""
        b = self.bundle(mode="EXISTING_MANUAL_ACCOUNT", cash_amount=None, positions_ref="manual/positions.json",
                        tax_lots_ref="manual/lots.json")
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            fs = ForwardSession(b, j, self.snap)
            with self.assertRaises(ContractError):
                fs.step(date(2024, 1, 5), "INIT")                                 # 未导入 Bootstrap
            sid = sorted(self.snap.master)[0]
            row = {"kind": "BOOTSTRAP_ACCOUNT", "bootstrap_id": "bs1", "occurred_at": "2024-01-05T19:00:00+08:00",
                   "cash": "400000", "evidence_ref": "TEST_ONLY_STATEMENT",
                   "positions": [{"security_id": sid, "market": self.snap.master[sid]["exchange"],
                                  "board": self.snap.master[sid]["board"], "industry": self.snap.industries[sid],
                                  "quantity": 1000, "mark": str(self.snap.bar(sid, date(2024, 1, 5))["close"]),
                                  "mark_date": "2024-01-05", "acquisition_tax_date": "2023-06-01"}]}
            res = import_broker_events(fs.runner.ledger, j, [row])
            self.assertEqual(res["applied"], 1)
            out = fs.step(date(2024, 1, 5), "INIT")
            self.assertEqual(out["startup"]["status"], "STARTED")
            kinds = [e["kind"] for e in j.read_committed()]
            self.assertNotIn("CAPITAL_FLOW", kinds)
            self.assertEqual(fs.runner.ledger.positions[sid].quantity, 1000)
            j.release()

    def test_final_intents_and_status_mapping(self):
        """H06.2 FinalIntent 字段齐全且 prohibit_buy 为裁决与计划禁买并集；券商状态映射未知时不释放资源。"""
        from smallcap.forward import map_broker_status_rows
        b = self.bundle()
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            fs = ForwardSession(b, j, self.snap)
            fs.step(date(2024, 1, 5), "INIT")
            fs.step(date(2024, 1, 8), "PREOPEN")
            ex = fs.step(date(2024, 1, 8), "EXEC")
            fi = ex["final_intents"]
            need = {"security_id", "ordinary_mode", "ordinary_amount_or_quantity", "max_remaining_shares_or_amount",
                    "risk_episode_ids", "priority", "prohibit_buy", "valid_window", "cap_reference_date",
                    "reason_codes", "account_revision", "quote_basis", "plan_id"}
            self.assertTrue(fi and all(need <= set(x) for x in fi))
            buys = {c["security_id"] for c in ex["checklist"] if c["side"] == "BUY"}
            self.assertTrue(all(not x["prohibit_buy"] for x in fi if x["security_id"] in buys))
            o = ex["checklist"][0]
            smap = {"50": "SUBMITTED", "56": "FILLED", "54": "CANCELED"}
            rows = map_broker_status_rows([
                {"kind": "MANUAL_SUBMITTED", "client_order_id": o["client_order_id"],
                 "occurred_at": "2024-01-08T10:01:00+08:00"},
                {"kind": "ORDER_STATUS", "broker_status": "50", "client_order_id": o["client_order_id"],
                 "broker_event_id": "s1", "cum_qty": 0, "cum_value": "0", "source_revision": 1,
                 "occurred_at": "2024-01-08T10:01:05+08:00"},
                {"kind": "ORDER_STATUS", "broker_status": "99", "client_order_id": o["client_order_id"],
                 "broker_event_id": "s2", "cum_qty": 0, "cum_value": "0", "source_revision": 2,
                 "occurred_at": "2024-01-08T10:02:00+08:00"}], smap)
            L = fs.runner.ledger
            res = import_broker_events(L, j, rows)
            self.assertEqual([c["code"] for c in res["conflicts"]], ["BROKER_STATUS_UNMAPPED"])
            self.assertEqual(L.orders[o["client_order_id"]].status, "SENT_UNKNOWN")   # 未知状态不释放预约
            with self.assertRaises(ContractError):
                map_broker_status_rows([], {"x": "DONE"})
            j.release()

    @case("G16-15")
    def test_retirement_history_unchecked_blocks_forward(self):
        b = self.bundle(retirement_history_checked=False)
        self.assertIn("RETIREMENT_HISTORY_UNCHECKED", [r.code for r in forward_preconditions(b)])
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            with self.assertRaises(ContractError) as cm:
                ForwardSession(b, j, self.snap)
            self.assertIn("RETIREMENT_HISTORY_UNCHECKED", str(cm.exception))
            j.release()

    @case("G09-20")
    def test_manual_existing_open_sell_not_duplicated(self):
        """人工前向跨日：已有未结卖单的证券不再新建同向/反向委托，D09.4 取消只进入人工清单。"""
        m = StubMarket(n=40)
        r = make_runner(m, mode="FORWARD_MANUAL")
        seed_holdings(r, date(2024, 1, 5), {"X0001.SH": 300}, 97000)
        r.phase_key, r.k = "old", 0
        r.emit("ORDER_CREATE", sell_order("old-s", qty=100, trade_date="2024-01-05", limit="9.97"), sh_dt(D0, time(8)))
        modes = {"X0001.SH": {"mode": "EXIT", "target_amount": "0", "prohibit_buy": True, "reason": "OFF_POOL"}}
        put_plan(r, date(2024, 1, 5), modes, [], 100000, D0)
        exec_day(r, D0)                                                           # 不抛 DUPLICATE_OPEN_ORDER
        L = r.ledger
        self.assertEqual([o.client_order_id for o in L.open_orders("X0001.SH")], ["old-s"])
        self.assertIn({"kind": "SELL_SKIPPED", "security_id": "X0001.SH", "reason": "OPEN_ORDER_EXISTS"},
                      L.decision_log)


class BindingAndMatrixFixes(unittest.TestCase):
    @case("G12-11")
    def test_resume_with_different_study_refused(self):
        m = StubMarket(n=40, end=date(2024, 2, 9))
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.acquire()
            r = make_runner(m, capital="500000", journal=j)
            r.run()
            j.release()
            j2 = Journal(d)
            j2.acquire()
            spec = CellSpec("t", "B1", "100000", 2024, "S0", m.data_end, "REPLAY_DAILY_PROXY", r.spec.package)
            with self.assertRaises(ContractError) as cm:
                resume_cell(spec, m, r.rb, r.fees, j2)
            self.assertEqual(cm.exception.code, "STUDY_DEFINITION_MISMATCH")
            j2.release()

    @case("G15-02")
    def test_matrix_blocks_and_cell_errors_recorded(self):
        """D16.2/H08.2：D晚于数据终点为矩阵阻断；单格业务错误（如因子实现缺失）记录为该格证据不足，不中断矩阵。"""
        from smallcap.synthetic import SyntheticMarket
        b = build("REPLAY_DAILY_PROXY")
        b["study"].update(end_date="2024-03-29", capital_mode="ACTUAL_ONE", capitals=["500000"])
        mk = lambda: SyntheticMarket(seed=5, n=300, start=date(2022, 1, 3), end=date(2024, 3, 29), with_events=False)
        late = copy.deepcopy(b)
        late["study"]["end_date"] = "2024-06-28"
        res = run_matrix(load_bundle(late), market=mk())
        self.assertIn("END_AFTER_DATA", res["matrix_blocks"])
        bad = copy.deepcopy(b)
        bad["profiles"]["factor"]["payload"]["factors"][0]["implementation_id"] = "NOT_REGISTERED"
        with tempfile.TemporaryDirectory() as d:
            res = run_matrix(load_bundle(bad), market=mk(), out_dir=d)
            recs = {r["role"]: r for r in res["records"] if r["start"] == "A3" and r["scenario"] == "S0"}
            self.assertEqual(recs["B1"]["startup_status"], "NOT_STARTED_UNKNOWN")
            self.assertIn("FACTOR_IMPLEMENTATION_MISSING", recs["B1"]["startup_reason"])
            b0 = recs["B0"]
            self.assertEqual(b0["startup_status"], "STARTED")
            self.assertTrue(Path(b0["ledger_path"]).exists())
            self.assertTrue(Path(b0["plan_path"]).exists())
            self.assertIsNone(b0["event_path"])                                   # 未要求持久化完整事件
            self.assertEqual(b0["first_execution"], "2024-01-08")
            self.assertGreaterEqual(Decimal(b0["average_names"]), 24)
            self.assertEqual(b0["input_versions"]["rulebook_id"], "rulebook-test")
            self.assertIsNotNone(b0["exposure"]["mv_geometric_mean"])
            self.assertIn("2024", b0["annual_returns"])


class EvaluationFixes(unittest.TestCase):
    @case("G16-01")
    def test_annual_returns_chain(self):
        s = [("2024-01-05", Fraction(1)), ("2024-12-31", Fraction(9, 10)), ("2025-06-30", Fraction(1)),
             ("2025-12-31", Fraction(99, 100))]
        self.assertEqual(annual_returns(s), {"2024": "-0.10000000", "2025": "0.10000000"})

    @case("G15-17")
    def test_provisional_valuation_decisive(self):
        """D12.3：暂估换成下界后可能跨越25%/30%门槛 → 决定性证据不足；远离门槛则不影响。"""
        def pt(nav, prov=Fraction(0), lo=Fraction(0)):
            return {"date": "d", "nav": nav, "units": Fraction(100000), "equity": nav * 100000, "hwm": Fraction(1),
                    "provisional_value": prov, "provisional_lo": lo}
        near = [pt(Fraction(1)), pt(Fraction(78, 100), prov=Fraction(4000))]     # 下界0 → DD 26%
        self.assertEqual(provisional_decisive(near, Fraction(22, 100), False),
                         ["PROVISIONAL_VALUATION_DECISIVE:RETIREMENT_25"])
        far = [pt(Fraction(1)), pt(Fraction(95, 100), prov=Fraction(2000))]
        self.assertEqual(provisional_decisive(far, Fraction(5, 100), False), [])

    def test_build_facts_trail(self):
        """build-facts：QMT优先；缺QMT用同口径Tushare替代；同源同版本不同值为冲突；未映射行报原因。"""
        maps = [{"canonical_field": "close_raw", "provider": "QMT", "endpoint": "kline", "source_field": "close",
                 "source_unit": "CNY", "canonical_unit": "CNY", "multiply": "1", "basis_id": "RAW",
                 "historical_semantics": "HISTORICAL", "scope_ref": "t", "evidence_ref": "TEST_ONLY"},
                {"canonical_field": "close_raw", "provider": "TUSHARE", "endpoint": "daily", "source_field": "close",
                 "source_unit": "CNY", "canonical_unit": "CNY", "multiply": "1", "basis_id": "RAW",
                 "historical_semantics": "HISTORICAL", "scope_ref": "t", "evidence_ref": "TEST_ONLY"}]
        at = "2024-01-05T16:00:00+08:00"
        rows = [{"record_id": "q1", "provider": "QMT", "endpoint": "kline", "source_field": "close",
                 "security_id": "A", "period": "2024-01-05", "value": "10.00", "available_at": at},
                {"record_id": "t1", "provider": "TUSHARE", "endpoint": "daily", "source_field": "close",
                 "security_id": "A", "period": "2024-01-05", "value": "10.00", "available_at": at},
                {"record_id": "t2", "provider": "TUSHARE", "endpoint": "daily", "source_field": "close",
                 "security_id": "B", "period": "2024-01-05", "value": "8.00", "available_at": at},
                {"record_id": "q3", "provider": "QMT", "endpoint": "kline", "source_field": "close",
                 "security_id": "C", "period": "2024-01-05", "value": "5.00", "available_at": at},
                {"record_id": "q4", "provider": "QMT", "endpoint": "kline", "source_field": "close",
                 "security_id": "C", "period": "2024-01-05", "value": "5.10", "available_at": at},
                {"record_id": "x", "provider": "TUSHARE", "endpoint": "nope", "source_field": "close",
                 "security_id": "D", "period": "2024-01-05", "value": "1", "available_at": at}]
        maps.append(dict(maps[0], provider="TEST", endpoint="synthetic"))
        rows += [{"record_id": "e1", "provider": "TEST", "endpoint": "synthetic", "source_field": "close",
                  "security_id": "E", "period": "2024-01-05", "value": "7.00", "available_at": at},
                 {"record_id": "e2", "provider": "TEST", "endpoint": "synthetic", "source_field": "close",
                  "security_id": "A", "period": "2024-01-05", "value": "10.00", "available_at": at}]
        obs, dec = build_facts(rows, maps)
        by = {d.get("security_id", d.get("record_id")): d for d in dec}
        self.assertEqual((by["E"]["source"], by["E"]["value"]), ("TEST", "7.00"))   # 仅夹具来源：如实标 TEST
        self.assertEqual(by["A"]["reason_code"], "TEST_SOURCE_MIXED_WITH_REAL")     # 夹具不得与真实来源混用
        rows = rows[:-1]
        obs, dec = build_facts(rows, maps)
        by = {d.get("security_id", d.get("record_id")): d for d in dec}
        self.assertEqual((by["A"]["source"], by["A"]["action"]), ("QMT", "PRIMARY"))
        self.assertEqual((by["B"]["source"], by["B"]["action"]), ("TUSHARE", "SUBSTITUTE"))
        self.assertEqual(by["C"]["action"], "CONFLICT")
        self.assertEqual(by["x"]["reason_code"], "MAPPING_MISSING")


if __name__ == "__main__":
    unittest.main()
