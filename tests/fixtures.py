"""TEST_ONLY 夹具：虚构证券 X0001.SH 等、零滑点/万2.5最低5元佣金、主板100股步长。

这些不是真实市场事实、券商合同或历史法定费率，不得复制成真实 Profile。
"""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from smallcap.account import Ledger
from smallcap.clock import TradingCalendar
from smallcap.fees import FeeSchedule
from smallcap.rules import RuleBook

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "handoff" / "小市值周频策略_V5D2"
GOLDEN = json.loads((HANDOFF / "verification" / "golden_vectors.json").read_text(encoding="utf-8"))
GOLDEN_BY_ID = {c["id"]: c for c in GOLDEN["cases"]}
TAX_FIXTURE = json.loads((HANDOFF / "inputs" / "dividend_tax_rules.test_fixture.json").read_text(encoding="utf-8"))
ENGINEERING_BUNDLE = json.loads((HANDOFF / "inputs" / "engineering_inputs.example.json").read_text(encoding="utf-8"))
EXTERNAL_TEMPLATE = json.loads((HANDOFF / "inputs" / "external_inputs.template.json").read_text(encoding="utf-8"))


def golden(cid: str) -> dict:
    return copy.deepcopy(GOLDEN_BY_ID[cid])


def fee_rule(kind, rate="0", minimum="0", side="BOTH", market="BOTH", start="2000-01-01", end="2100-01-01",
             base="NOTIONAL", unit="ORDER_CUMULATIVE", included=False, fid=None):
    return {"fee_id": fid or f"T-{kind}-{market}-{side}-{start}", "fee_kind": kind, "effective_from": start,
            "effective_to": end, "market": market, "side": side, "base_kind": base, "rate": rate, "minimum": minimum,
            "maximum": None, "included_in_commission": included, "charge_unit": unit, "round_quantum": "0.01",
            "round_mode": "HALF_UP", "settlement_id": "TEST-SETTLE", "evidence_ref": "TEST_ONLY"}


def test_fee_payload(stamp_rate="0", transfer_rate="0", tax_rules=None, extra_rules=()):
    rules = [fee_rule("COMMISSION", "0.00025", "5"),
             fee_rule("STAMP", stamp_rate, side="SELL"),
             fee_rule("STAMP", "0", side="BUY", fid="T-STAMP-BUY"),
             fee_rule("TRANSFER", transfer_rate)] + list(extra_rules)
    return {"fee_profile_id": "fees-test", "purpose": "TEST_ONLY", "fee_rules": rules,
            "dividend_tax_rules": copy.deepcopy(tax_rules if tax_rules is not None else TAX_FIXTURE["rules"]),
            "research_commission_rate": "0.00025", "research_minimum_commission": "5", "broker_contract_ref": None}


def test_fees(**kw) -> FeeSchedule:
    return FeeSchedule(test_fee_payload(**kw))


def trade_rule(rule_id, market="SH", board="MAIN_SH", start="2000-01-01", end="2100-01-01", buy_min=100, buy_step=100,
               sell_min=100, sell_step=100, method="PERCENT_REFERENCE", up="0.10", down="0.10",
               floor_status="NOT_APPLICABLE", floor=None, public_at="1999-12-01T00:00:00+08:00"):
    return {"rule_id": rule_id, "market": market, "board": board, "security_scope_ref": "TEST_SCOPE",
            "effective_from": start, "effective_to": end, "public_at": public_at,
            "quantity_grid": {"buy_min": buy_min, "buy_step": buy_step, "sell_min": sell_min, "sell_step": sell_step,
                              "allow_full_odd_exit": True, "max_order_quantity": 1000000},
            "price_rule": {"tick": "0.01", "method": method, "reference_field": "reference_preclose",
                           "up_percent": up if method == "PERCENT_REFERENCE" else None,
                           "down_percent": down if method == "PERCENT_REFERENCE" else None,
                           "bound_data_ref": None,
                           "bound_rounding": "HALF_UP_TICK" if method == "PERCENT_REFERENCE" else "NOT_APPLICABLE",
                           "intraday_band_id": None},
            "settlement_id": "TEST-SETTLE", "marketcap_floor": floor, "marketcap_floor_status": floor_status,
            "evidence_ref": "TEST_ONLY"}


SETTLEMENT = {"settlement_id": "TEST-SETTLE", "cash_core_mode": "TRADE_CASH_BOOK", "buy_sellable_delay_sessions": 1,
              "buy_sellable_release_time": "09:15:00", "sell_proceeds_trade_delay_sessions": 0,
              "sell_proceeds_withdraw_delay_sessions": 1, "sell_proceeds_release_time": "15:00:00",
              "tax_acquisition_date_field": "TRADE_DATE", "tax_delivery_date_field": "TRADE_DATE",
              "fee_settlement_timing": "WITH_TRADE", "evidence_ref": "TEST_ONLY"}


def test_rulebook_payload(extra=()):
    rules = [trade_rule("T-MAIN-SH"), trade_rule("T-MAIN-SZ", market="SZ", board="MAIN_SZ"),
             trade_rule("T-STAR", board="STAR", buy_min=200, buy_step=1, sell_min=200, sell_step=1, up="0.20",
                        down="0.20"),
             trade_rule("T-CHINEXT", market="SZ", board="CHINEXT", up="0.20", down="0.20")] + list(extra)
    return {"rulebook_id": "rulebook-test", "rules": rules, "settlements": [copy.deepcopy(SETTLEMENT)]}


def test_rulebook() -> RuleBook:
    return RuleBook(test_rulebook_payload())


def weekday_calendar(start=date(2009, 1, 1), end=date(2027, 12, 31), holidays=()) -> TradingCalendar:
    """合成日历：工作日减去给定假日。TEST_ONLY，不是经核验的交易所日历。"""
    hol = set(holidays)
    days, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in hol:
            days.append(d)
        d += timedelta(days=1)
    return TradingCalendar(days, known_through=end, evidence="TEST_ONLY")


class EventFactory:
    """生成带递增 event_id 的已确认事件。"""

    def __init__(self, prefix="ev"):
        self.n = 0
        self.prefix = prefix

    def __call__(self, kind, payload, at="2026-01-05T10:00:00+08:00", eid=None):
        self.n += 1
        return {"event_id": eid or f"{self.prefix}-{self.n:05d}", "kind": kind, "occurred_at": at, "known_at": at,
                "payload": payload}


def new_ledger(cash="100000", fees=None, account_id="acct-test", etf_ids=()):
    lg = Ledger(account_id, fees if fees is not None else test_fees(), etf_ids)
    ev = EventFactory("init")
    lg.apply(ev("CAPITAL_FLOW", {"flow_id": "init", "amount": cash}))
    return lg, ev


def buy_order(coid, sid="X0001.SH", qty=300, limit="10", trade_date="2026-01-05", kind="ORDINARY", market="SH",
              board="MAIN_SH", capital_revision=1, industry="I00", batch="b1", plan="p1"):
    return {"order": {"client_order_id": coid, "batch_id": batch, "plan_id": plan, "security_id": sid,
                      "market": market, "board": board, "side": "BUY", "qty": qty, "limit": limit, "kind": kind,
                      "trade_date": trade_date, "capital_revision": capital_revision,
                      "window_end": f"{trade_date}T10:30:00+08:00", "industry": industry}}


def sell_order(coid, sid="X0001.SH", qty=300, limit="10", trade_date="2026-01-05", kind="RISK", market="SH",
               board="MAIN_SH", capital_revision=1, upper="11.00", batch="b1", plan="p1"):
    return {"order": {"client_order_id": coid, "batch_id": batch, "plan_id": plan, "security_id": sid,
                      "market": market, "board": board, "side": "SELL", "qty": qty, "limit": limit, "kind": kind,
                      "trade_date": trade_date, "capital_revision": capital_revision,
                      "window_end": f"{trade_date}T10:30:00+08:00", "upper_price_bound": upper}}


def fill(coid, key, qty, price, trade_date="2026-01-05", sellable_from="2026-01-06", at="2026-01-05T10:00:30+08:00",
         received=None, **extra):
    p = {"client_order_id": coid, "trade_key": key, "qty": qty, "price": price, "amount": None,
         "occurred_at": at, "received_at": received or at, "trade_date": trade_date, "sellable_from": sellable_from,
         "fee_settlement": "WITH_TRADE"}
    p.update(extra)
    return p


def D(x) -> Decimal:
    return Decimal(str(x))


# ---------------------------------------------------------------- 场景夹具：可控 StubMarket
from datetime import datetime, time as _time  # noqa: E402

from smallcap.contracts import sh_dt  # noqa: E402


class StubMarket:
    """TEST_ONLY 可控市场：默认每股价格不变、成交额充足；可逐日覆盖 bar 字段、风险事实与公司行动。"""

    def __init__(self, n=40, start=date(2022, 1, 3), end=date(2024, 3, 29), price="10", shares=100_000_000,
                 amount="50000000", industry_of=None, board_of=None, holidays=()):
        self.calendar = weekday_calendar(start, end + timedelta(days=40), holidays)
        self.data_end = end
        self.price = Decimal(price)
        self.amount = Decimal(amount)
        self.master = {}
        for i in range(n):
            sid = f"X{i + 1:04d}.SH"
            board = (board_of or {}).get(sid, "MAIN_SH")
            self.master[sid] = {"security_id": sid, "trade_code": sid, "exchange": "SH", "board": board,
                                "list_date": start, "security_type": "A_ORDINARY",
                                "industry": (industry_of or {}).get(sid, f"I{i % 10:02d}"), "shares": shares}
        self.overrides: dict = {}
        self.fvals: dict = {}             # sid -> Decimal|None （默认按编号）
        self.fvals_by_date: dict = {}     # (sid, date) -> value
        self.risk: list = []
        self.actions: list = []
        self.lifecycle_events: list = []
        self.minute_override: dict = {}

    def universe(self):
        return sorted(self.master)

    def set_bar(self, sid, d, **kw):
        self.overrides.setdefault((sid, d), {}).update({k: (Decimal(str(v)) if k in ("open", "high", "low", "close",
                                                                                     "amount", "ref_preclose") else v)
                                                        for k, v in kw.items()})

    def _close(self, sid, d):
        o = self.overrides.get((sid, d), {})
        return o.get("close", self.price)

    def bar(self, sid, d):
        if not self.calendar.is_session(d) or d < self.master[sid]["list_date"]:
            return None
        o = self.overrides.get((sid, d), {})
        if o.get("missing"):
            return None
        i = self.calendar.index(d)
        prev = self.calendar.sessions[i - 1] if i > 0 else d
        ref = o.get("ref_preclose", self._close(sid, prev))
        c = o.get("close", self.price)
        b = {"open": o.get("open", c), "high": o.get("high", max(c, o.get("open", c))),
             "low": o.get("low", min(c, o.get("open", c))), "close": c, "volume": 1_000_000,
             "amount": o.get("amount", self.amount), "status": o.get("status", "TRADING"), "ref_preclose": ref,
             "mv": c * self.master[sid]["shares"]}
        if b["status"] == "SUSPENDED":
            b["amount"] = Decimal(0)
        return b

    def industry(self, sid, d):
        return self.master[sid]["industry"]

    def risk_status(self, sid, d, asof):
        st = "NORMAL"
        for e in self.risk:
            if e["security_id"] == sid and e["available_at"] <= asof and e["effective"] <= d and \
                    (e.get("end") is None or d < e["end"]):
                st = e["status"]
        return st

    def factor_value(self, sid, d):
        if (sid, d) in self.fvals_by_date:
            v = self.fvals_by_date[(sid, d)]
        else:
            v = self.fvals.get(sid, Decimal(1000 - int(sid[1:5])))
        return None if v is None else ("VALID", Decimal(v))

    def action_terms(self, a):
        return a

    def minute_bars(self, sid, d):
        b = self.bar(sid, d)
        if b is None or b["status"] != "TRADING":
            return []
        out = []
        for k in range(-1, 30):
            st = datetime.combine(d, _time(10, 0)) + timedelta(minutes=k)
            ov = self.minute_override.get((sid, d, k), {})
            p = ov.get("open", b["open"])
            out.append({"start": st, "open": p, "high": ov.get("high", p + Decimal("0.05")),
                        "low": ov.get("low", p - Decimal("0.05")),
                        "close": ov.get("close", p), "volume": ov.get("volume", 1_000_000)})
        return out


def stub_tax_rules():
    return [{"rule_id": "TEST_ALL", "effective_from": "2000-01-01", "effective_to": "2100-01-01",
             "security_class": "INDIVIDUAL_A_SHARE", "dividend_type": "CASH", "initial_withholding_rate": "0",
             "tiers": [{"max_calendar_months_inclusive": 1, "rate": "0.20"},
                       {"max_calendar_months_inclusive": 12, "rate": "0.10"},
                       {"max_calendar_months_inclusive": None, "rate": "0"}],
             "tax_base_definition": "TEST", "acquisition_inheritance_rule": "INHERIT_PARENT",
             "rounding_unit": "ENTITLEMENT", "evidence_ref": "TEST_ONLY", "selection_date_basis": "EQUITY_RECORD_DATE"}]


def test_package():
    return copy.deepcopy(ENGINEERING_BUNDLE["profiles"]["factor"]["payload"])


def make_runner(market, role="B1", scenario="S0", capital="100000", start_year=2024, end=None,
                mode="REPLAY_DAILY_PROXY", fees=None, journal=None, crash_after=None):
    from smallcap.replay import CellRunner, CellSpec
    spec = CellSpec("t", role, capital, start_year, scenario, end or market.data_end, mode,
                    test_package() if role != "B0" else None)
    return CellRunner(spec, market, test_rulebook(), fees or test_fees(tax_rules=stub_tax_rules()), journal, crash_after)


def ev_at(d, hh=9, mm=0):
    return sh_dt(d, _time(hh, mm))


def seed_holdings(runner, d, holdings, cash, mark="10"):
    """用 Bootstrap 事件建立已核对的初始账户（不经过买入）。holdings: {sid: qty}。"""
    m = runner.m
    runner.phase_key = f"seed/{d}"
    runner.k = 0
    runner.emit("BOOTSTRAP_ACCOUNT", {"cash": str(cash), "positions": [
        {"security_id": s, "market": "SH", "board": m.master[s]["board"], "industry": m.master[s]["industry"],
         "quantity": q, "mark": str(mark), "mark_date": d.isoformat(), "acquisition_tax_date": "2023-01-03"}
        for s, q in holdings.items()]}, ev_at(d, 8))
    runner.emit("RUN_STATE", {"to": "NORMAL", "reason": "TEST_SEED"}, ev_at(d, 8))
    runner.startup = {"status": "STARTED"}


def put_plan(runner, d_cutoff, modes, buys, e_decision, execute, capital_revision=None, ordinary_buy_allowed=True,
             industries=None, score_status="OK", score_reason=None):
    from smallcap.contracts import wire_ratio
    plan = {"plan_id": f"plan-{d_cutoff}", "role": runner.spec.role, "source_cutoff": f"{d_cutoff}T20:00:00+08:00",
            "E_decision": wire_ratio(Fraction(e_decision)), "execute_session": execute.isoformat(),
            "capital_revision": runner.ledger.capital_revision if capital_revision is None else capital_revision,
            "score_status": score_status, "score_reason": score_reason, "ordinary_buy_allowed": ordinary_buy_allowed,
            "modes": modes, "buy_candidates_ordered": buys, "industries": industries or {
                s: runner.m.master[s]["industry"] for s in buys}}
    runner.phase_key = f"plan/{d_cutoff}"
    runner.k = 0
    runner.emit("PLAN", {"plan": plan}, ev_at(d_cutoff, 20))
    return plan


def exec_day(runner, d):
    runner.phase_key = f"{d.isoformat()}/EXEC"
    runner.k = 0
    runner._qc = {}
    runner._phase_exec(d)


def close_day(runner, d):
    runner.phase_key = f"{d.isoformat()}/CLOSE"
    runner.k = 0
    runner._qc = {}
    runner._phase_close(d)


from fractions import Fraction  # noqa: E402
